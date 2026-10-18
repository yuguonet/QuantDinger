# -*- coding: utf-8 -*-
"""EvalWorker 停摆自愈（B）+ 健康探针（C）守卫（2026-10-18，用户裁定 B+C）。

背景（用户 2026-10-18 裁定「确认走 B+C」，方案选项见会话纪要/设计文档附录 AJ）：

盘后闭环 = `scheduler._post_market_batch`（15:30，backfill 1m/1d → `post_market_done.set()`）
        → EvalWorker（`auto_evaluate` + `run_v5_collection` + `brew_skills`）。

  B) 停摆不自健：flask 在 15:30 之后才起（宿主机几天没开 / 重启）时，
     scheduler 主循环会在 ~10s 内补跑当日 post_market_batch（**启动补跑路径刻意
     跳过它，主循环不跳过** —— scheduler.py:833-881），但 EvalWorker 按
     `_seconds_until_post_market()` 睡到次日 15:30 ⇒ 错过事件、当日闭环静默
     顺延一天，v5 情境帧（C 类不可重建）永久丢失。
     ⇒ 线程启动时判定：已过盘后时刻 + 批次今日未完成 ⇒ **等事件**而非睡到明日；
       批次已完成而评估今日未成功跑过（进程在两者之间死过）⇒ **立即补评**。

  B') 事件消费即清：`post_market_done` 历史上从不 clear ⇒ 长跑进程从第 2 天起
     "等待盘后数据同步完成"恒跳过，15:30 评估与 backfill 赛跑（靠 resolver 视界门②
     + 重试治理兜底，但违背设计意图）。改为**消费即清**（清在消费侧，不在 batch
     起始侧——后者与 15:30 准点 wake 有竞态）。

  C) 停摆不可见：`_worker_health` 只在进程内 ⇒ 宿主不起时外部无从得知。
     ⇒ 健康快照落盘 JSONL（对齐 trace 机制：AGENT_*_FILE 相对路径锚定 app/agent/）
       + 只读探针 `probe_worker_health()`（staleness = 上次成功 → 下一交易日
       15:30 + 宽限 仍未成功）+ CLI `--health`（stalled ⇒ exit 1）。

⚠️ 导入纪律（4e 教训）：入口脚本模块级把 `app/agent` 插进 sys.path，收集期 import
   会污染进程全局判据 ⇒ 本文件全部**函数内延迟导入**，模块级只留 stdlib/pytest。
⚠️ 本文件不真起线程、不跑盘后批次、零网络零 DB。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest


# ── 公共工具 ────────────────────────────────────────────────────────────

def _patch_plan_env(monkeypatch, *, done: bool, trading: bool):
    """钉死 plan 的两个外部判定（函数内延迟导入 ⇒ 打源模块属性即生效）。"""
    from app.market_cn import scheduler as _sched
    from app.utils import trading_calendar as _cal
    monkeypatch.setattr(_sched, "scheduler_task_done", lambda *a, **k: done)
    monkeypatch.setattr(_cal, "is_trading_day", lambda *a, **k: trading)


def _write_health(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for ln in lines:
            f.write(json.dumps(ln, ensure_ascii=False) + "\n")


def _fake_next_trading_day(monkeypatch):
    """把 next_trading_day 换成"给定日期 +1 自然日"（测试不需要真日历语义）。"""
    from app.utils import trading_calendar as _cal

    def _fake(d, n=1):
        base = datetime.strptime(str(d)[:10], "%Y-%m-%d")
        return (base + timedelta(days=n)).strftime("%Y-%m-%d")

    monkeypatch.setattr(_cal, "next_trading_day", _fake)


# ── B：停摆自愈判定 _post_market_catchup_plan ───────────────────────────

def test_catchup_wait_when_past_trigger_and_batch_not_done(monkeypatch, tmp_path):
    """B 主型：flask 20:00 起（交易日、批次未跑）⇒ 等事件，不睡到明日 15:30。"""
    _patch_plan_env(monkeypatch, done=False, trading=True)
    monkeypatch.setenv("AGENT_EVAL_HEALTH_FILE", str(tmp_path / "h.jsonl"))
    from app.agent.chain import evaluator as EV
    plan = EV._post_market_catchup_plan(now=datetime(2026, 10, 14, 20, 0, 0))  # 周三晚
    assert plan["mode"] == "wait", plan
    assert "批次未完成" in plan["reason"], plan


def test_catchup_boundary_at_trigger_exact(monkeypatch, tmp_path):
    """15:30:00 整 = 已到盘后时刻（scheduler Task 同口径），必须判 wait 不是 none。"""
    _patch_plan_env(monkeypatch, done=False, trading=True)
    monkeypatch.setenv("AGENT_EVAL_HEALTH_FILE", str(tmp_path / "h.jsonl"))
    from app.agent.chain import evaluator as EV
    plan = EV._post_market_catchup_plan(now=datetime(2026, 10, 14, 15, 30, 0))
    assert plan["mode"] == "wait", plan


def test_catchup_none_before_trigger(monkeypatch, tmp_path):
    """未到 15:30（哪怕批次也没跑）⇒ none：正常节拍自己会等到。"""
    _patch_plan_env(monkeypatch, done=False, trading=True)
    monkeypatch.setenv("AGENT_EVAL_HEALTH_FILE", str(tmp_path / "h.jsonl"))
    from app.agent.chain import evaluator as EV
    assert EV._post_market_catchup_plan(now=datetime(2026, 10, 14, 10, 0, 0))["mode"] == "none"


def test_catchup_none_on_non_trading_day(monkeypatch, tmp_path):
    """周六 20:00 批次未跑 ⇒ none（scheduler 主循环非交易日也不跑批次）。"""
    _patch_plan_env(monkeypatch, done=False, trading=False)
    monkeypatch.setenv("AGENT_EVAL_HEALTH_FILE", str(tmp_path / "h.jsonl"))
    from app.agent.chain import evaluator as EV
    assert EV._post_market_catchup_plan(now=datetime(2026, 10, 17, 20, 0, 0))["mode"] == "none"


def test_catchup_none_when_batch_done_and_eval_succeeded_today(monkeypatch, tmp_path):
    """批次与评估今日都已成功 ⇒ none（重启不重跑，防 EMA 权重双算）。"""
    _patch_plan_env(monkeypatch, done=True, trading=True)
    p = tmp_path / "h.jsonl"
    _write_health(p, [{"ts": "2026-10-14 16:00:00", "event": "run",
                       "last_success_at": "2026-10-14 16:00:00", "total_successes": 6}])
    monkeypatch.setenv("AGENT_EVAL_HEALTH_FILE", str(p))
    from app.agent.chain import evaluator as EV
    plan = EV._post_market_catchup_plan(now=datetime(2026, 10, 14, 20, 0, 0))
    assert plan["mode"] == "none", plan


def test_catchup_run_now_when_batch_done_but_eval_missing(monkeypatch, tmp_path):
    """Case 2：批次已完成、评估今日未成功（有历史成功证据）⇒ 立即补评。"""
    _patch_plan_env(monkeypatch, done=True, trading=True)
    p = tmp_path / "h.jsonl"
    _write_health(p, [{"ts": "2026-10-13 16:00:00", "event": "run",
                       "last_success_at": "2026-10-13 16:00:00", "total_successes": 5}])
    monkeypatch.setenv("AGENT_EVAL_HEALTH_FILE", str(p))
    from app.agent.chain import evaluator as EV
    plan = EV._post_market_catchup_plan(now=datetime(2026, 10, 14, 20, 0, 0))
    assert plan["mode"] == "run_now", plan


def test_catchup_none_without_health_evidence(monkeypatch, tmp_path):
    """无健康落盘（首次部署/从未跑过）⇒ 无法区分"没跑过"与"跑过没留痕" ⇒ 不补评。"""
    _patch_plan_env(monkeypatch, done=True, trading=True)
    monkeypatch.setenv("AGENT_EVAL_HEALTH_FILE", str(tmp_path / "none.jsonl"))
    from app.agent.chain import evaluator as EV
    plan = EV._post_market_catchup_plan(now=datetime(2026, 10, 14, 20, 0, 0))
    assert plan["mode"] == "none", plan


def test_catchup_none_when_never_succeeded(monkeypatch, tmp_path):
    """有落盘但从未成功过（total_successes=0）⇒ 同样不敢双算 ⇒ none。"""
    _patch_plan_env(monkeypatch, done=True, trading=True)
    p = tmp_path / "h.jsonl"
    _write_health(p, [{"ts": "2026-10-14 09:55:00", "event": "worker_start",
                       "last_success_at": None, "total_successes": 0}])
    monkeypatch.setenv("AGENT_EVAL_HEALTH_FILE", str(p))
    from app.agent.chain import evaluator as EV
    plan = EV._post_market_catchup_plan(now=datetime(2026, 10, 14, 20, 0, 0))
    assert plan["mode"] == "none", plan


# ── B'：_wait_post_market_done（等待 + 消费即清）────────────────────────

def test_wait_consumes_and_clears_event():
    """事件已置 ⇒ 返回 True 且**清掉**（长跑进程次日的等待才有效）。"""
    from app.market_cn import scheduler as _sched
    _sched.post_market_done.set()
    from app.agent.chain import evaluator as EV
    assert EV._wait_post_market_done(timeout=0.2) is True
    assert not _sched.post_market_done.is_set(), "消费即清失效 ⇒ 次日等待恒跳过"


def test_wait_timeout_returns_false_and_keeps_unset():
    """未置 ⇒ 等到超时返回 False，事件保持未置（不冒充"数据就绪"）。"""
    from app.market_cn import scheduler as _sched
    _sched.post_market_done.clear()
    from app.agent.chain import evaluator as EV
    assert EV._wait_post_market_done(timeout=0.15) is False
    assert not _sched.post_market_done.is_set()


def test_wait_returns_true_when_set_during_wait():
    """等待期间被 set（批次跑完）⇒ True 且清掉。"""
    import threading
    from app.market_cn import scheduler as _sched
    _sched.post_market_done.clear()
    threading.Timer(0.05, _sched.post_market_done.set).start()
    from app.agent.chain import evaluator as EV
    assert EV._wait_post_market_done(timeout=5.0) is True
    assert not _sched.post_market_done.is_set()


def test_wait_proceeds_when_event_unavailable(monkeypatch):
    """market_cn 不可用（导入失败）⇒ 按旧语义返回 True 继续执行，不炸 worker。"""
    import sys
    monkeypatch.setitem(sys.modules, "app.market_cn.scheduler", None)
    from app.agent.chain import evaluator as EV
    assert EV._wait_post_market_done(timeout=0.1) is True


# ── C：健康落盘 + 只读探针 ──────────────────────────────────────────────

def test_persist_appends_one_jsonl_per_event(monkeypatch, tmp_path):
    """每次事件一行 JSON；含 _worker_health 全字段 + ts/pid。"""
    p = tmp_path / "traces" / "eval_worker_health.jsonl"
    monkeypatch.setenv("AGENT_EVAL_HEALTH_FILE", str(p))
    from app.agent.chain import evaluator as EV
    fake_health = dict(EV._worker_health)
    fake_health.update(total_runs=1, last_run_at="2026-10-14 16:00:00",
                       last_success_at="2026-10-14 16:00:00", total_successes=1)
    monkeypatch.setattr(EV, "_worker_health", fake_health)
    EV._persist_worker_health({"event": "worker_start"})
    EV._persist_worker_health({"event": "run"})
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(rows) == 2
    assert rows[0]["event"] == "worker_start" and rows[1]["event"] == "run"
    assert rows[1]["total_runs"] == 1
    assert rows[1]["last_success_at"] == "2026-10-14 16:00:00"
    assert rows[1]["ts"] and rows[1]["pid"]


def test_persist_is_fail_soft(monkeypatch, tmp_path):
    """落盘失败绝不炸 worker（C 是观测，不是新故障源）。"""
    blocker = tmp_path / "dir_as_file"
    blocker.mkdir()
    monkeypatch.setenv("AGENT_EVAL_HEALTH_FILE", str(blocker))  # 目录当文件 ⇒ 写必失败
    from app.agent.chain import evaluator as EV
    EV._persist_worker_health({"event": "run"})   # 不抛即过


def test_probe_ok_when_recent_success(monkeypatch, tmp_path):
    p = tmp_path / "h.jsonl"
    _write_health(p, [
        {"ts": "2026-10-14 09:55:00", "event": "worker_start",
         "last_success_at": None, "total_runs": 0},
        {"ts": "2026-10-14 16:00:00", "event": "run",
         "last_success_at": "2026-10-14 16:00:00", "total_runs": 1},
    ])
    monkeypatch.setenv("AGENT_EVAL_HEALTH_FILE", str(p))
    _fake_next_trading_day(monkeypatch)
    from app.agent.chain import evaluator as EV
    r = EV.probe_worker_health(now=datetime(2026, 10, 14, 18, 0, 0))
    assert r["status"] == "ok" and r["stalled"] is False, r
    assert r["last_success_at"] == "2026-10-14 16:00:00"


def test_probe_stalled_when_success_stale(monkeypatch, tmp_path):
    """上次成功是上一交易日，过了次日 15:30+宽限仍无新成功 ⇒ stalled。"""
    p = tmp_path / "h.jsonl"
    _write_health(p, [
        {"ts": "2026-10-13 16:00:00", "event": "run",
         "last_success_at": "2026-10-13 16:00:00", "total_runs": 1},
    ])
    monkeypatch.setenv("AGENT_EVAL_HEALTH_FILE", str(p))
    _fake_next_trading_day(monkeypatch)
    from app.agent.chain import evaluator as EV
    r = EV.probe_worker_health(now=datetime(2026, 10, 14, 18, 0, 0))
    assert r["status"] == "stalled" and r["stalled"] is True, r


def test_probe_pending_when_started_today_never_ran(monkeypatch, tmp_path):
    """今早刚起、首跑还没到 ⇒ pending 不是 stalled（防误报）。"""
    p = tmp_path / "h.jsonl"
    _write_health(p, [{"ts": "2026-10-14 09:55:00", "event": "worker_start",
                       "last_success_at": None, "total_runs": 0}])
    monkeypatch.setenv("AGENT_EVAL_HEALTH_FILE", str(p))
    from app.agent.chain import evaluator as EV
    r = EV.probe_worker_health(now=datetime(2026, 10, 14, 10, 0, 0))
    assert r["status"] == "pending" and r["stalled"] is False, r


def test_probe_stalled_when_ran_but_never_succeeded(monkeypatch, tmp_path):
    """跑过但从未成功、且已过当天宽限 ⇒ stalled（闭环从未完成）。"""
    p = tmp_path / "h.jsonl"
    _write_health(p, [{"ts": "2026-10-14 16:30:00", "event": "run",
                       "last_success_at": None, "total_runs": 3,
                       "consecutive_failures": 3, "last_error": "boom"}])
    monkeypatch.setenv("AGENT_EVAL_HEALTH_FILE", str(p))
    from app.agent.chain import evaluator as EV
    r = EV.probe_worker_health(now=datetime(2026, 10, 14, 18, 0, 0))
    assert r["status"] == "stalled" and r["stalled"] is True, r


def test_probe_tolerates_torn_last_line(monkeypatch, tmp_path):
    """尾撕裂行（半写）不得毒化读者 —— 用上前一条好行。"""
    p = tmp_path / "h.jsonl"
    _write_health(p, [
        {"ts": "2026-10-14 16:00:00", "event": "run",
         "last_success_at": "2026-10-14 16:00:00", "total_runs": 1},
    ])
    with open(p, "a", encoding="utf-8") as f:
        f.write('{"ts": "2026-10-14 16:0')   # 撕裂行
    monkeypatch.setenv("AGENT_EVAL_HEALTH_FILE", str(p))
    _fake_next_trading_day(monkeypatch)
    from app.agent.chain import evaluator as EV
    r = EV.probe_worker_health(now=datetime(2026, 10, 14, 18, 0, 0))
    assert r["status"] == "ok", r


def test_probe_no_data_when_file_missing(monkeypatch, tmp_path):
    """文件缺失 ⇒ no_data（无证据 ≠ 健康，也不是 stalled）。"""
    monkeypatch.setenv("AGENT_EVAL_HEALTH_FILE", str(tmp_path / "none.jsonl"))
    from app.agent.chain import evaluator as EV
    r = EV.probe_worker_health(now=datetime(2026, 10, 14, 18, 0, 0))
    assert r["status"] == "no_data" and r["stalled"] is False, r


def test_probe_unreadable_when_no_parseable_line(monkeypatch, tmp_path):
    """有文件但无可解析行 ⇒ unreadable（CLI 侧按故障 exit 1）。"""
    p = tmp_path / "h.jsonl"
    p.write_text('{"ts": "2026-10-14 16:0\n', encoding="utf-8")
    monkeypatch.setenv("AGENT_EVAL_HEALTH_FILE", str(p))
    from app.agent.chain import evaluator as EV
    r = EV.probe_worker_health(now=datetime(2026, 10, 14, 18, 0, 0))
    assert r["status"] == "unreadable" and r["stalled"] is False, r


# ── 接线守卫 + 进程内视图 + CLI ─────────────────────────────────────────

def test_worker_loop_wires_catchup_and_wait_helper():
    """接线守卫：_worker 必须真的消费自愈判定与事件等待助手（防落成死代码）。"""
    import inspect
    from app.agent.chain import evaluator as EV
    src = inspect.getsource(EV.start_eval_worker)
    assert "_post_market_catchup_plan" in src, "停摆自愈判定未接线 ⇒ B 失效"
    assert "_wait_post_market_done" in src, "事件等待助手未接线 ⇒ B' 失效"


def test_get_worker_health_merges_probe_fields(monkeypatch, tmp_path):
    """进程内视图合并只读探针字段；既有 smoke 判据字段不回归。

    注：get_worker_health 的探针用**真实 now**（进程内视图的语义），夹具日期
    必须相对今天构造，否则"过去的成功记录"会被正确判成 stalled。
    """
    p = tmp_path / "h.jsonl"
    today = datetime.now().strftime("%Y-%m-%d")
    _write_health(p, [{"ts": today + " 16:00:00", "event": "run",
                       "last_success_at": today + " 16:00:00", "total_runs": 1,
                       "last_accountability": None, "last_weight_feed": None}])
    monkeypatch.setenv("AGENT_EVAL_HEALTH_FILE", str(p))
    _fake_next_trading_day(monkeypatch)
    from app.agent.chain import evaluator as EV
    h = EV.get_worker_health()
    assert h["stalled"] is False and h["health_status"] == "ok", h
    assert h["health_file"] == str(p)
    assert "last_accountability" in h, "smoke 既有判据（worker 健康视图含追责字段）被改丢"


def test_cli_health_exit_codes(monkeypatch, tmp_path, capsys):
    """--health：ok ⇒ exit 0；过期 ⇒ exit 1（供外部监控/ cron 判活）。"""
    p = tmp_path / "h.jsonl"
    monkeypatch.setenv("AGENT_EVAL_HEALTH_FILE", str(p))
    _fake_next_trading_day(monkeypatch)
    from app.agent.chain import evaluator as EV

    today = datetime.now().strftime("%Y-%m-%d")
    stale = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    _write_health(p, [{"ts": today + " 16:00:00", "event": "run",
                       "last_success_at": today + " 16:00:00", "total_runs": 1}])
    assert EV.main(["--health"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok"

    _write_health(p, [{"ts": stale + " 16:00:00", "event": "run",
                       "last_success_at": stale + " 16:00:00", "total_runs": 1}])
    assert EV.main(["--health"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "stalled"


def test_cli_catchup_plan_is_read_only(monkeypatch, capsys):
    """--catchup-plan 只打印判定，不触跑任何闭环。"""
    _patch_plan_env(monkeypatch, done=False, trading=True)
    from app.agent.chain import evaluator as EV
    assert EV.main(["--catchup-plan"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] in ("wait", "run_now", "none")


# ── tracing.anchored_path（C 复用的路径锚定收口）────────────────────────

def test_anchored_path_relative_anchors_to_agent_dir(monkeypatch, tmp_path):
    """相对路径锚定 app/agent/（不随 CWD 漂移）；绝对路径原样使用。"""
    from app.agent.utils import tracing as T
    monkeypatch.setenv("AGENT_EVAL_HEALTH_FILE", "traces/x.jsonl")
    assert T.anchored_path("AGENT_EVAL_HEALTH_FILE", "traces/x.jsonl") \
        == T._AGENT_DIR / "traces" / "x.jsonl"
    monkeypatch.setenv("AGENT_EVAL_HEALTH_FILE", str(tmp_path / "abs.jsonl"))
    assert T.anchored_path("AGENT_EVAL_HEALTH_FILE", "traces/x.jsonl") \
        == tmp_path / "abs.jsonl"


def test_anchored_path_unset_uses_default(monkeypatch):
    """未设 env ⇒ 用调用方给的默认相对路径（锚定后同规则）。"""
    from app.agent.utils import tracing as T
    monkeypatch.delenv("AGENT_EVAL_HEALTH_FILE", raising=False)
    assert T.anchored_path("AGENT_EVAL_HEALTH_FILE", "traces/h.jsonl") \
        == T._AGENT_DIR / "traces" / "h.jsonl"
