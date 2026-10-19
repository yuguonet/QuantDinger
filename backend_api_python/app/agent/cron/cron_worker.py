# -*- coding: utf-8 -*-
"""
Cron Worker — Agent 定时任务后台执行器（APScheduler 版，2026-10-19 重实现）。

设计依据：《cron_重实现方案_20261019.md》原方案 + 附录 A/B/C。核心变化：
  · 调度核心：threading.Timer 自调度 → APScheduler DateTrigger 链式武装
    （表达式是唯一真源，next_fire 由 scheduler.trigger 推导；DB 的 next_run 仅展示）
  · 跨进程互斥：claim 式（UPDATE/DELETE ... RETURNING 原子抢执行权），
    TTL = timeout_seconds + margin（附录 A7-1），不长占 PG 连接（附录 A1-1）
  · one_shot：DELETE ... RETURNING 作原子 claim，天然至多一次（附录 B1-4 选项一）
  · 重启 catch-up：启动时对照 DB next_run 补触发宽限内的错过触发，标 is_misfire
    （附录 B1-3——APScheduler misfire 只覆盖进程活着的场景）
  · 对账循环（默认 45s）：DB ⇄ 内存 trigger diff，解决多 worker CRUD 传播
    （附录 B1-2）；同时做 runs 表保留清理
  · 执行统一包装器：claim/交易日/并发预算/超时/状态/SSE/runs 一条龙，
    手动触发（routes/cron.py 借用 _execute_*）与定时执行同一条路径（附录 B2-5）
  · 交易日闸：trading_days_only 任务仅交易日执行，日历不可用 fail-open（附录 B2-9）
  · function 任务补 trace 事件（trigger=manual/cron/catchup）

保留的对外契约（tests/test_v6_cron_boundary.py 钉住，勿改名）：
  start_cron_worker / schedule_job_from_db / unschedule_job / get_scheduled_jobs /
  subscribe / unsubscribe / _execute_prompt_job / _execute_function_job

已知限制（有意为之，附录 B2-8）：SSE 总线是进程内存队列，多 worker 时前端只能收到
本进程事件；跨进程事实源是 qd_agent_cron_runs 表（前端可轮询）。
"""
from __future__ import annotations

import importlib
import logging
import os
import queue
import threading
import time as _time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from ..modpath import resolve_function_path
from . import scheduler as _sched

logger = logging.getLogger(__name__)

TZ_CN = timezone(timedelta(hours=8))

# ═══════════════════════════════════════════════════════════════
#  配置
# ═══════════════════════════════════════════════════════════════

#: claim TTL = timeout_seconds + margin（附录 A7-1：防执行未完被他进程 re-claim 双跑）
CLAIM_MARGIN_SECONDS = int(os.getenv("QD_CRON_CLAIM_MARGIN", "300"))
#: 并发预算（附录 C1-15）：同时执行的 cron 任务上限，超出排队并记录等待时长
MAX_CONCURRENT_RUNS = int(os.getenv("QD_CRON_MAX_CONCURRENT", "3"))
#: 对账周期（秒）（附录 B1-2：多 worker CRUD 传播）
RECONCILE_SECONDS = int(os.getenv("QD_CRON_RECONCILE_SECONDS", "45"))
#: runs 表保留天数（附录 B3-11）
RUNS_KEEP_DAYS = int(os.getenv("QD_CRON_RUNS_KEEP_DAYS", "30"))

_run_slots = threading.BoundedSemaphore(MAX_CONCURRENT_RUNS)


def claim_ttl_seconds(timeout_seconds: int) -> int:
    """claim 持有时长 = 执行超时 + margin。"""
    return int(timeout_seconds or 300) + CLAIM_MARGIN_SECONDS


# ═══════════════════════════════════════════════════════════════
#  SSE 事件总线（已知限制见文件头：跨进程事实源是 qd_agent_cron_runs）
# ═══════════════════════════════════════════════════════════════

_subscribers: List[queue.Queue] = []
_subscribers_lock = threading.Lock()


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
#  cron 表达式工具（兼容保留：cron_tools / 外部引用）
#  ⚠ 下一触发时刻的唯一推导入口是 next_cron_time → scheduler.next_fire_time
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
    """检查时间是否匹配 cron 表达式（含标准 cron 0=周日语义）。支持 | 多段。"""
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


def next_cron_time(cron_expr: str, after: datetime,
                   max_search_days: int = 62) -> Optional[datetime]:
    """计算 `after` 之后的下一触发时刻。支持 | 多段，取最早。

    Returns:
        下一个匹配时刻；**表达式非法或窗口内无匹配时返回 None**（绝不假调度——
        旧事故：非法表达式退化成每分钟跑一遍 agent）。实现委托 APScheduler
        trigger（星期映射见 scheduler.py），本函数是唯一推导入口。
    """
    return _sched.next_fire_time(cron_expr, after, max_search_days=max_search_days)


# ═══════════════════════════════════════════════════════════════
#  Schema 双路迁移（附录 B1-1：docker 首启 SQL + 启动幂等 ensure）
#  ⚠ 与 migrations/qd_cron_jobs_v2_20261019.sql 同源，改动须两处同步
# ═══════════════════════════════════════════════════════════════

_SCHEMA_STATEMENTS = (
    "ALTER TABLE qd_agent_cron_jobs ADD COLUMN IF NOT EXISTS session_id VARCHAR(128)",
    "ALTER TABLE qd_agent_cron_jobs ADD COLUMN IF NOT EXISTS misfire_grace_time INTEGER",
    "ALTER TABLE qd_agent_cron_jobs ADD COLUMN IF NOT EXISTS trading_days_only BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE qd_agent_cron_jobs ADD COLUMN IF NOT EXISTS timeout_seconds INTEGER NOT NULL DEFAULT 300",
    "ALTER TABLE qd_agent_cron_jobs ADD COLUMN IF NOT EXISTS locked_until TIMESTAMPTZ",
    "ALTER TABLE qd_agent_cron_jobs ADD COLUMN IF NOT EXISTS run_token VARCHAR(64)",
    "ALTER TABLE qd_agent_cron_jobs ADD COLUMN IF NOT EXISTS next_run TIMESTAMPTZ",
    """CREATE TABLE IF NOT EXISTS qd_agent_cron_runs (
        id SERIAL PRIMARY KEY,
        job_id INTEGER,
        job_name VARCHAR(128),
        trigger_source VARCHAR(16) NOT NULL DEFAULT 'cron',
        fire_time TIMESTAMPTZ,
        started_at TIMESTAMPTZ,
        finished_at TIMESTAMPTZ,
        status VARCHAR(24) NOT NULL,
        is_misfire BOOLEAN NOT NULL DEFAULT FALSE,
        queue_wait_seconds REAL,
        duration_seconds REAL,
        output_preview TEXT,
        error TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_cron_runs_job ON qd_agent_cron_runs(job_id, started_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_cron_runs_started ON qd_agent_cron_runs(started_at DESC)",
)

_schema_ensured = False


def _ensure_cron_schema():
    """存量库幂等补列/建表（fail-open：单条失败只告警，不让启动崩）。"""
    global _schema_ensured
    if _schema_ensured:
        return
    try:
        from app.utils.db import get_db_connection
        with get_db_connection() as conn:
            cur = conn.cursor()
            for stmt in _SCHEMA_STATEMENTS:
                try:
                    cur.execute(stmt)
                except Exception as e:
                    logger.warning("[CronWorker] schema 语句失败(跳过): %s — %s",
                                   stmt.splitlines()[0][:80], e)
            conn.commit()
        _schema_ensured = True
        logger.info("[CronWorker] schema ensure 完成（%d 条语句）", len(_SCHEMA_STATEMENTS))
    except Exception as e:
        logger.error("[CronWorker] schema ensure 失败: %s", e)


# ═══════════════════════════════════════════════════════════════
#  DB 小工具
# ═══════════════════════════════════════════════════════════════

_JOB_COLUMNS = ("id, name, cron_expr, mode, prompt, function_path, description, "
                "one_shot, enabled, session_id, misfire_grace_time, trading_days_only, "
                "timeout_seconds, locked_until, run_token, next_run, last_run_at")


def _as_aware(dt):
    """naive → 本时区补正（B3-13：TIMESTAMPTZ 正常返回 aware，防御性兜底比较不抛）。"""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=TZ_CN)
    return dt


def _load_job_row(job_id: int) -> Optional[dict]:
    try:
        from app.utils.db import get_db_connection
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(f"SELECT {_JOB_COLUMNS} FROM qd_agent_cron_jobs WHERE id = %s", (job_id,))
            row = cur.fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error("[CronWorker] 读取任务 %s 失败: %s", job_id, e)
        return None


def _load_enabled_jobs() -> List[dict]:
    try:
        from app.utils.db import get_db_connection
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(f"SELECT {_JOB_COLUMNS} FROM qd_agent_cron_jobs WHERE enabled = TRUE ORDER BY id")
            rows = cur.fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("[CronWorker] 读取 enabled 任务失败: %s", e)
        return []


def _record_run(*, job_id, job_name, status: str, trigger_source: str = "cron",
                fire_time=None, started_at=None, finished_at=None,
                is_misfire: bool = False, queue_wait=None, duration=None,
                output_preview: str = "", error: str = ""):
    """运行历史落账（附录 A2-5：cron 层证据链，含 skipped/missed）。"""
    try:
        from app.utils.db import get_db_connection
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO qd_agent_cron_runs
                    (job_id, job_name, trigger_source, fire_time, started_at,
                     finished_at, status, is_misfire, queue_wait_seconds,
                     duration_seconds, output_preview, error)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (job_id, job_name, trigger_source, fire_time, started_at or datetime.now(TZ_CN),
                  finished_at or datetime.now(TZ_CN), status, is_misfire,
                  round(queue_wait, 3) if queue_wait is not None else None,
                  round(duration, 3) if duration is not None else None,
                  (output_preview or "")[:500], (error or "")[:1000]))
            conn.commit()
    except Exception as e:
        logger.error("[CronWorker] runs 落账失败 job=%s: %s", job_id, e)


def _update_job_status(job_id: int, success: bool, error: str = ""):
    try:
        from app.utils.db import get_db_connection
        now = datetime.now(TZ_CN)
        with get_db_connection() as conn:
            cur = conn.cursor()
            if success:
                cur.execute("""
                    UPDATE qd_agent_cron_jobs
                    SET last_run_at = %s, last_success_at = %s, last_error = NULL,
                        error_count = 0, total_runs = total_runs + 1, updated_at = %s
                    WHERE id = %s
                """, (now, now, now, job_id))
            else:
                cur.execute("""
                    UPDATE qd_agent_cron_jobs
                    SET last_run_at = %s, last_error = %s,
                        error_count = error_count + 1, total_runs = total_runs + 1, updated_at = %s
                    WHERE id = %s
                """, (now, error[:1000], now, job_id))
            conn.commit()
    except Exception as e:
        logger.error("[CronWorker] 更新任务状态失败 id=%s: %s", job_id, e)


def _disable_job(job_id: int, reason: str):
    """非法表达式 → 禁用 + 日志（附录 A7-3：不静默跳过、不让启动崩）。"""
    logger.error("[CronWorker] 任务 %s 非法已禁用: %s", job_id, reason)
    try:
        from app.utils.db import get_db_connection
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE qd_agent_cron_jobs
                SET enabled = FALSE, last_error = %s, updated_at = %s
                WHERE id = %s
            """, (f"[cron] 表达式非法已禁用: {reason[:200]}", datetime.now(TZ_CN), job_id))
            conn.commit()
    except Exception as e:
        logger.error("[CronWorker] 禁用任务 %s 失败: %s", job_id, e)


# ═══════════════════════════════════════════════════════════════
#  claim 互斥（附录 A1-1 / B1-4 / A7-1）
#  · recurring: UPDATE ... WHERE enabled AND 空闲 RETURNING *（短连接，不长占）
#  · one_shot : DELETE ... RETURNING * 作原子 claim，天然至多一次
#  · 执行结束由执行线程释放（finally）；崩溃靠 TTL 自然恢复
# ═══════════════════════════════════════════════════════════════


def _claim_job(job: dict) -> Optional[dict]:
    """抢执行权。成功返回**以 DB 为准**的完整任务行；失败 None（被他人持有/已消失/已禁用）。"""
    job_id = job.get("id")
    if not job_id:
        return None
    token = uuid.uuid4().hex
    try:
        from app.utils.db import get_db_connection
        with get_db_connection() as conn:
            cur = conn.cursor()
            if job.get("one_shot"):
                # 原子 claim：拿到行的进程才有执行权（行即删除 ⇒ 至多一次）
                cur.execute("DELETE FROM qd_agent_cron_jobs WHERE id = %s AND enabled = TRUE RETURNING *",
                            (job_id,))
            else:
                ttl = claim_ttl_seconds(int(job.get("timeout_seconds") or 300))
                now = datetime.now(TZ_CN)
                cur.execute("""
                    UPDATE qd_agent_cron_jobs
                    SET locked_until = %s, run_token = %s
                    WHERE id = %s AND enabled = TRUE
                      AND (locked_until IS NULL OR locked_until < %s)
                    RETURNING *
                """, (now + timedelta(seconds=ttl), token, job_id, now))
            row = cur.fetchone()
            conn.commit()
        if not row:
            return None
        claimed = dict(row)
        claimed["__run_token"] = token
        return claimed
    except Exception as e:
        logger.error("[CronWorker] claim 失败 job=%s: %s", job_id, e)
        return None


def _release_claim(job_id: int, token: str):
    """执行线程自然结束时释放锁（附录 B2-7：超时不释放 ⇒ 任何时刻至多一个执行体）。"""
    try:
        from app.utils.db import get_db_connection
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE qd_agent_cron_jobs
                SET locked_until = NULL, run_token = NULL
                WHERE id = %s AND run_token = %s
            """, (job_id, token))
            conn.commit()
    except Exception as e:
        logger.error("[CronWorker] 释放 claim 失败 job=%s: %s", job_id, e)


# ═══════════════════════════════════════════════════════════════
#  交易日闸（附录 A1-4 / B2-9：fail-open + 日志，抄 market_cn/scheduler 先例）
# ═══════════════════════════════════════════════════════════════


def _is_trading_day_today() -> bool:
    try:
        from app.utils.trading_calendar import is_trading_day
        return bool(is_trading_day(datetime.now(TZ_CN).date()))
    except Exception as e:
        logger.warning("[CronWorker] 交易日历不可用，fail-open 按交易日执行: %s", e)
        return True


# ═══════════════════════════════════════════════════════════════
#  执行体（只干活 + 填结果 + 释放 claim，不做任何记账）
# ═══════════════════════════════════════════════════════════════


def _import_function(path: str) -> Callable:
    # ⚠ DB 里存量定时任务存的是**裸名**（如 `cron.cron_tools.xxx`），改造后必须
    # 补包前缀才 import 得到；新写的配置可以是绝对名。resolve_function_path 两者都认。
    module_path, func_name = resolve_function_path(path)
    mod = importlib.import_module(module_path)
    fn = getattr(mod, func_name)
    if not callable(fn):
        raise TypeError(f"{path} is not callable")
    return fn


def _trace(job: dict, event: str, **fields):
    """function 任务 trace 事件（方案步骤 5：trigger=cron/manual/catchup）。"""
    try:
        from app.agent.audit.trace_adapter import TraceAdapter
        ta = TraceAdapter()
        ta.emit(event, job_id=job.get("id"), job_name=job.get("name"),
                mode=job.get("mode"), **fields)
    except Exception as e:
        logger.debug("[CronWorker] trace 事件失败(忽略): %s", e)


def _run_prompt_body(job: dict, outcome: dict):
    from ..message_queue import submit
    sid = job.get("session_id") or ("cron:" + str(job.get("id") or "default"))
    timeout = int(job.get("timeout_seconds") or 300)
    future = submit(job.get("prompt", ""), session_id=sid, timeout=timeout)
    outcome["result"] = future.result(timeout=timeout)
    outcome["ok"] = True


def _run_function_body(job: dict, outcome: dict):
    _trace(job, "run_start", trigger=outcome.get("trigger_source", "manual"))
    try:
        fn = _import_function(job.get("function_path", ""))
        result = fn()
        outcome["result"] = str(result)[:500] if result is not None else "ok"
        outcome["ok"] = True
        _trace(job, "run_finish", status="ok")
    except Exception:
        _trace(job, "run_finish", status="error")
        raise


# ═══════════════════════════════════════════════════════════════
#  统一执行包装器（附录 A2-7 / B2-5：定时/手动/补跑同一条路径）
# ═══════════════════════════════════════════════════════════════


def _run_job(job: dict, trigger_source: str = "cron",
             fire_time=None, is_misfire: bool = False,
             force_mode: Optional[str] = None):
    """claim → 交易日闸 → 并发预算 → 执行(带超时) → 记账/SSE。

    语义要点：
      · one_shot 的 claim 即删除（含手动触发——"执行一次后自动删除"含手动，已知语义）
      · 交易日闸只作用于 cron/catchup；手动触发是显式意图，直跑（附录 B2-5）
      · 超时：记 status=timeout 且**不释放锁**，执行线程自然结束时释放（附录 B2-7）
      · 并发预算：超出 MAX_CONCURRENT_RUNS 排队，等待时长落 runs 表（附录 C1-15）
    """
    job_id = job.get("id", 0)
    job_name = job.get("name", "unknown")
    mode = force_mode or job.get("mode", "prompt")

    # ── 交易日闸（手动触发豁免）──
    if trigger_source != "manual" and job.get("trading_days_only") and not _is_trading_day_today():
        if job.get("one_shot"):
            _claim_job(job)     # 一次性任务没有下次触发：消费掉，如实记账
        _record_run(job_id=job_id, job_name=job_name, status="skipped_nontrading",
                    trigger_source=trigger_source, fire_time=fire_time, is_misfire=is_misfire)
        _publish(_make_event("job_skip", job_id, job_name,
                             mode=mode, reason="non_trading_day", trigger=trigger_source))
        logger.info("[CronWorker] 非交易日跳过: %s", job_name)
        return

    # ── 并发预算（排队）──
    t_wait = _time.monotonic()
    _run_slots.acquire()
    queue_wait = _time.monotonic() - t_wait
    try:
        # ── claim（以 DB 返回的完整行为准）──
        claimed = _claim_job(job)
        if claimed is None:
            _record_run(job_id=job_id, job_name=job_name, status="skipped_lock",
                        trigger_source=trigger_source, fire_time=fire_time,
                        is_misfire=is_misfire, queue_wait=queue_wait)
            logger.info("[CronWorker] 未取得执行权(锁占用/已删除/已禁用): %s", job_name)
            return
        job = claimed
        job_id = job.get("id", job_id)
        job_name = job.get("name", job_name)
        token = job.get("__run_token", "")
        one_shot = bool(job.get("one_shot"))
        timeout = int(job.get("timeout_seconds") or 300)

        started_at = datetime.now(TZ_CN)
        _publish(_make_event("job_start", job_id, job_name,
                             mode=mode, trigger=trigger_source, is_misfire=is_misfire))

        outcome: Dict[str, Any] = {"ok": False, "result": None, "error": None,
                                   "trigger_source": trigger_source}
        body = _run_prompt_body if mode == "prompt" else _run_function_body

        def _body():
            try:
                body(job, outcome)
            except Exception as e:               # noqa: BLE001 — 交给记账层
                outcome["error"] = e
            finally:
                # 执行线程自然结束才释放 claim（超时后防双跑的关键，附录 B2-7）
                if not one_shot and token:
                    _release_claim(job_id, token)

        th = threading.Thread(target=_body, daemon=True, name=f"cron-run-{job_id}")
        th.start()
        th.join(timeout=timeout)

        if th.is_alive():
            # 超时：如实记 timeout；锁由 _body 的 finally 在线程真正结束时释放
            _record_run(job_id=job_id, job_name=job_name, status="timeout",
                        trigger_source=trigger_source, fire_time=fire_time,
                        started_at=started_at, is_misfire=is_misfire,
                        queue_wait=queue_wait, duration=_time.monotonic() - t_wait,
                        error=f"执行超时 {timeout}s（线程仍在收尾，锁随其结束释放）")
            _update_job_status(job_id, success=False, error=f"timeout {timeout}s")
            _publish(_make_event("job_error", job_id, job_name, mode=mode,
                                 trigger=trigger_source, error=f"timeout {timeout}s"))
            logger.warning("[CronWorker] ⏱ 超时: %s (%ds)", job_name, timeout)
            return

        duration = _time.monotonic() - t_wait
        if outcome.get("ok"):
            preview = str(outcome.get("result") or "")[:500]
            _update_job_status(job_id, success=True)
            _record_run(job_id=job_id, job_name=job_name, status="ok",
                        trigger_source=trigger_source, fire_time=fire_time,
                        started_at=started_at, is_misfire=is_misfire,
                        queue_wait=queue_wait, duration=duration,
                        output_preview=preview)
            _publish(_make_event("job_success", job_id, job_name,
                                 mode=mode, trigger=trigger_source,
                                 content_preview=preview))
            logger.info("[CronWorker]  完成: %s", job_name)
        else:
            err = str(outcome.get("error") or "unknown")
            _update_job_status(job_id, success=False, error=err)
            _record_run(job_id=job_id, job_name=job_name, status="error",
                        trigger_source=trigger_source, fire_time=fire_time,
                        started_at=started_at, is_misfire=is_misfire,
                        queue_wait=queue_wait, duration=duration, error=err)
            _publish(_make_event("job_error", job_id, job_name, mode=mode,
                                 trigger=trigger_source, error=err))
            logger.error("[CronWorker]  失败: %s — %s", job_name, err)
    finally:
        _run_slots.release()


def _execute_prompt_job(job: Dict[str, Any]):
    """手动触发（routes/cron.py 借用的私有名，契约勿改）→ 统一包装器。"""
    _run_job(job, trigger_source="manual", force_mode="prompt")


def _execute_function_job(job: Dict[str, Any]):
    """手动触发（routes/cron.py 借用的私有名，契约勿改）→ 统一包装器。"""
    _run_job(job, trigger_source="manual", force_mode="function")


# ═══════════════════════════════════════════════════════════════
#  调度核心：APScheduler DateTrigger 链 + 对账循环（附录 B1-2）
#  每个 gunicorn worker 各跑一份，靠 claim 竞争夺执行权（附录 A7-2）
# ═══════════════════════════════════════════════════════════════

_scheduler = None
_known_sigs: Dict[int, str] = {}
_sched_lock = threading.Lock()
_stop_event = threading.Event()
_reconcile_thread: Optional[threading.Thread] = None


def _job_signature(row: dict) -> str:
    return repr((row.get("cron_expr"), row.get("mode"), row.get("prompt"),
                 row.get("function_path"), row.get("one_shot"), row.get("session_id"),
                 row.get("timeout_seconds"), row.get("trading_days_only"),
                 row.get("misfire_grace_time")))


#: 武装时的搜索窗口：比 next_cron_time 公共契约（62 天）宽，合法的稀疏表达式
#: （如年度 `0 9 1 1 *`）也能武装；62 天窗口返回 None ≠ 非法，不得误禁用。
_ARM_SEARCH_DAYS = 400


def _arm_job(row: dict):
    """为单个任务武装下次触发（DateTrigger 一次性链式）。"""
    global _scheduler
    if _scheduler is None:
        return
    job_id = row["id"]
    now = datetime.now(TZ_CN)
    nxt = next_cron_time(row.get("cron_expr", ""), now, max_search_days=_ARM_SEARCH_DAYS)
    if nxt is None:
        # 附录 A7-3：**真非法**才禁用；合法但窗口内无匹配（如 2/29）只跳过不武装
        if _sched.trigger_from_expr(row.get("cron_expr", "")) is None:
            _disable_job(job_id, row.get("cron_expr", ""))
        else:
            logger.warning("[CronWorker] 任务 %s 合法但 %d 天内无触发点，暂不武装: %r",
                           job_id, _ARM_SEARCH_DAYS, row.get("cron_expr"))
        return
    grace = row.get("misfire_grace_time")
    if grace is None:
        grace = _sched.DEFAULT_MISFIRE_GRACE
    with _sched_lock:
        _scheduler.add_job(
            _on_fire, trigger="date", run_date=nxt,
            id=f"cron-{job_id}", args=[job_id, nxt],
            replace_existing=True, coalesce=True, max_instances=1,
            misfire_grace_time=int(grace),
        )
        _known_sigs[job_id] = _job_signature(row)
    # next_run 仅展示（附录 A2-6：表达式是唯一真源）
    if row.get("next_run") != nxt:
        try:
            from app.utils.db import get_db_connection
            with get_db_connection() as conn:
                cur = conn.cursor()
                cur.execute("UPDATE qd_agent_cron_jobs SET next_run = %s WHERE id = %s",
                            (nxt, job_id))
                conn.commit()
        except Exception as e:
            logger.debug("[CronWorker] next_run 回写失败(展示用,忽略): %s", e)


def _on_fire(job_id: int, planned_fire_time=None):
    """触发点回调：以 DB 当前行状态为准（enabled/one_shot/参数都可能已变）。"""
    row = _load_job_row(job_id)
    if not row or not row.get("enabled"):
        return
    _run_job(row, trigger_source="cron", fire_time=planned_fire_time)


def _unschedule_aps(job_id: int):
    global _scheduler
    with _sched_lock:
        _known_sigs.pop(job_id, None)
        if _scheduler is not None:
            try:
                _scheduler.remove_job(f"cron-{job_id}")
            except Exception:
                pass


def _reconcile_once():
    """DB ⇄ 内存 trigger 对账（多 worker CRUD 传播，附录 B1-2）。"""
    global _scheduler
    rows = _load_enabled_jobs()
    seen = set()
    for row in rows:
        job_id = row["id"]
        seen.add(job_id)
        now = datetime.now(TZ_CN)
        nxt = next_cron_time(row.get("cron_expr", ""), now, max_search_days=_ARM_SEARCH_DAYS)
        if nxt is None:
            if _sched.trigger_from_expr(row.get("cron_expr", "")) is None:
                _disable_job(job_id, row.get("cron_expr", ""))   # 附录 A7-3（真非法）
                _unschedule_aps(job_id)
            # 合法但稀疏：保持现状，下轮对账再试
            continue
        existing = None
        with _sched_lock:
            if _scheduler is not None:
                existing = _scheduler.get_job(f"cron-{job_id}")
        if (existing is not None
                and _known_sigs.get(job_id) == _job_signature(row)
                and getattr(existing, "next_run_time", None) == nxt):
            continue   # 无变化
        _arm_job(row)
    # 已删除/禁用的任务撤武装
    for job_id in list(_known_sigs.keys()):
        if job_id not in seen:
            _unschedule_aps(job_id)


def _cleanup_runs():
    """runs 表保留策略（附录 B3-11）。"""
    try:
        from app.utils.db import get_db_connection
        cutoff = datetime.now(TZ_CN) - timedelta(days=RUNS_KEEP_DAYS)
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM qd_agent_cron_runs WHERE started_at < %s", (cutoff,))
            conn.commit()
    except Exception as e:
        logger.debug("[CronWorker] runs 清理失败(忽略): %s", e)


def _catchup_missed():
    """重启 catch-up（附录 B1-3）：DB next_run 已过期的触发补跑/记 missed。

    宽限内 ⇒ 补跑一次（is_misfire=True，claim 防多进程重复补）；
    超宽限 ⇒ 记 missed；one_shot 过期 ⇒ 记完清掉（没有下次触发了）。
    """
    now = datetime.now(TZ_CN)
    for row in _load_enabled_jobs():
        nxt = _as_aware(row.get("next_run"))
        if not nxt or nxt >= now:
            continue
        last = _as_aware(row.get("last_run_at"))
        if last is not None and last >= nxt:
            # 该计划触发**已执行过**：_on_fire 后 next_run 要等 ≤对账周期才回写，
            # 此窗口内重启 ⇒ 旧行为会补跑刚完成的任务（grace 内）或误记 missed
            # （超 grace，污染证据链）。防重/防误记守卫（第三轮审查 #1）。
            continue
        grace = row.get("misfire_grace_time")
        if grace is None:
            grace = _sched.DEFAULT_MISFIRE_GRACE
        late = (now - nxt).total_seconds()
        if late <= int(grace):
            logger.info("[CronWorker] catch-up 补跑: %s（错过 %.0f 分钟）", row.get("name"), late / 60)
            threading.Thread(target=_run_job, args=(row,), daemon=True,
                             kwargs={"trigger_source": "catchup", "fire_time": nxt,
                                     "is_misfire": True},
                             name=f"cron-catchup-{row['id']}").start()
        else:
            _record_run(job_id=row["id"], job_name=row.get("name"), status="missed",
                        trigger_source="catchup", fire_time=nxt, is_misfire=True,
                        error=f"超出补跑宽限 {grace}s（错过 {int(late)}s）")
            _publish(_make_event("job_missed", row["id"], row.get("name", "?"),
                                 mode=row.get("mode", "prompt"), missed_seconds=int(late)))
            if row.get("one_shot"):
                _claim_job(row)   # 一次性任务过期：消费掉（claim 即删除）


# ═══════════════════════════════════════════════════════════════
#  进程内 MISSED 监听（附录 A2-5：EVENT_JOB_MISSED 落账）
#  与 _catchup_missed 互补：listener 覆盖**进程活着但超宽限**的错过
#  （睡眠/卡顿/排队过久）；catch-up 覆盖**重启窗口**。缺一即静默丢触发。
# ═══════════════════════════════════════════════════════════════


def _on_job_missed(event):
    """APScheduler 报告错过触发（超出 misfire 宽限）→ 落账 missed + SSE。"""
    try:
        raw = str(getattr(event.job, "id", "") or "")
        if not raw.startswith("cron-"):
            return
        job_id = int(raw.split("-", 1)[1])
    except (ValueError, IndexError):
        return
    row = _load_job_row(job_id) or {"id": job_id, "name": "?", "mode": "prompt",
                                    "one_shot": False, "enabled": True}
    fire = _as_aware(getattr(event, "scheduled_run_time", None))
    late_s = (datetime.now(TZ_CN) - fire).total_seconds() if fire else None
    err = "进程内错过触发（超出 misfire 宽限）"
    if late_s is not None:
        err += f"，已过 {int(late_s)}s"
    _record_run(job_id=job_id, job_name=row.get("name", "?"), status="missed",
                trigger_source="cron", fire_time=fire, is_misfire=True, error=err)
    _publish(_make_event("job_missed", job_id, row.get("name", "?"),
                         mode=row.get("mode", "prompt"),
                         missed_seconds=int(late_s) if late_s is not None else None))
    logger.warning("[CronWorker] ⚠ 进程内错过触发: %s（%s）", row.get("name", "?"), err)
    if row.get("one_shot"):
        _claim_job(row)   # 与 catch-up 超宽限分支一致：消费掉，不留残行


def _attach_listeners(scheduler):
    """挂 APScheduler 事件监听（_boot 启动调度器前调用一次）。"""
    from apscheduler.events import EVENT_JOB_MISSED
    scheduler.add_listener(_on_job_missed, EVENT_JOB_MISSED)


# ═══════════════════════════════════════════════════════════════
#  对外接口（契约：tests/test_v6_cron_boundary.py 钉住）
# ═══════════════════════════════════════════════════════════════


def schedule_job_from_db(job_id: int):
    """外部调用：任务创建/更新后，立即重算并武装。"""
    row = _load_job_row(job_id)
    if not row or not row.get("enabled"):
        _unschedule_aps(job_id)
        return
    _arm_job(row)


def unschedule_job(job_id: int):
    """外部调用：任务删除/禁用后，撤销武装。"""
    _unschedule_aps(job_id)
    logger.info("[CronWorker] 已取消任务 %s 的调度", job_id)


def get_scheduled_jobs() -> List[dict]:
    """健康检查用（routes/health.py 消费 job_id/timer_name/alive 三个键，勿改）。"""
    global _scheduler
    if _scheduler is None:
        return []
    out = []
    with _sched_lock:
        jobs = list(_scheduler.get_jobs())
    for j in jobs:
        if not str(j.id).startswith("cron-"):
            continue
        try:
            jid = int(str(j.id).split("-", 1)[1])
        except (ValueError, IndexError):
            continue
        out.append({"job_id": jid, "timer_name": j.id, "alive": bool(_scheduler.running)})
    return out


# ═══════════════════════════════════════════════════════════════
#  启动 / 停止
# ═══════════════════════════════════════════════════════════════

_worker_started = False


def _wait_db_ready(max_try: int = 60, interval: float = 2.0) -> bool:
    """DB-ready 轮询（替换旧 sleep(10) 硬编码；compose 里 DB 起慢不丢首跑）。"""
    for _ in range(max_try):
        try:
            from app.utils.db import get_db_connection
            with get_db_connection() as conn:
                conn.cursor().execute("SELECT 1")
            return True
        except Exception:
            _time.sleep(interval)
    return False


def start_cron_worker():
    """启动 Cron Worker（app/__init__.py 调用）。幂等。"""
    global _worker_started, _scheduler, _reconcile_thread
    if _worker_started:
        return
    _worker_started = True

    def _boot():
        global _scheduler
        if not _wait_db_ready():
            logger.error("[CronWorker] DB 始终未就绪，调度器不启动")
            return
        _ensure_cron_schema()
        _catchup_missed()
        _scheduler = _sched.make_scheduler()
        _attach_listeners(_scheduler)
        _scheduler.start()
        _reconcile_once()
        logger.info("[CronWorker] APScheduler 版已启动（对账周期 %ds, 并发 %d, claim margin %ds）",
                    RECONCILE_SECONDS, MAX_CONCURRENT_RUNS, CLAIM_MARGIN_SECONDS)
        while not _stop_event.is_set():
            _stop_event.wait(RECONCILE_SECONDS)
            if _stop_event.is_set():
                break
            try:
                _reconcile_once()
                _cleanup_runs()
            except Exception as e:
                logger.error("[CronWorker] 对账异常: %s", e, exc_info=True)

    _reconcile_thread = threading.Thread(target=_boot, daemon=True, name="cron-worker-init")
    _reconcile_thread.start()
    logger.info("[CronWorker] 自调度模式启动中（DB-ready 后接管）")


def stop_cron_worker():
    global _scheduler
    _stop_event.set()
    with _sched_lock:
        if _scheduler is not None:
            try:
                _scheduler.shutdown(wait=False)
            except Exception:
                pass
            _scheduler = None
        _known_sigs.clear()
    logger.info("[CronWorker] 已停止所有定时任务")
