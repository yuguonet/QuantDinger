# -*- coding: utf-8 -*-
"""v38：cron 重实现（APScheduler 调度核心）的钉板测试（2026-10-19）。

设计依据：《cron_重实现方案_20261019.md》原方案 §4-2 + 附录 A6/B3-14/C1-15。
口径（B3-14）：触发计算全用纯函数断言（trigger.get_next_fire_time 固定 now），
不起真实 scheduler、不 sleep；DB 用假连接记录 SQL（断言语义而非连库）。

黄金用例（附录 A3-9 星期陷阱）：标准 cron 0=周日，APScheduler 3.x day_of_week
0=周一（实测）——`30 18 * * 0` 必须落**周日**而非周一。
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

TZ = ZoneInfo("Asia/Shanghai")
# 2026-10-19 是**周一**（黄金用例的锚点）
MON = datetime(2026, 10, 19, 0, 0, tzinfo=TZ)


# ── 假 DB：只记录 SQL 语义，不连库 ─────────────────────────────

class _FakeCursor:
    def __init__(self, log, fetchone_queue, all_rows):
        self._log = log
        self._q = fetchone_queue
        self._all = all_rows

    def execute(self, sql, params=None):
        self._log.append((sql, params))

    def fetchone(self):
        return self._q.pop(0) if self._q else None

    def fetchall(self):
        out = list(self._all)
        self._all.clear()
        return out


class _FakeConn:
    def __init__(self, log, fetchone_queue, all_rows):
        self._cur = _FakeCursor(log, fetchone_queue, all_rows)

    def cursor(self):
        return self._cur

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _install_fake_db(monkeypatch, fetchone_rows=None, all_rows=None):
    """把 app.utils.db 换成假实现，返回 SQL 日志列表。"""
    log = []
    q = list(fetchone_rows or [])
    allr = list(all_rows or [])
    mod = types.ModuleType("app.utils.db")
    mod.get_db_connection = lambda: _FakeConn(log, q, allr)
    monkeypatch.setitem(sys.modules, "app.utils.db", mod)
    return log


# ═══════════════════════════════════════════════════════════════
#  ① 星期映射（最大陷阱，附录 A3-9）
# ═══════════════════════════════════════════════════════════════

def test_weekday_zero_is_sunday_not_monday():
    """标准 cron 0=周日 ⇒ `30 18 * * 0` 落周日（10-25），绝不是周一（10-19）。"""
    from app.agent.cron import scheduler as sched
    nxt = sched.next_fire_time("30 18 * * 0", MON)
    assert nxt == datetime(2026, 10, 25, 18, 30, tzinfo=TZ)
    assert nxt.strftime("%A") == "Sunday"


def test_weekday_one_is_monday():
    from app.agent.cron import scheduler as sched
    nxt = sched.next_fire_time("30 18 * * 1", MON)
    assert nxt == datetime(2026, 10, 19, 18, 30, tzinfo=TZ)   # 本周一（今天）
    assert nxt.strftime("%A") == "Monday"


def test_weekday_range_mon_fri():
    from app.agent.cron import scheduler as sched
    # 10-24 是周六 ⇒ 工作日表达式跳到下周一 10-26
    sat = datetime(2026, 10, 24, 8, 0, tzinfo=TZ)
    nxt = sched.next_fire_time("0 9 * * 1-5", sat)
    assert nxt == datetime(2026, 10, 26, 9, 0, tzinfo=TZ)


# ═══════════════════════════════════════════════════════════════
#  ② 多段 `|` 语义
# ═══════════════════════════════════════════════════════════════

def test_multi_segment_takes_earliest():
    from app.agent.cron import scheduler as sched
    a = sched.next_fire_time("0 18 * * 1|30 9 * * 1", MON)
    b = sched.next_fire_time("30 9 * * 1|0 18 * * 1", MON)   # 顺序无关
    assert a == b == datetime(2026, 10, 19, 9, 30, tzinfo=TZ)


def test_strictly_after_boundary():
    """after 恰为触发点 ⇒ 返回下一次，不返回自身（旧实现契约）。"""
    from app.agent.cron import scheduler as sched
    at_fire = datetime(2026, 10, 19, 18, 30, tzinfo=TZ)
    nxt = sched.next_fire_time("30 18 * * 1", at_fire)
    assert nxt == at_fire + timedelta(days=7)


# ═══════════════════════════════════════════════════════════════
#  ③ 非法表达式 → None（绝不假调度）
# ═══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("expr", ["bad expr", "0 18 * *", "", "70 18 * * *", "0 18 * * 9"])
def test_invalid_expr_returns_none(expr):
    from app.agent.cron import scheduler as sched
    from app.agent.cron.cron_worker import next_cron_time
    assert sched.trigger_from_expr(expr) is None
    assert sched.next_fire_time(expr, MON) is None
    assert next_cron_time(expr, MON) is None     # cron_worker 委托路径同语义


def test_window_no_match_returns_none():
    """max_search_days 窗口内无匹配 → None（如年度表达式）。"""
    from app.agent.cron import scheduler as sched
    # 1 月 1 日 09:00；从 1 月 2 日看，62 天窗口内无匹配
    jan2 = datetime(2026, 1, 2, 0, 0, tzinfo=TZ)
    assert sched.next_fire_time("0 9 1 1 *", jan2, max_search_days=62) is None
    # 12 月中看，17 天后就是 1 月 1 日 ⇒ 有
    dec15 = datetime(2026, 12, 15, 0, 0, tzinfo=TZ)
    assert sched.next_fire_time("0 9 1 1 *", dec15, max_search_days=62) \
        == datetime(2027, 1, 1, 9, 0, tzinfo=TZ)


# ═══════════════════════════════════════════════════════════════
#  ④ guards：跨段最小间隔（附录 B2-6 修复的绕过洞）
# ═══════════════════════════════════════════════════════════════

def test_guards_cross_segment_interval_not_bypassable():
    """`0 9 * * *|1 9 * * *` 实际相邻 1 分钟 —— 旧实现按段各估 1440 放行（bug）。"""
    from app.agent.cron.guards import validate_cron_expr, min_interval_minutes
    assert min_interval_minutes("0 9 * * *|1 9 * * *") == 1
    v = validate_cron_expr("0 9 * * *|1 9 * * *")
    assert v["ok"] is False


def test_guards_daily_and_hourly_unchanged():
    from app.agent.cron.guards import min_interval_minutes
    assert min_interval_minutes("0 9 * * *") == 1440        # 每天一次
    assert min_interval_minutes("*/10 * * * *") == 10       # 每 10 分钟
    assert min_interval_minutes("0 9,18 * * *") == 540      # 双时点（旧实现误估 1440）


# ═══════════════════════════════════════════════════════════════
#  ⑤ claim 互斥语义（附录 A1-1 / B1-4 / A7-1）
# ═══════════════════════════════════════════════════════════════

def test_claim_ttl_is_timeout_plus_margin():
    from app.agent.cron import cron_worker as cw
    assert cw.claim_ttl_seconds(300) == 300 + cw.CLAIM_MARGIN_SECONDS
    assert cw.claim_ttl_seconds(600) == 600 + cw.CLAIM_MARGIN_SECONDS


def test_claim_recurring_uses_update_with_enabled_and_ttl(monkeypatch):
    from app.agent.cron import cron_worker as cw
    log = _install_fake_db(monkeypatch, fetchone_rows=[{"id": 7, "name": "t", "one_shot": False}])
    claimed = cw._claim_job({"id": 7, "one_shot": False, "timeout_seconds": 300})
    assert claimed and claimed["id"] == 7 and claimed["__run_token"]
    sql, params = log[0]
    assert sql.strip().startswith("UPDATE qd_agent_cron_jobs")
    assert "locked_until" in sql and "run_token" in sql
    assert "enabled = TRUE" in sql                    # 禁用/删除的行拿不到锁（B1-2）
    assert "RETURNING *" in sql
    locked_until = params[0]
    assert locked_until - datetime.now(cw.TZ_CN) > timedelta(seconds=300)   # ≥ timeout+margin


def test_claim_one_shot_uses_delete_returning(monkeypatch):
    """one_shot 的原子 claim = DELETE...RETURNING（附录 B1-4 选项一，至多一次）。"""
    from app.agent.cron import cron_worker as cw
    log = _install_fake_db(monkeypatch, fetchone_rows=[{"id": 9, "name": "once", "one_shot": True}])
    claimed = cw._claim_job({"id": 9, "one_shot": True})
    assert claimed and claimed["id"] == 9
    sql, _ = log[0]
    assert sql.strip().startswith("DELETE FROM qd_agent_cron_jobs")
    assert "RETURNING *" in sql and "enabled = TRUE" in sql


def test_claim_fails_when_lock_held(monkeypatch):
    from app.agent.cron import cron_worker as cw
    _install_fake_db(monkeypatch, fetchone_rows=[])   # UPDATE 0 行 ⇒ 他人持有
    assert cw._claim_job({"id": 3, "one_shot": False}) is None


def test_release_claim_token_guard(monkeypatch):
    from app.agent.cron import cron_worker as cw
    log = _install_fake_db(monkeypatch)
    cw._release_claim(5, "tok123")
    sql, params = log[0]
    assert "locked_until = NULL" in sql and "run_token = %s" in sql
    assert params == (5, "tok123")                    # 只释放自己的锁


# ═══════════════════════════════════════════════════════════════
#  ⑥ 统一执行包装器：记账 / 跳过路径（附录 A2-7 / B2-5）
# ═══════════════════════════════════════════════════════════════

def test_run_job_records_skipped_lock_when_claim_fails(monkeypatch):
    from app.agent.cron import cron_worker as cw
    log = _install_fake_db(monkeypatch, fetchone_rows=[])   # claim 失败
    cw._run_job({"id": 3, "name": "t", "mode": "prompt"}, trigger_source="cron")
    inserts = [s for s, _ in log if "INSERT INTO qd_agent_cron_runs" in s]
    assert inserts, "claim 失败必须落账 skipped_lock（附录 A2-5）"
    status_params = [p for _, p in log if p and "skipped_lock" in p]
    assert status_params


def test_run_job_ok_path_records_run_and_status(monkeypatch):
    from app.agent.cron import cron_worker as cw
    row = {"id": 7, "name": "t", "mode": "prompt", "one_shot": False,
           "timeout_seconds": 300, "__run_token": "tok"}
    log = _install_fake_db(monkeypatch, fetchone_rows=[dict(row)])

    def _quick_body(job, outcome):
        outcome["ok"] = True
        outcome["result"] = "done"
    monkeypatch.setattr(cw, "_run_prompt_body", _quick_body)

    cw._run_job({"id": 7, "name": "t", "mode": "prompt"}, trigger_source="cron")
    sqls = [s for s, _ in log]
    assert any("INSERT INTO qd_agent_cron_runs" in s for s in sqls)
    assert any("UPDATE qd_agent_cron_jobs" in s and "last_success_at" in s for s in sqls)
    assert any(p and "ok" in p for _, p in log if p), "runs 行状态应为 ok"


def test_run_job_non_trading_skip_for_one_shot(monkeypatch):
    """trading_days_only 任务非交易日跳过并落账；one_shot 消费掉（没有下次）。"""
    from app.agent.cron import cron_worker as cw
    monkeypatch.setattr(cw, "_is_trading_day_today", lambda: False)
    log = _install_fake_db(monkeypatch,
                           fetchone_rows=[{"id": 7, "name": "t", "one_shot": True}],
                           all_rows=[])
    cw._run_job({"id": 7, "name": "t", "mode": "prompt",
                 "trading_days_only": True, "one_shot": True}, trigger_source="cron")
    sqls = [s for s, _ in log]
    assert any(s.strip().startswith("DELETE FROM qd_agent_cron_jobs") for s in sqls), \
        "一次性任务非交易日跳过也应消费（否则永远残留）"
    assert any("skipped_nontrading" in (p or ()) for _, p in log if p)


def test_manual_trigger_bypasses_trading_day_gate(monkeypatch):
    """手动触发是显式意图：交易日闸不拦（附录 B2-5）。"""
    from app.agent.cron import cron_worker as cw
    monkeypatch.setattr(cw, "_is_trading_day_today", lambda: False)
    log = _install_fake_db(monkeypatch, fetchone_rows=[])
    cw._run_job({"id": 7, "name": "t", "mode": "prompt",
                 "trading_days_only": True}, trigger_source="manual")
    assert not any("skipped_nontrading" in (p or ()) for _, p in log if p)


# ═══════════════════════════════════════════════════════════════
#  ⑦ 重启 catch-up（附录 B1-3）
# ═══════════════════════════════════════════════════════════════

class _SyncThread:
    """不起真线程：start() 直接跑（保证断言确定性，B3-14 不 sleep）。"""
    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self._target, self._args, self._kwargs = target, args, (kwargs or {})

    def start(self):
        if self._target:
            self._target(*self._args, **self._kwargs)

    def join(self, timeout=None):
        pass

    def is_alive(self):
        return False


def test_catchup_within_grace_fires_as_misfire(monkeypatch):
    from app.agent.cron import cron_worker as cw
    now = datetime.now(cw.TZ_CN)
    rows = [{"id": 1, "name": "late", "one_shot": False, "enabled": True,
             "next_run": now - timedelta(seconds=60), "misfire_grace_time": 300},
            {"id": 2, "name": "expired", "one_shot": True, "enabled": True,
             "next_run": now - timedelta(hours=3), "misfire_grace_time": 300}]
    log = _install_fake_db(monkeypatch, all_rows=rows)
    monkeypatch.setattr(cw.threading, "Thread", _SyncThread)

    fired, recorded = [], []
    monkeypatch.setattr(cw, "_run_job",
                        lambda job, **kw: fired.append((job["id"], kw)))
    monkeypatch.setattr(cw, "_record_run",
                        lambda **kw: recorded.append(kw))
    # 注：_claim_job 走真实现（假 DB）——过期 one_shot 的消费即 DELETE，需断言其 SQL

    cw._catchup_missed()

    assert any(jid == 1 and kw.get("is_misfire") and kw.get("trigger_source") == "catchup"
               for jid, kw in fired), "宽限内必须补跑并标 is_misfire"
    assert any(r.get("status") == "missed" and r.get("job_id") == 2 for r in recorded), \
        "超宽限必须记 missed"
    assert any(s.strip().startswith("DELETE FROM qd_agent_cron_jobs") for s, _ in log), \
        "过期 one_shot 应消费（claim 即删除）"


# ═══════════════════════════════════════════════════════════════
#  ⑧ 武装去重（同 id 不叠加）+ 非法表达式禁用（附录 A7-3）
# ═══════════════════════════════════════════════════════════════

class _FakeScheduler:
    def __init__(self):
        self.running = True
        self.added, self.removed = [], []

    def add_job(self, func, **kw):
        self.added.append(kw)

    def get_job(self, job_id):
        return None

    def remove_job(self, job_id):
        self.removed.append(job_id)


def test_arm_job_reschedules_same_id_not_stacking(monkeypatch):
    from app.agent.cron import cron_worker as cw
    fs = _FakeScheduler()
    monkeypatch.setattr(cw, "_scheduler", fs)
    log = _install_fake_db(monkeypatch)
    row = {"id": 7, "name": "t", "cron_expr": "30 18 * * 1", "mode": "prompt",
           "one_shot": False, "enabled": True, "next_run": None}
    cw._arm_job(row)
    cw._arm_job(row)
    assert len(fs.added) == 2
    assert all(kw.get("replace_existing") for kw in fs.added), "同 id 必须 replace 而非叠加"
    assert all(kw.get("id") == "cron-7" for kw in fs.added)
    assert all(kw.get("max_instances") == 1 and kw.get("coalesce") for kw in fs.added)
    # next_run 回写（仅展示）
    assert any("UPDATE qd_agent_cron_jobs SET next_run" in s for s, _ in log)


def test_arm_job_disables_invalid_expr(monkeypatch):
    from app.agent.cron import cron_worker as cw
    disabled = []
    monkeypatch.setattr(cw, "_scheduler", _FakeScheduler())
    monkeypatch.setattr(cw, "_disable_job", lambda jid, reason: disabled.append(jid))
    _install_fake_db(monkeypatch)
    cw._arm_job({"id": 8, "name": "bad", "cron_expr": "not-a-cron", "enabled": True})
    assert disabled == [8], "非法表达式必须禁用+日志（附录 A7-3，历史事故：非法=每分钟跑）"


def test_arm_job_keeps_sparse_but_valid_expr(monkeypatch):
    """合法但稀疏（年度 1/1）不得误禁用——窗口返回 None ≠ 非法（2026-10-19 复查抓的 bug）。"""
    from app.agent.cron import cron_worker as cw
    disabled = []
    fs = _FakeScheduler()
    monkeypatch.setattr(cw, "_scheduler", fs)
    monkeypatch.setattr(cw, "_disable_job", lambda jid, reason: disabled.append(jid))
    _install_fake_db(monkeypatch)
    cw._arm_job({"id": 9, "name": "yearly", "cron_expr": "0 9 1 1 *", "enabled": True})
    assert disabled == [], "合法稀疏表达式不应被禁用"
    assert fs.added, "宽窗口（>62 天）应能武装年度表达式"


# ═══════════════════════════════════════════════════════════════
#  ⑨ schema 双路幂等（附录 B1-1）
# ═══════════════════════════════════════════════════════════════

def test_schema_statements_all_idempotent():
    from app.agent.cron.cron_worker import _SCHEMA_STATEMENTS
    for stmt in _SCHEMA_STATEMENTS:
        assert "IF NOT EXISTS" in stmt, f"非幂等 DDL: {stmt[:60]}"


def test_migration_sql_matches_ensure_path():
    from pathlib import Path
    sql = (Path(__file__).resolve().parent.parent / "migrations"
           / "qd_cron_jobs_v2_20261019.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS locked_until" in sql
    assert "CREATE TABLE IF NOT EXISTS qd_agent_cron_runs" in sql


# ═══════════════════════════════════════════════════════════════
#  ⑩ 配置与契约形状
# ═══════════════════════════════════════════════════════════════

def test_concurrency_budget_semaphore():
    from app.agent.cron import cron_worker as cw
    import threading
    assert isinstance(cw._run_slots, threading.BoundedSemaphore)
    assert cw.MAX_CONCURRENT_RUNS >= 1


def test_get_scheduled_jobs_shape_before_start():
    from app.agent.cron.cron_worker import get_scheduled_jobs
    out = get_scheduled_jobs()
    assert isinstance(out, list)
    for item in out:
        assert set(("job_id", "timer_name", "alive")) <= set(item.keys())


# ═══════════════════════════════════════════════════════════════
#  ⑪ 第三轮审查修复（2026-10-19）：catch-up 防重守卫 + 进程内 MISSED listener
# ═══════════════════════════════════════════════════════════════

def test_job_columns_include_last_run_for_catchup_guard():
    """守卫依赖 last_run_at：必须进 SELECT 列清单（否则生产路径拿不到）。"""
    from app.agent.cron.cron_worker import _JOB_COLUMNS
    assert "last_run_at" in _JOB_COLUMNS


def test_catchup_skips_already_run_trigger(monkeypatch):
    """已跑过的触发不得补跑、不得误记 missed。

    窗口：_on_fire 执行后 next_run 要等 ≤45s 对账才回写；期间重启且在 grace 内
    ⇒ 旧实现会重复执行刚跑完的任务；超 grace ⇒ 误记 missed 污染证据链。
    """
    from app.agent.cron import cron_worker as cw
    now = datetime.now(cw.TZ_CN)
    rows = [{"id": 1, "name": "ran", "one_shot": False, "enabled": True,
             "next_run": now - timedelta(seconds=60),      # 计划点 60s 前（grace 内）
             "last_run_at": now - timedelta(seconds=30),   # 实际 30s 前已跑 ⇒ 非错过
             "misfire_grace_time": 300}]
    _install_fake_db(monkeypatch, all_rows=rows)
    fired, recorded = [], []
    monkeypatch.setattr(cw, "_run_job",
                        lambda job, **kw: fired.append((job["id"], kw)))
    monkeypatch.setattr(cw, "_record_run",
                        lambda **kw: recorded.append(kw))
    cw._catchup_missed()
    assert not fired, "已跑过的触发不得补跑（防重）"
    assert not recorded, "已跑过的触发不得误记 missed"


def test_catchup_naive_timestamps_do_not_crash(monkeypatch):
    """DB 若回 naive datetime：守卫比较不得抛 TypeError（B3-13 统一 aware）。"""
    from app.agent.cron import cron_worker as cw
    now = datetime.now(cw.TZ_CN)
    rows = [{"id": 2, "name": "naive", "one_shot": False, "enabled": True,
             "next_run": (now - timedelta(seconds=60)).replace(tzinfo=None),
             "last_run_at": None, "misfire_grace_time": 300}]
    _install_fake_db(monkeypatch, all_rows=rows)
    fired = []
    monkeypatch.setattr(cw, "_run_job", lambda job, **kw: fired.append(job["id"]))
    cw._catchup_missed()
    assert fired == [2], "naive next_run 应按本时区补正后正常补跑"


def test_missed_listener_registered():
    """进程内错过（睡眠/卡顿超 grace）必须有 EVENT_JOB_MISSED listener 落账（A2-5）。"""
    from apscheduler.events import EVENT_JOB_MISSED
    from app.agent.cron import cron_worker as cw

    class _S:
        def __init__(self):
            self.listeners = []

        def add_listener(self, cb, mask):
            self.listeners.append((cb, mask))

    s = _S()
    cw._attach_listeners(s)
    assert s.listeners, "scheduler 必须挂 listener"
    cb, mask = s.listeners[0]
    assert mask & EVENT_JOB_MISSED
    assert cb is cw._on_job_missed


def test_on_job_missed_records_missed(monkeypatch):
    from app.agent.cron import cron_worker as cw
    _install_fake_db(monkeypatch, fetchone_rows=[
        {"id": 5, "name": "sleeper", "one_shot": False, "enabled": True, "mode": "prompt"}])
    recorded = []
    monkeypatch.setattr(cw, "_record_run", lambda **kw: recorded.append(kw))
    ev = types.SimpleNamespace(
        job=types.SimpleNamespace(id="cron-5"),
        scheduled_run_time=datetime.now(cw.TZ_CN) - timedelta(minutes=10))
    cw._on_job_missed(ev)
    assert recorded and recorded[0]["status"] == "missed"
    assert recorded[0]["is_misfire"] is True
    assert recorded[0]["job_id"] == 5


def test_on_job_missed_consumes_one_shot(monkeypatch):
    """one_shot 进程内错过 ⇒ 消费掉（与 catch-up 超宽限分支一致，不留残行）。"""
    from app.agent.cron import cron_worker as cw
    log = _install_fake_db(monkeypatch, fetchone_rows=[
        {"id": 6, "name": "once", "one_shot": True, "enabled": True, "mode": "prompt"}])
    recorded = []
    monkeypatch.setattr(cw, "_record_run", lambda **kw: recorded.append(kw))
    ev = types.SimpleNamespace(
        job=types.SimpleNamespace(id="cron-6"),
        scheduled_run_time=datetime.now(cw.TZ_CN) - timedelta(minutes=10))
    cw._on_job_missed(ev)
    assert recorded and recorded[0]["status"] == "missed"
    assert any(s.strip().startswith("DELETE FROM qd_agent_cron_jobs") for s, _ in log)


# ═══════════════════════════════════════════════════════════════
#  ⑧ 告警模式（mode='alert'）one_shot 语义守卫（2026-10-23）
#  · 一次性告警 = 「命中才消费」：未命中时行必须保留，否则永远等不到条件成立。
#  · 该语义只收口在 _claim_job 内部（按 mode 判断），7 个调用点零 mode 记忆。
# ═══════════════════════════════════════════════════════════════

def test_claim_alert_one_shot_uses_update_not_delete(monkeypatch):
    """mode='alert' 的一次性任务：claim 阶段必须 UPDATE 加锁，绝不 DELETE。"""
    from app.agent.cron import cron_worker as cw
    log = _install_fake_db(monkeypatch, fetchone_rows=[
        {"id": 11, "name": "al", "mode": "alert", "one_shot": True,
         "timeout_seconds": 300, "enabled": True}])
    claimed = cw._claim_job({"id": 11, "mode": "alert", "one_shot": True,
                             "timeout_seconds": 300})
    assert claimed and claimed["id"] == 11
    sql, _ = log[0]
    assert sql.strip().startswith("UPDATE qd_agent_cron_jobs")
    assert "locked_until" in sql and "run_token" in sql
    assert "enabled = TRUE" in sql


def test_claim_prompt_one_shot_still_delete(monkeypatch):
    """mode='prompt' 的一次性任务：仍 DELETE...RETURNING 原子 claim（至多一次）。"""
    from app.agent.cron import cron_worker as cw
    log = _install_fake_db(monkeypatch, fetchone_rows=[
        {"id": 12, "name": "pt", "mode": "prompt", "one_shot": True,
         "timeout_seconds": 300, "enabled": True}])
    claimed = cw._claim_job({"id": 12, "mode": "prompt", "one_shot": True,
                             "timeout_seconds": 300})
    assert claimed and claimed["id"] == 12
    sql, _ = log[0]
    assert sql.strip().startswith("DELETE FROM qd_agent_cron_jobs")
    assert "RETURNING *" in sql and "enabled = TRUE" in sql


def test_run_job_alert_does_not_consume_on_non_trading_day(monkeypatch):
    """非交易日跳过 alert 任务：不能消费（未检查过的告警行必须保留）。"""
    from app.agent.cron import cron_worker as cw
    monkeypatch.setattr(cw, "_is_trading_day_today", lambda: False)
    log = _install_fake_db(monkeypatch,
                           fetchone_rows=[{"id": 13, "name": "al", "mode": "alert",
                                           "one_shot": True, "enabled": True}],
                           all_rows=[])
    cw._run_job({"id": 13, "name": "al", "mode": "alert",
                 "trading_days_only": True, "one_shot": True}, trigger_source="cron")
    sqls = [s for s, _ in log]
    assert not any(s.strip().startswith("DELETE FROM qd_agent_cron_jobs") for s in sqls), \
        "alert 任务非交易日跳过不能消费（未检查过，须保留行等下次）"
    assert any("skipped_nontrading" in (p or ()) for _, p in log if p)


def test_on_job_missed_keeps_alert_row(monkeypatch):
    """进程内错过 alert 任务：不消费（条件未检查过，行必须保留）。"""
    from app.agent.cron import cron_worker as cw
    log = _install_fake_db(monkeypatch, fetchone_rows=[
        {"id": 14, "name": "al", "mode": "alert", "one_shot": True,
         "enabled": True}])
    recorded = []
    monkeypatch.setattr(cw, "_record_run", lambda **kw: recorded.append(kw))
    ev = types.SimpleNamespace(
        job=types.SimpleNamespace(id="cron-14"),
        scheduled_run_time=datetime.now(cw.TZ_CN) - timedelta(minutes=10))
    cw._on_job_missed(ev)
    assert recorded and recorded[0]["status"] == "missed"
    assert not any(s.strip().startswith("DELETE FROM qd_agent_cron_jobs") for s, _ in log), \
        "alert 任务进程内错过不能消费（条件未检查过，须保留行）"


def test_on_job_missed_ignores_foreign_job_ids(monkeypatch):
    from app.agent.cron import cron_worker as cw
    log = _install_fake_db(monkeypatch)
    recorded = []
    monkeypatch.setattr(cw, "_record_run", lambda **kw: recorded.append(kw))
    ev = types.SimpleNamespace(
        job=types.SimpleNamespace(id="other-1"),
        scheduled_run_time=None)
    cw._on_job_missed(ev)
    assert not recorded and not log


# ═══════════════════════════════════════════════════════════════
#  ⑫ 遗留修复（附录 D，2026-10-19）：day+weekday 语义钉板 + REST guards/新列入口
# ═══════════════════════════════════════════════════════════════

def test_day_and_weekday_is_intersection_not_union():
    """遗留3（拍板：仅注释）：日+周同时受限 = 交集(AND)；Vixie 标准为并集(OR)。
    `0 9 15 * 1`（15号且周一）：AND → 2027-02-15；OR → 下周一 2026-10-26。钉住旧实现口径。"""
    from app.agent.cron import scheduler as sched
    nxt = sched.next_fire_time("0 9 15 * 1", MON, max_search_days=400)
    assert nxt.day == 15 and nxt.weekday() == 0
    assert nxt == datetime(2027, 2, 15, 9, 0, tzinfo=TZ)


@pytest.fixture
def cron_client():
    """最小 Flask 应用只挂 cron_bp（不走全量 create_app，避免拉起 worker）。"""
    from flask import Flask
    from app.routes.cron import cron_bp
    app = Flask(__name__)
    app.register_blueprint(cron_bp)
    app.config["TESTING"] = True
    return app.test_client()


def _patch_cron_pkg(monkeypatch):
    """REST 创建/更新成功后会回调调度注册——测试里换 no-op。"""
    import app.agent.cron as cron_pkg
    monkeypatch.setattr(cron_pkg, "schedule_job_from_db", lambda job_id: None)
    monkeypatch.setattr(cron_pkg, "unschedule_job", lambda job_id: None)


def test_rest_create_rejects_invalid_cron_expr(cron_client, monkeypatch):
    """遗留2：REST create 必须过 guard（非法表达式 400，不得先 INSERT 再禁用）。"""
    log = _install_fake_db(monkeypatch, fetchone_rows=[{"n": 0}])
    resp = cron_client.post("/api/agent/cron/jobs", json={
        "name": "bad", "cron_expr": "bad expr", "mode": "prompt", "prompt": "p"})
    assert resp.status_code == 400
    assert not any("INSERT INTO qd_agent_cron_jobs" in s for s, _ in log)


def test_rest_create_rejects_dense_interval(cron_client, monkeypatch):
    """遗留2：`*/1 * * * *` 最小间隔 1 分钟 < 5 分钟，REST 同闸拒绝。"""
    _install_fake_db(monkeypatch, fetchone_rows=[{"n": 0}])
    resp = cron_client.post("/api/agent/cron/jobs", json={
        "name": "dense", "cron_expr": "*/1 * * * *", "mode": "prompt", "prompt": "p"})
    assert resp.status_code == 400


def test_rest_create_rejects_at_job_limit(cron_client, monkeypatch):
    """遗留2：任务数达上限（50）→ 400。"""
    log = _install_fake_db(monkeypatch, fetchone_rows=[{"n": 50}])
    resp = cron_client.post("/api/agent/cron/jobs", json={
        "name": "full", "cron_expr": "0 9 * * *", "mode": "prompt", "prompt": "p"})
    assert resp.status_code == 400
    assert not any("INSERT INTO qd_agent_cron_jobs" in s for s, _ in log)


def test_rest_create_accepts_v2_new_columns(cron_client, monkeypatch):
    """遗留1：create INSERT 必须能写入 v2 新列（此前 4 列白名单外静默丢弃）。"""
    _patch_cron_pkg(monkeypatch)
    log = _install_fake_db(monkeypatch, fetchone_rows=[{"n": 0}, {"id": 42}])
    resp = cron_client.post("/api/agent/cron/jobs", json={
        "name": "n", "cron_expr": "0 9 * * *", "mode": "prompt", "prompt": "p",
        "session_id": "sess-1", "misfire_grace_time": 60,
        "trading_days_only": True, "timeout_seconds": 120})
    assert resp.status_code == 201 and resp.get_json()["id"] == 42
    insert_sql = next(s for s, _ in log if "INSERT INTO qd_agent_cron_jobs" in s)
    for col in ("session_id", "misfire_grace_time", "trading_days_only", "timeout_seconds"):
        assert col in insert_sql, f"INSERT 缺新列 {col}"


def test_rest_update_accepts_v2_new_columns(cron_client, monkeypatch):
    """遗留1：PUT 白名单缺新列 ⇒ 此前任何新列更新都 400「没有要更新的字段」。"""
    _patch_cron_pkg(monkeypatch)
    log = _install_fake_db(monkeypatch, fetchone_rows=[{"id": 5, "enabled": True}])
    resp = cron_client.put("/api/agent/cron/jobs/5", json={
        "session_id": "sess-2", "misfire_grace_time": 90,
        "trading_days_only": True, "timeout_seconds": 60})
    assert resp.status_code == 200
    upd_sql = next(s for s, _ in log if "UPDATE qd_agent_cron_jobs" in s)
    for col in ("session_id", "misfire_grace_time", "trading_days_only", "timeout_seconds"):
        assert col in upd_sql, f"UPDATE 缺新列 {col}"


def test_rest_update_rejects_invalid_cron_expr(cron_client, monkeypatch):
    """遗留2：PUT 改 cron_expr 也必须过校验（此前直写 DB，等下轮对账才禁用）。"""
    log = _install_fake_db(monkeypatch)
    resp = cron_client.put("/api/agent/cron/jobs/5", json={"cron_expr": "bad expr"})
    assert resp.status_code == 400
    assert not any("UPDATE qd_agent_cron_jobs" in s for s, _ in log)


def test_rest_update_rejects_bad_int_type(cron_client, monkeypatch):
    """遗留1：新列类型校验——timeout_seconds 非整数 → 400（不进 DB）。
    断言错误信息含字段名：此前白名单缺失也会 400「没有要更新的字段」，需区分。"""
    log = _install_fake_db(monkeypatch)
    resp = cron_client.put("/api/agent/cron/jobs/5", json={"timeout_seconds": "abc"})
    assert resp.status_code == 400
    assert "timeout_seconds" in resp.get_json().get("error", "")
    assert not log


# ═══════════════════════════════════════════════════════════════
#  ⑬ _reconcile_once 传播直测（附录 D 遗留：多 worker CRUD 对账此前无直接测试）
# ═══════════════════════════════════════════════════════════════

class _FakeSchedulerWithJobs(_FakeScheduler):
    """get_job 返回可比对 next_run_time 的既有任务（默认 None）。"""
    def __init__(self, jobs=None):
        super().__init__()
        self._jobs = dict(jobs or {})

    def get_job(self, job_id):
        return self._jobs.get(job_id)


def _reconcile_row(**over):
    row = {"id": 7, "name": "t", "cron_expr": "30 18 * * 1", "mode": "prompt",
           "prompt": "p", "function_path": None, "one_shot": False,
           "session_id": None, "timeout_seconds": 300, "trading_days_only": False,
           "misfire_grace_time": None, "enabled": True, "next_run": None}
    row.update(over)
    return row


def test_reconcile_arms_new_job(monkeypatch):
    """多 worker 新建的任务：本进程下轮对账必须武装（附录 B1-2）。"""
    from app.agent.cron import cron_worker as cw
    monkeypatch.setattr(cw, "_known_sigs", {})
    fs = _FakeSchedulerWithJobs()
    monkeypatch.setattr(cw, "_scheduler", fs)
    log = _install_fake_db(monkeypatch, all_rows=[_reconcile_row()])
    cw._reconcile_once()
    assert len(fs.added) == 1 and fs.added[0]["id"] == "cron-7"
    assert cw._known_sigs and 7 in cw._known_sigs
    assert any("UPDATE qd_agent_cron_jobs SET next_run" in s for s, _ in log)


def test_reconcile_unarms_removed_job(monkeypatch):
    """多 worker 删除/禁用的任务：本进程必须撤武装（否则幽灵触发）。"""
    from app.agent.cron import cron_worker as cw
    monkeypatch.setattr(cw, "_known_sigs", {9: "old-sig"})
    fs = _FakeSchedulerWithJobs()
    monkeypatch.setattr(cw, "_scheduler", fs)
    _install_fake_db(monkeypatch, all_rows=[])   # enabled 集合里已无 9
    cw._reconcile_once()
    assert "cron-9" in fs.removed
    assert 9 not in cw._known_sigs


def test_reconcile_rearms_on_signature_change(monkeypatch):
    """cron_expr 等签名字段被他进程改掉：同 id 必须重武装到新触发点。"""
    from app.agent.cron import cron_worker as cw
    FIXED = datetime(2026, 10, 26, 18, 30, tzinfo=TZ)
    monkeypatch.setattr(cw, "next_cron_time", lambda *a, **k: FIXED)
    monkeypatch.setattr(cw, "_known_sigs", {7: "stale-sig"})
    fs = _FakeSchedulerWithJobs({"cron-7": types.SimpleNamespace(next_run_time=FIXED)})
    monkeypatch.setattr(cw, "_scheduler", fs)
    _install_fake_db(monkeypatch, all_rows=[_reconcile_row()])
    cw._reconcile_once()
    assert len(fs.added) == 1, "签名不匹配必须重武装"
    assert cw._known_sigs[7] != "stale-sig"


def test_reconcile_skips_unchanged_job(monkeypatch):
    """无变化（签名同 + next_run 同）：不得反复 remove/add 抖动。"""
    from app.agent.cron import cron_worker as cw
    FIXED = datetime(2026, 10, 26, 18, 30, tzinfo=TZ)
    row = _reconcile_row()
    monkeypatch.setattr(cw, "next_cron_time", lambda *a, **k: FIXED)
    monkeypatch.setattr(cw, "_known_sigs", {7: cw._job_signature(row)})
    fs = _FakeSchedulerWithJobs({"cron-7": types.SimpleNamespace(next_run_time=FIXED)})
    monkeypatch.setattr(cw, "_scheduler", fs)
    _install_fake_db(monkeypatch, all_rows=[row])
    cw._reconcile_once()
    assert fs.added == [] and fs.removed == []
