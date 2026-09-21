"""ide/functions.py — 金融函数库与 Ctx (as-of 安全上下文)。

Ctx 是门表求值器的**唯一数据入口**：门表达式只能通过 Ctx 暴露的函数读数据，且
**所有偏移函数强制 k<=0**，任何正偏移（读未来 bar）在调用时直接拒绝。这是
"as-of 可静态证明"的运行期兜底（expr.static_asof_check 是加载期那一道）。

约定：
- 价格类变化函数 (chg) 返回**百分比** (×100)，与策略文档/现有代码口径一致；
- 量比 (vol_ratio) 返回**原始比值**；
- 绝对价格函数 (open/high/low/close/volume) 返回绝对值；
- 偏移 k：相对决策日 i。k=0 = 决策日；k=-1 = 前一交易日；k<=0 才合法。

M1 实现 dragon_callback 需要的函数 + 常用基础 (ma/rsi/count_limit_up/is_limit_up)；
M2 补齐技术指标 (macd/kdj/boll/atr，见 Ctx 各方法)。扩展 = 在 Ctx 上加方法
（内核），或经 register_function 注册策略专属函数/指标（门表表达式按名直接调用，
不改动求值器）。所有指标按决策日因果切片 [0..i] 计算，as-of 安全。

**偏移约定**：所有带偏移的函数（`chg/open/.../ma/rsi`）一律把偏移量 `k` 作为
**第一个位置参数**（如 `ma(0,"close",5)`），使 expr.static_asof_check 只查 args[0]
即可静态发现未来函数，不会误伤窗口/字段等后续参数。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.market_cn.auto.core.market import MarketSpec, get_board_type, is_limit_up


class AsOfViolation(Exception):
    """调用方试图读取未来 bar (k>0)。"""


class Ctx:
    """门表求值的 as-of 安全上下文。

    bars      : 完整日线序列 (list[dict])；Ctx 永不读 > i 的索引。
    i         : 决策日索引 (反转日 / 候选日)。
    lu_idx    : 当前遍历的涨停日索引 (< i)。
    params    : 策略参数命名空间。
    board_type: 板块类型 (main / gem_star)，供 is_limit_up 等使用。
    market    : 本策略所属市场的 MarketSpec (架构 §9)。涨跌停 / 分板口径**只从这里取** ——
                core 不写死任何市场常量; None = 进程默认市场 (A)。
    code      : 股票代码 (静态元数据, 非时序 —— 供 is_bse 等交易所过滤使用, 无 as-of 风险)。
    stock_info: 静态股本元数据 (circ_shares / total_shares)，供换手率类门表使用 —— 非时序, 无 as-of 风险。
    ext       : 策略预计算上下文 (dict) —— 编排层一次性算好、跨候选日复用的派生量。
                例: g56 的 G1 特征序列 (f=_g1_arrays(bars), O(n) 一次) 与横截面 regime 池。
                只承载"由 bars[0..i] 派生、与决策日无关的缓存"，as-of 安全由编排层保证。
    latest    : 盘中快照行 (intraday_window 策略) —— 触发槽位的 {time,open,high,low,last,
                previousClose,volume}；None = 非盘中语境。非时序切片, 由引擎注入。
    series    : 盘中快照序列 (bar[0..pos-1] 的快照形行) —— knife/tail 的尾盘/VWAP 判定用；
                非时序切片, 由引擎注入。None/[] = 非盘中语境。
    mkt_gain  : 全市场均涨幅% (盘中门控) —— 引擎在同一槽位一次算好注入; None = 非盘中语境。
    """

    def __init__(self, bars: List[Dict[str, Any]], i: int, lu_idx: int,
                 params: Dict[str, Any], board_type: str = "main", code: str = "",
                 stock_info: Optional[Dict[str, Any]] = None,
                 ext: Optional[Dict[str, Any]] = None,
                 latest: Optional[Dict[str, Any]] = None,
                 series: Optional[List[Dict[str, Any]]] = None,
                 mkt_gain: Optional[float] = None,
                 market: Optional[MarketSpec] = None):
        self.bars = bars
        self.i = i
        self.lu_idx = lu_idx
        self.params = params
        self.board_type = board_type
        self.market = market
        self.code = code
        self.stock_info = stock_info
        self.ext = ext if ext is not None else {}
        self.latest = latest
        self.series = series if series is not None else []
        self.mkt_gain = mkt_gain
        self.n = len(bars)
        # 指标缓存（每 Ctx 自带；等价回测/展示不依赖这些指标，O(n) 单次可接收）。
        # 如需跨候选日共享，可改 id(bars) 受限缓存，但必须保证只依赖 bars 与参数（as-of 安全）。
        self._ind_cache: Dict[str, Dict[Any, Any]] = {"macd": {}, "kdj": {}, "boll": {}, "atr": {}}

    # ---- 内部：只暴露 ≤ i 的 bar；k>0 直接拒（未来函数） ----
    def _bar(self, k: int) -> Optional[Dict[str, Any]]:
        if k > 0:
            raise AsOfViolation(f"未来偏移 k={k} 被拒绝 (as-of 守护)")
        j = self.i + k
        if 0 <= j < self.n:
            return self.bars[j]
        return None

    def _check_k(self, k: int):
        """偏移守卫：k>0 = 读未来 bar，直接拒绝（自定义偏移函数的运行期兜底）。"""
        if k > 0:
            raise AsOfViolation(f"未来偏移 k={k} 被拒绝 (as-of 守护)")

    @staticmethod
    def _f(bar, key, default=0.0):
        if not bar:
            return default
        v = bar.get(key)
        try:
            return float(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    # ---- 价格变化 / 量比（偏移 ≤ 0） ----
    def chg(self, k: int = 0) -> float:
        """第 i+k 日相对前一日涨幅 (%)。k=0=决策日 vs 昨；k=-1=昨 vs 前日。"""
        cur, prev = self._bar(k), self._bar(k - 1)
        c, p = self._f(cur, "close"), self._f(prev, "close")
        return (c / p - 1) * 100 if p > 0 else 0.0

    def vol_ratio(self, k: int = 0) -> float:
        """第 i+k 日量 / 前一日量。"""
        cur, prev = self._bar(k), self._bar(k - 1)
        c, p = self._f(cur, "volume"), self._f(prev, "volume")
        return c / p if p > 0 else 0.0

    # ---- 绝对价格 / 量（偏移 ≤ 0） ----
    def open(self, k: int = 0) -> float:
        return self._f(self._bar(k), "open")

    def high(self, k: int = 0) -> float:
        return self._f(self._bar(k), "high")

    def low(self, k: int = 0) -> float:
        return self._f(self._bar(k), "low")

    def close(self, k: int = 0) -> float:
        return self._f(self._bar(k), "close")

    def volume(self, k: int = 0) -> float:
        return self._f(self._bar(k), "volume")

    # ---- 涨停 / 回调结构（lu_idx < i，天然无未来） ----
    def lu_close(self) -> float:
        return self._f(self.bars[self.lu_idx], "close") if 0 <= self.lu_idx < self.n else 0.0

    def pullback_days(self) -> int:
        """回调天数 = (决策日-1) - 涨停日索引。"""
        return (self.i - 1) - self.lu_idx

    def monotonic_down(self) -> bool:
        """涨停后 lu+1..i-1 连续 close < 涨停收盘（未反弹回到涨停价 = 真回调）。"""
        lu_c = self.lu_close()
        if lu_c <= 0:
            return False
        for j in range(self.lu_idx + 1, self.i):
            if self._f(self.bars[j], "close") >= lu_c:
                return False
        return True

    def is_limit_up(self, k: int = 0) -> bool:
        """第 i+k 日是否涨停。"""
        cur, prev = self._bar(k), self._bar(k - 1)
        c, p = self._f(cur, "close"), self._f(prev, "close")
        if p <= 0:
            return False
        return bool(is_limit_up(c, p, self.board_type, self.market))

    def count_limit_up(self, window: int = 60) -> int:
        """决策日往前 window 日（含）内涨停次数。"""
        if window < 1:
            return 0
        lo = max(1, self.i - window + 1)
        cnt = 0
        for j in range(lo, self.i + 1):
            c, p = self._f(self.bars[j], "close"), self._f(self.bars[j - 1], "close")
            if p > 0 and is_limit_up(c, p, self.board_type, self.market):
                cnt += 1
        return cnt

    # ---- 技术指标（M1 基础集：ma / rsi；macd/kdj/boll/atr 见 TODO） ----
    # 约定：所有偏移函数的 k 必须是**第一个位置参数**（与 chg/open 等一致），
    # 以便 expr.static_asof_check 静态校验（只查 args[0]），不会误伤窗口/字段参数。
    def ma(self, k: int = 0, field: str = "close", n: int = 5) -> float:
        """第 i+k 日往前 n 日 (含) 的 field 简单均值。k 为第一个位置参数。"""
        end = self.i + k
        if end < 0:
            return 0.0
        start = end - n + 1
        vals = [self._f(self.bars[j], field) for j in range(max(0, start), end + 1)]
        vals = [v for v in vals if v > 0]
        return sum(vals) / len(vals) if vals else 0.0

    def rsi(self, k: int = 0, n: int = 14) -> float:
        """第 i+k 日 Wilder RSI(%)。k 为第一个位置参数。"""
        end = self.i + k
        if end < n:
            return 0.0
        gains, losses = [], []
        for j in range(end - n + 1, end + 1):
            c, p = self._f(self.bars[j], "close"), self._f(self.bars[j - 1], "close")
            if p <= 0:
                continue
            d = c - p
            gains.append(max(d, 0.0))
            losses.append(max(-d, 0.0))
        if not gains:
            return 0.0
        ag, al = sum(gains) / len(gains), sum(losses) / len(losses)
        if al == 0:
            return 100.0
        rs = ag / al
        return 100.0 - 100.0 / (1.0 + rs)


    # ----------------------------------------------------------------
    # 技术指标（M2 补齐：MACD / KDJ / BOLL / ATR）
    # as-of 安全：所有序列都从 bars[0] 因果递推，访问只取 ≤ i+k 的索引；
    # 缓存 self._ind_cache 每 Ctx 自带（见 __init__）。
    # 约定：所有偏移函数的 k 必须是第一个位置参数（见上方 ma/rsi）。
    # ----------------------------------------------------------------
    @staticmethod
    def _ema(values: List[float], period: int) -> List[float]:
        """指数平滑（从 index 0 递推，因果安全）。未足 period 也给出递推值。"""
        n = len(values)
        out = [0.0] * n
        if n == 0:
            return out
        a = 2.0 / (period + 1)
        out[0] = values[0]
        for j in range(1, n):
            out[j] = a * values[j] + (1.0 - a) * out[j - 1]
        return out

    # ---- MACD（DIF / DEA / MACD柱，柱=2*(DIF-DEA) 的 A股惯例）----
    # 因果切片 [0..i]：EMA 从 index 0 递推，dif[i] 只依赖 closes[0..i]，与 common/indicators
    # 的 calc_macd(closes[:i+1]) 逐值一致；as-of 安全（绝不读 > i 的 bar）。
    def _macd(self, fast: int, slow: int, signal: int):
        cache = self._ind_cache["macd"]
        key = (fast, slow, signal)
        if key not in cache:
            m = self.i + 1
            closes = [self._f(self.bars[j], "close") for j in range(m)]
            ema_f = self._ema(closes, fast)
            ema_s = self._ema(closes, slow)
            dif = [ema_f[j] - ema_s[j] for j in range(m)]
            dea = self._ema(dif, signal)
            hist = [2.0 * (dif[j] - dea[j]) for j in range(m)]
            cache[key] = (dif, dea, hist)
        return cache[key]

    def macd_dif(self, k: int = 0, fast: int = 12, slow: int = 26, signal: int = 9) -> float:
        dif, _, _ = self._macd(fast, slow, signal)
        j = self.i + k
        return dif[j] if 0 <= j < self.n else 0.0

    def macd_dea(self, k: int = 0, fast: int = 12, slow: int = 26, signal: int = 9) -> float:
        _, dea, _ = self._macd(fast, slow, signal)
        j = self.i + k
        return dea[j] if 0 <= j < self.n else 0.0

    def macd_hist(self, k: int = 0, fast: int = 12, slow: int = 26, signal: int = 9) -> float:
        _, _, hist = self._macd(fast, slow, signal)
        j = self.i + k
        return hist[j] if 0 <= j < self.n else 0.0

    # ---- KDJ（RSV n 日；K/D 用 1/3 权重平滑）---- 因果切片 [0..i]
    def _kdj(self, n: int, ks: int, ds: int):
        cache = self._ind_cache["kdj"]
        key = (n, ks, ds)
        if key not in cache:
            m = self.i + 1
            K = [0.0] * m
            D = [0.0] * m
            J = [0.0] * m
            k_prev, d_prev = 50.0, 50.0
            for j in range(m):
                lo = max(0, j - n + 1)
                hi = max(self._f(self.bars[t], "high") for t in range(lo, j + 1))
                low = min(self._f(self.bars[t], "low") for t in range(lo, j + 1))
                c = self._f(self.bars[j], "close")
                rsv = ((c - low) / (hi - low) * 100.0) if hi > low else 50.0
                k_prev = (2.0 / 3.0) * k_prev + (1.0 / 3.0) * rsv
                d_prev = (2.0 / 3.0) * d_prev + (1.0 / 3.0) * k_prev
                K[j], D[j], J[j] = k_prev, d_prev, 3.0 * k_prev - 2.0 * d_prev
            cache[key] = (K, D, J)
        return cache[key]

    def kdj_k(self, k: int = 0, n: int = 9, ks: int = 3, ds: int = 3) -> float:
        K, _, _ = self._kdj(n, ks, ds)
        j = self.i + k
        return K[j] if 0 <= j < self.n else 0.0

    def kdj_d(self, k: int = 0, n: int = 9, ks: int = 3, ds: int = 3) -> float:
        _, D, _ = self._kdj(n, ks, ds)
        j = self.i + k
        return D[j] if 0 <= j < self.n else 0.0

    def kdj_j(self, k: int = 0, n: int = 9, ks: int = 3, ds: int = 3) -> float:
        _, _, J = self._kdj(n, ks, ds)
        j = self.i + k
        return J[j] if 0 <= j < self.n else 0.0

    # ---- BOLL（中轨 MA / 上下轨 ±mult·σ，总体标准差）---- 因果切片 [0..i]
    def _boll(self, n: int, mult: float):
        cache = self._ind_cache["boll"]
        key = (n, mult)
        if key not in cache:
            m = self.i + 1
            mid = [0.0] * m
            up = [0.0] * m
            low = [0.0] * m
            for j in range(m):
                lo = max(0, j - n + 1)
                vals = [v for v in (self._f(self.bars[t], "close")
                                    for t in range(lo, j + 1)) if v > 0]
                if len(vals) >= 2:
                    m_ = sum(vals) / len(vals)
                    var = sum((v - m_) ** 2 for v in vals) / len(vals)
                    sd = var ** 0.5
                    mid[j], up[j], low[j] = m_, m_ + mult * sd, m_ - mult * sd
                elif vals:
                    mid[j] = up[j] = low[j] = vals[0]
            cache[key] = (mid, up, low)
        return cache[key]

    def boll_mid(self, k: int = 0, n: int = 20, mult: float = 2.0) -> float:
        mid, _, _ = self._boll(n, mult)
        j = self.i + k
        return mid[j] if 0 <= j < self.n else 0.0

    def boll_upper(self, k: int = 0, n: int = 20, mult: float = 2.0) -> float:
        _, up, _ = self._boll(n, mult)
        j = self.i + k
        return up[j] if 0 <= j < self.n else 0.0

    def boll_lower(self, k: int = 0, n: int = 20, mult: float = 2.0) -> float:
        _, _, low = self._boll(n, mult)
        j = self.i + k
        return low[j] if 0 <= j < self.n else 0.0

    # ---- ATR（Wilder 平滑真实波幅）---- 因果切片 [0..i]
    def _atr(self, n: int):
        cache = self._ind_cache["atr"]
        key = n
        if key not in cache:
            m = self.i + 1
            tr = [0.0] * m
            for j in range(m):
                h = self._f(self.bars[j], "high")
                l = self._f(self.bars[j], "low")
                if j == 0:
                    tr[j] = h - l
                else:
                    pc = self._f(self.bars[j - 1], "close")
                    tr[j] = max(h - l, abs(h - pc), abs(l - pc))
            atr = [0.0] * m
            if m >= n and n > 0:
                atr[n - 1] = sum(tr[:n]) / n
                for j in range(n, m):
                    atr[j] = (atr[j - 1] * (n - 1) + tr[j]) / n
            elif m > 0:
                atr[m - 1] = sum(tr) / m
            cache[key] = atr
        return cache[key]

    def atr(self, k: int = 0, n: int = 14) -> float:
        a = self._atr(n)
        j = self.i + k
        return a[j] if (n - 1) <= j < self.n else 0.0


# ================================================================
# 决策日依赖表 (Frame A: 函数是否读**决策日 i**(偏移 0) 的行情数据)
# ----------------------------------------------------------------
# 用途: 展示预计算管线 (ide/present.py) 要在 T-1 夜把"不需要买入日 T 数据的门"
# 提前算完 (架构 §5)。判定依据必须是**可静态证明**的, 不能手填 —— 手填会漂移。
#   偏移函数  → 看 args[0] 是否 ≥ 0 (见 present.reads_decision_bar)
#   非偏移函数 → 查本表 D0_DEPS / REGISTERED_D0
# 保守原则: 查不到 → 视为 1 (需要决策日数据)。宁可在盘中多算一门, 不可让夜
# 预计算用"占位 D0 bar"误算而误杀候选 (那是静默丢信号)。
# ================================================================
D0_DEPS: Dict[str, int] = {
    "abs": 0,               # 纯数学内建
    "lu_close": 0,          # bars[lu_idx], lu_idx < i
    "pullback_days": 0,     # (i-1) - lu_idx, 纯结构量
    "monotonic_down": 0,    # bars[lu_idx+1 .. i-1], 不含决策日
    "count_limit_up": 1,    # range(.., i+1) 含决策日
}


# 偏移函数名集合（供 expr.static_asof_check 识别"未来函数"）
OFFSET_FUNCS = {
    "chg", "vol_ratio", "open", "high", "low", "close", "volume",
    "is_limit_up", "ma", "rsi",
    "macd_dif", "macd_dea", "macd_hist",
    "kdj_k", "kdj_d", "kdj_j",
    "boll_mid", "boll_upper", "boll_lower",
    "atr",
}


# ================================================================
# 自定义函数注册入口（M2 泛化：策略可挂专属函数/指标，摆脱硬编码 build_funcs）
# ================================================================
# 注册的函数签名约定：
#   - 非偏移函数：def fn(ctx, *args) —— 内部用 ctx.bars / ctx.i / ctx.board_type 读数据，
#     必须只依赖 ≤ i 的数据（as-of 安全）；由引擎注入 ctx。
#   - 偏移函数：def fn(ctx, k, *args) —— k 为第一个位置参数（相对决策日 i 的偏移），
#     必须由调用方（或 build_funcs 包裹层）保证 k<=0（未来偏移直接拒）。
# 注册即生效，无需改求值器；门表表达式直接按注册名调用。
REGISTERED_FUNCS: Dict[str, Any] = {}
REGISTERED_OFFSET: set = set()

# 注册函数的决策日依赖 (Frame A): 0=只读 ≤ i-1 / 1=读决策日 i。
# 不声明 → present.reads_decision_bar 保守视为 1 (不会误算)。仅 0 需要显式声明。
REGISTERED_D0: Dict[str, int] = {}


def register_function(name: str, fn, is_offset: bool = False,
                      needs_d0: Optional[int] = None) -> None:
    """注册一个策略自定义函数（或指标取值函数）。

    needs_d0: 该函数是否读决策日 i 的行情数据 (见 D0_DEPS 说明)。
      0 = 只依赖 ≤ i-1 的数据 (可被展示端 T-1 夜预计算)
      1 / 不传 = 读决策日数据 (保守默认)
    """
    REGISTERED_FUNCS[name] = fn
    if is_offset:
        REGISTERED_OFFSET.add(name)
    if needs_d0 is not None:
        REGISTERED_D0[name] = int(needs_d0)


def declared_d0_dep(name: str) -> Optional[int]:
    """内核/注册函数声明的决策日依赖；未声明 → None (调用方按 1 处理)。"""
    if name in REGISTERED_D0:
        return REGISTERED_D0[name]
    return D0_DEPS.get(name)


def offset_funcs() -> set:
    """当前全部偏移函数名（内置 + 已注册），供静态 as-of 校验。"""
    return OFFSET_FUNCS | REGISTERED_OFFSET


def build_funcs(ctx: Ctx, names=None) -> Dict[str, Any]:
    """把 Ctx 方法 + 已注册函数 绑定为求值器可用的函数字典。

    names: 可选函数名集合 (StrategySpec.func_names) —— 只绑定**用得到**的注册函数。
      求值热点 (全市场 × 每票每 lu, 十万级) 每次构造 ~50 项字典是显著开销, 而单条表达式
      通常只用 2~4 个函数。names 必须是"策略全部表达式引用名"的**超集**; 传 None =
      绑全量 (调用方不确定时的安全回退, 语义与优化前一致)。Ctx 方法 (24 项) 恒绑定。
    """
    funcs = {
        "chg": ctx.chg,
        "vol_ratio": ctx.vol_ratio,
        "open": ctx.open,
        "high": ctx.high,
        "low": ctx.low,
        "close": ctx.close,
        "volume": ctx.volume,
        "lu_close": ctx.lu_close,
        "pullback_days": ctx.pullback_days,
        "monotonic_down": ctx.monotonic_down,
        "is_limit_up": ctx.is_limit_up,
        "count_limit_up": ctx.count_limit_up,
        "ma": ctx.ma,
        "rsi": ctx.rsi,
        "macd_dif": ctx.macd_dif,
        "macd_dea": ctx.macd_dea,
        "macd_hist": ctx.macd_hist,
        "kdj_k": ctx.kdj_k,
        "kdj_d": ctx.kdj_d,
        "kdj_j": ctx.kdj_j,
        "boll_mid": ctx.boll_mid,
        "boll_upper": ctx.boll_upper,
        "boll_lower": ctx.boll_lower,
        "atr": ctx.atr,
        "abs": abs,             # 纯数学内建 (无 ctx, 非偏移) —— 供门表写 |x| <= t 类条件
    }
    for name, fn in REGISTERED_FUNCS.items():
        if names is not None and name not in names:
            continue
        if name in REGISTERED_OFFSET:
            # 偏移函数：k 为第一位置参数，运行期拒绝 k>0
            funcs[name] = lambda k, *a, ctx=ctx, fn=fn: (ctx._check_k(k) or fn(ctx, k, *a))
        else:
            funcs[name] = lambda *a, ctx=ctx, fn=fn: fn(ctx, *a)
    return funcs
