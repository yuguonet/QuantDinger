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
滑窗型靠"窗口内值即状态"。改任何实现前必须保持它 —— 见 `verify_resume.py`。

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
声明式检查点 (2026-10-05 用户提议) —— **展示层对策略/指标完全无感**
====================================================================
分工:
  策略侧  声明 `resume_points` (要哪些断点记忆点: 形式/格式/位置) +
          提供 codec (snapshot/resume) 或复用标准注册表
  展示层  只做「收集声明 → 预处理产出不透明 blob → 实时分发」,
          **不 import 任何指标、不认识 macd/atr/g56 是什么**

于是新增策略/新增指标时, 展示层**一行不改** —— 这是"策略不同方法相同"的最终形态。
展示层唯一的通用职责是**断点对齐**与**失效判定**, 见下。

====================================================================
失效判定: 前缀数据指纹 (复权是其中一种触发原因)
====================================================================
`ResumeBook` 每条状态都带 `input_fp = bars_fingerprint(前缀)`。
取用时与当前前缀指纹比对, 不符即**失效 → 调用方退回全量重算**。

⚠️ 为什么不做"遇复权就重算"这条特例规则:
   复权 (qfq) 会**改写历史 bar** ⇒ 前缀 close 序列变 ⇒ 指纹自然变。
   而数据订正、窗口变动、换数据源同样会让指纹变。
   **用"前缀变了就重算"这一条通则覆盖全部情形**, 展示层连"什么是复权"都不必知道。
   (特例规则的坏处: 每新增一种数据变更来源就要补一条分支, 迟早漏。)

====================================================================
实现清单 (Ctx 的 4 个长记忆指标 = 标准注册表; 策略可自带私有 codec)
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


# ================================================================
# 4. 注册表 + ResumeBook
# ================================================================

#: 默认检查点清单 (kind, params) —— Ctx 的 4 个长记忆指标。
#: 新增指标时改这里; 两处 build 都从本常量取, 不再各写一份。
DEFAULT_SPECS: Tuple[Tuple[str, tuple], ...] = (
    ("macd", (12, 26, 9)),
    ("atr", (14,)),
    ("boll", (20, 2.0)),
    ("kdj", (9, 3, 3)),
)


# ================================================================
# 3b. 声明式断点记忆点 (策略侧声明, 展示层不解释)
# ================================================================


@dataclass(frozen=True)
class ResumePoint:
    """断点记忆点声明 —— 由**策略**给出, 展示层只搬不解释。

    字段:
      kind     记忆点种类名 (标准注册表键, 或策略私有命名空间)
      params   参数元组; 参数不同状态不可复用, 故进键
      at       断点**位置**语义。当前唯一取值 "window_start"
               = 「展示窗口首根之前那一根结束时」(见模块头对齐约定)
      note     形式/格式/用途说明 —— 给读代码的人看, 展示层**不解析**
      snapshot/resume  私有 codec (callable)。为 None 时回落标准注册表 RESUMABLE。
               私有 codec 签名: snapshot(bars, *params) -> state
                                resume(state, bars_post, *params) -> 序列

    ⚠️ `kind` 建议带策略前缀 (如 "g56/g1") 命名私有记忆点, 避免跨策略撞名。
    """
    kind: str
    params: tuple = ()
    at: str = "window_start"
    note: str = ""
    snapshot: Optional[Callable] = None
    resume: Optional[Callable] = None

    @property
    def key(self) -> Tuple:
        """状态键 —— 与 `state_key()` 同形, 保证与 Ctx 的查找键一致。"""
        return (self.kind,) + tuple(self.params)


#: 策略侧声明挂载点 (strategies/base.StrategyBase.resume_points) 的元素类型。
#: 展示层经 `core.present.resume_io.collect_points()` 收集, **不认识其语义**。


def bars_fingerprint(bars) -> str:
    """前缀数据指纹 (blake2b-8B)。

    覆盖 (date, close) 全序列: 复权改写历史 close ⇒ 指纹变 ⇒ 检查点失效;
    数据订正 / 窗口变动 / 换数据源同理。**一条通则覆盖全部情形**。
    用 close 而非整根 bar: 各指标只读 close/high/low, 其中 close 是复权直接作用量,
    且 (date, close) 已足以区分任何一次历史改写 (改 high/low 不改 close 不影响
    复权语义; 若未来需要更严, 换成整根 hash 即可, 接口不变)。
    """
    import hashlib as _h
    import struct as _st
    h = _h.blake2b(digest_size=8)
    for b in bars:
        h.update(str(b.get("time") or "")[:10].encode("ascii", "replace"))
        h.update(_st.pack("<d", float(b.get("close") or 0.0)))
    return h.hexdigest()


RESUMABLE: Dict[str, Tuple[Callable, Callable, Callable]] = {
    "macd": (macd_snapshot, macd_resume, macd_compute),
    "atr": (atr_snapshot, atr_resume, atr_compute),
    "boll": (boll_snapshot, boll_resume, boll_compute),
    "kdj": (kdj_snapshot, kdj_resume, kdj_compute),
}


def _default_points() -> Tuple["ResumePoint", ...]:
    """Ctx 的 4 个标准指标作为默认声明 (未声明 resume_points 的策略用)。"""
    return tuple(ResumePoint(k, p) for k, p in DEFAULT_SPECS)


def _snap_point(p: "ResumePoint", bars):
    """按声明取 snapshot: 私有 codec 优先, 否则标准注册表。"""
    if p.snapshot is not None:
        return p.snapshot(bars, *p.params)
    return snapshot(p.kind, bars, *p.params)


def resume_point(p: "ResumePoint", state, bars_post):
    """按声明取 resume: 私有 codec 优先, 否则标准注册表。"""
    if p.resume is not None:
        return p.resume(state, bars_post, *p.params)
    return resume(p.kind, state, bars_post, *p.params)


def state_key(kind: str, *params) -> Tuple:
    """检查点键。kind + 参数元组 —— 参数不同状态不可复用。"""
    return (kind,) + tuple(params)


def snapshot(kind: str, bars, *params):
    fn = RESUMABLE[kind][0]
    return fn(bars, *params)


def resume(kind: str, state, bars_post, *params):
    fn = RESUMABLE[kind][1]
    return fn(state, bars_post, *params)


def compute(kind: str, bars, *params):
    fn = RESUMABLE[kind][2]
    return fn(bars, *params)


class ResumeBook:
    """每票一份检查点 (断点续传的"断点位置状态记忆")。

    由**预处理**产出、**实时**消费:
      预处理: book = ResumeBook.build(code, long_bars)   # 断点 = long_bars 末根
      实时  : book.get(code, "macd", fast, slow, signal) → state
              → resume(state, bars_after, ...) 只算增量

    ⚠️ 对齐约定: state 的语义是"传给 resume 的 bars_post[0] **之前**那一根结束时"的状态。
       所以预处理取 `long_bars` 的状态、实时传的 `bars_post` 必须**紧接** long_bars 末根
       (中间不能空、不能重叠)。中间空了 ⇒ 值错; 重叠了 ⇒ 重复计入。
    """

    def __init__(self, break_date: str = ""):
        self.break_date = break_date
        self._st: Dict[Tuple[str, str, Tuple], Any] = {}
        #: 每票的**前缀数据指纹** (复权/数据修订检测, 见模块头"失效判定")
        self.fp: Dict[str, str] = {}
        #: 指纹失效次数 (可观测性: 频繁失效说明预处理断点不稳)
        self.stale_hits = 0

    def put(self, code: str, kind: str, *params, state: Any, input_fp: str = "") -> None:
        self._st[(code, kind, tuple(params))] = state
        if input_fp:
            self.fp[code] = input_fp

    def get(self, code: str, kind: str, *params, default: Any = None) -> Any:
        return self._st.get((code, kind, tuple(params)), default)

    def is_stale(self, code: str, bars_prefix) -> bool:
        """前缀指纹是否已变 (复权 / 数据订正 / 窗口变动)。变了 = 检查点不可用。

        Returns:
            bool: True = 失效, 调用方应退回全量重算。
        """
        want = self.fp.get(code)
        if not want:
            return False                      # 未记指纹 → 不判定 (调用方自担)
        if want == bars_fingerprint(bars_prefix):
            return False
        self.stale_hits += 1
        return True

    def mark_stale(self, code: str) -> None:
        """显式判废 (预处理发现复权/数据修订时调用), 该票全部记忆点作废。"""
        for k in [k for k in self._st if k[0] == code]:
            self._st.pop(k, None)
        self.fp.pop(code, None)
        self.stale_hits += 1

    def __len__(self) -> int:
        return len(self._st)

    def as_ctx_dict(self, code: str) -> Dict[tuple, Any]:
        """导出成 **Ctx.resume 期望的形状**: `{(kind, *params): state}`。

        ⚠️ 这是唯一的形状适配点, **不要**自己拼 dict ——
           `get()` 返回的是裸 state, 直接 `{kind: state}` 组装会得到
           `{str: state}` 而 Ctx 查的是 `{tuple: state}` ⇒ 全部取到 None,
           **静默退回短窗全量重算** (值错但不报错)。
           2026-10-05 实测踩过一次, 表现为"三个指标同时不等价"的假象。
        """
        pre = f"{code}\x00"
        out: Dict[tuple, Any] = {}
        for (c, kind, params), st in self._st.items():
            if c == code:
                out[(kind,) + tuple(params)] = st
        return out

    # ---- 构造: 从长窗口一次算出全部检查点 ----
    @classmethod
    def build(cls, code: str, bars, break_date: str = "",
              points: Sequence["ResumePoint"] = ()) -> "ResumeBook":
        """为单票建检查点。

        points: `ResumePoint` 声明序列 (策略侧给)。缺省建 Ctx 的 4 个标准指标。
        """
        book = cls(break_date)
        fp = bars_fingerprint(bars)
        for p in (points or _default_points()):
            book.put(code, p.kind, *p.params,
                     state=_snap_point(p, bars), input_fp=fp)
        return book

    @classmethod
    def build_many(cls, bars_by_code: Dict[str, list], break_date: str = "",
                   points: Sequence["ResumePoint"] = ()) -> "ResumeBook":
        """全市场批量建检查点 (预处理用)。"""
        book = cls(break_date)
        pts = tuple(points or _default_points())
        for code, bars in bars_by_code.items():
            fp = bars_fingerprint(bars)
            for p in pts:
                book.put(code, p.kind, *p.params,
                         state=_snap_point(p, bars), input_fp=fp)
        return book
