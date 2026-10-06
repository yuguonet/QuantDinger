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
from app.market_cn.auto.core.runtime import resume as RES   # 断点续传单一内核


class AsOfViolation(Exception):
    """调用方试图读取未来 bar (k>0)。"""


# ================================================================
# 指标缓存的**跨 Ctx 共享** (B2, 2026-10-04)
# ================================================================
# 背景: _macd / _kdj / _boll / _atr 都是"从 bars[0] 因果递推到 i"的纯函数,
#   **只依赖 (bars[0..i], 各自的窗口参数)**, 不读 params / stock_info / ext。
#   已逐个核对四个 _xxx 实现, 缓存键分别为 (fast,slow,signal) / (n,ks,ds) /
#   (n,mult) / (n), 除此之外再无输入 —— 所以跨 Ctx 共享是**数学恒等**, 不是近似。
#
# 旧实现每 Ctx 一份 _ind_cache ⇒ 展示层夜算 (已退役的 `precompute_night`) 对同一 (bars, i) 的每个候选
#   lu 各建一个 Ctx, 4 个指标被重复算 N 次 (N = 历史涨停日数, dragon_callback
#   的 enumeration=limit_up 可达几十)。这是那时展示管线夜算的主要重复计算源。
#   (展示管线已退役; 本注释保留是因为它解释了为何缓存键必须跨 Ctx 共享)
#
# ⚠️ 键**必须含 i** (本改动最容易翻车的地方):
#   这 4 个函数都按 `m = self.i + 1` 截断后缓存数组; 只按 id(bars) 共享会让不同 i
#   的 Ctx 拿到被截短的数组 → 列表索引越界或取到错误值。
#
# ⚠️ 条目持有 bars 引用 (不是裸 id): 防止 bars 被 GC 后 id() 被新对象复用而命中
#   别人的缓存。命中前再核对 `is`, 不符就重建。
#
# 体积上限 FIFO 淘汰: 共享收益集中在"同一 (bars,i) 连续构造多个 Ctx"那段,
#   小容量就够, 避免全市场扫描时堆几千份 O(n) 数组。
# ================================================================
_IND_CACHE: Dict[Any, Any] = {}          # (id(bars), i) -> (bars, {"macd":{},...})
_IND_CACHE_CAP = 256


def shared_ind_cache(bars: List[Any], i: int, resume_key: Any = None) -> Dict[str, Dict[Any, Any]]:
    """取 (bars, i) 的共享指标缓存; 无则新建。

    调用方**不得**修改键空间 (键 = 各指标函数自己的窗口参数元组)。
    Returns:
        dict: {"macd": {}, "kdj": {}, "boll": {}, "atr": {}} 形态的缓存容器。
    """
    key = (id(bars), int(i), resume_key)
    hit = _IND_CACHE.get(key)
    if hit is not None and hit[0] is bars:
        return hit[1]
    fresh: Dict[str, Dict[Any, Any]] = {"macd": {}, "kdj": {}, "boll": {}, "atr": {}}
    if len(_IND_CACHE) >= _IND_CACHE_CAP:
        for stale in list(_IND_CACHE)[: _IND_CACHE_CAP // 4]:   # dict 保序 = FIFO
            _IND_CACHE.pop(stale, None)
    _IND_CACHE[key] = (bars, fresh)
    return fresh



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
                 market: Optional[MarketSpec] = None,
                 resume: Optional[Dict[tuple, Any]] = None,
                 i_age: Optional[int] = None):
        self.bars = bars
        self.i = i
        #: **逻辑数据年龄** (2026-10-05 新增, 可选): 该票自数据起点累计的日线根数。
        #: ★ 与 `i` 的区别: `i` 是**本次传入 bars 内**的下标; 增量/播种路径下 bars
        #:   只是窗口 (可能仅 21~35 根), 而票的真实历史有几百根 —— 暖机类门
        #:   (如 g56 的 `warmup() >= 68`) 判的是"这票有多少历史", 必须用 `i_age`。
        #: ⚠ 缺省 None = 视作 `i_age == i` (旧语义, 逐位一致); 只有**播种路径**才该传。
        self.i_age = i_age
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
        #: 断点续传状态 (core/runtime/resume.py 的 codec 产出 (由预处理注入, 当前默认关闭)):
        #:   key = ("macd", fast, slow, signal) / ("kdj", n, ks, ds) / ...
        #:   语义 = "bars[0] **之前**那一根结束时"的有界摘要; 缺省 {} = 全量重算。
        #: 由**预处理**产出、实时消费, 见 resume.py 模块头的等价性契约。
        self.resume = resume or {}
        self.n = len(bars)
        # 指标缓存（每 Ctx 自带；等价回测/展示不依赖这些指标，O(n) 单次可接收）。
        # 如需跨候选日共享，可改 id(bars) 受限缓存，但必须保证只依赖 bars 与参数（as-of 安全）。
        # B2 (2026-10-04): 跨 Ctx 共享, 键含 i —— 见模块级 shared_ind_cache 的注意事项
        # B2: 跨 Ctx 共享, 键含 i 与 resume 指纹 (见 shared_ind_cache 注释)
        self._ind_cache: Dict[str, Dict[Any, Any]] = shared_ind_cache(
            bars, self.i, id(self.resume) if self.resume else None)

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
        self._check_k(k)   # A7 运行期 as-of 守卫 (k>0 = 读未来, 直接拒)
        end = self.i + k
        if end < 0:
            return 0.0
        start = end - n + 1
        vals = [self._f(self.bars[j], field) for j in range(max(0, start), end + 1)]
        vals = [v for v in vals if v > 0]
        return sum(vals) / len(vals) if vals else 0.0

    def rsi(self, k: int = 0, n: int = 14) -> float:
        """第 i+k 日 Cutler (简单均值) RSI(%)。k 为第一个位置参数。

        2026-09-29 审计修正: 此处原标 "Wilder RSI" 是错的 —— 实现是 Cutler
        (涨跌幅直接算术平均), 与 app/utils/indicators.rsi (Wilder 平滑) 口径不同
        (实测差 ~1pt), 两套口径并存属已知限度 (门表 rsi(k,n) vs 特征 rsi6())。
        """
        self._check_k(k)   # A7 运行期 as-of 守卫 (k>0 = 读未来, 直接拒)
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
    # 缓存 self._ind_cache **按 (bars, i) 跨 Ctx 共享**（见模块级 shared_ind_cache）。
    # 约定：所有偏移函数的 k 必须是第一个位置参数（见上方 ma/rsi）。
    # ----------------------------------------------------------------
    # (原 Ctx._ema 已于 2026-10-05 删除: MACD 改走 core.runtime.resume 单一内核,
    #  本地那份 EMA 变成死代码 —— 死代码是"看起来有实现、实际零作用"的隐患, 不留。)

    # ---- MACD（DIF / DEA / MACD柱，柱=2*(DIF-DEA) 的 A股惯例）----
    # 因果切片 [0..i]：EMA 从 index 0 递推，dif[i] 只依赖 closes[0..i]，与
    # app.utils.indicators 的 calc_macd(closes[:i+1]) 逐值一致；as-of 安全（绝不读 > i 的 bar）。
    def _macd(self, fast: int, slow: int, signal: int):
        """MACD —— 委托 core.runtime.resume (单一内核, 见 resume.py 模块头)。

        `self.resume` 非空时用断点状态续算 (逐位 == 全量), 否则全量重算。
        两种路径的口径同源, 不再有"两套 MACD 靠注释约定一致"的漂移风险。
        """
        cache = self._ind_cache["macd"]
        key = (fast, slow, signal)
        if key not in cache:
            m = self.i + 1
            bars_i = self.bars[:m]
            st = self.resume.get(("macd", fast, slow, signal))
            cache[key] = (RES.macd_resume(st, bars_i, fast, slow, signal)
                          if st is not None else
                          RES.macd_compute(bars_i, fast, slow, signal))
        return cache[key]

    def macd_dif(self, k: int = 0, fast: int = 12, slow: int = 26, signal: int = 9) -> float:
        self._check_k(k)   # A7 运行期 as-of 守卫 (k>0 = 读未来, 直接拒)
        dif, _, _ = self._macd(fast, slow, signal)
        j = self.i + k
        return dif[j] if 0 <= j < self.n else 0.0

    def macd_dea(self, k: int = 0, fast: int = 12, slow: int = 26, signal: int = 9) -> float:
        self._check_k(k)   # A7 运行期 as-of 守卫 (k>0 = 读未来, 直接拒)
        _, dea, _ = self._macd(fast, slow, signal)
        j = self.i + k
        return dea[j] if 0 <= j < self.n else 0.0

    def macd_hist(self, k: int = 0, fast: int = 12, slow: int = 26, signal: int = 9) -> float:
        self._check_k(k)   # A7 运行期 as-of 守卫 (k>0 = 读未来, 直接拒)
        _, _, hist = self._macd(fast, slow, signal)
        j = self.i + k
        return hist[j] if 0 <= j < self.n else 0.0

    # ---- KDJ（RSV n 日；K/D 用 1/3 权重平滑）---- 因果切片 [0..i]
    def _kdj(self, n: int, ks: int, ds: int):
        """KDJ —— 委托 core.runtime.resume (state = (K, D, 最近 n 根 (h,l,c)))。"""
        cache = self._ind_cache["kdj"]
        key = (n, ks, ds)
        if key not in cache:
            bars_i = self.bars[: self.i + 1]
            st = self.resume.get(("kdj", n, ks, ds))
            cache[key] = (RES.kdj_resume(st, bars_i, n, ks, ds) if st is not None
                          else RES.kdj_compute(bars_i, n, ks, ds))
        return cache[key]

    def kdj_k(self, k: int = 0, n: int = 9, ks: int = 3, ds: int = 3) -> float:
        self._check_k(k)   # A7 运行期 as-of 守卫 (k>0 = 读未来, 直接拒)
        K, _, _ = self._kdj(n, ks, ds)
        j = self.i + k
        return K[j] if 0 <= j < self.n else 0.0

    def kdj_d(self, k: int = 0, n: int = 9, ks: int = 3, ds: int = 3) -> float:
        self._check_k(k)   # A7 运行期 as-of 守卫 (k>0 = 读未来, 直接拒)
        _, D, _ = self._kdj(n, ks, ds)
        j = self.i + k
        return D[j] if 0 <= j < self.n else 0.0

    def kdj_j(self, k: int = 0, n: int = 9, ks: int = 3, ds: int = 3) -> float:
        self._check_k(k)   # A7 运行期 as-of 守卫 (k>0 = 读未来, 直接拒)
        _, _, J = self._kdj(n, ks, ds)
        j = self.i + k
        return J[j] if 0 <= j < self.n else 0.0

    # ---- BOLL（中轨 MA / 上下轨 ±mult·σ，总体标准差）---- 因果切片 [0..i]
    def _boll(self, n: int, mult: float):
        """BOLL —— 委托 core.runtime.resume (state = 最近 n 根 close 窗口)。"""
        cache = self._ind_cache["boll"]
        key = (n, mult)
        if key not in cache:
            bars_i = self.bars[: self.i + 1]
            st = self.resume.get(("boll", n, mult))
            cache[key] = (RES.boll_resume(st, bars_i, n, mult) if st is not None
                          else RES.boll_compute(bars_i, n, mult))
        return cache[key]

    def boll_mid(self, k: int = 0, n: int = 20, mult: float = 2.0) -> float:
        self._check_k(k)   # A7 运行期 as-of 守卫 (k>0 = 读未来, 直接拒)
        mid, _, _ = self._boll(n, mult)
        j = self.i + k
        return mid[j] if 0 <= j < self.n else 0.0

    def boll_upper(self, k: int = 0, n: int = 20, mult: float = 2.0) -> float:
        self._check_k(k)   # A7 运行期 as-of 守卫 (k>0 = 读未来, 直接拒)
        _, up, _ = self._boll(n, mult)
        j = self.i + k
        return up[j] if 0 <= j < self.n else 0.0

    def boll_lower(self, k: int = 0, n: int = 20, mult: float = 2.0) -> float:
        self._check_k(k)   # A7 运行期 as-of 守卫 (k>0 = 读未来, 直接拒)
        _, _, low = self._boll(n, mult)
        j = self.i + k
        return low[j] if 0 <= j < self.n else 0.0

    # ---- ATR（Wilder 平滑真实波幅）---- 因果切片 [0..i]
    def _atr(self, n: int):
        """ATR —— 委托 core.runtime.resume (state = (末根 atr, warm, 末根 close))。

        ⚠️ 断点状态必须含 `末根 close`: 后缀首根的 TR 要拿它当"前一根收盘",
           缺了就退化成 `h-l` ⇒ 不等价 (resume.py:_tr_series 注释有实测记录)。
        """
        cache = self._ind_cache["atr"]
        key = n
        if key not in cache:
            bars_i = self.bars[: self.i + 1]
            st = self.resume.get(("atr", n))
            cache[key] = (RES.atr_resume(st, bars_i, n) if st is not None
                          else RES.atr_compute(bars_i, n))
        return cache[key]

    def atr(self, k: int = 0, n: int = 14) -> float:
        self._check_k(k)   # A7 运行期 as-of 守卫 (k>0 = 读未来, 直接拒)
        a = self._atr(n)
        j = self.i + k
        return a[j] if (n - 1) <= j < self.n else 0.0


def _closes_upto(ctx: Ctx) -> List[float]:
    """决策日 i 及之前全部收盘价（as-of 安全切片 [0..i]）。

    供 gate_stdlib / 策略私有门函数取"完整已定日线"计算技术指标（MACD/BOLL/MA）。
    只依赖 bars[0..i]，绝不读未来。原定义在 strategy_funcs.py，重构后迁入门表 DSL
    标准库入口（functions），消除"堆料文件"依赖。
    """
    return [Ctx._f(ctx.bars[j], "close") for j in range(ctx.i + 1)]


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


def declared_d0_dep(name: str, key: str = None) -> Optional[int]:
    """内核/注册函数声明的决策日依赖；未声明 → None (调用方按 1 处理)。

    若传入 key，优先查策略命名空间 STRATEGY_GATE_D0[key][name]（见 declared_d0_for_strategy）；
    这是重构后 feat('x') 等跨策略同名函数能各自声明 D0 依赖的关键。
    """
    if key is not None and key in STRATEGY_GATE_D0 and name in STRATEGY_GATE_D0[key]:
        return STRATEGY_GATE_D0[key][name]
    if name in REGISTERED_D0:
        return REGISTERED_D0[name]
    return D0_DEPS.get(name)


def offset_funcs() -> set:
    """当前全部偏移函数名（内置 + 已注册），供静态 as-of 校验。"""
    return OFFSET_FUNCS | REGISTERED_OFFSET


# ================================================================
# 策略作用域门函数注册表（架构重构：策略私有函数 key 命名空间隔离）
# ----------------------------------------------------------------
# 重构前：所有策略私有门函数堆在 strategy_funcs.py，经全局 REGISTERED_FUNCS 注册，
# 同名函数（feat/warmup/finite/pool_stat/...）跨策略互相污染。重构后：
#   - 策略私有函数只挂在 STRATEGY_GATE_FUNCS[key]，build_funcs 先解策略私有、再回退 stdlib；
#   - 同名策略私有函数互不可见（key 隔离），避免 g56/break/knife/tail 的 feat 互相覆盖；
#   - 真正跨策略共享的函数留在 gate_stdlib（REGISTERED_FUNCS），由本表兜底。
# ================================================================
STRATEGY_GATE_FUNCS: Dict[str, Dict[str, Any]] = {}
STRATEGY_GATE_OFFSET: Dict[str, set] = {}
STRATEGY_GATE_D0: Dict[str, Dict[str, int]] = {}


def register_strategy_funcs(key: str, names_fns: Dict[str, Any],
                            offset: set = None, d0: Dict[str, int] = None) -> None:
    """注册某策略的私有门函数（key 命名空间隔离）。

    names_fns: {name: fn}，fn 签名 fn(ctx, ...)（非偏移）或 fn(ctx, k, ...)（偏移，k 第一位置参数）。
    offset:    该策略内哪些 name 是偏移函数（k 为第一位置参数）。
    d0:        {name: 0|1} 决策日依赖声明（0=只依赖 ≤i-1，1/缺省=读决策日 i）。
    策略私有函数优先于 stdlib 被 build_funcs 解析（key 隔离，互不可见）。
    """
    offset = offset or set()
    d0 = d0 or {}
    STRATEGY_GATE_FUNCS.setdefault(key, {}).update(names_fns)
    STRATEGY_GATE_OFFSET.setdefault(key, set()).update(offset)
    STRATEGY_GATE_D0.setdefault(key, {}).update(d0)


def declared_d0_for_strategy(name: str, key: str) -> Optional[int]:
    """策略命名空间下函数的决策日依赖；未声明 → None（调用方按 1 处理）。

    优先查策略私有声明，其次 stdlib 声明，最后内核 D0_DEPS。
    """
    if key in STRATEGY_GATE_D0 and name in STRATEGY_GATE_D0[key]:
        return STRATEGY_GATE_D0[key][name]
    if name in REGISTERED_D0:
        return REGISTERED_D0[name]
    return D0_DEPS.get(name)


def ensure_gate_init() -> None:
    """惰性触发策略模块发现 + stdlib 导入（YAML/展示路径不 import .py 模块）。

    门表 YAML 解析路径（present.pipeline）不 import strategies 的 .py 模块，故解析前必须
    确保各策略私有函数已注册。扫描/回测路径由 autodiscover 在开头调用，但 present 路径需
    自行触发。幂等（autodiscover 内部幂等 + 模块导入幂等）。
    """
    import app.market_cn.auto.strategies as strategies_pkg  # 触发 autodiscover（注册策略私有函数）
    strategies_pkg.autodiscover()
    import app.market_cn.auto.core.runtime.gate_stdlib  # noqa: F401（注册 stdlib 共享函数）


def _bind(ctx: Ctx, name: str, fn, is_offset: bool):
    """把函数绑定到具体 ctx（offset 函数运行期拒绝 k>0）。"""
    if is_offset:
        return lambda k, *a, ctx=ctx, fn=fn: (ctx._check_k(k) or fn(ctx, k, *a))
    return lambda *a, ctx=ctx, fn=fn: fn(ctx, *a)


def build_funcs(ctx: Ctx, key: str, names=None) -> Dict[str, Any]:
    """把 Ctx 方法 + 策略私有函数 + stdlib 共享函数 绑定为求值器可用的函数字典。

    key:   策略 key（STRATEGY_GATE_FUNCS 命名空间）—— 解析顺序：策略私有 → stdlib 兜底。
    names: 可选函数名集合 (StrategySpec.func_names) —— 只绑定**用得到**的函数（性能热点）。
           传 None = 绑全量（安全回退，语义与优化前一致）。Ctx 方法 (24 项) 恒绑定。

    解析顺序（关键）：同名函数，策略私有优先；只有策略未定义的名才回退 stdlib。这样
    g56/break/knife/tail 各自的 feat/warmup/finite 互不可见，彻底消除全局 REGISTERED_FUNCS
    的"同名互相覆盖"隐患。去 fail-open：查不到的策略私有名，若非 stdlib 也不可用，
    求值器会在表达式求值时抛 NameError 而非静默通过/失败。
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
    sfuncs = STRATEGY_GATE_FUNCS.get(key, {})
    soffset = STRATEGY_GATE_OFFSET.get(key, set())
    for name, fn in sfuncs.items():
        if names is not None and name not in names:
            continue
        funcs[name] = _bind(ctx, name, fn, name in soffset)
    for name, fn in REGISTERED_FUNCS.items():
        if name in funcs:        # 策略私有已覆盖，跳过（key 隔离）
            continue
        if names is not None and name not in names:
            continue
        funcs[name] = _bind(ctx, name, fn, name in REGISTERED_OFFSET)
    return funcs
