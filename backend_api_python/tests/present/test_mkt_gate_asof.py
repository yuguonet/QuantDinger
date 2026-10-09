"""市场门 逐槽 as-of 回归锁（2026-10-09，口径变更 R5）。

背景（`docs/市场门口径评估_20261009.md`）:
  盘中市场门原先吃**当日 close** 横截面（日频、全天恒定）⇒ 用「15:00 收盘」去门控
  一个 **14:56** 的入场 = 单向**前视**（4 分钟）。修复 = 市场门读**评估槽当时**的
  横截面（与生产 `scan._mkt_gain(snaps)` 同口径）。

本组四把锁（零 DB）:
  ① `IntradayFeed`：`mkt_slots` → `ctx["mkt_series"]` 与快照序列**逐槽对齐**，缺槽=None；
  ② knife `evaluate`：市场门按**评估槽**取值 —— 14:56 门拒 / 14:57 门过 ⇒ 成交顺延到
     **14:57**（入场价随之从 85.1 变 85.2）；回退日频标量时恒在 14:56；
  ③ 回退链零破坏：无 `mkt_series` ⇒ 退回 `ctx.mkt_gain` 标量（合成 golden 走这条）；
  ④ `load_market_slots` 取数：假帧 → `{date: {HH:MM: 该槽全市场均涨幅%}}`（口径 = 该槽 open）。
"""
from __future__ import annotations

import numpy as np

from tests.golden.inputs import _knife_bars, _knife_rows

_D0 = "2026-10-05"


# ================================================================
# ① IntradayFeed: mkt_slots → mkt_series（逐槽对齐，缺槽 None）
# ================================================================
def test_feed_injects_aligned_mkt_series(monkeypatch):
    from app.market_cn.auto.core.replay.intraday import IntradayFeed

    bars = _knife_bars()
    rows = [{"time": f"{_D0} 14:56:00"}, {"time": f"{_D0} 14:57:00"},
            {"time": f"{_D0} 14:58:00"}]
    day = {_D0: {"14:56": -1.5, "14:58": -3.0}}      # 14:57 故意缺 → None (该槽 fail-closed)
    feed = IntradayFeed(bars, "600001", preload=False, mkt_slots=day)
    monkeypatch.setattr(feed, "series", lambda date, pc: rows)

    k = next(i for i, b in enumerate(bars) if str(b["time"])[:10] == _D0)
    ctx = feed._ctx_for(k, bars)
    assert ctx["mkt_series"] == [-1.5, None, -3.0], ctx["mkt_series"]
    assert len(ctx["mkt_series"]) == len(ctx["series"])


def test_feed_without_mkt_slots_has_no_mkt_series(monkeypatch):
    """未传 mkt_slots ⇒ 不注入 mkt_series（回退标量路径，零破坏）。"""
    from app.market_cn.auto.core.replay.intraday import IntradayFeed

    bars = _knife_bars()
    feed = IntradayFeed(bars, "600001", preload=False)
    monkeypatch.setattr(feed, "series", lambda date, pc: [{"time": f"{_D0} 14:56:00"}])
    k = next(i for i, b in enumerate(bars) if str(b["time"])[:10] == _D0)
    assert "mkt_series" not in feed._ctx_for(k, bars)


# ================================================================
# ② / ③ 市场门按评估槽取值（含回退链对照）
# ================================================================
def _fold_entry(mkt_series=None, scalar=-3.0):
    """折叠跑合成 knife 触发日 → 返回 (entry_date, entry_price)。"""
    from app.market_cn.auto import strategies as reg
    from app.market_cn.auto.core.replay import TradesCollector, replay_batch

    reg.autodiscover()
    strat = reg.get_strategy("knife_catch")
    bars = _knife_bars()
    rows = _knife_rows()[_D0]

    def _ctx(code, date, _bars):
        if date != _D0:
            return {}
        ctx = {"series": rows, "mkt_gain": scalar}
        if mkt_series is not None:
            ctx["mkt_series"] = mkt_series
        return ctx

    coll = TradesCollector(code="600001", strategy="knife_catch")
    res = replay_batch(strat, {"600001": bars},
                       collectors={"600001": [coll]}, ctx_provider=_ctx)
    r = res.get("600001")
    ts = (r.trades or []) if r is not None else []
    assert ts, "合成触发日必须出 1 笔（否则本测试是空对空假绿）"
    return [(t.get("entry_date"), t.get("entry_price")) for t in ts]


def test_mkt_gate_is_per_slot_asof():
    """14:56 市场门拒、14:57 门过 ⇒ **在 14:57 成交**（入场价 85.1 → 85.2）。"""
    bars = _knife_bars()
    rows = _knife_rows()[_D0]
    # 14:56 槽市场 -0.5%（> -1.0 阈值 → 拒）；14:57 起 -3.0%（→ 过）
    series = [(-0.5 if r["time"][11:16] < "14:57" else -3.0) for r in rows]
    got = _fold_entry(mkt_series=series)
    assert got == [("2026-10-05", 85.2)], (
        "市场门未按槽取值 —— 14:56 门拒后应在 14:57 成交(85.2), 实得 %r" % (got,))


def test_mkt_gate_control_day_level_scalar_unchanged():
    """对照：日频标量路径（无 mkt_series）恒在 14:56 成交 —— 证明 ② 的差异只来自逐槽。"""
    got = _fold_entry(mkt_series=None, scalar=-3.0)
    assert got == [("2026-10-05", 85.1)], got


def test_mkt_series_all_fail_no_trade():
    """逐槽全拒（-0.5）⇒ 无成交（该槽门 fail-closed 语义）。"""
    from app.market_cn.auto import strategies as reg
    from app.market_cn.auto.core.replay import TradesCollector, replay_batch

    reg.autodiscover()
    strat = reg.get_strategy("knife_catch")
    bars = _knife_bars()
    rows = _knife_rows()[_D0]
    series = [-0.5] * len(rows)

    def _ctx(code, date, _bars):
        return {"series": rows, "mkt_gain": -3.0, "mkt_series": series} if date == _D0 else {}

    coll = TradesCollector(code="600001", strategy="knife_catch")
    res = replay_batch(strat, {"600001": bars},
                       collectors={"600001": [coll]}, ctx_provider=_ctx)
    r = res.get("600001")
    assert not ((r.trades or []) if r is not None else []), "逐槽全拒却出了单"


# ================================================================
# ④ load_market_slots 取数（假帧，零 DB）
# ================================================================
def test_load_market_slots_maps_slots_from_frame(monkeypatch):
    from app.market_cn.auto.core.data import frames as fr
    from app.market_cn.auto.core.replay.mkt_slots import load_market_slots

    # 2 只票 × 2 槽: 槽0 → 0% / 槽1 → +5% 与 -2%（均值 +1.5%）
    frame = fr.MinuteFrame("2026-10-05", ["A", "B"], np.asarray([0, 2]),
                           np.asarray([2, 2]),
                           np.asarray([10.0, 10.5, 20.0, 19.6]),
                           np.zeros(4), np.zeros(4), np.zeros(4), np.zeros(4))
    monkeypatch.setattr(fr, "trading_dates", lambda *a, **k: ["2026-10-05"])
    monkeypatch.setattr(fr, "prev_closes", lambda d: {"A": 10.0, "B": 20.0})
    monkeypatch.setattr(fr, "build_frame", lambda d, *a, **k: frame)

    out = load_market_slots("2026-10-05", "2026-10-05", min_n=2)
    assert out == {"2026-10-05": {"09:31": 0.0, "09:32": 1.5}}, out
    # 样本不足 (min_n 高) ⇒ 该日整块不给值 (fail-closed, 不伪造)
    assert load_market_slots("2026-10-05", "2026-10-05", min_n=200) == {}


def test_load_market_slots_fails_closed_on_error(monkeypatch):
    """取数失败 ⇒ {}（市场门回退调用方的日频标量/fail-closed；绝不伪造）。"""
    from app.market_cn.auto.core.data import frames as fr
    from app.market_cn.auto.core.replay.mkt_slots import load_market_slots

    def _boom(*a, **k):
        raise RuntimeError("no db")

    monkeypatch.setattr(fr, "trading_dates", _boom)
    assert load_market_slots("2026-10-05", "2026-10-06") == {}
