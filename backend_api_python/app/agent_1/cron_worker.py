# -*- coding: utf-8 -*-
"""
Cron Worker — Agent 定时任务后台执行器（自调度模式）。

启动时从 DB 加载所有 enabled 任务 → 计算下次执行时间 → 设 Timer。
任务创建/更新/删除时 → 重算 → 重建 Timer。
不轮询，不空转。

SSE 实时推送：
  前端连接 /api/cron/events 接收任务执行事件。
  事件类型：job_start / job_success / job_error

启动：app/__init__.py → start_cron_worker()
"""
from __future__ import annotations

import importlib
import json
import logging
import queue
import threading
import time as _time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

TZ_CN = timezone(timedelta(hours=8))


# ═══════════════════════════════════════════════════════════════
#  SSE 事件总线（发布/订阅）
# ═══════════════════════════════════════════════════════════════

_subscribers: List[queue.Queue] = []
_subscribers_lock = threading.Lock()


def subscribe() -> queue.Queue:
    """前端连接时调用，返回一个事件队列。"""
    q = queue.Queue(maxsize=256)
    with _subscribers_lock:
        _subscribers.append(q)
    return q


def unsubscribe(q: queue.Queue):
    """前端断开时调用。"""
    with _subscribers_lock:
        try:
            _subscribers.remove(q)
        except ValueError:
            pass


def _publish(event: dict):
    """向所有订阅者推送事件。"""
    dead = []
    with _subscribers_lock:
        subs = list(_subscribers)
    for q in subs:
        try:
            q.put_nowait(event)
        except queue.Full:
            dead.append(q)
    if dead:
        with _subscribers_lock:
            for q in dead:
                try:
                    _subscribers.remove(q)
                except ValueError:
                    pass


def _make_event(event_type: str, job_id: int, job_name: str, **extra) -> dict:
    ev = {
        "type": event_type,
        "job_id": job_id,
        "job_name": job_name,
        "timestamp": datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S"),
    }
    ev.update(extra)
    return ev


# ═══════════════════════════════════════════════════════════════
#  Cron 表达式解析（5 段式）
# ═══════════════════════════════════════════════════════════════


def _parse_cron_field(field_str: str, min_val: int, max_val: int) -> set:
    values = set()
    for part in field_str.split(","):
        part = part.strip()
        if part == "*":
            values.update(range(min_val, max_val + 1))
        elif part.startswith("*/"):
            step = int(part[2:])
            values.update(range(min_val, max_val + 1, step))
        elif "-" in part and "/" not in part:
            lo, hi = part.split("-", 1)
            values.update(range(int(lo), int(hi) + 1))
        elif "-" in part and "/" in part:
            range_part, step_part = part.split("/", 1)
            lo, hi = range_part.split("-", 1)
            step = int(step_part)
            values.update(range(int(lo), int(hi) + 1, step))
        else:
            values.add(int(part))
    return {v for v in values if min_val <= v <= max_val}


def cron_matches(cron_expr: str, dt: datetime) -> bool:
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        return False
    minutes = _parse_cron_field(parts[0], 0, 59)
    hours = _parse_cron_field(parts[1], 0, 23)
    days = _parse_cron_field(parts[2], 1, 31)
    months = _parse_cron_field(parts[3], 1, 12)
    weekdays = _parse_cron_field(parts[4], 0, 6)
    py_wday = dt.isoweekday() % 7
    return (
        dt.minute in minutes
        and dt.hour in hours
        and dt.day in days
        and dt.month in months
        and py_wday in weekdays
    )


def next_cron_time(cron_expr: str, after: datetime, max_search_minutes: int = 1440 * 2) -> datetime:
    """计算下一个匹配时间。最多搜索 2 天。"""
    dt = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(max_search_minutes):
        if cron_matches(cron_expr, dt):
            return dt
        dt += timedelta(minutes=1)
    # 兜底：1 分钟后
    return after + timedelta(minutes=1)


# ═══════════════════════════════════════════════════════════════
#  任务执行
# ═══════════════════════════════════════════════════════════════


def _import_function(path: str) -> Callable:
    module_path, _, func_name = path.rpartition(".")
    if not module_path:
        raise ImportError(f"Invalid function path: {path}")
    mod = importlib.import_module(module_path)
    fn = getattr(mod, func_name)
    if not callable(fn):
        raise TypeError(f"{path} is not callable")
    return fn


def _execute_prompt_job(job: Dict[str, Any]):
    prompt = job["prompt"]
    job_name = job["name"]
    job_id = job["id"]

    _publish(_make_event("job_start", job_id, job_name, mode="prompt"))

    try:
        from app.agent.agent import build_agent_executor
        from app.agent.intent_analyzer import analyze_intent

        # skill_registry 已移除：新架构 skill 通过 SKILL.md 管理
        # 先做意图分析，拿到 domain 以过滤工具
        intent = analyze_intent(prompt, session_id=f"cron_{job_id}")
        domain = intent.domain if intent else "unknown"

        executor = build_agent_executor(
            skills=[],
            user_id="cron",
            max_steps=8,
            timeout_seconds=120,
            domain=domain,
        )

        session_id = f"cron_{job_id}_{int(_time.time())}"
        result = executor.chat(
            message=prompt,
            session_id=session_id,
            context={"source": "cron", "job_name": job_name},
            user_id="cron",
        )

        if result.success:
            _update_job_status(job_id, success=True)
            _publish(_make_event("job_success", job_id, job_name,
                                 mode="prompt", steps=result.total_steps,
                                 content_preview=(result.content or "")[:500]))
            logger.info("[CronWorker] ✅ prompt 任务完成: %s (steps=%d)", job_name, result.total_steps)
        else:
            err = result.error or "Agent 返回失败"
            _update_job_status(job_id, success=False, error=err)
            _publish(_make_event("job_error", job_id, job_name, mode="prompt", error=err))
            logger.warning("[CronWorker] ❌ prompt 任务失败: %s — %s", job_name, err)

    except Exception as e:
        _update_job_status(job_id, success=False, error=str(e))
        _publish(_make_event("job_error", job_id, job_name, mode="prompt", error=str(e)))
        logger.error("[CronWorker] ❌ prompt 任务异常: %s — %s", job_name, e, exc_info=True)
    finally:
        # 一次性任务：执行完直接删除，不循环
        if job.get("one_shot"):
            _delete_one_shot(job_id, job_name)
        else:
            _reschedule_job(job_id)


def _execute_function_job(job: Dict[str, Any]):
    func_path = job["function_path"]
    job_name = job["name"]
    job_id = job["id"]

    _publish(_make_event("job_start", job_id, job_name, mode="function", function_path=func_path))

    try:
        fn = _import_function(func_path)
        result = fn()
        result_preview = str(result)[:500] if result else "ok"
        _update_job_status(job_id, success=True)
        _publish(_make_event("job_success", job_id, job_name,
                             mode="function", result_preview=result_preview))
        logger.info("[CronWorker] ✅ function 任务完成: %s → %s", job_name, result_preview)
    except Exception as e:
        _update_job_status(job_id, success=False, error=str(e))
        _publish(_make_event("job_error", job_id, job_name, mode="function", error=str(e)))
        logger.error("[CronWorker] ❌ function 任务异常: %s — %s", job_name, e, exc_info=True)
    finally:
        if job.get("one_shot"):
            _delete_one_shot(job_id, job_name)
        else:
            _reschedule_job(job_id)


def _update_job_status(job_id: int, success: bool, error: str = ""):
    try:
        from app.utils.db import get_db_connection
        now = datetime.now(TZ_CN)
        with get_db_connection() as conn:
            cur = conn.cursor()
            if success:
                cur.execute("""
                    UPDATE qd_cron_jobs
                    SET last_run_at = %s, last_success_at = %s, last_error = NULL,
                        error_count = 0, total_runs = total_runs + 1, updated_at = %s
                    WHERE id = %s
                """, (now, now, now, job_id))
            else:
                cur.execute("""
                    UPDATE qd_cron_jobs
                    SET last_run_at = %s, last_error = %s,
                        error_count = error_count + 1, total_runs = total_runs + 1, updated_at = %s
                    WHERE id = %s
                """, (now, error[:1000], now, job_id))
            conn.commit()
    except Exception as e:
        logger.error("[CronWorker] 更新任务状态失败 id=%d: %s", job_id, e)


# ═══════════════════════════════════════════════════════════════
#  自调度核心：Timer 管理
# ═══════════════════════════════════════════════════════════════

# job_id → threading.Timer
_timers: Dict[int, threading.Timer] = {}
_timers_lock = threading.Lock()


def _schedule_job(job_id: int, job: dict, delay_seconds: float):
    """为单个任务设定 Timer。"""
    with _timers_lock:
        # 取消旧 Timer
        old = _timers.pop(job_id, None)
        if old:
            old.cancel()

        def _fire():
            """Timer 触发 → 异步执行 → 执行完自动 reschedule。"""
            with _timers_lock:
                _timers.pop(job_id, None)

            mode = job.get("mode", "prompt")
            target = _execute_function_job if mode == "function" else _execute_prompt_job
            t = threading.Thread(target=target, args=(job,), daemon=True,
                                 name=f"cron-job-{job_id}")
            t.start()

        timer = threading.Timer(delay_seconds, _fire)
        timer.daemon = True
        timer.name = f"cron-timer-{job_id}"
        timer.start()
        _timers[job_id] = timer

        run_at = datetime.now(TZ_CN) + timedelta(seconds=delay_seconds)
        logger.info("[CronWorker] 调度任务 %d (%s): %.0f 秒后执行 → %s",
                     job_id, job.get("name", "?"), delay_seconds,
                     run_at.strftime("%Y-%m-%d %H:%M:%S"))


def _delete_one_shot(job_id: int, job_name: str):
    """一次性任务执行完后删除。"""
    try:
        from app.utils.db import get_db_connection
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM qd_cron_jobs WHERE id = %s", (job_id,))
            conn.commit()
        logger.info("[CronWorker] 🗑️ 一次性任务 %d (%s) 已删除", job_id, job_name)
    except Exception as e:
        logger.error("[CronWorker] 删除一次性任务 %d 失败: %s", job_id, e)


def _reschedule_job(job_id: int):
    """从 DB 重新读取任务，计算下次时间，设定 Timer。"""
    try:
        from app.utils.db import get_db_connection
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, name, cron_expr, mode, prompt, function_path, enabled, one_shot
                FROM qd_cron_jobs WHERE id = %s
            """, (job_id,))
            row = cur.fetchone()

        if not row or not row["enabled"]:
            logger.info("[CronWorker] 任务 %d 不存在或已禁用，停止调度", job_id)
            return

        job = dict(row)
        cron_expr = job["cron_expr"]
        now = datetime.now(TZ_CN)
        nxt = next_cron_time(cron_expr, now)
        delay = max((nxt - now).total_seconds(), 1)
        _schedule_job(job_id, job, delay)

    except Exception as e:
        logger.error("[CronWorker] reschedule 任务 %d 失败: %s", job_id, e)


def schedule_job_from_db(job_id: int):
    """外部调用：任务创建/更新后，重新调度。"""
    _reschedule_job(job_id)


def unschedule_job(job_id: int):
    """外部调用：任务删除/禁用后，取消 Timer。"""
    with _timers_lock:
        old = _timers.pop(job_id, None)
        if old:
            old.cancel()
            logger.info("[CronWorker] 取消任务 %d 的 Timer", job_id)


# ═══════════════════════════════════════════════════════════════
#  启动：加载所有任务
# ═══════════════════════════════════════════════════════════════

_worker_started = False


def _load_and_schedule_all():
    """从 DB 加载所有 enabled 任务，计算下次时间，设 Timer。"""
    try:
        from app.utils.db import get_db_connection
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, name, cron_expr, mode, prompt, function_path, one_shot
                FROM qd_cron_jobs WHERE enabled = TRUE
            """)
            rows = cur.fetchall()

        now = datetime.now(TZ_CN)
        for row in rows:
            job = dict(row)
            job_id = job["id"]
            cron_expr = job["cron_expr"]

            nxt = next_cron_time(cron_expr, now)
            delay = max((nxt - now).total_seconds(), 1)
            _schedule_job(job_id, job, delay)

        logger.info("[CronWorker] 已加载 %d 个定时任务（自调度模式）", len(rows))

    except Exception as e:
        logger.error("[CronWorker] 加载任务失败: %s", e, exc_info=True)


def start_cron_worker():
    """启动 Cron Worker（在 app/__init__.py 中调用）。"""
    global _worker_started
    if _worker_started:
        return
    _worker_started = True

    # 延迟 10 秒加载，等应用完全初始化
    def _delayed_start():
        _time.sleep(10)
        _load_and_schedule_all()

    t = threading.Thread(target=_delayed_start, daemon=True, name="cron-worker-init")
    t.start()
    logger.info("[CronWorker] 自调度模式已启动（延迟 10 秒加载任务）")


def stop_cron_worker():
    """停止所有 Timer。"""
    with _timers_lock:
        for timer in _timers.values():
            timer.cancel()
        _timers.clear()
    logger.info("[CronWorker] 已停止所有定时任务")


def get_scheduled_jobs() -> List[dict]:
    """获取当前已调度的任务信息（供状态查询）。"""
    with _timers_lock:
        result = []
        for job_id, timer in _timers.items():
            result.append({
                "job_id": job_id,
                "timer_name": timer.name,
                "alive": timer.is_alive(),
            })
        return result
