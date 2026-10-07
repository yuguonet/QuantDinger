#!/usr/bin/env python3
"""core/increm.py — 唯一增量形态：state = bars[..t] 的**有界摘要** + 逐格推进 + 取值。

背景（改进方案 §1-④ / §2.5）。
------------------------------------------------------------
仓库里一度并存 **4 种私有的增量写法**，同一数学各写一份:

  ① resume 三元组 (`core/runtime/resume.py`)      —— macd/atr/boll/kdj 的 codec
  ② cross_section 的 g1_state/锚 + 滚动核          —— 单票 G1 特征
  ③ 策略自维护 ring/锚 (`strategies/*.py`)         —— 如 dragon 的 ring+EMA 锚
  ④ window_cache (`core/data/window_cache.py`)     —— 数据层滑动取数

本模块把「增量」抽象成**一个协议** —— 任何指标/滚动核只需回答三个问题:

    snapshot(bars) -> state      # seed: 一次 O(n)，只在新票/除权重建时做
    step(state, bar) -> state    # 逐格推进 O(1)~O(window): 每日只喂 1 根新 bar
    value(state) -> float|tuple  # 由 state 取当前值（**不再回看历史**）

统一后的关系（各层职责不重叠，不是"两套实现并存"）:
    app/utils/indicators.py       纯数学（EMA/MACD 递推本体；唯一）
    core/runtime/resume.py        指标 codec: snapshot/resume/compute（序列口径）
    core/increm.py（本模块）       统一增量协议 + 通用滚动核（逐格口径）
策略侧（③）用 RingKernel + `step` 组合出自己的状态机；数据层（④）与之正交，
不进本模块。

设计纪律（沿用全仓铁律）:
  - **数学只有一份**: 指标递推一律**委托** resume.py / indicators.py，本模块
    不重写 EMA/ATR/RSI/BOLL/KDJ 的递推本体（否则又是"第二份"，改一处忘一处）。
  - **零业务常量**: core 不得含市场专属常量（涨跌停/板块/窗口期）。滚动核一律
    参数化（n / mult / w / min_hist 由调用方给），默认值只放"标准技术指标参数"。
    ⇒ G1 的 `win=68` / `ROLL=20` / `MIN_HIST=5` 这类业务窗口**留在 cross_section**，
      不搬进来。
  - **逐位等价是唯一契约**:
        step(snapshot(bars[:k]), bars[k]) == snapshot(bars[:k+1])   （状态级，逐位）
    见 `tests/present/test_increm.py` 的对拍。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

# ★ 数学单一来源：指标 codec (resume) 与基座叶子层 (indicators)。
from app.market_cn.auto.core.runtime import resume as _RES
from app.utils.indicators import (
    macd_anchor_step as _macd_anchor_step,
    rsi_state as _wilder_state,
    rsi_step as _wilder_step,
    rsi_value as _wilder_value,
)


# ================================================================
# 1. 通用滚动核（纯 numpy，业务无关）
#    2026-10-07 从 `core/features/cross_section.py` 提炼到此处（单一 home）。
#    cross_section 反向 import 本模块，不再本地另写一份。
# ================================================================

def roll_sum(x, n):
    """滑窗和（逐窗**独立**累加）—— 结果与**窗口长度无关**，全量/截窗逐位一致。

    ⚠ 2026-10-05 从 `cumsum` 差改为 `np.convolve`:
       · cumsum 差的结果依赖**累加起点** ⇒ 全量 198 根与窗口 35 根差 ~1e-13，
         而门是**阶跃函数** ⇒ 边界值（如 rma 恰好 -2.5）会翻转布尔结果。
         这是"增量路径"的致命隐患：浮点等价(1e-6 容差) **不等于** 门判定等价。
       · convolve 每个窗口都由**同样的 n 个数按同样顺序**累加 ⇒ 与起点无关，
         且更精确（无灾难性抵消）；实测还**更快**（0.79x）。
    """
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        out[n - 1:] = np.convolve(np.asarray(x, dtype=float), np.ones(n), "valid")
    return out


def sma(x, n):
    """简单均线（滑窗均值）。"""
    return roll_sum(x, n) / n


def rsi(c, n=14):
    """RSI（滑窗口径，前 n 根为 NaN）。"""
    d = np.diff(c, prepend=c[0])
    g = roll_sum(np.clip(d, 0, None)[1:], n)
    lo = roll_sum(np.clip(-d, 0, None)[1:], n)
    out = np.full(len(c), np.nan)
    out[1:] = 100 - 100 / (1 + (g / n) / np.where(lo == 0, np.nan, lo / n))
    return out


def atr_pct(h, l, c, n=14):
    """ATR%（TR 的滑窗均值 / 收盘 × 100）。"""
    pc = np.roll(c, 1)
    pc[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    return roll_sum(tr, n) / n / c * 100


def boll_pctb(c, n=20, k=2.0):
    """布林 %b（0-100 口径: lo=0, up=100）；前 n-1 根 NaN；退化记 50 中性。"""
    m = len(c)
    pctb = np.full(m, np.nan)
    w = np.ones(n) / n
    ma = np.convolve(c, w, "valid")
    c2 = np.convolve(c * c, w, "valid")
    sd = np.sqrt(np.maximum(c2 - ma * ma, 0.0))
    lo = ma - k * sd
    up = ma + k * sd
    denom = up - lo
    pctb[n - 1:] = np.where(denom > 0, (c[n - 1:] - lo) / denom * 100, 50.0)
    return pctb


def pctl_roll(day_val, w, min_hist):
    """日度序列滚动分位（不含当日，零前视）；历史不足 min_hist 为 nan。

    ⚠ `w` / `min_hist` 是**业务窗口**，必须由调用方显式给（本模块不留默认值，
      避免把 G1 的口径常量搬进 core）。
    """
    out = np.full(len(day_val), np.nan)
    for i in range(len(day_val)):
        hist = day_val[max(0, i - w):i]
        if len(hist) >= min_hist:
            out[i] = (hist < day_val[i]).mean()
    return out


def window_of(bars, win):
    """bars → 紧凑 OHLC 微缩窗口 (`[[YYYY-MM-DD, high, low, close], ...]`)。

    ★ 状态自带窗口的意义：`_g1_arrays` 的 ATR 要 high/low，光有 closes 队列
      不足以算特征 ⇒ 若状态不含窗口，每天推进后还得回库再取历史 bars，
      "每天只处理 D+1 的量"就是假的。只存 4 个有用字段（open 不参与），省 ~20%。
    """
    return [[str(b["time"])[:10], float(b["high"]), float(b["low"]), float(b["close"])]
            for b in bars[-win:]]


def anchor_step(anchor, closes, fast=12, slow=26, signal=9):
    """EMA 锚 (ef, es, dea) 沿 closes 往后推进，返回推进后的末根状态。

    ★ 委托 `app/utils/indicators.macd_anchor_step`（MACD 递推的唯一实现）。
    ⚠ 必须从**相对下标 0** 起推（`out[0]` 就是"前一根的下一根"），不能写成
      "继承第 n-1 个" —— 那会整条错位 n-1 根且**不报错**。
    """
    return _macd_anchor_step(anchor, closes, fast, slow, signal)


# ================================================================
# 2. 增量协议
# ================================================================

class IncrKernel:
    """增量指标内核：state = 历史的有界摘要。

    子类实现三件事；**不得**在子类里重写递推数学（委托 codec/indicators）。
    """

    key: str = ""

    def snapshot(self, bars: Sequence[Dict[str, Any]]) -> Any:
        """seed：bars → 状态（语义 = 「bars 末根结束时」的摘要）。"""
        raise NotImplementedError

    def step(self, state: Any, bar: Dict[str, Any]) -> Any:
        """逐格推进：D 日状态 + D+1 的 1 根新 bar → D+1 状态（O(1)~O(window)）。"""
        raise NotImplementedError

    def value(self, state: Any):
        """由 state 取当前值（不回看历史）。"""
        raise NotImplementedError


# ================================================================
# 3. 指标核（全部委托 resume.py 的 codec —— 不重写递推）
# ================================================================

class MacdKernel(IncrKernel):
    """MACD。state = (ema_fast, ema_slow, dea)。"""

    key = "macd"

    def __init__(self, fast=12, slow=26, signal=9):
        self.fast, self.slow, self.signal = fast, slow, signal

    def snapshot(self, bars):
        return _RES.macd_snapshot(bars, self.fast, self.slow, self.signal)

    def step(self, state, bar):
        # EMA 接力无瞬态；委托 indicators 唯一递推（见 anchor_step）。
        return anchor_step(state, [float(bar["close"])],
                           self.fast, self.slow, self.signal)

    def value(self, state):
        ef, es, dea = (float(x) for x in state)
        dif = ef - es                      # DIF = EMA(fast) - EMA(slow)（定义式）
        return (dif, dea, 2.0 * (dif - dea))   # hist = 2·(DIF-DEA)（定义式）


class AtrKernel(IncrKernel):
    """ATR（Wilder）。state = (末根 atr, 是否已暖机, 末根 close)。"""

    key = "atr"

    def __init__(self, n=14):
        self.n = n

    def snapshot(self, bars):
        return _RES.atr_snapshot(bars, self.n)

    def step(self, state, bar):
        # ⚠ 前置：state 必须已暖机（seed 长度 >= n）。未暖机 in atr_resume 直接抛，
        #   不静默兜底（暖机区不可续算是数学事实，见 resume.atr_resume 注释）。
        arr = _RES.atr_resume(state, [bar], self.n)
        return (float(arr[0]), True, float(bar["close"]))

    def value(self, state):
        return float(state[0])


class BollKernel(IncrKernel):
    """BOLL。state = 最近 n 根 close 窗口（滑窗型 O(window)）。"""

    key = "boll"

    def __init__(self, n=20, mult=2.0):
        self.n, self.mult = n, mult

    def snapshot(self, bars):
        return tuple(_RES.boll_snapshot(bars, self.n, self.mult))

    def step(self, state, bar):
        return tuple((list(state) + [float(bar["close"])])[-self.n:])

    def value(self, state):
        win = [float(c) for c in state]
        if not win:
            return (0.0, 0.0, 0.0)
        mid, up, lo = _RES.boll_compute([{"close": c} for c in win], self.n, self.mult)
        return (mid[-1], up[-1], lo[-1])


class KdjKernel(IncrKernel):
    """KDJ。state = (K, D, 最近 n 根 (h,l,c) 窗口)。"""

    key = "kdj"

    def __init__(self, n=9, ks=3, ds=3):
        self.n, self.ks, self.ds = n, ks, ds

    def snapshot(self, bars):
        return _RES.kdj_snapshot(bars, self.n, self.ks, self.ds)

    def step(self, state, bar):
        k_prev, d_prev, win = state
        K, D, _ = _RES.kdj_resume(state, [bar], self.n, self.ks, self.ds)
        nw = tuple(list(win)[1:] + [(float(bar["high"]), float(bar["low"]),
                                     float(bar["close"]))])[-self.n:]
        return (float(K[-1]), float(D[-1]), nw)

    def value(self, state):
        k, d, _ = state
        # J = 3K - 2D（与 kdj_compute 同一恒等式；K/D 来自 state，不重算递推）
        return (float(k), float(d), 3.0 * float(k) - 2.0 * float(d))


class RsiKernel(IncrKernel):
    """Wilder RSI。state = [avg_g, avg_l, prev_close]（有界，3 个 float）。

    ⚠ 与模块级 `rsi()`（G1 用的**滑窗均值** RSI，序列口径）**不是**同一指标：
      本核是 Wilder 平滑（dragon 口径）。数值不同，各有门阈值，勿混用。
    state 自带 prev_close ⇒ `step(state, bar)` 自洽（无需外部单传前收）。
    数学委托 `app/utils/indicators.rsi_state/rsi_step/rsi_value`（唯一 home）。
    """

    key = "rsi"

    def __init__(self, period=6):
        self.period = period

    def snapshot(self, bars):
        closes = [float(b["close"]) for b in bars]
        st = _wilder_state(closes, self.period)
        if st is None:
            return None
        return [st[0], st[1], closes[-1]]

    def step(self, state, bar):
        if state is None:
            return None
        c = float(bar["close"])
        ns = _wilder_step([state[0], state[1]], state[2], c, self.period)
        return [ns[0], ns[1], c]

    def value(self, state):
        if state is None:
            return None
        return _wilder_value([state[0], state[1]])


# ================================================================
# 4. RingKernel —— 策略自维护 ring/锚（形态③）的通用底座
# ================================================================
class RingKernel(IncrKernel):
    """定长 ring：state = 最近 size 根 bar 的拷贝（队尾最新）。

    策略侧（如 dragon 的 ring+EMA 锚）在本底座之上叠加自己的冻结/锚逻辑；
    本类只回答"定长滑窗"这一最通用的部分（**不**含任何策略语义）。
    """

    key = "ring"

    def __init__(self, size, project=None):
        self.size = int(size)
        #: bar → 紧凑记录；缺省原样拷贝。策略用它把 init/step 的**同一** record
        #: 形状收敛到一处（此前 init_state 与 step 各写一份 dict 字面量 = 漂移温床）。
        self._project = project or (lambda b: dict(b))

    def snapshot(self, bars):
        return [self._project(b) for b in bars[-self.size:]]

    def step(self, state, bar):
        return (list(state) + [self._project(bar)])[-self.size:]

    def value(self, state):
        return list(state)


# ================================================================
# 5. 注册表（工具的枚举入口）
# ================================================================

_KERNELS: Dict[str, IncrKernel] = {}


def register(kernel: IncrKernel) -> IncrKernel:
    if not kernel.key:
        raise ValueError("IncrKernel.key 不能为空")
    _KERNELS[kernel.key] = kernel
    return kernel


def get(key: str) -> Optional[IncrKernel]:
    return _KERNELS.get(key)


def keys() -> List[str]:
    return sorted(_KERNELS)


def kernels() -> List[IncrKernel]:
    return [_KERNELS[k] for k in keys()]


#: 内置指标核（默认参数；调用方要自定义 n/fast 时直接实例化对应类）。
for _k in (MacdKernel(), AtrKernel(), BollKernel(), KdjKernel(), RsiKernel()):
    register(_k)
