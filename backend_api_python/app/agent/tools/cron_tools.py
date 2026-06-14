# -*- coding: utf-8 -*-
"""
Cron Tools — Agent 自调度定时任务工具。

Agent 可通过以下工具创建、管理定时任务：
  - create_cron_job: 创建定时任务
  - list_cron_jobs: 列出所有任务
  - update_cron_job: 暂停/恢复/修改
  - delete_cron_job: 删除任务

任务存储在 qd_cron_jobs 表，由 cron_worker.py 后台扫描执行。

双模式：
  - prompt 模式：cron 触发时调 agent.chat(prompt)，消耗 token
  - function 模式：直接调 Python 函数，0 token
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.agent.tools.registry import tool

logger = logging.getLogger(__name__)

TZ_CN = timezone(timedelta(hours=8))


# ── cron 表达式校验 ───────────────────────────────────────────

def _validate_cron(expr: str) -> Optional[str]:
    """校验 5 段式 cron 表达式，返回错误信息或 None。"""
    parts = expr.strip().split()
    if len(parts) != 5:
        return f"需要 5 段（分 时 日 月 周），实际 {len(parts)} 段"

    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    names = ["分", "时", "日", "月", "周"]

    for i, (part, (lo, hi), name) in enumerate(zip(parts, ranges, names)):
        for sub in part.split(","):
            sub = sub.strip()
            if sub == "*":
                continue
            if sub.startswith("*/"):
                try:
                    v = int(sub[2:])
                    if v < 1 or v > hi:
                        return f"{name}字段步长越界: {sub}"
                except ValueError:
                    return f"{name}字段格式错误: {sub}"
                continue
            # N-M 或 N-M/S
            range_part = sub.split("/")[0] if "/" in sub else sub
            if "-" in range_part:
                try:
                    lo_v, hi_v = range_part.split("-", 1)
                    lo_v, hi_v = int(lo_v), int(hi_v)
                    if lo_v < lo or hi_v > hi or lo_v > hi_v:
                        return f"{name}字段范围越界: {sub}"
                except ValueError:
                    return f"{name}字段格式错误: {sub}"
            else:
                try:
                    v = int(sub)
                    if v < lo or v > hi:
                        return f"{name}字段越界: {v}（允许 {lo}-{hi}）"
                except ValueError:
                    return f"{name}字段格式错误: {sub}"
    return None


# ── DB 辅助 ───────────────────────────────────────────────────

def _get_db():
    from app.utils.db import get_db_connection
    return get_db_connection()


def _job_to_dict(row) -> dict:
    """将数据库行转为可序列化的 dict。"""
    d = dict(row)
    for k in ("last_run_at", "last_success_at", "created_at", "updated_at"):
        v = d.get(k)
        if v and hasattr(v, "strftime"):
            d[k] = v.strftime("%Y-%m-%d %H:%M:%S")
    return d


# ═══════════════════════════════════════════════════════════════
# 1. create_cron_job
# ═══════════════════════════════════════════════════════════════

@tool(
    description=(
        "创建定时任务。支持两种模式：\n"
        "- prompt 模式：定时调用 Agent 执行一段消息（消耗 token，但 Agent 能用所有工具）\n"
        "- function 模式：定时直接调用 Python 函数（0 token，纯算法）\n"
        "适用场景：盘后自动回溯、定时行情检查、每日报告生成等"
    ),
    category="定时任务",
    layer="支撑层",
    domain=[],
)
def create_cron_job(
    name: str,
    cron_expr: str,
    mode: str = "prompt",
    prompt: str = "",
    function_path: str = "",
    description: str = "",
) -> Dict[str, Any]:
    """创建 Agent 定时任务。

    Args:
        name: 任务名称（简短可读，如"盘后回溯验证"）
        cron_expr: 5段式 cron 表达式（分 时 日 月 周），如 "0 18 * * 1-5" 表示工作日18:00
        mode: "prompt"（调 Agent）或 "function"（调 Python 函数）
        prompt: mode=prompt 时必填，Agent 执行的消息内容
        function_path: mode=function 时必填，Python 函数点分路径（如 "app.agent.chain.evaluator.auto_evaluate"）
        description: 任务描述（可选）

    Returns:
        创建结果，含 job_id
    """
    # 校验
    if not name or not name.strip():
        return {"error": "任务名称不能为空"}

    err = _validate_cron_expr(cron_expr)
    if err:
        return {"error": f"cron 表达式错误: {err}", "cron_expr": cron_expr}

    if mode not in ("prompt", "function"):
        return {"error": f"mode 必须是 'prompt' 或 'function'，实际: {mode}"}

    if mode == "prompt" and not (prompt or "").strip():
        return {"error": "prompt 模式需要提供 prompt 参数"}

    if mode == "function" and not (function_path or "").strip():
        return {"error": "function 模式需要提供 function_path 参数"}

    # 校验函数路径是否可导入
    if mode == "function":
        try:
            module_path, _, func_name = function_path.rpartition(".")
            import importlib
            mod = importlib.import_module(module_path)
            fn = getattr(mod, func_name)
            if not callable(fn):
                return {"error": f"{function_path} 不是可调用对象"}
        except (ImportError, AttributeError) as e:
            return {"error": f"函数路径无效: {e}", "function_path": function_path}

    # 写入数据库
    try:
        with _get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO qd_cron_jobs (name, cron_expr, mode, prompt, function_path, description)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (name.strip(), cron_expr.strip(), mode,
                  prompt.strip() if prompt else None,
                  function_path.strip() if function_path else None,
                  description.strip() if description else None))
            row = cur.fetchone()
            conn.commit()
            job_id = row["id"]

        logger.info("[CronTool] 创建定时任务: id=%d name=%s cron=%s mode=%s",
                     job_id, name, cron_expr, mode)

        # 自调度：立即注册 Timer
        try:
            from app.agent.cron_worker import schedule_job_from_db
            schedule_job_from_db(job_id)
        except Exception as e:
            logger.warning("[CronTool] 调度注册失败（将在下次启动时加载）: %s", e)

        return {
            "job_id": job_id,
            "name": name.strip(),
            "cron_expr": cron_expr.strip(),
            "mode": mode,
            "status": "已创建",
            "提示": "任务已创建，cron_worker 将在下一个匹配时间点自动触发执行",
        }
    except Exception as e:
        logger.error("[CronTool] 创建定时任务失败: %s", e)
        return {"error": f"数据库写入失败: {e}"}


def _validate_cron_expr(expr: str) -> Optional[str]:
    """校验 cron 表达式，返回错误信息或 None。"""
    if not expr or not expr.strip():
        return "cron 表达式不能为空"
    return _validate_cron(expr)


# ═══════════════════════════════════════════════════════════════
# 2. list_cron_jobs
# ═══════════════════════════════════════════════════════════════

@tool(
    description="列出所有定时任务及其运行状态。用于查看已有任务、检查执行结果。",
    category="定时任务",
    layer="支撑层",
    domain=[],
)
def list_cron_jobs(enabled_only: bool = False) -> Dict[str, Any]:
    """列出所有定时任务。

    Args:
        enabled_only: 是否只显示启用的任务（默认显示全部）

    Returns:
        任务列表及统计
    """
    try:
        with _get_db() as conn:
            cur = conn.cursor()
            if enabled_only:
                cur.execute("SELECT * FROM qd_cron_jobs WHERE enabled = TRUE ORDER BY id")
            else:
                cur.execute("SELECT * FROM qd_cron_jobs ORDER BY id")
            rows = cur.fetchall()

        jobs = [_job_to_dict(r) for r in rows]

        # 摘要
        total = len(jobs)
        enabled = sum(1 for j in jobs if j.get("enabled"))
        with_errors = sum(1 for j in jobs if j.get("error_count", 0) > 0)

        return {
            "total": total,
            "enabled": enabled,
            "disabled": total - enabled,
            "with_errors": with_errors,
            "jobs": jobs,
        }
    except Exception as e:
        logger.error("[CronTool] 列出定时任务失败: %s", e)
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════
# 3. update_cron_job
# ═══════════════════════════════════════════════════════════════

@tool(
    description="更新定时任务：暂停/恢复/修改 cron 表达式/修改 prompt。只传需要改的字段。",
    category="定时任务",
    layer="支撑层",
    domain=[],
)
def update_cron_job(
    job_id: int,
    enabled: Optional[bool] = None,
    cron_expr: Optional[str] = None,
    prompt: Optional[str] = None,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """更新定时任务。

    Args:
        job_id: 任务 ID
        enabled: 设为 True 恢复，False 暂停
        cron_expr: 新的 cron 表达式
        prompt: 新的 prompt（仅 prompt 模式）
        description: 新的描述

    Returns:
        更新结果
    """
    updates = []
    params = []

    if enabled is not None:
        updates.append("enabled = %s")
        params.append(enabled)

    if cron_expr is not None:
        err = _validate_cron_expr(cron_expr)
        if err:
            return {"error": f"cron 表达式错误: {err}"}
        updates.append("cron_expr = %s")
        params.append(cron_expr.strip())

    if prompt is not None:
        updates.append("prompt = %s")
        params.append(prompt.strip())

    if description is not None:
        updates.append("description = %s")
        params.append(description.strip())

    if not updates:
        return {"error": "没有提供任何要更新的字段"}

    updates.append("updated_at = NOW()")
    params.append(job_id)

    try:
        with _get_db() as conn:
            cur = conn.cursor()
            sql = f"UPDATE qd_cron_jobs SET {', '.join(updates)} WHERE id = %s RETURNING id, name, enabled, cron_expr"
            cur.execute(sql, params)
            row = cur.fetchone()
            conn.commit()

        if not row:
            return {"error": f"任务 ID {job_id} 不存在"}

        logger.info("[CronTool] 更新定时任务: id=%d", job_id)

        # 自调度：重算 Timer
        try:
            from app.agent.cron_worker import schedule_job_from_db, unschedule_job
            if row["enabled"]:
                schedule_job_from_db(row["id"])
            else:
                unschedule_job(row["id"])
        except Exception as e:
            logger.warning("[CronTool] 调度更新失败: %s", e)

        return {
            "job_id": row["id"],
            "name": row["name"],
            "enabled": row["enabled"],
            "cron_expr": row["cron_expr"],
            "status": "已更新",
        }
    except Exception as e:
        logger.error("[CronTool] 更新定时任务失败: %s", e)
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════
# 4. delete_cron_job
# ═══════════════════════════════════════════════════════════════

@tool(
    description="删除定时任务。不可恢复。",
    category="定时任务",
    layer="支撑层",
    domain=[],
)
def delete_cron_job(job_id: int) -> Dict[str, Any]:
    """删除定时任务。

    Args:
        job_id: 任务 ID

    Returns:
        删除结果
    """
    try:
        with _get_db() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM qd_cron_jobs WHERE id = %s RETURNING id, name", (job_id,))
            row = cur.fetchone()
            conn.commit()

        if not row:
            return {"error": f"任务 ID {job_id} 不存在"}

        logger.info("[CronTool] 删除定时任务: id=%d name=%s", row["id"], row["name"])

        # 自调度：取消 Timer
        try:
            from app.agent.cron_worker import unschedule_job
            unschedule_job(row["id"])
        except Exception as e:
            logger.warning("[CronTool] 取消调度失败: %s", e)

        return {
            "job_id": row["id"],
            "name": row["name"],
            "status": "已删除",
        }
    except Exception as e:
        logger.error("[CronTool] 删除定时任务失败: %s", e)
        return {"error": str(e)}
