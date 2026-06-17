# -*- coding: utf-8 -*-
"""
Cron Jobs API — 定时任务 REST 接口。

委托给 nanobot cron service，数据库仅做持久化。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

cron_bp = Blueprint('cron', __name__, url_prefix='/api/agent/cron')


def _get_db():
    from app.utils.db import get_db_connection
    return get_db_connection()


def _row_to_dict(row) -> dict:
    d = dict(row)
    for k in ("last_run_at", "last_success_at", "created_at", "updated_at"):
        v = d.get(k)
        if v and hasattr(v, "strftime"):
            d[k] = v.strftime("%Y-%m-%d %H:%M:%S")
    return d


def _get_cron_service():
    """获取 nanobot cron service。"""
    try:
        from app.agent.nanobot_bridge import get_nanobot_loop
        loop = get_nanobot_loop()
        return loop.cron_service
    except Exception as e:
        logger.warning("[CronAPI] nanobot cron service 不可用: %s", e)
        return None


# ── 列表 ──────────────────────────────────────────────────

@cron_bp.route('/jobs', methods=['GET'])
def list_jobs():
    try:
        with _get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM qd_cron_jobs ORDER BY id")
            rows = cur.fetchall()
        jobs = [_row_to_dict(r) for r in rows]
        return jsonify({
            "jobs": jobs,
            "total": len(jobs),
            "enabled": sum(1 for j in jobs if j.get("enabled")),
        })
    except Exception as e:
        logger.error("[CronAPI] 列表失败: %s", e)
        return jsonify({"error": str(e)}), 500


# ── 创建 ──────────────────────────────────────────────────

@cron_bp.route('/jobs', methods=['POST'])
def create_job():
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    cron_expr = (data.get("cron_expr") or "").strip()
    mode = data.get("mode", "prompt")
    prompt = (data.get("prompt") or "").strip() or None
    function_path = (data.get("function_path") or "").strip() or None
    description = (data.get("description") or "").strip() or None

    if not name:
        return jsonify({"error": "任务名称不能为空"}), 400
    if not cron_expr:
        return jsonify({"error": "cron 表达式不能为空"}), 400
    if mode not in ("prompt", "function"):
        return jsonify({"error": "mode 必须是 prompt 或 function"}), 400
    if mode == "prompt" and not prompt:
        return jsonify({"error": "prompt 模式需要提供 prompt"}), 400
    if mode == "function" and not function_path:
        return jsonify({"error": "function 模式需要提供 function_path"}), 400

    if mode == "function" and function_path:
        try:
            module_path, _, func_name = function_path.rpartition(".")
            import importlib
            mod = importlib.import_module(module_path)
            fn = getattr(mod, func_name)
            if not callable(fn):
                return jsonify({"error": f"{function_path} 不可调用"}), 400
        except (ImportError, AttributeError) as e:
            return jsonify({"error": f"函数路径无效: {e}"}), 400

    try:
        with _get_db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO qd_cron_jobs (name, cron_expr, mode, prompt, function_path, description)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
            """, (name, cron_expr, mode, prompt, function_path, description))
            row = cur.fetchone()
            conn.commit()

        # 注册到 nanobot cron service
        _sync_job_to_nanobot(row["id"])

        return jsonify({"id": row["id"], "status": "created"}), 201
    except Exception as e:
        logger.error("[CronAPI] 创建失败: %s", e)
        return jsonify({"error": str(e)}), 500


# ── 更新 ──────────────────────────────────────────────────

@cron_bp.route('/jobs/<int:job_id>', methods=['PUT'])
def update_job(job_id):
    data = request.get_json() or {}
    updates, params = [], []
    for field in ("name", "cron_expr", "mode", "prompt", "function_path", "description"):
        if field in data:
            updates.append(f"{field} = %s")
            params.append(data[field] if data[field] else None)
    if "enabled" in data:
        updates.append("enabled = %s")
        params.append(bool(data["enabled"]))
    if not updates:
        return jsonify({"error": "没有要更新的字段"}), 400
    updates.append("updated_at = NOW()")
    params.append(job_id)

    try:
        with _get_db() as conn:
            cur = conn.cursor()
            cur.execute(f"UPDATE qd_cron_jobs SET {', '.join(updates)} WHERE id = %s RETURNING id, enabled", params)
            row = cur.fetchone()
            conn.commit()
        if not row:
            return jsonify({"error": "任务不存在"}), 404

        _sync_job_to_nanobot(job_id)
        return jsonify({"status": "updated"})
    except Exception as e:
        logger.error("[CronAPI] 更新失败: %s", e)
        return jsonify({"error": str(e)}), 500


# ── 删除 ──────────────────────────────────────────────────

@cron_bp.route('/jobs/<int:job_id>', methods=['DELETE'])
def delete_job(job_id):
    try:
        with _get_db() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM qd_cron_jobs WHERE id = %s RETURNING id", (job_id,))
            row = cur.fetchone()
            conn.commit()
        if not row:
            return jsonify({"error": "任务不存在"}), 404

        # 从 nanobot cron 移除
        cs = _get_cron_service()
        if cs:
            try:
                import asyncio
                loop = asyncio.new_event_loop()
                loop.run_until_complete(cs.remove_job(f"qd_{job_id}"))
                loop.close()
            except Exception:
                pass

        return jsonify({"status": "deleted"})
    except Exception as e:
        logger.error("[CronAPI] 删除失败: %s", e)
        return jsonify({"error": str(e)}), 500


# ── 手动触发 ──────────────────────────────────────────────

@cron_bp.route('/jobs/<int:job_id>/trigger', methods=['POST'])
def trigger_job(job_id):
    try:
        with _get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, name, cron_expr, mode, prompt, function_path FROM qd_cron_jobs WHERE id = %s", (job_id,))
            row = cur.fetchone()
        if not row:
            return jsonify({"error": "任务不存在"}), 404

        job = dict(row)
        _execute_job(job)
        return jsonify({"status": "triggered", "job_id": job_id, "name": job["name"]})
    except Exception as e:
        logger.error("[CronAPI] 触发失败: %s", e)
        return jsonify({"error": str(e)}), 500


# ── 内部：同步 DB job → nanobot cron ─────────────────────

def _sync_job_to_nanobot(job_id: int):
    """从 DB 读取 job，注册到 nanobot cron service。"""
    cs = _get_cron_service()
    if not cs:
        return
    try:
        with _get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM qd_cron_jobs WHERE id = %s", (job_id,))
            row = cur.fetchone()
        if not row or not row.get("enabled"):
            return

        job = dict(row)
        import asyncio
        from nanobot.cron.types import CronJob, CronSchedule, CronJobState

        cron_job = CronJob(
            id=f"qd_{job_id}",
            name=job.get("name", f"Job#{job_id}"),
            schedule=CronSchedule(kind="cron", expr=job["cron_expr"], tz="Asia/Shanghai"),
            message=f"执行定时任务: {job.get('prompt') or job.get('function_path') or job['name']}",
            state=CronJobState.ENABLED,
        )
        loop = asyncio.new_event_loop()
        loop.run_until_complete(cs.add_job(cron_job))
        loop.close()
        logger.info("[CronAPI] 已注册 nanobot cron: qd_%d", job_id)
    except Exception as e:
        logger.warning("[CronAPI] 注册失败: %s", e)


def _execute_job(job: dict):
    """执行一个 cron job（prompt 模式用 agent，function 模式直接调用）。"""
    import threading
    mode = job.get("mode", "prompt")

    if mode == "function":
        fn_path = job.get("function_path")
        if fn_path:
            module_path, _, func_name = fn_path.rpartition(".")
            import importlib
            mod = importlib.import_module(module_path)
            fn = getattr(mod, func_name)
            threading.Thread(target=fn, daemon=True).start()
    else:
        # prompt 模式：用 agent 执行
        prompt = job.get("prompt", "")
        if prompt:
            def _run():
                try:
                    from app.agent.agent import build_agent_executor
                    executor = build_agent_executor(skills=[], user_id="cron", max_steps=8, timeout_seconds=120)
                    executor.chat(message=prompt, session_id=f"cron_{job['id']}")
                except Exception as e:
                    logger.error("[CronAPI] prompt 执行失败: %s", e)
            threading.Thread(target=_run, daemon=True).start()
