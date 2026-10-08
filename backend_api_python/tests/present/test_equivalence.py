"""test_equivalence.py — **跨路径一致性**（scan 入口 vs fold 入口）。

⚠️ **命名修正（2026-10-08，golden 逐笔对拍任务）**：
  本文件原自述「与旧版 scan_signals / exit_decision 逐门对打（等价性验收）」，
  **与事实不符** —— 实测参数是 `cls_new=KnifeCatchStrategy, cls_old=kc_old.KnifeCatchStrategy`，
  两侧**同一个类**（`kc_old` 就是当前树的 `app.market_cn.auto.strategies.knife_catch`，
  不是什么「仓库提取物」）。真正在比的是：

    · `strategy.scan_signals(...)`（生产扫描入口，旧链）
    · `strategy.evaluate(state, DayInput, prev)`（折叠契约入口，新链）

  同一条策略的两个**入口**必须给同一判定 —— 这是「两条消费路径共用一个口径」
  的不变量（同 `base.signal_of_ready` 头注的「两条消费路径共用本函数，分叉必滴」），
  **有价值，保留**。但它是**内部一致性**，不是新旧对拍。

★ 真正的「与旧版对拍」在哪里：`tests/golden/test_golden_parity.py`（逐笔对拍冻结基线，
  删除旧代码的唯一开关）。两者互补，不重复。

易错点：
  - 不要用 `pytest.importorskip` —— 静默 skip = 假绿（本项目门禁通则）。
    原实现用了，且注释声称「不可用则整模块跳过」，实际上永远不会不可用。
  - fuzz 用固定 seed（42/7）：换 seed 不会变结果，但会让历史失败不可复现。
"""

import random

import pytest

from app.market_cn.auto.core.present.contract import DayInput, Progress
from app.market_cn.auto.strategies import knife_catch as kc_old   # = 当前树的 scan 入口
from app.market_cn.auto.strategies import tail_oversold as to_old  # 同上
from app.market_cn.auto.strategies.knife_catch import KnifeCatchStrategy
from app.market_cn.auto.strategies.tail_oversold import TailOversoldStrategy

from tests.present.common import (
    KNIFE_HIST_CLOSES, TAIL_HIST_CLOSES,
    gen_hist_bars, knife_day_rows, make_snapshot_rows, tail_day_rows,
)

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
    new = KnifeCatchStrategy()
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
    new = KnifeCatchStrategy()
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
    new = KnifeCatchStrategy()
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
    new = TailOversoldStrategy()
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
    new = TailOversoldStrategy()
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

@pytest.mark.parametrize("mod,cls_fold,cls_scan", [
    (kc_old, KnifeCatchStrategy, kc_old.KnifeCatchStrategy),
    (to_old, TailOversoldStrategy, to_old.TailOversoldStrategy),
])
def test_exec_price_matches_exit_decision(mod, cls_fold, cls_scan):
    """fold 入口（evaluate）与 scan 入口（exit_decision）出场价一致。

    ⚠️ 两侧是**同一个类的两个方法**（不是新旧版本）——见文件头注命名修正。
    """
    old = cls_scan()
    new = cls_fold()
    bars = gen_hist_bars(CODE, KNIFE_HIST_CLOSES if cls_fold is KnifeCatchStrategy
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
