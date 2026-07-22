# -*- coding: utf-8 -*-
"""
Cron Worker — Agent 定时任务后台执行器（自调度模式）。

启动时从 DB 加载所有 enabled 任务 → 计算下次执行时间 → 设 Timer。
任务创建/更新/删除时 → 重算 → 重建 Timer。
不轮询，不空转。

执行方式：
  - prompt 模式：调用 agent.chat(prompt) 执行
  - function 模式：直接调用 Python 函数

SSE 推送：
  前端连接 /api/cron/events 接收任务执行事件。
  事件类型：job_start / job_success / job_error

启动：app/__init__.py → start_cron_worker()
"""
from __future__ import annotations

import importlib
import logging
import queue
import threading
import time as _time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)

TZ_CN = timezone(timedelta(hours=8))


# ═══════════════════════════════════════════════════════════════
#  SSE 事件总线
# ═══════════════════════════════════════════════════════════════

_subscribers: List[queue.Queue] = []
_subscribers_lock = threading.Lock()

# ═══════════════════════════════════════════════════════════════
#  定时任务投递队列（cron_worker 只投，consumer 单线程消费）
# ═══════════════════════════════════════════════════════════════

_pending_tasks: queue.Queue = queue.Queue()
_consumer_thread: threading.Thread | None = None


def subscribe() -> queue.Queue:
    q = queue.Queue(maxsize=256)
    with _subscribers_lock:
        _subscribers.append(q)
    return q


def unsubscribe(q: queue.Queue):
    with _subscribers_lock:
        try:
            _subscribers.remove(q)
        except ValueError:
            pass


def _publish(event: dict):
    dead = []
    with _subscribers_lock:
        subs = list(_subscribers)
    for q in subs:
        try:
            q.put_nowait(event)
        except queue.Full:
            try:
                q.get_nowait()
                q.put_nowait(event)
            except queue.Empty:
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
    """检查时间是否匹配 cron 表达式。支持 | 分隔的多段表达式。"""
    for sub_expr in cron_expr.split("|"):
        parts = sub_expr.strip().split()
        if len(parts) != 5:
            continue
        minutes = _parse_cron_field(parts[0], 0, 59)
        hours = _parse_cron_field(parts[1], 0, 23)
        days = _parse_cron_field(parts[2], 1, 31)
        months = _parse_cron_field(parts[3], 1, 12)
        weekdays = _parse_cron_field(parts[4], 0, 6)
        py_wday = dt.isoweekday() % 7
        if (dt.minute in minutes
                and dt.hour in hours
                and dt.day in days
                and dt.month in months
                and py_wday in weekdays):
            return True
    return False





# ═══════════════════════════════════════════════════════════════
#  任务执行
# ═══════════════════════════════════════════════════════════════



def _next_hm(hours: set, minutes: set, cur_h: int, cur_m: int) -> tuple:
    """找到下一个匹配的 (hour, minute)，从 (cur_h, cur_m) 之后开始。"""
    for h in sorted(hours):
        if h < cur_h:
            continue
        for m in sorted(minutes):
            if h == cur_h and m <= cur_m:
                continue
            return h, m
    return min(hours), min(minutes)


def next_cron_time(cron_expr: str, after: datetime, max_search_days: int = 62) -> datetime:
    """计算下一个匹配时间。支持 | 分隔的多段表达式，返回最早的匹配。"""
    sub_exprs = cron_expr.split("|")
    if len(sub_exprs) > 1:
        candidates = []
        for sub in sub_exprs:
            t = _next_cron_single(sub.strip(), after, max_search_days)
            if t:
                candidates.append(t)
        return min(candidates) if candidates else after + timedelta(minutes=1)
    return _next_cron_single(cron_expr.strip(), after, max_search_days) or (after + timedelta(minutes=1))


def _next_cron_single(cron_expr: str, after: datetime, max_search_days: int = 62) -> datetime:
    """单段 cron 表达式的下一个匹配时间。"""
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        return None

    minutes_f = _parse_cron_field(parts[0], 0, 59)
    hours_f = _parse_cron_field(parts[1], 0, 23)
    days_f = _parse_cron_field(parts[2], 1, 31)
    months_f = _parse_cron_field(parts[3], 1, 12)
    weekdays_f = _parse_cron_field(parts[4], 0, 6)

    dt = after.replace(second=0, microsecond=0) + timedelta(minutes=1)

    # 月度 cron：逐天跳（day 字段非 *）
    if parts[2] != "*":
        for _ in range(max_search_days):
            py_wday = dt.isoweekday() % 7
            if (dt.month in months_f and dt.day in days_f
                    and dt.hour in hours_f and dt.minute in minutes_f
                    and py_wday in weekdays_f):
                return dt
            target_h, target_m = _next_hm(hours_f, minutes_f, dt.hour, dt.minute)
            if target_h > dt.hour or (target_h == dt.hour and target_m > dt.minute):
                dt = dt.replace(hour=target_h, minute=target_m)
            else:
                dt = (dt + timedelta(days=1)).replace(hour=min(hours_f), minute=min(minutes_f))
        return after + timedelta(days=1)

    # 每天固定时间：逐天跳
    if parts[1] != "*":
        for _ in range(max_search_days + 1):
            py_wday = dt.isoweekday() % 7
            if dt.hour in hours_f and dt.minute in minutes_f and py_wday in weekdays_f:
                return dt
            target_h, target_m = _next_hm(hours_f, minutes_f, dt.hour, dt.minute)
            if target_h > dt.hour or (target_h == dt.hour and target_m > dt.minute):
                dt = dt.replace(hour=target_h, minute=target_m)
            else:
                dt = (dt + timedelta(days=1)).replace(hour=min(hours_f), minute=min(minutes_f))
        return after + timedelta(days=1)

    # 分钟级/小时级：逐分钟遍历
    for _ in range(1440 * max_search_days):
        py_wday = dt.isoweekday() % 7
        if (dt.minute in minutes_f and dt.hour in hours_f
                and dt.day in days_f and dt.month in months_f
                and py_wday in weekdays_f):
            return dt
        dt += timedelta(minutes=1)
    return after + timedelta(minutes=1)


def _import_function(path: str) -> Callable:
    module_path, _, func_name = path.rpartition(".")
    if not module_path:
        raise ImportError(f"Invalid function path: {path}")
    mod = importlib.import_module(module_path)
    fn = getattr(mod, func_name)
    if not callable(fn):
        raise TypeError(f"{path} is not callable")
    return fn


def _pending_consumer():
    """单线程消费队列：到点通过统一入口执行 agent 任务。"""
    import asyncio
    from agent import run_agent

    logger.info("[CronConsumer] 消费线程启动")
    while True:
        job = _pending_tasks.get()
        if job is None:  # 毒丸，停止消费
            break
        job_id = job.get("id", 0)
        job_name = job.get("name", "unknown")
        prompt = job.get("prompt", "")
        session_id = job.get("session_id", "default")

        logger.info("[CronConsumer] 开始执行: %s (session=%s)", job_name, session_id)
        _publish(_make_event("job_start", job_id, job_name, mode="prompt"))

        try:
            content = run_agent(prompt, session_id=session_id, timeout=300)

            # 将结果写回用户会话
            if content:
                try:
                    from memory.postgres_memory import PostgresMemory
                    mem = PostgresMemory()
                    loop = asyncio.new_event_loop()
                    try:
                        loop.run_until_complete(mem.add(session_id, "assistant", content))
                    finally:
                        loop.close()
                except Exception as e:
                    logger.debug("[CronConsumer] 写入 memory 失败: %s", e)

            _update_job_status(job_id, success=True)
            _publish(_make_event("job_success", job_id, job_name,
                                 mode="prompt",
                                 content_preview=(content or "")[:500]))
            logger.info("[CronConsumer] ✅ 完成: %s", job_name)
        except asyncio.TimeoutError:
            _update_job_status(job_id, success=False, error="执行超时")
            _publish(_make_event("job_error", job_id, job_name, mode="prompt", error="执行超时"))
            logger.error("[CronConsumer] ⏰ 超时: %s", job_name)
        except Exception as e:
            _update_job_status(job_id, success=False, error=str(e))
            _publish(_make_event("job_error", job_id, job_name, mode="prompt", error=str(e)))
            logger.error("[CronConsumer] ❌ 失败: %s — %s", job_name, e)


def _start_consumer():
    """启动消费线程（幂等，只启动一次）。"""
    global _consumer_thread
    if _consumer_thread and _consumer_thread.is_alive():
        return
    _consumer_thread = threading.Thread(target=_pending_consumer, daemon=True, name="cron-consumer")
    _consumer_thread.start()


def _execute_prompt_job(job: Dict[str, Any]):
    prompt = job.get("prompt", "")
    job_name = job.get("name", "unknown")
    job_id = job.get("id", 0)

    _publish(_make_event("job_start", job_id, job_name, mode="prompt"))

    try:
        # prompt 模式：投递到队列，由 consumer 单线程串行执行
        _start_consumer()
        _pending_tasks.put(job)
        logger.info("[CronWorker] ⏳ prompt 任务已投递: %s", job_name)

    except Exception as e:
        _update_job_status(job_id, success=False, error=str(e))
        _publish(_make_event("job_error", job_id, job_name, mode="prompt", error=str(e)))
        logger.error("[CronWorker] ❌ prompt 任务异常: %s — %s", job_name, e, exc_info=True)
    finally:
        if job.get("one_shot"):
            _delete_one_shot(job_id, job_name)
        else:
            _reschedule_job(job_id)


def _execute_function_job(job: Dict[str, Any]):
    func_path = job.get("function_path", "")
    job_name = job.get("name", "unknown")
    job_id = job.get("id", 0)

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

_timers: Dict[int, threading.Timer] = {}
_timers_lock = threading.Lock()


def _schedule_job(job_id: int, job: dict, delay_seconds: float):
    with _timers_lock:
        old = _timers.pop(job_id, None)
        if old:
            old.cancel()

        def _fire():
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
        logger.info("[CronWorker] 调度任务 %d (%s): %.0f 秒后 → %s",
                     job_id, job.get("name", "?"), delay_seconds,
                     run_at.strftime("%Y-%m-%d %H:%M:%S"))


def _delete_one_shot(job_id: int, job_name: str):
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
#  启动
# ═══════════════════════════════════════════════════════════════

_worker_started = False


def _load_and_schedule_all():
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

        logger.info("[CronWorker] 已加载 %d 个定时任务", len(rows))

    except Exception as e:
        logger.error("[CronWorker] 加载任务失败: %s", e, exc_info=True)


def start_cron_worker():
    """启动 Cron Worker（在 app/__init__.py 中调用）。"""
    global _worker_started
    if _worker_started:
        return
    _worker_started = True

    def _delayed_start():
        _time.sleep(10)
        _load_and_schedule_all()

    t = threading.Thread(target=_delayed_start, daemon=True, name="cron-worker-init")
    t.start()
    logger.info("[CronWorker] 自调度模式已启动")


def stop_cron_worker():
    with _timers_lock:
        for timer in _timers.values():
            timer.cancel()
        _timers.clear()
    logger.info("[CronWorker] 已停止所有定时任务")


def get_scheduled_jobs() -> List[dict]:
    with _timers_lock:
        return [
            {"job_id": jid, "timer_name": t.name, "alive": t.is_alive()}
            for jid, t in _timers.items()
        ]
