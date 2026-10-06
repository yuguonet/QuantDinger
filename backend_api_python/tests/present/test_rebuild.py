"""test_rebuild.py — 生命周期四条件重建 + 同日幂等。

"日期不连续或除权或策略文件被修改 → 整票重建"（设计要点④）；
重建只重算数据切片 state，规则进度/事件流水保留。
"""

import copy
import os

from app.market_cn.auto.core.present.runner import DailyRunner, Record, StateStore, strategy_source_hash
from app.market_cn.auto.strategies.knife_catch import KnifeCatchStrategy
from tests.present.common import KNIFE_HIST_CLOSES, gen_hist_bars

CODE = "600001"


def _mk(tmp_path):
    store = StateStore(str(tmp_path / "state"))
    return DailyRunner(store), store, KnifeCatchStrategy()


def test_seed_then_advance(tmp_path):
    runner, store, s = _mk(tmp_path)
    bars = gen_hist_bars(CODE, KNIFE_HIST_CLOSES)
    # 前 8 根不够 seed（init_state 收 bars[:-1]，要求 >=8）→ 从第 9 根起
    rec, events = runner.run_day(s, CODE, bars[:9], None)
    assert rec.date == bars[8]["time"]
    st1 = copy.deepcopy(rec.state)
    rec, _ = runner.run_day(s, CODE, bars[:10], None)
    assert rec.state["age"] == st1["age"] + 1
    assert rec.date == bars[9]["time"]


def test_same_day_rerun_is_noop(tmp_path):
    runner, store, s = _mk(tmp_path)
    bars = gen_hist_bars(CODE, KNIFE_HIST_CLOSES)
    rec1, ev1 = runner.run_day(s, CODE, bars[:12], None)
    n_events = len(rec1.events)
    rec2, ev2 = runner.run_day(s, CODE, bars[:12], None)   # 同日重跑
    assert ev2 == []
    assert len(rec2.events) == n_events, "同日重跑不得重复出事件"
    assert rec2.state == rec1.state


def test_reordered_rebuilds(tmp_path):
    runner, store, s = _mk(tmp_path)
    bars = gen_hist_bars(CODE, KNIFE_HIST_CLOSES)
    runner.run_day(s, CODE, bars[:12], None)
    rec = store.load(s.key, CODE)
    reason = runner._rebuild_reason(s, rec, bars[10], bars[9], None)   # 旧数据重发
    assert reason == "reordered"


def test_date_gap_rebuilds(tmp_path):
    runner, store, s = _mk(tmp_path)
    bars = gen_hist_bars(CODE, KNIFE_HIST_CLOSES)
    runner.run_day(s, CODE, bars[:12], None)               # 切片日期 = bars[11]
    # 模拟漏推进一天：直接跳到 bars[:14]，切片不在昨根上
    reason = runner._rebuild_reason(s, store.load(s.key, CODE), bars[13], bars[12], None)
    assert reason == "date_gap"


def test_probe_mismatch_rebuilds(tmp_path):
    runner, store, s = _mk(tmp_path)
    bars = gen_hist_bars(CODE, KNIFE_HIST_CLOSES)
    runner.run_day(s, CODE, bars[:12], None)
    rec = store.load(s.key, CODE)
    # 除权：改写切片窗口内的历史 close（锚点首根）
    anchor_date = rec.state["win"][0]["d"]
    probe_bars = {anchor_date: {"time": anchor_date,
                                "close": rec.state["win"][0]["c"] * 0.8}}  # 前复权缩放
    reason = runner._rebuild_reason(s, rec, bars[12], bars[11], probe_bars)
    assert reason == "probe_mismatch"


def test_same_day_correction_rebuilds(tmp_path):
    runner, store, s = _mk(tmp_path)
    bars = gen_hist_bars(CODE, KNIFE_HIST_CLOSES)
    runner.run_day(s, CODE, bars[:12], None)
    rec = store.load(s.key, CODE)
    anchor_date = rec.state["win"][-1]["d"]               # = 切片日（今日）
    probe_bars = {anchor_date: {"time": anchor_date,
                                "close": rec.state["win"][-1]["c"] * 1.05}}  # 当日修正
    reason = runner._rebuild_reason(s, rec, bars[11], bars[10], probe_bars)
    assert reason == "probe_mismatch", "锚窗口末根必须防同日改写"


def test_strategy_changed_rebuilds(tmp_path):
    runner, store, s = _mk(tmp_path)
    bars = gen_hist_bars(CODE, KNIFE_HIST_CLOSES)
    runner.run_day(s, CODE, bars[:12], None)
    rec = store.load(s.key, CODE)
    rec.strategy_hash = "deadbeef"                          # 模拟策略文件被修改
    reason = runner._rebuild_reason(s, rec, bars[12], bars[11], None)
    assert reason == "strategy_changed"


def test_rebuild_keeps_progress_history(tmp_path):
    """重建只重建切片；规则进度/事件流水是事实，跨重建保留。"""
    runner, store, s = _mk(tmp_path)
    bars = gen_hist_bars(CODE, KNIFE_HIST_CLOSES)
    runner.run_day(s, CODE, bars[:12], None)
    rec = store.load(s.key, CODE)
    n_events = len(rec.events)
    # 策略哈希篡改 → 下一日重建
    rec.strategy_hash = "deadbeef"
    store.save(s.key, CODE, rec)
    rec2, _ = runner.run_day(s, CODE, bars[:13], None)
    assert len(rec2.events) >= n_events
    assert rec2.strategy_hash == strategy_source_hash(s)
    assert rec2.state["age"] == 13, "重建后照常推进一日（state 覆盖 bars[:13]）"


def test_realtime_never_writes_fold(tmp_path):
    from app.market_cn.auto.core.present.realtime import RealtimeBranch
    runner, store, s = _mk(tmp_path)
    bars = gen_hist_bars(CODE, KNIFE_HIST_CLOSES)
    runner.run_day(s, CODE, bars[:12], None)
    before = store.load(s.key, CODE).to_json()
    rb = RealtimeBranch(store, {s.key: s})
    rb.tick("14:56", [CODE], {CODE: {"time": "x", "last": 1, "previousClose": 1,
                                     "high": 1, "low": 1, "volume": 1}})
    after = store.load(s.key, CODE).to_json()
    assert before == after, "实时分支不得写 fold（state/progress 只由预处理推进）"
