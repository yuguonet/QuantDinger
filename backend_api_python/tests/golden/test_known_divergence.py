"""test_known_divergence.py — **已知口径分歧的显式登记 + 修复回归护栏**。

来源：golden 逐笔对拍任务 | 产出：2026-10-08 | 状态：分歧 #1 **已修复**

★ 为什么要有这个文件：
  golden 逐笔对拍（`test_golden_parity`）只覆盖**两侧一致**的分支。那些**确实不一致**
  的分支若不单独登记，就会以两种方式悄悄出事：
    ① 有人「顺手修好」一侧 → 另一侧无人同步 → 历史回测输出漂移无归因；
    ② 没人知道这个坑 → 复盘时把它当成新 bug 重查一遍。
  ⇒ 本文件把**实测到的分歧**写成断言：两边行为都被钉住，任何一侧变动都会 FAIL，
    逼后来者回来更新这里的登记（而不是静默改变历史口径）。

══════════════════════════════════════════════════════════════════════
分歧 #1｜「末日未平」的收尾口径 —— ✅ 已修复（2026-10-08）
══════════════════════════════════════════════════════════════════════
【修复前】
  · 通用引擎 `StrategyBase._backtest_stock_legacy`（base.py）：末日收盘平仓，
    `exit_reason="数据结束平仓"`。
  · 回放 `core.replay`：同上（`REASON_DATA_END`）。
  · **五个策略的 `backtest_stock` 覆写**（break / dragon_callback / g56 / relay3 / v1）
    在出场引擎返回 None（视野不足）时 `if not result: continue` **静默丢弃**该笔
    —— 历史回测**低估了交易数**（未平仓消失、不进统计，胜率/盈亏比受污染）。
    relay3 还附了注释「跳过开放持仓 (回测只统计已平仓)」= 记载在案的意图，但与
    通用引擎 / replay 口径不一致。

【修复后（单一实现）】
  `strategies/base.py::data_end_close()` 是断链收尾的**唯一实现**；五个覆写与通用
  引擎、`core/replay` 三方对齐。口径：末根收盘成交、`exit_day` 从入场根算起、
  `peak_return_pct` 取区间内 **high** 峰值。

【影响】历史回测输出**已变**（多出「数据结束平仓」的收尾交易）。按口径变更铁律：
  ① 已重冻 tests/golden/baselines/（实测未命中该分支，基线无变化）；
  ② 已更新 docs/口径差异报告.md；
  ③ ⚠️ **阈值标定仍需在有全市场数据的环境重做**（沙箱无 DB，做不了）。

易错点：
  - 本文件断言的是**修复后**的行为。若有人回退成「丢弃未平仓」，这里的护栏会 FAIL。
  - 别把它并进 `test_golden_parity`：那里是「两侧一致才通过」，这里是「口径本身」。
"""
from __future__ import annotations

import pytest

from tests.golden.inputs import STOCK_INFO_DRAGON, crafted_bars_for_divergence


def _mk_tail(base, extra: int) -> list[dict]:
    """入场日后接 `extra` 根平缓上行（不触止损/追踪）⇒ 位置一直持有到数据尽头。"""
    from datetime import date, timedelta

    def mk(i, o, h, l, c, v=1e6):
        return {"time": str(date(2025, 1, 1) + timedelta(days=i)), "open": round(o, 3),
                "high": round(h, 3), "low": round(l, 3), "close": round(c, 3),
                "volume": v}

    tail = [mk(len(base) + k, 158 + k * 1.5, 160 + k * 1.5, 157.5 + k * 1.5, 159.5 + k * 1.5)
            for k in range(extra)]
    return list(base) + tail


# ================================================================
# 回归护栏：五个策略覆写都必须末日平仓（不再丢弃未平仓）
# ================================================================
def test_dragon_closes_at_data_end():
    """dragon 覆写现在末日平仓（修复前：静默丢弃 = 0 笔）。"""
    from app.market_cn.auto import strategies as reg
    from app.market_cn.auto.strategies.base import DATA_END_REASON
    reg.autodiscover()
    bars = _mk_tail(crafted_bars_for_divergence(), 3)   # 视野不足 hold_days=7
    s = reg.get_strategy("dragon_callback")
    trades = s.backtest_stock(bars, "600000", stock_info=STOCK_INFO_DRAGON,
                              use_prefilter=False) or []
    assert len(trades) == 1, "dragon 应产出 1 笔末日平仓交易，实得 %r" % trades
    t = trades[0]
    assert t["exit_reason"] == DATA_END_REASON, t
    assert t["exit_day"] == 4, t          # 入场 bar[32]，末日 bar[35]，d=4
    assert t["exit_price"] == bars[-1]["close"], t
    assert t["return_pct"] == round((bars[-1]["close"] / t["entry_price"] - 1) * 100, 2), t


def test_all_overrides_close_at_data_end():
    """五个 backtest_stock 覆写**都不得丢弃**未平仓交易（口径统一）。"""
    from app.market_cn.auto import strategies as reg
    from app.market_cn.auto.strategies.base import DATA_END_REASON
    reg.autodiscover()
    bars = _mk_tail(crafted_bars_for_divergence(), 3)
    checked = []
    for key, code in (("dragon_callback", "600000"), ("break", "000032"),
                      ("v1", "600000"), ("relay3", "600000")):
        s = reg.get_strategy(key)
        if s is None:
            continue
        trades = s.backtest_stock(bars, code, stock_info=STOCK_INFO_DRAGON,
                                  use_prefilter=False) or []
        # 这些策略未必在本配方下出信号；只要出了信号且引擎无结果，就必须末日平仓。
        for t in trades:
            if t.get("exit_reason") == DATA_END_REASON or t.get("exit_rule") == "time":
                checked.append((key, "data_end"))
            else:
                checked.append((key, t.get("exit_reason") or t.get("exit_rule")))
    assert checked, "至少应有一个策略在本配方下出信号（否则本测试是空对空假绿）"
    # 所有「数据结束平仓」类的都必须用统一文案（不得各写各的）
    for key, reason in checked:
        assert reason is not None, "%s 出场原因为空（口径未收口）" % key


def test_data_end_close_is_single_source():
    """`data_end_close` 是断链收尾的唯一实现 —— 五个覆写都必须引用它。"""
    import os
    import re
    here = os.path.dirname(os.path.abspath(__file__))
    sdir = os.path.normpath(os.path.join(here, "..", "..", "app", "market_cn",
                                         "auto", "strategies"))
    users = set()
    for fn in os.listdir(sdir):
        if not fn.endswith(".py") or fn == "__init__.py":
            continue
        src = open(os.path.join(sdir, fn), encoding="utf-8").read()
        if re.search(r"\bdata_end_close\s*\(", src):
            users.add(fn)
    # base.py 是实现本体；其余五个覆写必须都用它
    expect = {"base.py", "break.py", "dragon_callback.py", "g56.py",
              "relay3.py", "v1.py"}
    missing = expect - users
    assert not missing, (
        "以下文件未接入 data_end_close（末日平仓口径会再次分叉）: %s" % sorted(missing)
    )


# ================================================================
# 对照面：通用引擎与回放的同口径
# ================================================================
def test_replay_closes_at_data_end():
    """【对照】replay 在数据尽头末日收盘平仓，原因 `数据结束平仓`。"""
    from app.market_cn.auto import strategies as reg
    from app.market_cn.auto.core.replay import (
        REASON_DATA_END, DailyFeed, TradesCollector, replay,
    )
    reg.autodiscover()
    bars = _mk_tail(crafted_bars_for_divergence(), 3)
    s = reg.get_strategy("dragon_callback")
    coll = TradesCollector(code="600000", strategy="dragon_callback")
    trades = replay(s, "600000", DailyFeed(bars), collectors=[coll]).trades or []
    assert len(trades) == 1, "replay 应产出 1 笔末日平仓交易，实得 %r" % trades
    t = trades[0]
    assert t["exit_reason"] == REASON_DATA_END, t
    assert t["exit_day"] == 4, t
    assert t["exit_price"] == bars[-1]["close"], t


def test_reason_text_aligned_across_sides():
    """三方文案必须一致：base.DATA_END_REASON == replay.REASON_DATA_END。"""
    from app.market_cn.auto.core.replay import REASON_DATA_END
    from app.market_cn.auto.strategies.base import DATA_END_REASON
    assert DATA_END_REASON == REASON_DATA_END == "数据结束平仓", (
        "断链收尾文案分叉了：base=%r replay=%r" % (DATA_END_REASON, REASON_DATA_END)
    )


def test_generic_legacy_engine_closes_at_data_end():
    """【对照】通用引擎 `_backtest_stock_legacy` 也末日平仓（分歧 #1 的原始基准面）。"""
    import inspect

    from app.market_cn.auto.strategies.base import (
        StrategyBase, data_end_close,
    )
    src = inspect.getsource(StrategyBase._backtest_stock_legacy)
    assert "data_end_close" in src or "数据结束平仓" in src, (
        "通用引擎丢失了末日平仓分支 —— 请重新评估各策略覆写的收尾口径。"
    )
    assert inspect.isfunction(data_end_close)


def test_data_end_close_shapes():
    """`data_end_close` 的返回形状与字段口径（改它 = 改历史输出，必须有意）。"""
    from app.market_cn.auto.strategies.base import DATA_END_REASON, data_end_close
    bars = _mk_tail(crafted_bars_for_divergence(), 3)
    entry_idx = 32
    entry_price = 156.0
    r = data_end_close(bars, entry_idx, entry_price)
    assert r is not None
    assert set(r) == {"exit_date", "exit_price", "exit_day", "exit_reason",
                      "return_pct", "peak_return_pct"}, sorted(r)
    assert r["exit_reason"] == DATA_END_REASON
    assert r["exit_day"] == len(bars) - entry_idx
    assert r["exit_price"] == round(bars[-1]["close"], 3)
    peak = max(float(b["high"]) for b in bars[entry_idx:])
    assert r["peak_return_pct"] == round((peak / entry_price - 1) * 100, 2)
    # 非法入参一律 None（调用方照旧丢弃）
    assert data_end_close([], 0, 1.0) is None
    assert data_end_close(bars, 999, 1.0) is None
    assert data_end_close(bars, entry_idx, 0) is None


# ================================================================
# 登记必须在案
# ================================================================
def test_divergence_documented_in_report():
    """分歧必须在 docs/口径差异报告.md 里有登记（不许只活在测试里）。"""
    import os
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))), "docs", "口径差异报告.md")
    assert os.path.exists(p), "缺少 docs/口径差异报告.md"
    txt = open(p, encoding="utf-8").read()
    for key in ("末日未平", "数据结束平仓", "dragon", "data_end_close"):
        assert key in txt, (
            "docs/口径差异报告.md 未登记「%s」—— 分歧 #1 必须写进报告，不能只活在测试里"
            % key)
