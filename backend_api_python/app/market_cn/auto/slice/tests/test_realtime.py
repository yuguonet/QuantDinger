"""test_realtime.py — 实时是预处理的临时分支：同一判定、不写 fold、看规则进度挑票。

全周期（knife，D0 触发 + D1 开盘两阶段）：
    D-1 晚 预处理 → watch（next_realtime=14:56-15:00）
    D0 14:56 实时副本试推 → ready（准备）        == D0 晚预处理的 ready
    D1 09:31 实时副本试推 → exec（执行）          == D1 晚预处理的 exec
"""

from app.market_cn.auto.slice.contract import DayInput, Progress
from app.market_cn.auto.slice.realtime import RealtimeBranch
from app.market_cn.auto.slice.runner import DailyRunner, StateStore
from app.market_cn.auto.slice.strategies import KnifeCatchSlim
from app.market_cn.auto.slice.tests.common import KNIFE_HIST_CLOSES, gen_hist_bars, knife_day_rows

CODE = "600001"
MKT = -3.0


def _strip(e: Progress):
    return (e.stage, e.date, e.payload, e.next_realtime)


def test_full_cycle_realtime_equals_preprocess(tmp_path):
    s = KnifeCatchSlim()
    store = StateStore(str(tmp_path / "state"))
    runner = DailyRunner(store)
    rb = RealtimeBranch(store, {s.key: s})

    bars = gen_hist_bars(CODE, KNIFE_HIST_CLOSES)          # 18 根，止于 D-1
    d0 = {"time": "2026-10-05", "open": 96.4, "high": 96.5, "low": 84.0,
          "close": 85.2, "volume": 1200.0}
    d1 = {"time": "2026-10-06", "open": 86.5, "high": 87.0, "low": 85.0,
          "close": 86.2, "volume": 900.0}
    rows = knife_day_rows(d0["time"], pc=100.0)

    # 1) 历史逐日预处理（D-1 收盘后 current 应为 watch）
    for i in range(9, len(bars) + 1):
        runner.run_day(s, CODE, bars[:i], None)
    rec = store.load(s.key, CODE)
    assert rec.current.stage == "watch"
    assert rec.current.next_realtime == "14:56-15:00"

    # 2) D0 14:56 实时：只挑规则进度到期的票
    rows_1456 = [r for r in rows if str(r["time"])[11:16] <= "14:56"]
    snap = rows_1456[-1]
    due = rb.tick("14:56", [CODE], {CODE: snap}, {CODE: rows_1456}, MKT)
    assert len(due) == 1 and due[0][0] == CODE
    code_rt, ev_rt = due[0]
    assert ev_rt.stage == "ready" and ev_rt.source == "realtime"
    assert store.load(s.key, CODE).current.stage == "watch", "实时不得写 fold"

    # 3) D0 晚预处理：同一 evaluate 应产出同一 ready
    _, evs = runner.run_day(s, CODE, bars + [d0],
                            {"latest": rows_1456[-1], "series": rows, "mkt_gain": MKT})
    assert [e.stage for e in evs] == ["ready"]
    assert _strip(evs[0]) == _strip(ev_rt), "实时 == 预处理（临时分支逐位一致）"

    # 4) D1 09:31 实时：exec 预览
    snap_d1 = {"time": f"{d1['time']} 09:31:00", "open": d1["open"],
               "high": d1["high"], "low": d1["low"], "last": 86.0,
               "previousClose": 85.2, "volume": 50.0}
    due = rb.tick("09:31", [CODE], {CODE: snap_d1}, {}, MKT)
    assert len(due) == 1
    _, ev_rt2 = due[0]
    assert ev_rt2.stage == "exec" and ev_rt2.payload["exit_price"] == 86.5

    # 5) D1 晚预处理：同一 exec
    _, evs = runner.run_day(s, CODE, bars + [d0, d1], None)
    assert [e.stage for e in evs] == ["exec"]
    assert _strip(evs[0]) == _strip(ev_rt2)

    # 6) 事件流水 = watch → ready → exec
    rec = store.load(s.key, CODE)
    tail_stages = [e["stage"] for e in rec.events][-3:]
    assert tail_stages == ["watch", "ready", "exec"]


def test_realtime_skips_undue_codes(tmp_path):
    """规则进度未到期的票不进实时候选（信息量极小）。"""
    s = KnifeCatchSlim()
    store = StateStore(str(tmp_path / "state"))
    runner = DailyRunner(store)
    rb = RealtimeBranch(store, {s.key: s})
    bars = gen_hist_bars(CODE, KNIFE_HIST_CLOSES)
    for i in range(9, len(bars) + 1):
        runner.run_day(s, CODE, bars[:i], None)
    snap = {"time": "2026-10-05 10:00:00", "last": 1.0, "previousClose": 1.0,
            "high": 1.0, "low": 1.0, "volume": 1.0}
    assert rb.tick("10:00", [CODE], {CODE: snap}, {}, MKT) == []


def test_realtime_superset_shortlist_never_misses(tmp_path):
    """实时便宜预筛是必要条件超集：构造触发日，预筛后仍应保留该票。"""
    s = KnifeCatchSlim()
    rows = knife_day_rows("2026-10-05", pc=100.0)
    snap = [r for r in rows if str(r["time"])[11:16] == "14:56"][0]
    out = s.realtime_shortlist([CODE], {CODE: snap}, MKT)
    assert CODE in out
