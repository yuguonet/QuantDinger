"""test_equivalence.py — 与旧版 scan_signals / exit_decision 逐门对打（等价性验收）。

旧版参照代码从仓库提取物 import（conftest 注入 sys.path）；不可用则整模块跳过。
对比口径：同一 (历史 bars, 盘中快照序列) 输入下——
  - 首次触发的分钟、成交价、评分、extra 特征 一致
  - 不触发场景两边一致
  - D1 开盘出场价与旧 exit_decision(day_close) 一致
"""

import random

import pytest

from app.market_cn.auto.slice.contract import DayInput, Progress
from app.market_cn.auto.slice.strategies import KnifeCatchSlim, TailOversoldSlim
from app.market_cn.auto.slice.tests.common import (
    KNIFE_HIST_CLOSES, TAIL_HIST_CLOSES,
    gen_hist_bars, knife_day_rows, make_snapshot_rows, tail_day_rows,
)

kc_old = pytest.importorskip("app.market_cn.auto.strategies.knife_catch",
                             reason="旧参照代码不可用")
to_old = pytest.importorskip("app.market_cn.auto.strategies.tail_oversold",
                             reason="旧参照代码不可用")

CODE = "600001"


def _old_first_fire(old_scan, bars, code, rows, mkt_gain, start_hhmm):
    """旧口径滚动扫描：每 tick 调 scan_signals，取首次触发。"""
    for i, row in enumerate(rows):
        hh = str(row["time"])[11:16]
        if hh < start_hhmm or hh > "15:00":
            continue
        sigs = old_scan(bars, code, ctx={"latest": row, "series": rows[:i + 1],
                                         "mkt_gain": mkt_gain})
        if sigs:
            return i, sigs[0]
    return None, None


def _new_ready(s, bars, code, rows, mkt_gain):
    state = s.init_state(code, bars)
    ctx = {"latest": rows[-1], "series": rows, "mkt_gain": mkt_gain}
    events = s.evaluate(state, DayInput(code, rows[-1], ctx), None)
    ready = [e for e in events if e.stage == "ready"]
    return ready[0] if ready else None


# ---------------- knife ----------------

def test_knife_crafted_trigger_day_matches_old():
    old = kc_old.KnifeCatchStrategy()
    new = KnifeCatchSlim()
    bars = gen_hist_bars(CODE, KNIFE_HIST_CLOSES)
    day = "2026-10-05"
    rows = knife_day_rows(day, pc=100.0)
    g = -3.0

    i, sig = _old_first_fire(old.scan_signals, bars, CODE, rows, g, "14:56")
    assert sig is not None, "构造场景应当触发"
    ev = _new_ready(new, bars, CODE, rows, g)
    assert ev is not None
    assert ev.payload["price"] == sig.price
    assert ev.payload["score"] == sig.score
    assert ev.payload["label"] == sig.label
    assert ev.payload["extra"] == sig.extra
    assert ev.date == sig.time
    assert str(rows[i]["time"])[11:16] == "14:56"


def test_knife_noise_day_matches_old():
    old = kc_old.KnifeCatchStrategy()
    new = KnifeCatchSlim()
    bars = gen_hist_bars(CODE, KNIFE_HIST_CLOSES)
    rows = knife_day_rows("2026-10-05", pc=100.0)
    for r in rows:                      # 横盘噪声：所有门都不过
        r["last"] = 99.0
        r["high"], r["low"] = 99.5, 98.5
    i, sig = _old_first_fire(old.scan_signals, bars, CODE, rows, -3.0, "14:56")
    assert sig is None
    assert _new_ready(new, bars, CODE, rows, -3.0) is None


def test_knife_fuzz_matches_old():
    old = kc_old.KnifeCatchStrategy()
    new = KnifeCatchSlim()
    rng = random.Random(42)
    for trial in range(15):
        n = rng.randint(10, 20)
        closes = [rng.uniform(80, 140) for _ in range(n)]
        bars = gen_hist_bars(CODE, closes)
        pc = rng.uniform(90, 110)
        day = "2026-10-05"
        prices = {}
        px = pc * rng.uniform(0.9, 1.05)
        for m in range(14 * 60, 15 * 60 + 1):
            px *= rng.uniform(0.99, 1.01)
            prices[f"{m // 60:02d}:{m % 60:02d}"] = round(px, 4)
        day_high = max(prices.values()) * 1.01
        day_low = min(prices.values()) * 0.99
        rows = make_snapshot_rows(day, pc, prices, day_high, day_low,
                                  total_vol=rng.uniform(500, 2000))
        g = rng.uniform(-6, 2)
        i, sig = _old_first_fire(old.scan_signals, bars, CODE, rows, g, "14:56")
        ev = _new_ready(new, bars, CODE, rows, g)
        if sig is None:
            assert ev is None, f"trial={trial} 旧不触发新触发"
        else:
            assert ev is not None, f"trial={trial} 旧触发新不触发"
            assert ev.payload["price"] == sig.price, trial
            assert ev.payload["score"] == sig.score, trial
            assert ev.payload["extra"] == sig.extra, trial


# ---------------- tail ----------------

def test_tail_crafted_trigger_day_matches_old():
    old = to_old.TailOversoldStrategy()
    new = TailOversoldSlim()
    bars = gen_hist_bars(CODE, TAIL_HIST_CLOSES)
    rows = tail_day_rows("2026-10-05", pc=100.0)

    i, sig = _old_first_fire(old.scan_signals, bars, CODE, rows, None, "14:50")
    assert sig is not None, "构造场景应当触发"
    ev = _new_ready(new, bars, CODE, rows, None)
    assert ev is not None
    assert ev.payload["price"] == sig.price
    assert ev.payload["score"] == sig.score
    assert ev.payload["label"] == sig.label
    assert ev.payload["extra"] == sig.extra
    assert str(rows[i]["time"])[11:16] >= "14:50"


def test_tail_fuzz_matches_old():
    old = to_old.TailOversoldStrategy()
    new = TailOversoldSlim()
    rng = random.Random(7)
    for trial in range(15):
        n = rng.randint(10, 20)
        closes = [rng.uniform(80, 140) for _ in range(n)]
        bars = gen_hist_bars(CODE, closes)
        pc = rng.uniform(90, 110)
        day = "2026-10-05"
        prices = {}
        px = pc * rng.uniform(0.85, 1.05)
        for m in range(14 * 60, 15 * 60 + 1):
            px *= rng.uniform(0.99, 1.01)
            prices[f"{m // 60:02d}:{m % 60:02d}"] = round(px, 4)
        rows = make_snapshot_rows(day, pc, prices,
                                  max(prices.values()) * 1.01,
                                  min(prices.values()) * 0.99,
                                  total_vol=rng.uniform(500, 2000))
        i, sig = _old_first_fire(old.scan_signals, bars, CODE, rows, None, "14:50")
        ev = _new_ready(new, bars, CODE, rows, None)
        if sig is None:
            assert ev is None, f"trial={trial} 旧不触发新触发"
        else:
            assert ev is not None, f"trial={trial} 旧触发新不触发"
            assert ev.payload["price"] == sig.price, trial
            assert ev.payload["score"] == sig.score, trial
            assert ev.payload["extra"] == sig.extra, trial


# ---------------- 出场（D1 开盘价 = 展示点 stage 2） ----------------

@pytest.mark.parametrize("mod,cls_new,cls_old", [
    (kc_old, KnifeCatchSlim, kc_old.KnifeCatchStrategy),
    (to_old, TailOversoldSlim, to_old.TailOversoldStrategy),
])
def test_exec_price_matches_old_exit_decision(mod, cls_new, cls_old):
    old = cls_old()
    new = cls_new()
    bars = gen_hist_bars(CODE, KNIFE_HIST_CLOSES if cls_new is KnifeCatchSlim
                         else TAIL_HIST_CLOSES)
    d0 = {"time": "2026-10-05", "open": 96.4, "high": 96.5, "low": 84.0,
          "close": 85.2, "volume": 1200.0}
    d1 = {"time": "2026-10-06", "open": 86.5, "high": 87.0, "low": 85.0,
          "close": 86.2, "volume": 900.0}
    full = bars + [d0, d1]
    row = {"entry_date": d0["time"], "entry_price": 85.1}
    dec = old.exit_decision(row, {"mode": "day_close", "bars": full,
                                  "entry_idx": len(full) - 2})
    assert dec.action == "exit"
    state = new.init_state(CODE, full[:-2])
    prev = Progress(stage="ready", date=d0["time"], payload={"price": 85.1})
    events = new.evaluate(state, DayInput(CODE, d1, None), prev)
    execs = [e for e in events if e.stage == "exec"]
    assert execs and execs[0].payload["exit_price"] == dec.price == 86.5
