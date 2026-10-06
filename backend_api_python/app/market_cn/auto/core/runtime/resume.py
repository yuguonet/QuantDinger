#!/usr/bin/env python3
"""core/runtime/resume.py — 断点续传状态层 (checkpoint/resume)。

**这是长记忆指标的统一增量计算通道** (2026-10-05, 用户提议的抽象, 取代此前的
per-indicator 锚点)。此前 MACD 专门做了一个 `anchor=(ef,es,dea)` 参数 —— 那是
"为每个指标发明各自的锚" 的补丁式做法; 本层把它泛化成**一套机制**:

    断点状态 = 「bars_suffix[0] 之前那一根结束时」的**有界摘要**

只要某指标的状态是「bars[0..a] 的有界摘要」, 它就走这条通道 —— 递推型 O(1)
(EMA / MACD / ATR-Wilder / RSI-Wilder / KDJ 的 K,D), 滑窗型 O(window)
(SMA / BOLL / KDJ 的 RSV 窗口 / roll_sum)。**同一接口, 调用方不区分**。

====================================================================
核心不变量 (逐位等价, 本模块的唯一契约)
====================================================================
    compute(bars_pre + bars_post)  ==  resume(snapshot(bars_pre), bars_post)

其中 state 的语义 = "bars_post[0] **之前**那一根结束时的状态"。
这是**数学恒等**不是近似: 递推型靠 Markov 性 (整个历史只通过 e[a] 一个数传递);
滑窗型靠"窗口内值即状态"。改任何实现前必须保持它。
等价性自检: 对同一 bars 比较 `resume(snapshot(bars[:k]), bars[k:])` 与
`compute(bars)[k:]` —— 必须逐位相等 (无同名脚本, 直接跑这段对比即可)。

====================================================================
为什么比"展示层重算短窗口"更好
====================================================================
1. **通用化**: 策略不同但方法相同。新增指标/策略只要实现 snapshot/resume,
   不必各自发明锚点; 新增长记忆指标 (Wilder 类) 自动受益。
2. **实时计算量更小**: 断点取在预处理的末根 → 实时只算**断点之后的增量**,
   每指标 O(1)~O(新增根数), 而不是 O(展示窗口)。
3. **口径只有一份**: snapshot/resume 与 compute 共享同一份递推式, 不会出现
   "两套 MACD 靠注释约定一致" 的漂移温床 (见 app/utils/indicators.py)。

====================================================================
声明式检查点 / 前缀指纹失效 —— **已于 2026-10-06 (P6) 移除**
====================================================================
曾有的 `ResumePoint` 声明表、`RESUMABLE` 名字注册表、`ResumeBook` 与
`bars_fingerprint`, 其**唯一**消费方是已退役的展示层 `core/present/`
(原件在 del/20261006_core_present/)。展示层的断点语义改由 slice 契约承担:
**策略自己在 `init_state/step` 里递推**, 展示层不再收集/分发不透明 blob。

本模块因此只剩**指标 codec 层**: 每指标的 `snapshot / resume / compute` 三件套。
调用方 (core/runtime/functions.py 的 Ctx) **直接点名**调用 (如 `RES.macd_resume`),
不再经名字注册表分发。
⚠️ `snapshot` 与 `resume` 必须**成对保留**: 删任一侧, 另一侧就拿不到状态 /
无法验证逐位等价 —— 不要因为"当前零调用"就砍掉其中一半。

====================================================================
实现清单 (Ctx 的 4 个长记忆指标; 每指标 snapshot/resume/compute 三件套)
====================================================================
  macd(fast,slow,signal)  state = (ema_fast, ema_slow, dea)      O(1)
  atr(n)   [Wilder]       state = (当前 atr, 是否已暖机)           O(1)
  boll(n,mult)            state = 最近 n 根 close 窗口             O(n)
  kdj(n,ks,ds)            state = (K, D, 最近 n 根 (h,l,c) 窗口)   O(n)

⚠️ 易错点 (实施时踩过, 改这里必看):
  ① **不能先跑完递推再覆写 out[0]**: 后续元素已在覆写前用错的 out[0] 算过,
     残余误差按 (1-α)^i 衰减 → 表现成"窗口越长误差越小"的假象, 极具欺骗性。
     必须从递推起点就接 state。
  ② 朴素播种 (out[0]=v[0]) 与 state 播种 (out[0]=f(state,v[0])) **浮点上不等价**
     (差 1 ULP: `v*a + v*(1-a) != v`)。二者是不同口径, **不要互相比较数值**;
     同一口径下必须逐位稳定。
  ③ ATR 的 Wilder 递推在 `窗口未暖机` 时有退化分支 (取均值), 状态里必须带上
     "是否已暖机" 否则 resume 会走错分支。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# ★ EMA / MACD 的纯数学只有一份, 在基座叶子层 `app/utils/indicators.py`。
#   依赖方向必须是 auto → utils (基座不得反向依赖业务层), 故本模块**委托**而非自带副本。
#   2026-10-05: 消除 resume.py 与 indicators.py 的两份 ema_fwd/ema_naive 漂移温床。
#   实测三份实现 (resume / indicators / 旧 calc_macd) 互比 0.0 误差, 委托后行为逐位不变。
from app.utils.indicators import (          # noqa: E402
    ema_fwd as _ema_fwd_base,
    ema_naive as _ema_naive_base,
    macd_core as _macd_core_base,
    macd_state as _macd_state_base,
)

# ================================================================
# 1. 序列抽取 (统一入口: 各实现只认 bars, 不各自散写取值)
# ================================================================


def closes_of(bars: Sequence[Dict[str, Any]]) -> List[float]:
    return [float(b.get("close") or 0.0) for b in bars]


def hlc_of(bars: Sequence[Dict[str, Any]]):
    h = [float(b.get("high") or 0.0) for b in bars]
    l = [float(b.get("low") or 0.0) for b in bars]
    c = [float(b.get("close") or 0.0) for b in bars]
    return h, l, c


# ================================================================
# 2. EMA 原语 (两种播种, 语义不同 —— 见模块头易错点②)
# ================================================================


def ema_fwd(values: Sequence[float], period: int, e_prev: float) -> List[float]:
    """从**前一根的状态** e_prev 往后递推。values[0] 对应 e[a+1]。

    用途: 断点续传的 resume 侧。无瞬态 (前提是 e_prev 精确)。
    ★ 委托 `app.utils.indicators.ema_fwd` (单一实现)。
    """
    return _ema_fwd_base(list(values), period, e_prev)


def ema_naive(values: Sequence[float], period: int) -> List[float]:
    """朴素播种的 EMA 序列: out[0] = values[0], 之后递推。

    ⚠️ **必须逐字保持这个形态**, 不要写成 `ema_fwd(values, period, values[0])`
       —— 后者首元素是 `v*a + v*(1-a)`, 浮点上不严格等于 `v` (差 1 ULP)。
       历史口径 (`app/utils/indicators.calc_macd` / 各策略阈值) 是按本形态拟合的。
    ★ 委托 `app.utils.indicators.ema_naive` (单一实现, 见其文档字符串的告诫)。
    """
    return _ema_naive_base(list(values), period)


# ================================================================
# 3. 各指标的 snapshot / resume / compute
#    约定:
#      snapshot(bars, *params)          -> state
#      resume(state, bars_post, *params)-> 与 bars_post 等长的输出
#      compute(bars, *params)           -> 与 bars 等长的输出 (参考实现)
# ================================================================

# ---- MACD ----


def macd_snapshot(bars, fast=12, slow=26, signal=9):
    """断点状态 = (ema_fast[a], ema_slow[a], dea[a]), a = bars 末根。

    ★ 委托 `app.utils.indicators.macd_state` (单一实现)。
    """
    return _macd_state_base(closes_of(bars), fast, slow, signal)


def macd_resume(state, bars_post, fast=12, slow=26, signal=9):
    """从断点续算 MACD。返回 (dif, dea, hist), 各与 bars_post 等长。

    ★ 委托 `app.utils.indicators.macd_core(anchor=...)` (单一实现)。
    """
    c = closes_of(bars_post)
    if not c:
        return [], [], []
    return _macd_core_base(c, fast, slow, signal, anchor=tuple(state))


def macd_compute(bars, fast=12, slow=26, signal=9):
    """全量参考实现 (= 历史 calc_macd 口径, 朴素播种)。

    ★ 委托 `app.utils.indicators.macd_core` (单一实现)。
    """
    return _macd_core_base(closes_of(bars), fast, slow, signal)


# ---- ATR (Wilder) ----


def _tr_series(bars, prev_close: Optional[float] = None) -> List[float]:
    """真实波幅 TR。prev_close = bars[0] **之前**那一根的收盘 (断点续传用)。

    ⚠️ 易错点 (2026-10-05 实测抓到): 断点续传时 `bars` 是后缀, 若把它当"全新序列"
       则 bars[0] 会走 `tr = h - l` 分支 —— 但全局上它有前一根, 应是
       `max(h-l, |h-pc|, |l-pc|)`。两者不等 ⇒ ATR 不等价。
       **ATR 的状态必须含 prev_close** (跨断点的依赖), 否则状态不是完整有界摘要。
    """
    h, l, c = hlc_of(bars)
    n = len(c)
    tr = [0.0] * n
    for j in range(n):
        pc = (c[j - 1] if j > 0 else prev_close)
        if pc is None:
            tr[j] = h[j] - l[j]
        else:
            tr[j] = max(h[j] - l[j], abs(h[j] - pc), abs(l[j] - pc))
    return tr


def atr_compute(bars, n=14, prev_close: Optional[float] = None) -> List[float]:
    """全量 ATR (与 Ctx._atr 逐值一致: n 期后 Wilder 递推, 之前退化为均值)。"""
    tr = _tr_series(bars, prev_close)
    m = len(tr)
    atr = [0.0] * m
    if m >= n and n > 0:
        atr[n - 1] = sum(tr[:n]) / n
        for j in range(n, m):
            atr[j] = (atr[j - 1] * (n - 1) + tr[j]) / n
    elif m > 0:
        atr[m - 1] = sum(tr) / m
    return atr


def atr_snapshot(bars, n=14):
    """state = (末根 atr, 是否已暖机, 末根 close)。

    三元缺一不可:
      ① 末根 atr   —— Wilder 递推的当前值
      ② warm       —— 暖机前后是两套式子, 只存数值会让 resume 走错分支
      ③ 末根 close —— resume 首根的 TR 要用它当"前一根收盘", 缺了就不等价
                      (2026-10-05 实测抓到, 见 _tr_series 注释)
    """
    a = atr_compute(bars, n)
    m = len(a)
    if m == 0:
        return (0.0, False, 0.0)
    return (a[-1], bool(m >= n and n > 0), closes_of(bars)[-1])


def atr_resume(state, bars_post, n=14) -> List[float]:
    a_prev, warm, prev_close = state
    tr = _tr_series(bars_post, prev_close)
    m = len(tr)
    out = [0.0] * m
    if not warm:
        # 断点落在 ATR **未暖机区**时无法续算: compute 的暖机分支是
        # `atr[j] = mean(tr[0..j])`, 它依赖断点**之前**的全部 tr —— 不是有界状态,
        # 因此本层不支持。按「不写兜底/不猜」原则直接拒绝, 由调用方保证断点在暖机区
        # (预处理断点取长窗末根, n=14 时必然满足)。
        raise ValueError(
            f"atr_resume: 断点落在未暖机区 (warm=False), 无法续算; "
            f"请把断点移到 len(bars) >= n 之后 (n={n})")
    prev = float(a_prev)
    for j in range(m):
        prev = (prev * (n - 1) + tr[j]) / n
        out[j] = prev
    return out


# ---- BOLL (滑窗) ----


def boll_compute(bars, n=20, mult=2.0):
    """全量 BOLL (与 Ctx._boll 逐值一致: 总体标准差)。返回 (mid, up, low)。"""
    c = closes_of(bars)
    m = len(c)
    mid = [0.0] * m
    up = [0.0] * m
    low = [0.0] * m
    for j in range(m):
        lo = max(0, j - n + 1)
        vals = [v for v in c[lo: j + 1] if v > 0]
        if len(vals) >= 2:
            m_ = sum(vals) / len(vals)
            var = sum((v - m_) ** 2 for v in vals) / len(vals)
            sd = var ** 0.5
            mid[j], up[j], low[j] = m_, m_ + mult * sd, m_ - mult * sd
        elif vals:
            mid[j] = up[j] = low[j] = vals[0]
    return mid, up, low


def boll_snapshot(bars, n=20, mult=2.0):
    """state = 最近 n 根 close 窗口 (滑窗型 O(window))。"""
    return tuple(closes_of(bars)[-n:])


def boll_resume(state, bars_post, n=20, mult=2.0):
    """用窗口状态续算 BOLL。返回 (mid, up, low), 各与 bars_post 等长。

    ⚠️ 要求 `len(state) >= min(n, 前缀长度)` 完整 —— 状态是"最近 n 根 close",
       前缀不足 n 根时窗口不完整, 续算不等价。此处不猜, 由调用方保证。
    """
    c = closes_of(bars_post)
    win = list(state) + c
    m = len(c)
    mid, up, low = [0.0] * m, [0.0] * m, [0.0] * m
    for j in range(m):
        # 窗口 = win 中对应 j 的最近 n 根 (含前缀)
        hi = len(state) + j
        lo = max(0, hi - n + 1)
        vals = [v for v in win[lo: hi + 1] if v > 0]
        if len(vals) >= 2:
            m_ = sum(vals) / len(vals)
            var = sum((v - m_) ** 2 for v in vals) / len(vals)
            sd = var ** 0.5
            mid[j], up[j], low[j] = m_, m_ + mult * sd, m_ - mult * sd
        elif vals:
            mid[j] = up[j] = low[j] = vals[0]
    return mid, up, low


# ---- KDJ ----


def kdj_compute(bars, n=9, ks=3, ds=3):
    """全量 KDJ (与 Ctx._kdj 逐值一致)。返回 (K, D, J)。"""
    h, l, c = hlc_of(bars)
    m = len(c)
    K, D, J = [0.0] * m, [0.0] * m, [0.0] * m
    k_prev, d_prev = 50.0, 50.0
    for j in range(m):
        lo = max(0, j - n + 1)
        hi = max(h[lo: j + 1]) if j >= 0 else 0.0
        low = min(l[lo: j + 1]) if j >= 0 else 0.0
        rsv = ((c[j] - low) / (hi - low) * 100.0) if hi > low else 50.0
        k_prev = (2.0 / 3.0) * k_prev + (1.0 / 3.0) * rsv
        d_prev = (2.0 / 3.0) * d_prev + (1.0 / 3.0) * k_prev
        K[j], D[j], J[j] = k_prev, d_prev, 3.0 * k_prev - 2.0 * d_prev
    return K, D, J


def kdj_snapshot(bars, n=9, ks=3, ds=3):
    """state = (K, D, 最近 n 根 (h,l,c) 窗口)。"""
    K, D, _ = kdj_compute(bars, n, ks, ds)
    h, l, c = hlc_of(bars)
    win = tuple(zip(h[-n:], l[-n:], c[-n:]))
    return ((K[-1] if K else 50.0), (D[-1] if D else 50.0), win)


def kdj_resume(state, bars_post, n=9, ks=3, ds=3):
    k_prev, d_prev, win = state
    h, l, c = hlc_of(bars_post)
    wh = [t[0] for t in win] + list(h)
    wl = [t[1] for t in win] + list(l)
    wc = [t[2] for t in win] + list(c)
    off = len(win)
    m = len(c)
    K, D, J = [0.0] * m, [0.0] * m, [0.0] * m
    kp, dp = float(k_prev), float(d_prev)
    for j in range(m):
        hi = off + j
        lo = max(0, hi - n + 1)
        hh = max(wh[lo: hi + 1])
        ll = min(wl[lo: hi + 1])
        rsv = ((wc[hi] - ll) / (hh - ll) * 100.0) if hh > ll else 50.0
        kp = (2.0 / 3.0) * kp + (1.0 / 3.0) * rsv
        dp = (2.0 / 3.0) * dp + (1.0 / 3.0) * kp
        K[j], D[j], J[j] = kp, dp, 3.0 * kp - 2.0 * dp
    return K, D, J
