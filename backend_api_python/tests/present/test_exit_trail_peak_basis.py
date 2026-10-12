"""test_exit_trail_peak_basis.py — 追踪线基准（peak vs peak_prev）的行为锁。

审计 P0-1 / `修复核对与偏离度_20261010.md` P1-4（当时被推迟为"口径专项"）
于 2026-10-11 完成根因修复，本文件锁定修复后的行为，防止静默回退。

病灶与根因
----------
`run_trail_stop` 循环内先 `peak = max(peak, 当日 high)`，再用**抬升后的** peak
算追踪线 `trig_t`，然后判「当日 low 是否触线」。日线 bar 无法分辨 high/low 的**先后**，
真实序列若为「先跌穿旧线、后冲高」，本不应触发却触发 ⇒ **日内先视**，出场系统性偏早。

原 `use_trig_prev` 只在触发**之后**二次修正成交价，**不修触发判定** —— 而函数
docstring 一直声明"追踪线用开盘时已知峰值 peak_prev"，实现与契约不符。

根因修法
--------
让 `use_trig_prev` 真正控制**追踪线基准峰值**（触发与成交同源）：
  True  → `peak_prev`（开盘时已知量，无先视）
  False → `peak`（含当日 high，dragon 历史口径，逐位保持不变）

影响面（重要）
--------------
- `dragon_callback`（use_trig_prev=False）：**零回归**，逐位不变。
- `v1`（v1.yaml 2026-10-10 上线 enabled=true，走默认 True）：出场判定改用 peak_prev
  ⇒ **回测输出会变**（"先跌穿后冲高"类笔的出场推迟）。这是消除先视的预期变化，
  若要 golden 对拍请先重冻基线。

场景（entry_price=10.0, hold_days=5, stop_loss=-7%, trailing_stop=-5%）
-----------------------------------------------------------------------
入场日 peak=10.5；D2 冲高 11.0 但最低仅 10.0（**未**跌破旧线 10.5×0.95=9.975）。

| 口径 | D2 用的基准 | D2 线 | D2 low=10.0 | 结果 |
|---|---|---|---|---|
| peak（False / dragon） | 11.0（含当日 high） | 10.45 | 10.0 ≤ 10.45 | **D2 触发**（历史口径，保留） |
| peak_prev（True / v1） | 10.5（开盘已知） | 9.975 | 10.0 > 9.975 | **D2 不触发**（先视已消除） |
"""
from app.market_cn.auto.core.exit_engines import run_trail_stop


def _bar(o, h, l, c):
    return {"time": "2026-01-01", "open": o, "high": h, "low": l, "close": c,
            "volume": 1e6}


# 入场日 peak=10.5；D2 冲高 11.0 后收回（low=10.0）；D4 才真正跌破
BARS = [_bar(10.0, 10.5, 9.9, 10.4),
        _bar(10.2, 11.0, 10.0, 10.8),
        _bar(10.8, 10.9, 10.5, 10.6),
        _bar(10.6, 10.7, 10.2, 10.3),
        _bar(10.3, 10.4, 10.0, 10.1)]

KW = dict(hold_days=5, stop_loss=-7.0, trailing_stop=-5.0)


def test_peak_basis_true_uses_peak_prev_no_lookahead():
    """use_trig_prev=True（默认/v1）：线用 peak_prev ⇒ D2 不误触发。"""
    r = run_trail_stop(BARS, 0, 10.0, use_trig_prev=True, **KW)
    assert r is not None, "视野内应最终出场（D4 才真正跌破）"
    assert r["exit_day"] == 4, (
        "D2 的 high=11.0 是当日才冲出来的，不得用来判 D2 的 low=10.0；"
        "出场应推迟到该峰值已知后的 D4，实际 %r" % r)
    # D4 触发时用的是截至 D3 的已知峰值 11.0 → 线 10.45；D4 open=10.6 > 线 ⇒ 按线成交
    assert abs(r["exit_price"] - 10.45) < 1e-9, r


def test_peak_basis_false_keeps_dragon_legacy_peak():
    """use_trig_prev=False（dragon 历史口径）：线仍用 peak ⇒ D2 触发，逐位不变。"""
    r = run_trail_stop(BARS, 0, 10.0, use_trig_prev=False, **KW)
    assert r["exit_day"] == 2, "dragon 历史口径必须保持 D2 触发（零回归），实际 %r" % r
    # open=10.2 ≤ 线 10.45 ⇒ 跳空按开盘价成交（无 trig_prev 二次修正）
    assert abs(r["exit_price"] - 10.2) < 1e-9, r


def test_trail_switch_uses_same_basis_as_trigger():
    """分段追踪档位切换与触发判定同基准（False → peak；不得一半 peak 一半 prev）。

    trails: hi=-5 / lo=-10 / switch_pct=5 ⇒ 基准涨幅 ≥5% 走 hi(紧)，否则 lo(松)。
      hi 场景: D2 peak=10.7 (+7%) → 线 10.7×0.95 = 10.165，open=10.3 > 线 ⇒ 按线成交
      lo 场景: D2 peak=10.3 (+3%) → 线 10.3×0.90 = 9.27，取 max(9.27, 止损 9.3)
               = 9.3 ⇒ 按线成交 9.3
    两档成交价必须不同（10.165 vs 9.3）；若档位判定误用另一基准，本例会失败。
    """
    trails = {"hi": -5.0, "lo": -10.0, "switch_pct": 5.0}
    kw = dict(hold_days=5, stop_loss=-7.0, trailing_stop=-5.0,
              trails=trails, use_trig_prev=False)
    hi = run_trail_stop([_bar(10.0, 10.6, 9.9, 10.5),
                         _bar(10.3, 10.7, 9.5, 9.8)], 0, 10.0, **kw)
    lo = run_trail_stop([_bar(10.0, 10.2, 9.9, 10.1),
                         _bar(10.1, 10.3, 9.0, 9.5)], 0, 10.0, **kw)
    assert hi is not None and lo is not None
    assert abs(hi["exit_price"] - 10.165) < 1e-6, hi
    assert abs(lo["exit_price"] - 9.3) < 1e-6, lo
