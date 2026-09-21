"""ide/present.py — 展示预计算管线 (M3: T-1 夜资格门 + D0 盘中增量)。

目标 (架构 §5): "今晚就能看到明日" + 盘中每分钟增量刷新。
三级时序:

    T-1 夜    night 门 (不需买入日 T 数据)  → 明日候选集
    D0 盘中   day   门 (需买入日 T 数据)    → 候选集内每分钟重判
    14:56     终审 → 落库 + 推送

====================================================================
参照系 (关键语义, 易误解点)
====================================================================
`needs_d0` 的定义统一为 **"该门是否需要「买入日 T」当日的行情数据"** (Frame B,
与架构 §5「今晚就能看到明日」一致)。

决策日 i 相对买入日 T 的偏移由 `entry.mode` 决定:
    entry.mode == "open"            → i = T-1   (次日开盘买) → **门表全部可夜算**
    close / intraday / auction      → i = T     (当日成交)   → 只有不读 i 的门可夜算

所以管线不是"逐门手工分组", 而是:
    night 门 = 决策日偏移 ≤ -1 时取全部门; 偏移 = 0 时取 Frame-A 证明"不读决策日"的门
Frame A (门是否读决策日 i 的数据) 由 `reads_decision_bar` **静态推导** (见下) —— 这是
可证明的, 不依赖手填 `needs_d0` (手填会漂移; 对账见 `audit_needs_d0`)。

====================================================================
为什么分段求值等价 (可证)
====================================================================
1. **Frame-A 可靠**: Ctx 的 as-of 保证任何门只读 ≤ i; `reads_decision_bar` 对"不能
   证明不读 i"一律判 True (保守), 因此被划到 night 的门必然只依赖 ≤ i-1 的数据。
   对偏移 = 0 的策略, night 侧喂 `bars[:i] + 占位 bar` → `bar[i+k] (k≤-1)` 与全量
   完全一致, 而占位 bar 永不被读 → 分段与全量逐值相同。
2. **偏移 ≤ -1 时 night 即全量**: i = T-1 = 历史末根, 无占位、无需分段。
3. **limit_up 枚举必须保留全部夜门通过的 lu_idx**: 全量口径是"升序遍历 lu_idx,
   首个**全部门**通过者出信号"。若夜侧只取首个通过者, 日门可能把它淘汰而后续
   lu_idx 本可通过 → 漏信号。故本模块夜侧保留**全部**通过者, 日侧再取首个 ——
   二者合成 = 首个 night∧day 全过者, 与全量一致 (见 `verify_split` 实测)。

====================================================================
性能前提 (架构 §5.3, 30s 目标)
====================================================================
① 共享 bars 缓存: `BarsCache` 一次加载, 全策略复用 (否则 N 策略 = N 倍 IO);
② 门求值复用同一 Ctx (指标序列记忆化);
③ 盘中只算候选集内的 day 门。
三条缺一条, 30 秒目标即破。
"""

from __future__ import annotations

import ast
import functools
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.market_cn.auto.core.market import find_limit_ups, get_board_type
from app.market_cn.auto.core.data.hub import synth_bar
from app.market_cn.auto.core.runtime.evaluate import Gate, StrategySpec, build_signal, evaluate_gates
from app.market_cn.auto.core.runtime.expr import parse as parse_expr
from app.market_cn.auto.core.runtime.functions import (
    Ctx, declared_d0_dep, offset_funcs,
)

# 手动独立运行 (诊断/基准) 时加载 .env —— 应用内运行由 app 初始化加载, 幂等无害。
# 路径锚点走 core/_paths (不再数 __file__ 层级: 目录一挪就静默读不到 .env)。
from app.market_cn.auto.core._paths import STRATEGY_DIR, load_env_first_found

load_env_first_found(os.path.join(os.getcwd(), ".env"))


# ================================================================
# 1. 时序推导: 决策日 i 相对 买入日 T 的偏移
# ================================================================
def decision_offset(spec: StrategySpec) -> int:
    """决策日 i 相对买入日 T 的偏移 (0 = 当日成交, -1 = 次日开盘成交)。

    口径来源 = `entry.mode` (架构 §4.1 入场模式模块):
      open      → 次日开盘成交 → 决策日在 T-1
      close     → 当日收盘成交 (如 dragon_callback 14:56)
      intraday  → 触发槽位现价成交 (如 knife/tail)
      auction   → 集合竞价成交 (当日) —— 保守按当日处理
    """
    mode = str((spec.entry or {}).get("mode", "open")).lower()
    return -1 if mode == "open" else 0


# ================================================================
# 2. Frame-A 推导: 门表达式是否读"决策日 i"的行情数据
# ================================================================
def _const_num(node: ast.AST) -> Optional[float]:
    """常量折叠 (支持 -1 / +1 这类一元正负号)。非字面量返回 None。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
            and not isinstance(node.value, bool):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = _const_num(node.operand)
        if v is None:
            return None
        return -v if isinstance(node.op, ast.USub) else v
    return None


@functools.lru_cache(maxsize=4096)
def reads_decision_bar(expr: str) -> bool:
    """门表达式是否**读取决策日 i**(偏移 0)的行情数据。

    判据:
      - 内置/注册**偏移函数**: `args[0]` 必须可常量折叠为 < 0 才安全; 无参调用
        (默认偏移 0) 或偏移值 ≥ 0 或偏移来自参数 → 判 True。
      - **非偏移函数**: 查 functions.D0_DEPS / REGISTERED_D0; 未声明 (None) → 判 True。
      - 字面量/参数名/纯运算: 不读行情数据。

    **保守原则**: 不能证明"不读"就返回 True。返回 False 的门才会被 T-1 夜用占位
    D0 bar 求值 —— 一旦判错就是静默丢信号, 故宁可多算一门到盘中。
    """
    t = parse_expr(expr)
    offs = offset_funcs()
    for n in ast.walk(t):
        if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Name):
            continue
        fname = n.func.id
        if fname in offs:
            if not n.args:
                return True                     # 无参偏移函数默认 k=0 → 读决策日
            k = _const_num(n.args[0])
            if k is None or k >= 0:
                return True                     # 偏移来自参数 / 读决策日
            continue
        dep = declared_d0_dep(fname)
        if dep != 0:
            return True                         # 未声明 / 声明为 1 → 保守判读决策日
    return False


# ================================================================
# 3. 门分组
# ================================================================
@dataclass
class GatePlan:
    """单策略的门分组结果 (夜可算 / 需盘中)。"""
    key: str
    decision_offset: int
    night: List[Gate] = field(default_factory=list)
    day: List[Gate] = field(default_factory=list)

    @property
    def needs_intraday(self) -> bool:
        return bool(self.day)

    def describe(self) -> str:
        frame = "i=T-1(次日开盘)" if self.decision_offset <= -1 else "i=T(当日成交)"
        return (f"{self.key:16s} {frame:16s} 夜{len(self.night)}门"
                f" 盘中{len(self.day)}门"
                + (f" [{','.join(g.id for g in self.day)}]" if self.day else ""))


@functools.lru_cache(maxsize=256)
def plan_gates_cached(key: str, offset: int, gates_sig: tuple) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """缓存友好版门分组 (只依赖 (key, offset, 门签名)) —— 纯函数, 可跨调用复用。"""
    night, day = [], []
    for gid, expr in gates_sig:
        (night if not reads_decision_bar(expr) else day).append(gid)
    return tuple(night), tuple(day)


def plan_gates(spec: StrategySpec) -> GatePlan:
    """把启用门划为 night (T-1 夜算完) / day (D0 盘中算)。

    偏移 ≤ -1 → 全部门都是 night (决策日就是 T-1, 数据已全)。
    偏移 = 0  → 按 Frame-A 推导划分。
    """
    off = decision_offset(spec)
    plan = GatePlan(key=spec.key, decision_offset=off)
    if off <= -1:
        plan.night = list(spec.enabled_gates)
        return plan
    sig = tuple((g.id, g.expr) for g in spec.enabled_gates)
    night_ids, day_ids = plan_gates_cached(spec.key, off, sig)
    by_id = {g.id: g for g in spec.enabled_gates}
    plan.night = [by_id[i] for i in night_ids]
    plan.day = [by_id[i] for i in day_ids]
    return plan


# ================================================================
# 4. needs_d0 对账 (声明 vs 推导)
# ================================================================
def audit_needs_d0(spec: StrategySpec) -> List[Dict[str, Any]]:
    """对账 YAML 声明的 `needs_d0` 与静态推导值, 返回不一致项。

    risk 方向:
      'over'   = 声明 1 / 推导 0 → 过度保守 (只是慢, 无害)
      'under'  = 声明 0 / 推导 1 → **危险**: 若管线信声明, 夜预计算会用占位 D0
                 bar 误算该门而静默丢信号 (本模块用推导值, 故实际不发作)
    """
    off = decision_offset(spec)
    out: List[Dict[str, Any]] = []
    for g in spec.enabled_gates:
        derived = 1 if (off == 0 and reads_decision_bar(g.expr)) else 0
        if derived != g.needs_d0:
            out.append({
                "key": spec.key, "gate": g.id, "name": g.name,
                "declared": g.needs_d0, "derived": derived,
                "risk": "under" if g.needs_d0 == 0 else "over",
                "expr": g.expr,
            })
    return out


# ================================================================
# 5. 编排层 ext (Ctx.ext) 提供者
# ----------------------------------------------------------------
# 有些策略的门依赖"编排层一次性算好的派生量"(Ctx.ext) —— 个股特征数组 (O(n) 一次)、
# 横截面池统计 (全市场聚合)。展示管线必须与回测/实盘同源构造它, 否则门表在展示侧
# 直接抛错 (如 g56 的 g56_feat 缺 ctx.ext['g56_feats'])。
# 声明方式: 策略 YAML 的 `meta.ext: <name>`; 实现注册在下面 (名字 → 提供者)。
# 新策略加一条 register_ext, 不必改管线 —— 跨策略共用同一 build_ext 入口。
# ================================================================
EXT_PROVIDERS: Dict[str, Any] = {}
EXT_MIN_N: Dict[str, int] = {}


def register_ext(name: str, min_n: int = 1):
    """注册编排层 ext 提供者: fn(spec, code, bars, asof_date, cache) -> dict。

    min_n: 该 ext 要求的最短日线根数 (如 g56 的 G1 特征需 len>=35, 因 calc_macd
      短序列返回 None)。**声明在提供者处而非调用点** —— 管线统一用
      `required_min_len(spec)` 施加, 新策略/新调用方零改动即受保护。
    cache: 共享 BarsCache (可空) —— 供提供者复用长窗口缓存派生短窗口量 (如 g56 池),
      避免二次全市场加载; 提供者必须容忍 cache=None (退化为自取数)。
    """
    def _deco(fn):
        EXT_PROVIDERS[name] = fn
        EXT_MIN_N[name] = int(min_n)
        return fn
    return _deco


# g56 横截面池日线的**单槽缓存** (键=交易日)。池统计按交易日是常量, 一次运行只服务一个
# target; 单槽避免为每个历史日各留一份全市场日线 (内存)。窗口固定 200 根, 必须与 g56
# 内部 hub.daily 口径一致。
_G56_POOL_DAYS = 200
_G56_POOL_BARS: Dict[str, Any] = {"date": None, "bars": None}


def _g56_pool_batch(pool_target: Optional[str],
                    cache: Optional["BarsCache"] = None
                    ) -> Optional[Dict[str, List[Dict[str, Any]]]]:
    """g56 池日线 (键=code) —— 窗口 200 根 + as_of=pool_target。

    必须与 `g56._ensure_pool_daily` 内部 `hub.daily(code, 200, as_of=pool_target)` 完全
    同口径 (同窗口/同复权/同截断), 否则横截面统计漂移 → 门判定不再等价 (逐笔等价前提)。

    取数优先级:
      ① 从**共享 BarsCache 切片** (days=300 缓存按 window_start(200) 切) —— 因为
         `fetch_kline_db(code,200)` 的定义就是 300 窗口的下界切片, 二者逐行一致,
         却省掉池的第二次全市场加载 (展示管线夜间的主要超支源);
      ② 缓存不覆盖 (未预热/口径不足) → 独立批量加载一次。
    """
    if not pool_target:
        return None
    if _G56_POOL_BARS["date"] == pool_target:
        return _G56_POOL_BARS["bars"]
    from app.market_cn.auto.core.data.kline import (
        all_codes as _all_codes, fetch_klines_batch, window_start,
    )
    codes = [c for c in _all_codes() if not c.startswith(("8", "4", "92"))]
    bars = None
    if cache is not None and (not cache.asof or cache.asof >= pool_target):
        cache.warm(codes)                     # 保证全市场在共享缓存内 (池口径完整)
        lo = window_start(_G56_POOL_DAYS)
        bars = {}
        for c in codes:
            bs = cache.get(c)
            if not bs:
                continue
            sl = [b for b in bs if lo <= b["time"] <= pool_target]
            if sl:
                bars[c] = sl
    if bars is None:
        bars = fetch_klines_batch(codes, days=_G56_POOL_DAYS, as_of=pool_target)
    _G56_POOL_BARS["date"], _G56_POOL_BARS["bars"] = pool_target, bars
    return bars


@register_ext("g56", min_n=35)
def _ext_g56(spec: StrategySpec, code: str, bars: List[Dict[str, Any]],
             asof_date: Optional[str],
             cache: Optional["BarsCache"] = None) -> Dict[str, Any]:
    """g56: 每股 G1 特征数组 + 当日横截面池 (逐字镜像 _run_backtest_day_g56 的 ext)。

    硬要求 len(bars) >= 35 (g56._g1_arrays 依赖 calc_macd, 短序列返 None) —— 已由
    `required_min_len("g56")` 在管线侧保证; 语义上的暖机要求 (>=68) 由 g1_warmup 门
    用 NaN 哨兵自然过滤。
    """
    from app.market_cn.auto.strategies.g56 import _ensure_pool_daily, _g1_arrays
    d = asof_date or (str(bars[-1]["time"])[:10] if bars else None)
    return {"g56_feats": _g1_arrays(bars),
            "g56_pool": _ensure_pool_daily(d, bars_batch=_g56_pool_batch(d, cache))}


def required_min_len(spec: StrategySpec) -> int:
    """该策略求值所需的最短日线根数 (来自其 `meta.ext` 提供者的声明)。"""
    name = (spec.meta or {}).get("ext")
    return EXT_MIN_N.get(name, 1) if name else 1


def build_ext(spec: StrategySpec, code: str, bars: List[Dict[str, Any]],
              cache: Optional["BarsCache"] = None,
              asof_date: Optional[str] = None) -> Dict[str, Any]:
    """按 `meta.ext` 构造该 (策略, 股票) 的 Ctx.ext; 未声明 → {}。

    asof 缺省取 **bars 末根日期** —— 这正是决策日 (偏移 ≤ -1 时 = T-1; 偏移 0 时
    = 占位/合成 bar 的 T), 与回测/实盘同源。调用方**不要**传买入日: 对 g56 这类
    "D-1 判定" 策略会错取横截面池。
    缓存键含日期, 防止夜侧(不含 D0)与盘中(含合成 D0)互相污染。
    """
    name = (spec.meta or {}).get("ext")
    if not name:
        return {}
    fn = EXT_PROVIDERS.get(name)
    if fn is None:
        raise KeyError(f"{spec.key}: meta.ext={name!r} 未注册 (见 present.register_ext)")
    d = asof_date or (str(bars[-1]["time"])[:10] if bars else "")
    if cache is not None:
        # 同 (code, ext, 日期) 只算一次 —— 多策略/多轮复用同一份特征数组 (§5.3 前提②)
        return cache.ext(code, f"{name}@{d}", lambda: fn(spec, code, bars, d, cache))
    return fn(spec, code, bars, d, cache)


# ================================================================
# 6. 共享 bars 缓存 (性能前提①)
# ================================================================
class BarsCache:
    """共享日线缓存: 一次加载, 全策略复用 (§5.3 达标前提①)。

    关键: 展示面是"全市场 × N 策略"口径。若每策略各取一遍 bars = N 倍 IO, 这是
    30s 目标的第一杀手。本类把 IO 收敛到 1 次, 并统计 load/hit 供基准核对。

    asof: 只保留该交易日(含)以前 —— 夜跑传 T-1, 回测/复算传目标日。
    """

    def __init__(self, days: int = 300, asof: Optional[str] = None,
                 min_len: int = 30, loader=None):
        self.days = days
        self.asof = str(asof)[:10] if asof else None
        self.min_len = min_len
        self._loader = loader
        self._bars: Dict[str, List[Dict[str, Any]]] = {}
        self._ext: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.loads = 0
        self.hits = 0

    def ext(self, code: str, name: str, builder) -> Dict[str, Any]:
        """按 (code, ext名) 记忆化派生量 —— 多策略共用一次 O(n) 特征计算。"""
        k = (code, name)
        v = self._ext.get(k)
        if v is None:
            v = self._ext[k] = builder()
        return v

    def _load(self, code: str) -> List[Dict[str, Any]]:
        if self._loader is not None:
            bars = self._loader(code, self.days)
        else:
            from app.market_cn.auto.core.data.kline import fetch_kline_db
            bars = fetch_kline_db(code, self.days)
        if not bars:
            return []
        if self.asof:
            bars = [b for b in bars if str(b["time"])[:10] <= self.asof]
        return bars

    def warm(self, codes) -> int:
        """批量预加载: 默认走 `fetch_klines_batch` 一次 SQL 取全窗口。

        关键: **逐票串行 DB 往返** 是全市场加载的第一杀手 (5234 票 × 1 往返 ≈ 34s) ——
        批量加载把 N 次往返收敛为 1 次, 且行内容与逐票 `fetch_kline_db` 完全一致
        (同窗口/同 qfq/同 as-of)。传入自定义 `loader` 时退回逐票 (保持可注入语义)。
        返回本次新加载且可用的股票数。
        """
        todo = [c for c in codes if c not in self._bars]
        if not todo:
            return 0
        if self._loader is not None:
            n = 0
            for c in todo:
                self.loads += 1
                self._bars[c] = self._load(c)
                if len(self._bars[c]) >= self.min_len:
                    n += 1
            return n
        from app.market_cn.auto.core.data.kline import fetch_klines_batch
        batch = fetch_klines_batch(todo, days=self.days, as_of=self.asof)
        n = 0
        for c in todo:
            bars = batch.get(c) or []
            self.loads += 1
            self._bars[c] = bars
            if len(bars) >= self.min_len:
                n += 1
        return n

    def get(self, code: str, virtual_bar: Optional[Dict[str, Any]] = None
            ) -> List[Dict[str, Any]]:
        """取日线 (as-of 已切片)。virtual_bar 非空 → 追加为末根 (虚拟/合成 D0)。"""
        bars = self._bars.get(code)
        if bars is None:
            self.loads += 1
            bars = self._bars.setdefault(code, self._load(code))
        self.hits += 1
        if virtual_bar:
            return bars + [virtual_bar]
        return bars

    def stats(self) -> Dict[str, Any]:
        return {"codes": len(self._bars), "loads": self.loads, "hits": self.hits}


def placeholder_bar(date: str) -> Dict[str, Any]:
    """夜间"虚拟 D0"占位 bar: 让 i 落在买入日 T 上, 使偏移 ≤ -1 对齐真实历史。

    close/volume = 0 —— 任何读决策日的门在它上面都会拿到 0 而失败, 所以只允许
    被 Frame-A 证明"不读决策日"的门在夜间用它求值 (`plan_gates` 保证)。
    """
    return {"time": str(date)[:10], "open": 0.0, "high": 0.0, "low": 0.0,
            "close": 0.0, "volume": 0.0}


# ================================================================
# 7. T-1 夜: 候选集
# ================================================================
@dataclass
class NightHit:
    """(策略, 股票, lu_idx) 夜门通过记录。"""
    code: str
    lu_idx: int = 0
    board_type: str = "main"


@dataclass
class StageStats:
    codes: int = 0          # 参与股数
    evaluated: int = 0      # 求值次数 (code × 候选 lu)
    passed: int = 0         # 通过项数
    elapsed: float = 0.0

    def add(self, other: "StageStats") -> None:
        self.codes += other.codes
        self.evaluated += other.evaluated
        self.passed += other.passed
        self.elapsed += other.elapsed


def _candidate_lus(spec: StrategySpec, bars: List[Dict[str, Any]],
                   board_type: str, i: int) -> List[int]:
    """决策日的 lu_idx 候选 (镜像回测的当日枚举)。

    limit_up (dragon_callback): i 之前的每个历史涨停日都是候选 (升序)。
    day / intraday: 单候选 lu_idx=0 (门表不依赖 lu_idx)。
    """
    if str(spec.meta.get("enumeration", "limit_up")).lower() == "limit_up":
        return [j for j in find_limit_ups(bars, board_type, spec.market_spec) if j < i]
    return [0]


def _night_bars(cache: BarsCache, code: str, off: int, buy_date: str
                ) -> Optional[List[Dict[str, Any]]]:
    """夜侧日线: 偏移 ≤ -1 用历史本身 (i=末根); 偏移 0 追加虚拟 D0 占位 bar。"""
    bars = cache.get(code)
    if len(bars) < 2:
        return None
    if off <= -1:
        return bars
    return bars + [placeholder_bar(buy_date)]


def precompute_night(specs: Dict[str, StrategySpec], codes, cache: BarsCache,
                     stock_info: Optional[Dict[str, Any]] = None,
                     buy_date: Optional[str] = None,
                     progress_every: int = 0) -> Dict[str, Any]:
    """T-1 夜预计算: 对每个策略求 night 门 → 明日候选集。

    返回 {"hits": {key: [NightHit]}, "plans": {key: GatePlan}, "stats": {key: StageStats}}
    """
    si = stock_info or {}
    plans = {k: plan_gates(s) for k, s in specs.items()}
    need_buy_date = any(p.decision_offset == 0 and p.night for p in plans.values())
    if need_buy_date and not buy_date:
        raise ValueError("存在 偏移=0 的策略有夜门 → 必须传 buy_date 以构造虚拟 D0 bar")

    hits: Dict[str, List[NightHit]] = {k: [] for k in specs}
    stats: Dict[str, StageStats] = {}
    codes = list(codes)

    for key, spec in specs.items():
        plan = plans[key]
        st = StageStats()
        t_key = time.time()
        min_n = max(cache.min_len, required_min_len(spec))
        for code in codes:
            bars = _night_bars(cache, code, plan.decision_offset, buy_date or "")
            if not bars or len(bars) < min_n:
                continue
            st.codes += 1
            if not plan.night:
                continue
            bt = get_board_type(code, spec.market_spec)
            i = len(bars) - 1
            info = si.get(code)
            ext = build_ext(spec, code, bars, cache=cache)
            for lu in _candidate_lus(spec, bars, bt, i):
                st.evaluated += 1
                ctx = Ctx(bars, i, lu, spec.params, board_type=bt, code=code,
                          stock_info=info, ext=ext, market=spec.market_spec)
                ok, _failed = evaluate_gates(spec, plan.night, ctx)
                if ok:
                    st.passed += 1
                    hits[key].append(NightHit(code=code, lu_idx=lu, board_type=bt))
        stats[key] = st
        st.elapsed = round(time.time() - t_key, 2)
        if progress_every and (list(specs).index(key) + 1) % progress_every == 0:
            print(f"  [night] {key} 通过 {st.passed} 用时 {st.elapsed:.1f}s", flush=True)

    return {"hits": hits, "plans": plans, "stats": stats}


# ================================================================
# 8. D0 盘中: 候选集内 day 门 (每分钟增量)
# ================================================================
@dataclass
class DayHit:
    """盘中命中: 该 (策略, 股票) 的 day 门全过 → 表单行数据。"""
    code: str
    lu_idx: int = 0
    fields: Dict[str, Any] = field(default_factory=dict)
    # P5: 该行所用**当日快照序列**的完整度 (展示层标注, 不阻断决策)。
    # 取值 "ok" / "warning" / "error" / "suspend"; 缺省 "ok" = 数据正常。
    # 语义见 core/features/quality.py: 只如实标注, **不承担回溯补判责任**
    # (监视层=展示层, 无回溯性 —— 09-21 用户裁定)。
    data_quality: str = "ok"
    data_quality_reasons: List[str] = field(default_factory=list)


def intraday_cycle(specs: Dict[str, StrategySpec], night: Dict[str, Any],
                   cache: BarsCache, snapshots: Dict[str, Dict[str, Any]],
                   series_map: Optional[Dict[str, List[Dict[str, Any]]]] = None,
                   trade_date: Optional[str] = None,
                   mkt_gain: Optional[float] = None,
                   stock_info: Optional[Dict[str, Any]] = None,
                   plugins: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """D0 盘中一轮: 候选集内求 day 门 (+合成 D0 bar) → 命中行。

    盘中股池的确定顺序 (每策略):
      ① 有夜候选 (夜门能缩小) → 只用夜候选 (架构 §5.3 "候选集内门求值");
      ② 夜门为空 (如 knife/tail: 判定天生全依赖 D0) → 接策略插件的
         `intraday_shortlist` 便宜预筛 (必要条件超集, 与实盘 run_scan_knife 同源);
      ③ 都没有 → 全市场快照。
    合成 D0 bar 每股只算一次并被该股所有策略共享。
    series_map: dict 或 callable(code)->series —— 传 callable 时按需取 (只有被股池
      命中的股票才取序列, 与实盘 fetch_day_snapshots 只拉候选股同源)。
    返回 {"hits": {key: [DayHit]}, "stats": {...}, "skipped": [...]}
    """
    si = stock_info or {}
    plugins = plugins or {}
    plans: Dict[str, GatePlan] = night["plans"]
    nhits: Dict[str, List[NightHit]] = night["hits"]
    t0 = time.time()
    out: Dict[str, List[DayHit]] = {}
    skipped: List[str] = []
    n_eval = 0
    n_hit = 0
    synth_cache: Dict[str, List[Dict[str, Any]]] = {}
    series_cache: Dict[str, List[Dict[str, Any]]] = {}

    def _snapshot_quality(snap: Optional[Dict[str, Any]],
                          tdate: Optional[str]) -> tuple:
        """P5 展示层快照完整度标注 (轻量, 单点检查)。

        ⚠️ 只看**当日**快照的累计字段完整性 (不出分钟级质检 —— 那是回测链 P5 的事);
        缺数据 / 字段缺失 / 时间戳不对 → 标记 warning/error; 异常**不影响展示判定**
        —— 评估异常时返 "ok" 阻断异常吞掉排查信号。
        09-21 用户规则: 展示层**无回溯性**; 不能擅自拒绝命中。
        """
        if not snap:
            return ("warning", ["no_snapshot"])
        if tdate and str(snap.get("time", ""))[:10] != tdate:
            return ("warning", ["snapshot_date_mismatch"])
        for k in ("last", "open", "high", "low"):
            v = snap.get(k)
            if v is None or v <= 0:
                return ("error", [f"missing_field:{k}"])
        return ("ok", [])

    def _series(code: str) -> List[Dict[str, Any]]:
        if code in series_cache:
            return series_cache[code]
        if series_map is None:
            v = []
        elif callable(series_map):
            try:
                v = list(series_map(code) or [])
            except Exception:
                v = []
        else:
            v = list(series_map.get(code) or [])
        series_cache[code] = v
        return v

    for key, spec in specs.items():
        plan = plans[key]
        out[key] = []
        if not plan.day:
            continue
        # 每股**全部**夜通过的 lu_idx (升序) —— 不可只留一个: 日门可能淘汰前者而后者
        # 本可通过, 只留一个会漏信号 (等价性论证见模块头)。日阶段按升序取首个通过者。
        lu_map: Dict[str, List[int]] = {}
        for h in nhits.get(key, []):
            lu_map.setdefault(h.code, []).append(h.lu_idx)
        if lu_map:
            pool = {c: s for c, s in snapshots.items() if c in lu_map}
        else:
            plug = plugins.get(key)
            short = None
            if plug is not None and hasattr(plug, "intraday_shortlist"):
                try:
                    short = plug.intraday_shortlist(snapshots, mkt_gain, **spec.params)
                except Exception as e:
                    skipped.append(f"{key}: intraday_shortlist 失败 ({e}) → 退回全市场")
            pool = short if short is not None else snapshots
        for code, snap in pool.items():
            if code not in snapshots:
                continue
            if code not in synth_cache:
                bars = cache.get(code)
                sb = synth_bar(_series(code), trade_date) if trade_date else None
                if sb is None:
                    skipped.append(f"{code}: 无当日快照序列 → 无法合成 D0 bar")
                    synth_cache[code] = []
                    continue
                synth_cache[code] = bars + [sb]
            bars_t = synth_cache[code]
            if not bars_t or len(bars_t) < required_min_len(spec):
                continue
            i = len(bars_t) - 1
            bt = get_board_type(code, spec.market_spec)
            for h_lu in lu_map.get(code, [0]):
                n_eval += 1
                ctx = Ctx(bars_t, i, h_lu, spec.params, board_type=bt,
                          code=code, stock_info=si.get(code) if hasattr(si, "get") else None,
                          ext=build_ext(spec, code, bars_t, cache=cache),
                          latest=snapshots.get(code),
                          series=_series(code),
                          mkt_gain=mkt_gain, market=spec.market_spec)
                ok, _failed = evaluate_gates(spec, plan.day, ctx)
                if not ok:
                    continue
                n_hit += 1
                dq, dqr = _snapshot_quality(snapshots.get(code), trade_date)
                out[key].append(DayHit(code=code, lu_idx=h_lu,
                                       fields=build_signal(ctx, spec),
                                       data_quality=dq,
                                       data_quality_reasons=dqr))
                break                          # 每股每轮首个通过者 (与回测/实盘同序)

    return {"hits": out, "stats": {"evaluated": n_eval, "hit": n_hit,
                                   "elapsed": round(time.time() - t0, 2)},
            "skipped": skipped[:20]}


# ================================================================
# 9. 等价性自检: 全量单次判定 == 夜/日分段判定
# ================================================================
def _first_full_pick(spec: StrategySpec, bars: List[Dict[str, Any]], code: str,
                     board_type: str, si, asof_date: Optional[str] = None,
                     ext: Optional[Dict[str, Any]] = None
                     ) -> Tuple[Optional[int], List[str]]:
    """全量口径: 升序遍历 lu_idx, 取首个**全部门**通过者 (镜像回测/实盘 scan)。"""
    i = len(bars) - 1
    _ext = ext if ext is not None else build_ext(spec, code, bars, asof_date=asof_date)
    last_failed: List[str] = []
    for lu in _candidate_lus(spec, bars, board_type, i):
        ctx = Ctx(bars, i, lu, spec.params, board_type=board_type, code=code,
                  stock_info=si, ext=_ext, market=spec.market_spec)
        ok, failed = evaluate_gates(spec, spec.enabled_gates, ctx)
        if ok:
            return lu, []
        last_failed = failed
    return None, last_failed


def verify_split(spec: StrategySpec, bars: List[Dict[str, Any]], code: str,
                 i: int, stock_info: Optional[Dict[str, Any]] = None,
                 ext: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """等价性自检 (单 (code, 决策日 i)): 全量 vs 夜/日分段, 比较选中的 lu_idx 与展示字段。

    分段侧忠实复刻管线口径 (不是"另写一遍近似"):
      夜 = plan.night 门在 night bars 上求值 → 保留**全部**通过者;
      日 = 对夜通过者按**升序**取首个 day 门通过者。
    ext 可预传 (O(n) 特征 / 横截面池) —— 批量自检时避免每股每日重算。
    返回 {"ok": bool, "full_lu":…, "split_lu":…, "reason": …}。
    """
    bars = bars[:i + 1]
    if len(bars) < 5:
        return {"ok": True, "reason": "样本过短, 跳过"}
    bt = get_board_type(code, spec.market_spec)
    off = decision_offset(spec)
    plan = plan_gates(spec)
    asof = str(bars[-1]["time"])[:10]
    _ext = ext if ext is not None else build_ext(spec, code, bars, asof_date=asof)

    full_lu, full_failed = _first_full_pick(spec, bars, code, bt, stock_info,
                                            asof_date=asof, ext=_ext)

    # 夜侧 bars: 偏移 ≤ -1 → 就是全量 bars; 偏移 0 → 掐掉决策日 + 占位
    if off <= -1:
        n_bars = bars
    else:
        n_bars = bars[:-1] + [placeholder_bar(bars[-1]["time"])]
    n_ext = _ext if off <= -1 else build_ext(spec, code, n_bars, asof_date=asof)
    night_pass: List[int] = []
    last_night_failed: List[str] = []
    for lu in _candidate_lus(spec, n_bars, bt, len(n_bars) - 1):
        ctx = Ctx(n_bars, len(n_bars) - 1, lu, spec.params, board_type=bt,
                  code=code, stock_info=stock_info, ext=n_ext,
                  market=spec.market_spec)
        ok, failed = evaluate_gates(spec, plan.night, ctx)
        if ok:
            night_pass.append(lu)
        else:
            last_night_failed = failed

    split_lu: Optional[int] = None
    split_failed: List[str] = []
    for lu in night_pass:                      # 升序 → 首个日门通过者
        ctx = Ctx(bars, i, lu, spec.params, board_type=bt, code=code,
                  stock_info=stock_info, ext=n_ext, market=spec.market_spec)
        ok, failed = evaluate_gates(spec, plan.day, ctx)
        if ok:
            split_lu = lu
            break
        split_failed = failed

    ok = (full_lu == split_lu)
    detail: Dict[str, Any] = {
        "ok": ok, "code": code, "date": str(bars[-1]["time"])[:10], "key": spec.key,
        "full_lu": full_lu, "split_lu": split_lu,
        "night_pass": len(night_pass),
        "full_failed": full_failed, "night_failed": last_night_failed,
        "split_failed": split_failed,
    }
    if not ok:
        detail["reason"] = (f"全量选中 lu={full_lu} / 分段选中 lu={split_lu}"
                            f" (夜通过 {night_pass[:6]})")
    return detail


# ================================================================
# 10. CLI (diagnostics; 基准在 tools/present_bench.py)
# ================================================================
def _load_specs(keys=None) -> Dict[str, StrategySpec]:
    from app.market_cn.auto.core.runtime.evaluate import load_strategy
    import app.market_cn.auto.core.runtime.strategy_funcs  # noqa: F401  副作用: 注册策略函数
    if keys is None:
        import glob
        keys = sorted(os.path.splitext(os.path.basename(p))[0]
                      for p in glob.glob(os.path.join(STRATEGY_DIR, "*.yaml")))
    return {k: load_strategy(k) for k in keys}


def main():
    import argparse
    ap = argparse.ArgumentParser(description="M3 展示预计算管线 (诊断/自检)")
    ap.add_argument("--audit", action="store_true", help="needs_d0 声明 vs 推导 对账")
    ap.add_argument("--plan", action="store_true", help="打印门分组 (夜/盘中)")
    ap.add_argument("--verify", type=int, default=0, metavar="N",
                    help="等价性自检: 抽样 N 只股票 (全量 vs 分段)")
    ap.add_argument("--verify-window", type=int, default=40, metavar="D",
                    help="自检每只股票回看多少个决策日 (默认 40)")
    ap.add_argument("--days", type=int, default=300)
    ap.add_argument("--keys", default="", help="逗号分隔策略 key (默认全部)")
    a = ap.parse_args()
    keys = [k.strip() for k in a.keys.split(",") if k.strip()] or None
    specs = _load_specs(keys)

    if a.plan or not (a.audit or a.verify):
        print("=== 门分组 (夜 = T-1 可算 / 盘中 = 需买入日数据) ===")
        for k, s in specs.items():
            print(" ", plan_gates(s).describe())

    if a.audit:
        print("\n=== needs_d0 对账 (声明 vs 静态推导) ===")
        bad = 0
        for k, s in specs.items():
            rows = audit_needs_d0(s)
            for r in rows:
                flag = "危险" if r["risk"] == "under" else "过保守"
                print(f"  [{flag}] {r['key']:16s} {r['gate']:12s} "
                      f"声明={r['declared']} 推导={r['derived']}  {r['name']}")
            bad += len(rows)
        print(f"  合计不一致 {bad} 项")

    if a.verify:
        from app.market_cn.auto.core.data.hub import all_codes, stock_info as hub_si
        try:
            si = hub_si()
        except Exception:
            si = {}
        codes = all_codes()[:a.verify]
        cache = BarsCache(days=a.days)
        win = max(1, a.verify_window)
        n_ok = n_bad = n_skip = 0
        examples = []
        for key, spec in specs.items():
            if str(spec.meta.get("enumeration", "")).lower() == "intraday":
                print(f"  [verify] {key:16s} SKIP — intraday 家族需 D0 快照序列, "
                      f"纯日线自检不适用 (逐笔等价见 tmp/_intraday_equivalence.py)")
                continue
            for code in codes:
                bars = cache.get(code)
                if len(bars) < 80:
                    n_skip += 1
                    continue
                # ext (O(n) 特征 / 横截面池) 每股只算一次, 循环内复用
                ext = build_ext(spec, code, bars, cache=cache,
                                asof_date=str(bars[-1]["time"])[:10])
                for i in range(max(5, len(bars) - win), len(bars) - 1):
                    r = verify_split(spec, bars, code, i, si.get(code), ext=ext)
                    if str(r.get("reason", "")).startswith("样本过短"):
                        n_skip += 1
                        continue
                    if r["ok"]:
                        n_ok += 1
                    else:
                        n_bad += 1
                        if len(examples) < 8:
                            examples.append(r)
            print(f"  [verify] {key:16s} 累计 ok={n_ok} bad={n_bad}", flush=True)
        print(f"\n  等价性自检 (分段 vs 全量): ok={n_ok} bad={n_bad} skip={n_skip}")
        for e in examples:
            print("   !", e)


if __name__ == "__main__":
    main()
