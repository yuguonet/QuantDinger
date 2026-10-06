"""test_g1.py — G1 内核：锚播种增量 == 全量重算（g56 的地基）。

对照旧参照实现 app.market_cn.auto.core.features.cross_section（importorskip）。
特征级浮点等价（锚补偿 EMA 无限记忆），门判定（mask）必须逐位一致。
"""

import numpy as np
import pytest

from app.market_cn.auto.slice import g1
from app.market_cn.auto.slice.tests.common import gen_hist_bars

cs_old = pytest.importorskip("app.market_cn.auto.core.features.cross_section",
                             reason="旧参照代码不可用")


def _rand_bars(n=300, seed=11):
    rng = np.random.default_rng(seed)
    closes = 100 * np.cumprod(1 + rng.normal(0, 0.02, n))
    bars = []
    for i, c in enumerate(closes):
        c = float(c)
        bars.append({"time": f"2025-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
                     "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1e5})
    return bars


FEATURE_KEYS = ("rma", "rma_chg", "atr", "rsi", "big20", "ma20",
                "rhist_chg", "dif0", "pctb", "dist_ma20")


def test_g1_kernel_matches_old_cross_section():
    """同一条全量 bars：新 _g1_arrays/_g1_mask 与旧实现逐位一致（同源移植）。"""
    bars = _rand_bars(300)
    f_new = g1._g1_arrays(bars)
    f_old = cs_old._g1_arrays(bars)
    for k in FEATURE_KEYS:
        np.testing.assert_allclose(f_new[k], f_old[k], rtol=0, atol=0,
                                   err_msg=k)
    for board in ("main", "gem_star"):
        np.testing.assert_array_equal(g1._g1_mask(f_new, board),
                                      cs_old._g1_mask(f_old, board))


def test_g1_incremental_equals_full_recompute():
    """锚播种逐日推进 vs 从头全量：特征浮点等价、mask 逐位一致。"""
    bars = _rand_bars(300)
    win = 35
    st = g1.g1_state_init(bars[:120], win=win, keep_window=True)
    for b in bars[120:]:
        st = g1.g1_state_step(st, [b])
    f_inc = g1.g1_state_features(st)
    full = cs_old._g1_arrays(bars)          # 朴素播种全量
    # 窗口头部是暖机段（NaN），只比窗口尾部有效段 + 末位（台账"判末根"口径）
    tail = 10
    for k in FEATURE_KEYS:
        np.testing.assert_allclose(f_inc[k][-tail:], full[k][-win:][-tail:],
                                   rtol=0, atol=1e-8, err_msg=k)
    m_inc = g1._g1_mask(f_inc, "main", age=st["age"])
    m_full = cs_old._g1_mask(full, "main")
    np.testing.assert_array_equal(m_inc[-tail:], m_full[-tail:])


def test_g1_state_step_matches_fresh_init():
    """fold 等价：逐步推进 == 一次性重算（closes/age/date 精确，head 浮点等价）。"""
    bars = _rand_bars(200)
    win = 35
    st = g1.g1_state_init(bars[:80], win=win, keep_window=True)
    for b in bars[80:]:
        st = g1.g1_state_step(st, [b])
    fresh = g1.g1_state_init(bars, win=win, keep_window=True)
    assert st["closes"] == fresh["closes"]
    assert st["date"] == fresh["date"]
    assert st["age"] == fresh["age"]
    assert st["window"] == fresh["window"]
    np.testing.assert_allclose(st["head"], fresh["head"], rtol=0, atol=1e-6)


def test_g1_state_step_rejects_resend():
    bars = _rand_bars(60)
    st = g1.g1_state_init(bars[:50], win=35, keep_window=True)
    with pytest.raises(ValueError):
        g1.g1_state_step(st, [bars[49]])       # 重发/乱序必须 fail-fast
