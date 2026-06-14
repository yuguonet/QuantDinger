# -*- coding: utf-8 -*-
"""
Cron Jobs API — 定时任务 REST 接口。

供前端管理定时任务（CRUD + 手动触发）。
Agent 工具通过 cron_tools.py 直接操作数据库，不经过此路由。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

cron_bp = Blueprint('cron', __name__, url_prefix='/api/agent/cron')

TZ_CN = timezone(timedelta(hours=8))


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


# ── 列表 ──────────────────────────────────────────────────

@cron_bp.route('/jobs', methods=['GET'])
def list_jobs():
    """获取所有定时任务。"""
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
    """创建定时任务。"""
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

    # 校验函数路径
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
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (name, cron_expr, mode, prompt, function_path, description))
            row = cur.fetchone()
            conn.commit()

        # 自调度：立即注册 Timer
        try:
            from app.agent.cron_worker import schedule_job_from_db
            schedule_job_from_db(row["id"])
        except Exception as e:
            logger.warning("[CronAPI] 调度注册失败: %s", e)

        return jsonify({"id": row["id"], "status": "created"}), 201
    except Exception as e:
        logger.error("[CronAPI] 创建失败: %s", e)
        return jsonify({"error": str(e)}), 500


# ── 更新 ──────────────────────────────────────────────────

@cron_bp.route('/jobs/<int:job_id>', methods=['PUT'])
def update_job(job_id):
    """更新定时任务。"""
    data = request.get_json() or {}
    updates = []
    params = []

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

        # 自调度：重算 Timer
        try:
            from app.agent.cron_worker import schedule_job_from_db, unschedule_job
            if row["enabled"]:
                schedule_job_from_db(row["id"])
            else:
                unschedule_job(row["id"])
        except Exception as e:
            logger.warning("[CronAPI] 调度更新失败: %s", e)

        return jsonify({"status": "updated"})
    except Exception as e:
        logger.error("[CronAPI] 更新失败: %s", e)
        return jsonify({"error": str(e)}), 500


# ── 删除 ──────────────────────────────────────────────────

@cron_bp.route('/jobs/<int:job_id>', methods=['DELETE'])
def delete_job(job_id):
    """删除定时任务。"""
    try:
        with _get_db() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM qd_cron_jobs WHERE id = %s RETURNING id", (job_id,))
            row = cur.fetchone()
            conn.commit()

        if not row:
            return jsonify({"error": "任务不存在"}), 404

        # 自调度：取消 Timer
        try:
            from app.agent.cron_worker import unschedule_job
            unschedule_job(job_id)
        except Exception as e:
            logger.warning("[CronAPI] 取消调度失败: %s", e)

        return jsonify({"status": "deleted"})
    except Exception as e:
        logger.error("[CronAPI] 删除失败: %s", e)
        return jsonify({"error": str(e)}), 500


# ── 手动触发 ──────────────────────────────────────────────

@cron_bp.route('/jobs/<int:job_id>/trigger', methods=['POST'])
def trigger_job(job_id):
    """手动触发任务。"""
    try:
        with _get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, name, cron_expr, mode, prompt, function_path FROM qd_cron_jobs WHERE id = %s", (job_id,))
            row = cur.fetchone()

        if not row:
            return jsonify({"error": "任务不存在"}), 404

        job = dict(row)
        mode = job.get("mode", "prompt")

        import threading
        from app.agent.cron_worker import _execute_prompt_job, _execute_function_job

        target = _execute_function_job if mode == "function" else _execute_prompt_job
        t = threading.Thread(target=target, args=(job,), daemon=True,
                             name=f"cron-manual-{job_id}")
        t.start()

        return jsonify({"status": "triggered", "job_id": job_id, "name": job["name"]})
    except Exception as e:
        logger.error("[CronAPI] 触发失败: %s", e)
        return jsonify({"error": str(e)}), 500
