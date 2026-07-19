# -*- coding: utf-8 -*-
"""
cron_tools.py — Agent 定时任务工具

让 Agent 能通过自然语言创建、管理定时任务。

用户示例：
  "每5分钟分析一次300129" → create_cron_job(name="分析300129", prompt="分析300129", cron_expr="*/5 * * * *")
  "14:45分析大盘"          → create_cron_job(name="分析大盘", prompt="分析大盘", at="14:45")
  "明天9:25提醒我看牙医"   → create_cron_job(name="提醒看牙医", prompt="提醒：看牙医", at="tomorrow 09:25")
  "每个月1号总结上月盈亏"  → create_cron_job(name="月度总结", prompt="总结上月盈亏", cron_expr="0 9 1 * *")

工具由 ToolProvider 自动发现注册，domain=common，所有领域可用。
"""
from __future__ import annotations

import re
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

logger = logging.getLogger(__name__)

TZ_CN = timezone(timedelta(hours=8))


# ═══════════════════════════════════════════════════════════════
#  辅助：自然语言时间 → cron 表达式
# ═══════════════════════════════════════════════════════════════

def _parse_at_time(at: str) -> tuple[str, bool]:
    """解析自然语言时间描述，返回 (cron_expr, one_shot)。

    支持格式：
      - "14:45"           → 今天 14:45（已过则明天）
      - "tomorrow 09:25"  → 明天 09:25
      - "2026-07-20 09:25"→ 指定日期时间
      - "每天 9:00"       → 0 9 * * *
      - "每周一 9:00"     → 0 9 * * 1
      - "每月1号 9:00"    → 0 9 1 * *

    Returns:
        (cron_expr, one_shot) — one_shot=True 表示只执行一次
    """
    at = at.strip()
    now = datetime.now(TZ_CN)

    # 明天 HH:MM
    m = re.match(r'tomorrow\s+(\d{1,2}):(\d{2})', at, re.I)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        target = (now + timedelta(days=1)).replace(hour=h, minute=mi, second=0, microsecond=0)
        return f"{target.minute} {target.hour} {target.day} {target.month} *", True

    # 指定日期时间 YYYY-MM-DD HH:MM
    m = re.match(r'(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})', at)
    if m:
        y, mo, d, h, mi = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
        return f"{mi} {h} {d} {mo} *", True

    # 今天/明天 HH:MM（纯时间）
    m = re.match(r'(\d{1,2}):(\d{2})$', at)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        target = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return f"{target.minute} {target.hour} {target.day} {target.month} *", True

    # 每天 HH:MM
    m = re.match(r'每天\s*(\d{1,2}):(\d{2})', at)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        return f"{mi} {h} * * *", False

    # 每周X HH:MM
    _wday_map = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 0, "天": 0}
    m = re.match(r'每(?:星期|周)([一二三四五六日天])\s*(\d{1,2}):(\d{2})', at)
    if m:
        w = _wday_map.get(m.group(1), 0)
        h, mi = int(m.group(2)), int(m.group(3))
        return f"{mi} {h} * * {w}", False

    # 每月X号 HH:MM
    m = re.match(r'每月(\d{1,2})[号日]\s*(\d{1,2}):(\d{2})', at)
    if m:
        d, h, mi = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{mi} {h} {d} * *", False

    # 开盘期间/交易时间 + 每N分钟
    m = re.match(r'(?:开盘期间|交易时间|盘中)\s*,?\s*每(\d+)分钟', at)
    if m:
        step = int(m.group(1))
        # A股: 9:30-11:30 + 13:00-15:00, 周一到周五
        expr = (
            f"30-59/{step} 9 * * 1-5|"
            f"*/{step} 10 * * 1-5|"
            f"0,5,10,15,20,25,30 11 * * 1-5|"
            f"*/{step} 13-14 * * 1-5"
        )
        return expr, False

    # 无法解析，返回 None
    return "", False


# ═══════════════════════════════════════════════════════════════
#  工具函数
# ═══════════════════════════════════════════════════════════════


def create_cron_job(
    name: str,
    prompt: str,
    cron_expr: str = "",
    at: str = "",
    one_shot: bool = False,
    description: str = "",
) -> Dict:
    """创建定时任务。

    两种触发方式（二选一）：
      - cron_expr: 5段式 cron 表达式（如 "*/5 * * * *"、"0 9 * * 1-5"）
        支持 | 分隔多段："30-59/5 9 * * 1-5|*/5 10 * * 1-5"
      - at: 自然语言时间（如 "14:45"、"tomorrow 09:25"、"每天 9:00"、"开盘期间,每5分钟"）

    Args:
        name: 任务名称（简短描述，如"分析300129"）
        prompt: 执行时发送给 Agent 的消息（如"分析300129的技术面"）
        cron_expr: 5段式 cron 表达式（分 时 日 月 周），与 at 二选一
        at: 自然语言时间描述，与 cron_expr 二选一
        one_shot: 是否只执行一次（at 模式自动为 True）
        description: 任务详细描述（可选）

    Returns:
        {"job_id": int, "name": str, "cron_expr": str, "next_run": str, "one_shot": bool}
    """
    if not name:
        return {"error": "任务名称不能为空"}
    if not prompt:
        return {"error": "执行内容不能为空"}

    # 解析触发时间
    if at and not cron_expr:
        parsed_cron, parsed_one_shot = _parse_at_time(at)
        if not parsed_cron:
            return {"error": f"无法解析时间描述: '{at}'。支持格式: 14:45 / tomorrow 09:25 / 每天 9:00 / 每周一 9:00 / 每月1号 9:00"}
        cron_expr = parsed_cron
        one_shot = one_shot or parsed_one_shot
    elif cron_expr:
        # 校验 cron 表达式格式（支持 | 分隔的多段）
        for sub in cron_expr.split("|"):
            parts = sub.strip().split()
            if len(parts) != 5:
                return {"error": f"cron 表达式格式错误（需要5段）: '{sub.strip()}'. 格式: 分 时 日 月 周, 多段用 | 分隔"}
    else:
        return {"error": "必须提供 cron_expr 或 at 参数"}

    try:
        from app.utils.db import get_db_connection
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO qd_cron_jobs (name, cron_expr, mode, prompt, description, one_shot)
                VALUES (%s, %s, 'prompt', %s, %s, %s)
                RETURNING id
            """, (name, cron_expr, prompt, description or None, one_shot))
            row = cur.fetchone()
            conn.commit()
            job_id = row["id"]

        # 注册到 Worker
        try:
            from app.agent.cron.cron_worker import schedule_job_from_db
            schedule_job_from_db(job_id)
        except Exception as e:
            logger.warning("[CronTools] 调度注册失败: %s", e)

        # 计算下次执行时间（用于返回给用户）
        try:
            from app.agent.cron.cron_worker import next_cron_time
            nxt = next_cron_time(cron_expr, datetime.now(TZ_CN))
            next_run = nxt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            next_run = "(计算失败)"

        logger.info("[CronTools] 创建任务: id=%d name=%s cron=%s one_shot=%s",
                     job_id, name, cron_expr, one_shot)

        return {
            "job_id": job_id,
            "name": name,
            "cron_expr": cron_expr,
            "next_run": next_run,
            "one_shot": one_shot,
            "status": "created",
        }

    except Exception as e:
        logger.error("[CronTools] 创建任务失败: %s", e)
        return {"error": f"创建失败: {e}"}


def list_cron_jobs(enabled_only: bool = False) -> Dict:
    """列出所有定时任务。

    Args:
        enabled_only: 是否只显示启用的任务

    Returns:
        {"jobs": [...], "total": int, "enabled": int}
    """
    try:
        from app.utils.db import get_db_connection
        with get_db_connection() as conn:
            cur = conn.cursor()
            if enabled_only:
                cur.execute("""
                    SELECT id, name, cron_expr, mode, prompt, one_shot, enabled,
                           last_run_at, last_error, error_count, total_runs, created_at
                    FROM qd_cron_jobs WHERE enabled = TRUE ORDER BY id
                """)
            else:
                cur.execute("""
                    SELECT id, name, cron_expr, mode, prompt, one_shot, enabled,
                           last_run_at, last_error, error_count, total_runs, created_at
                    FROM qd_cron_jobs ORDER BY id
                """)
            rows = cur.fetchall()

        jobs = []
        for r in rows:
            d = dict(r)
            # 格式化时间
            for k in ("last_run_at", "created_at"):
                v = d.get(k)
                if v and hasattr(v, "strftime"):
                    d[k] = v.strftime("%Y-%m-%d %H:%M")
            # 截断长 prompt
            if d.get("prompt") and len(d["prompt"]) > 100:
                d["prompt"] = d["prompt"][:100] + "..."
            jobs.append(d)

        return {
            "jobs": jobs,
            "total": len(jobs),
            "enabled": sum(1 for j in jobs if j.get("enabled")),
        }

    except Exception as e:
        logger.error("[CronTools] 列表查询失败: %s", e)
        return {"error": f"查询失败: {e}"}


def remove_cron_job(job_id: int) -> Dict:
    """删除定时任务。

    Args:
        job_id: 任务 ID

    Returns:
        {"status": "deleted", "job_id": int, "name": str}
    """
    try:
        from app.utils.db import get_db_connection
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM qd_cron_jobs WHERE id = %s RETURNING id, name", (job_id,))
            row = cur.fetchone()
            conn.commit()

        if not row:
            return {"error": f"任务 {job_id} 不存在"}

        # 取消 Worker Timer
        try:
            from app.agent.cron.cron_worker import unschedule_job
            unschedule_job(job_id)
        except Exception as e:
            logger.warning("[CronTools] 取消调度失败: %s", e)

        logger.info("[CronTools] 删除任务: id=%d name=%s", job_id, row["name"])
        return {"status": "deleted", "job_id": job_id, "name": row["name"]}

    except Exception as e:
        logger.error("[CronTools] 删除任务失败: %s", e)
        return {"error": f"删除失败: {e}"}


def toggle_cron_job(job_id: int, enabled: bool) -> Dict:
    """启用/禁用定时任务。

    Args:
        job_id: 任务 ID
        enabled: True=启用, False=禁用

    Returns:
        {"status": "toggled", "job_id": int, "enabled": bool}
    """
    try:
        from app.utils.db import get_db_connection
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE qd_cron_jobs SET enabled = %s, updated_at = NOW()
                WHERE id = %s RETURNING id, name, enabled
            """, (enabled, job_id))
            row = cur.fetchone()
            conn.commit()

        if not row:
            return {"error": f"任务 {job_id} 不存在"}

        # 更新 Worker 调度
        try:
            from app.agent.cron.cron_worker import schedule_job_from_db, unschedule_job
            if enabled:
                schedule_job_from_db(job_id)
            else:
                unschedule_job(job_id)
        except Exception as e:
            logger.warning("[CronTools] 调度更新失败: %s", e)

        action = "启用" if enabled else "禁用"
        return {"status": "toggled", "job_id": job_id, "name": row["name"], "enabled": enabled}

    except Exception as e:
        logger.error("[CronTools] %s任务失败: %s", "启用" if enabled else "禁用", e)
        return {"error": f"操作失败: {e}"}
