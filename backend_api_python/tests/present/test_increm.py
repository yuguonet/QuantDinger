"""test_increm.py — 增量基座门禁（core/increm.py）。

唯一契约（逐位等价，不是近似）：
    step(snapshot(bars[:k]), bars[k])  ==  snapshot(bars[:k+1])      （状态级）

覆盖：
  - 4 个指标核（macd/atr/boll/kdj）的**链式**推进 == 一次性重算（逐位，非容差）。
  - value(state) == 全量 compute 的末位（同口径）。
  - 通用滚动核：全量/截窗**逐位**一致（这是"增量不改变门判定"的前提，见 roll_sum 注释）。
  - RingKernel 定长滑窗。
  - 注册表：协议完整性。
  - 零业务常量：pctl_roll 的窗口参数**必须**由调用方给（无默认值）。
"""

import inspect

import numpy as np
import pytest

from app.market_cn.auto.core import increm
from app.market_cn.auto.core.runtime import resume as RES


def _bars(n=140, seed=11):
    rng = np.random.default_rng(seed)
    closes = 100 * np.cumprod(1 + rng.normal(0, 0.02, n))
    out = []
    for i, c in enumerate(closes):
        c = float(c)
        out.append({"time": f"2025-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
                    "open": c * 0.995, "high": c * 1.02, "low": c * 0.98,
                    "close": c, "volume": 1e5 + i})
    return out


def _same(a, b, atol=0.0):
    """状态级逐位比对：tuple/list 递归；标量严格相等（atol=0）。"""
    if isinstance(a, (tuple, list)) and isinstance(b, (tuple, list)):
        return len(a) == len(b) and all(_same(x, y, atol) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k], atol) for k in a)
    return abs(float(a) - float(b)) <= atol


KERNEL_KEYS = ("macd", "atr", "boll", "kdj", "rsi")


@pytest.mark.parametrize("key", KERNEL_KEYS)
def test_kernel_chain_step_equals_fresh_snapshot(key):
    """链式推进（seed 后逐根 step）与一次性重算**状态逐位**一致。"""
    bars = _bars(140)
    k = increm.get(key)
    st = k.snapshot(bars[:50])
    for j in range(50, len(bars)):
        st = k.step(st, bars[j])
        fresh = k.snapshot(bars[: j + 1])
        assert _same(st, fresh), f"{key} 在 idx={j} 状态发散\n step={st}\n fresh={fresh}"


@pytest.mark.parametrize("key", KERNEL_KEYS)
def test_kernel_state_is_bounded(key):
    """状态必须有界：跨全历史推进后规模不随 bars 增长（"增量"的定义）。"""
    bars = _bars(200, seed=5)
    k = increm.get(key)
    st0 = k.snapshot(bars[:60])
    st1 = k.snapshot(bars)
    size0, size1 = _state_size(st0), _state_size(st1)
    assert size0 == size1, f"{key} 状态规模随历史增长：{size0} -> {size1}"


def _state_size(state):
    """状态占用的标量数（标量=1，序列=长度，嵌套求和）。"""
    if isinstance(state, (tuple, list)):
        return sum(_state_size(x) for x in state)
    return 1


def test_macd_value_matches_compute_tail():
    """MACD value == 全量 macd_compute 末位（dif/dea/hist 三口径）。"""
    bars = _bars(140)
    k = increm.get("macd")
    st = k.snapshot(bars)
    dif, dea, hist = k.value(st)
    cdif, cdea, chist = RES.macd_compute(bars)
    assert dif == cdif[-1] and dea == cdea[-1] and hist == chist[-1]


def test_atr_value_matches_compute_tail():
    bars = _bars(140)
    k = increm.get("atr")
    st = k.snapshot(bars)
    assert k.value(st) == RES.atr_compute(bars)[-1]


def test_boll_value_matches_compute_tail():
    bars = _bars(140)
    k = increm.get("boll")
    st = k.snapshot(bars)
    mid, up, lo = k.value(st)
    cmid, cup, clo = RES.boll_compute(bars)
    assert (mid, up, lo) == (cmid[-1], cup[-1], clo[-1])


def test_kdj_value_matches_compute_tail():
    bars = _bars(140)
    k = increm.get("kdj")
    st = k.snapshot(bars)
    K, D, J = k.value(st)
    cK, cD, cJ = RES.kdj_compute(bars)
    assert (K, D, J) == (cK[-1], cD[-1], cJ[-1])


def test_rsi_value_matches_terminal():
    """Wilder RSI：增量链的终值 == 全量 indicators.rsi（同一数学，单一 home）。"""
    from app.utils.indicators import rsi as ind_rsi
    bars = _bars(140)
    closes = [b["close"] for b in bars]
    k = increm.get("rsi")
    st = k.snapshot(bars[:70])
    for b in bars[70:]:
        st = k.step(st, b)
    assert k.value(st) == ind_rsi(closes, 6)


def test_dragon_rsi_delegates_to_canonical():
    """dragon 的 rsi_init/rsi_step/rsi_value 已委托叶子层（不再自有第二份）。"""
    from app.market_cn.auto.strategies import dragon_callback as D
    from app.utils.indicators import rsi as ind_rsi, rsi_state, rsi_step, rsi_value
    closes = [b["close"] for b in _bars(140)]
    assert D.rsi_init(closes, 6) == rsi_state(closes, 6)
    assert D.rsi_value(D.rsi_init(closes, 6)) == ind_rsi(closes, 6)
    st = D.rsi_init(closes[:70], 6)
    for j in range(70, len(closes)):
        st = D.rsi_step(st, closes[j - 1], closes[j], 6)
    assert D.rsi_value(st) == ind_rsi(closes, 6)


def test_roll_sum_window_length_independent():
    """全量/截窗**逐位**一致 —— 增量路径不改变门判定的前提（roll_sum 注释）。"""
    x = _bars(140)
    c = np.array([b["close"] for b in x])
    full = increm.roll_sum(c, 20)
    for tail in (20, 21, 40, 140):
        part = increm.roll_sum(c[-tail:], 20)
        # 截窗末位 == 全量末位（同一窗口、同样顺序累加 ⇒ convolve 保证逐位）
        assert part[-1] == full[-1]


def test_rolling_helpers_reference():
    """通用滚动核 vs 朴素参照（同一数学，独立写法）。"""
    rng = np.random.default_rng(2)
    c = 100 + np.cumsum(rng.normal(0, 0.5, 60))
    h, l = c * 1.01, c * 0.99
    # SMA = 窗口均值（前 n-1 根窗口不完整 ⇒ NaN，与实现同口径）
    ref = np.full(60, np.nan)
    for i in range(4, 60):
        ref[i] = np.mean(c[i - 4: i + 1])
    np.testing.assert_allclose(increm.sma(c, 5), ref, rtol=0, atol=1e-12)
    # RSI 首 n 根 NaN，之后有限
    r = increm.rsi(c, 14)
    assert np.isnan(r[:14]).all() and np.isfinite(r[14:]).all()
    # ATR% 有限且非负
    a = increm.atr_pct(h, l, c, 14)
    assert np.isfinite(a[14:]).all() and (a[14:] >= 0).all()
    # %b 落在合理区间（退化记 50）
    b = increm.boll_pctb(c, 20, 2.0)
    assert np.isfinite(b[19:]).all()


def test_pctl_roll_zero_lookahead():
    """滚动分位只用**严格早于**当日的历史，且不足 min_hist 为 nan。"""
    x = np.arange(10.0)
    out = increm.pctl_roll(x, w=20, min_hist=5)
    assert np.isnan(out[:5]).all()
    assert out[5] == (x[:5] < x[5]).mean() == 1.0
    # 当日进不了自己的分数（零前视）
    assert out[6] == (x[:6] < x[6]).mean() == 1.0


def test_ring_kernel_slides():
    """RingKernel：定长滑窗，队尾最新。"""
    bars = _bars(30)
    k = increm.get("ring") or increm.RingKernel(10)
    k = increm.RingKernel(10)
    st = k.snapshot(bars[:8])           # 不足 10
    assert len(st) == 8 and st[-1]["close"] == bars[7]["close"]
    for b in bars[8:]:
        st = k.step(st, b)
    assert len(st) == 10
    assert st[-1]["close"] == bars[-1]["close"]
    assert st[0]["close"] == bars[-10]["close"]


def test_registry_protocol_complete():
    """注册表非空；每个核实现 snapshot/step/value 三件套。"""
    assert increm.keys(), "注册表为空"
    for k in increm.kernels():
        assert k.key
        for m in ("snapshot", "step", "value"):
            assert callable(getattr(k, m)), f"{k.key} 缺 {m}"
    assert increm.get("macd") is not None


def test_pctl_roll_no_business_defaults():
    """零业务常量：pctl_roll 的窗口参数必须由调用方给（core 不留 G1 口径常量）。"""
    sig = inspect.signature(increm.pctl_roll)
    for name in ("w", "min_hist"):
        assert sig.parameters[name].default is inspect.Parameter.empty, \
            f"pctl_roll.{name} 不应有默认值（那是业务常量，属于 cross_section 层）"
