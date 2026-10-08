"""test_hold_verdict.py — P5-④ tick verdict hold 语义门禁（P5_REALTIME_HOLD_AUDIT 闭环）。

三条语义（复核报告 P1/P2 的修复断言）:
  1. **活仓无出场 → hold**: prev=exec 且 evaluate 无事件 = "评过了, 继续持有" 真判定;
  2. **死仓/无仓无事件 → 不产出**: current=exit 时 evaluate 结算已终结链恒无事件,
     产 hold 会把该出场的票判成持有（P2 实证 dragon 605179）⇒ 必须回退;
  3. **有事件 → 事件原样**: hold 只在 events 空时考虑。

契约锚点: monitor._progress_map "绝不能把无判定当成判定为持有"。
测试策略: mock evaluate_day 返回空事件, 单独钉 hold 门的真值条件
(策略 state 复杂度不该混进门语义测试)。
"""

from __future__ import annotations

import pytest

from app.market_cn.auto.core.present.contract import Progress
from app.market_cn.auto.core.present.realtime import RealtimeBranch, _open_ready_anchor
from app.market_cn.auto.core.present.runner import Record, StateStore

KEY = "break"


def _p(stage, date, nr=None, payload=None):
    return Progress(stage=stage, date=date, payload=payload or {},
                    next_realtime=nr, source="preprocess")


def _to_d(p):
    from app.market_cn.auto.core.present.runner import _progress_to_dict
    return _progress_to_dict(p)


def _rec(current, events):
    return Record(date="2026-10-07", state={"v": 1},
                  current=current, events=[_to_d(e) for e in events],
                  strategy_hash="x")


def _exit_rec_605179_shape():
    """605179 真实形态: ready→exec→ready(重合)→**exit→ready(同日补齐, 未消费)**。"""
    return _rec(
        current=_p("exit", "2026-08-27", None, {"exit_price": 11.0}),
        events=[_p("ready", "2026-08-25", "09:25", {"price": 10.0}),
                _p("exec", "2026-08-26", None,
                   {"entry_date": "2026-08-26", "entry_price": 10.0, "buyable": True}),
                _p("ready", "2026-08-26", "09:25", {"price": 10.5}),
                _p("exit", "2026-08-27", None, {"exit_price": 11.0}),
                _p("ready", "2026-08-27", "09:25", {"price": 10.5})])


def _exec_rec(nr="15:01"):
    return _rec(
        current=_p("exec", "2026-10-07", nr,
                   {"entry_date": "2026-10-07", "entry_price": 10.0, "buyable": True}),
        events=[_p("ready", "2026-10-06", "09:25", {"price": 10.0}),
                _p("exec", "2026-10-07", nr,
                   {"entry_date": "2026-10-07", "entry_price": 10.0, "buyable": True})])


@pytest.fixture
def branch(tmp_path, monkeypatch):
    from app.market_cn.auto.strategies import autodiscover, get_strategy
    autodiscover()
    s = get_strategy(KEY)
    store = StateStore(str(tmp_path / "state"))
    # evaluate_day mock: 一律返回空事件 —— 本测试只钉 hold 门, 不测策略状态机
    monkeypatch.setattr(
        "app.market_cn.auto.core.present.realtime.evaluate_day",
        lambda *a, **k: ([], None))
    return RealtimeBranch(store, {KEY: s}), store


_SNAP_0925 = {"time": "2026-10-08 09:25:00", "open": 10.0, "high": 10.5,
              "low": 9.0, "last": 9.1, "volume": 1000.0}
_SNAP_1501 = {"time": "2026-10-08 15:01:00", "open": 10.0, "high": 10.5,
              "low": 9.0, "last": 10.1, "volume": 1000.0}


def test_open_ready_anchor_605179_shape():
    """exit 之后的 stateless 补齐 ready = 未消费（锚可见）。"""
    assert _open_ready_anchor(_exit_rec_605179_shape().events) == "09:25"


def test_dead_position_no_hold(branch, tmp_path):
    """P2 回归: current=exit + 未消费 ready ⇒ due, 但 evaluate 无事件 ⇒ 不产 hold。"""
    rb, store = branch
    store.save(KEY, "600002", _exit_rec_605179_shape())
    assert rb._due(KEY, "600002", "09:25") is True, "605179 形态 09:25 应 due"
    out = rb.tick("09:25", ["600002"], {"600002": _SNAP_0925})
    stages = [p.stage for _, p in out]
    assert "hold" not in stages, (
        "死仓无事件不得产 hold（无判定 ⇒ 回退, P1/P2）: %s" % stages)
    assert out == [], "死仓且无事件 ⇒ 输出必须为空, 调用方才走回退"


def test_live_exec_produces_hold(branch, tmp_path):
    """活仓(prev=exec) + 锚命中 + evaluate 无出场 ⇒ hold 真判定。"""
    rb, store = branch
    store.save(KEY, "600001", _exec_rec(nr="15:01"))
    assert rb._due(KEY, "600001", "15:01") is True
    out = rb.tick("15:01", ["600001"], {"600001": _SNAP_1501})
    assert [(c, p.stage, p.source) for c, p in out] == \
        [("600001", "hold", "realtime")], \
        "活仓评定无出场必须产 hold（15:01 持有分支走新路径的唯一形态）"


def test_no_anchor_never_due(branch, tmp_path):
    """P6 形态: 历史 exec nr=null ⇒ _due=False ⇒ tick 空 ⇒ 天然回退。"""
    rb, store = branch
    store.save(KEY, "600003", _exec_rec(nr=None))
    assert rb._due(KEY, "600003", "15:01") is False, "nr=null 且无未消费 ready ⇒ 不得 due"
    assert rb.tick("15:01", ["600003"], {"600003": _SNAP_1501}) == []
