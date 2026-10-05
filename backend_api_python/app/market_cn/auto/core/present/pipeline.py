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
# 断点续传搬运层 (2026-10-05 接线): 无感 —— 不 import 任何指标实现
from app.market_cn.auto.core.present.resume_io import build_book, collect_points, fetch

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
def reads_decision_bar(expr: str, key: str = None) -> bool:
    """门表达式是否**读取决策日 i**(偏移 0)的行情数据。

    判据:
      - 内置/注册**偏移函数**: `args[0]` 必须可常量折叠为 < 0 才安全; 无参调用
        (默认偏移 0) 或偏移值 ≥ 0 或偏移来自参数 → 判 True。
      - **非偏移函数**: 查 functions.D0_DEPS / REGISTERED_D0 / 策略命名空间 STRATEGY_GATE_D0
        (declared_d0_for_strategy); 未声明 (None) → 判 True。
      - 字面量/参数名/纯运算: 不读行情数据。

    key: 策略命名空间 (feat/warmup/finite 等跨策略同名函数各自的 D0 依赖不同, 必须按 key 查)。

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
        dep = declared_d0_dep(fname, key)
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
        # M14 (2026-09-28): 必须传 key —— functions.declared_d0_dep 只在传 key 时查
        # STRATEGY_GATE_D0[key] (策略命名空间 D0 声明); 漏传则声明失效, 划分口径与
        # audit_needs_d0 (带 spec.key) 分叉。
        (night if not reads_decision_bar(expr, key) else day).append(gid)
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
        derived = 1 if (off == 0 and reads_decision_bar(g.expr, spec.key)) else 0
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
# ── 注册表本体已迁到 present/ext_registry.py (B1, 2026-10-05) ──
# 这里 re-export 是为了保持 `pipeline.register_ext` / `EXT_PROVIDERS` 的对外契约
# 不变 (present/__init__.py 与其它历史调用方都从这里取)。
from app.market_cn.auto.core.present.ext_registry import (  # noqa: F401
    EXT_MIN_N, EXT_PROVIDERS, register_ext)


# ── g56 的编排层 ext 已迁到 present/ext_g56.py (B1, 2026-10-05) ──
# ⚠️ **不是未使用的导入**: register_ext 是副作用式注册, 不 import 这个模块
#    ⇒ 注册表里没有 'g56' ⇒ build_ext 抛 KeyError 而不是 ImportError (更隐蔽)。
#    新策略加 ext: 照抄一份 present/ext_<策略>.py, 在这里加一行 import。
from app.market_cn.auto.core.present import ext_g56  # noqa: F401


def required_min_len(spec: StrategySpec) -> int:
    """该策略求值所需的最短日线根数 (来自其 `meta.ext` 提供者的声明)。"""
    name = (spec.meta or {}).get("ext")
    return EXT_MIN_N.get(name, 1) if name else 1


def _bars_identity(bars: List[Dict[str, Any]]) -> str:
    """bars 的廉价身份: (根数, 末根日期, 末根收盘)。

    ★★ 必须进 `build_ext` 的缓存键 (2026-10-05 B7)。原因:
      同一个 (code, ext名@日期) 可能被**不同内容**的 bars 调用 —— 典型是
      `verify_split` 里 `off == 0` 策略的夜侧 bars:
          n_bars = bars[:-1] + [placeholder_bar(bars[-1]["time"])]
      它与全量 `bars` **根数相同、末根日期相同**, 但末根是 **close=0 的占位 bar**
      (见 `placeholder_bar`) ⇒ ext 值根本不同。只按 (code, 名称@日期) 缓存会让两者
      **撞同一个键**: 谁先算谁写进去, 另一个直接命中拿到错的 ext, 且**静默不报错**。

      区分点: placeholder_bar 的 close/volume 恒为 0, 而真实 bars 末根 close ≠ 0
      (唯一例外是本就停牌到 0 成交的情况, 届时两者 ext 语义也确实等价)。
      根数一并入键, 兜住「不同长度的同一批 bars」这类更常见的分歧 (如回测切片。)。

    ⚠️ 不要用 `id(bars)`: 每次调用都是新列表对象, 恒不命中, 缓存变摆设。
    """
    if not bars:
        return "0|empty|0"
    last = bars[-1]
    return "%d|%s|%s" % (len(bars), str(last.get("time"))[:10], last.get("close"))


def build_ext(spec: StrategySpec, code: str, bars: List[Dict[str, Any]],
              cache: Optional["BarsCache"] = None,
              asof_date: Optional[str] = None,
              variant: str = "") -> Dict[str, Any]:
    """按 `meta.ext` 构造该 (策略, 股票) 的 Ctx.ext; 未声明 → {}。

    asof 缺省取 **bars 末根日期** —— 这正是决策日 (偏移 ≤ -1 时 = T-1; 偏移 0 时
    = 占位/合成 bar 的 T), 与回测/实盘同源。调用方**不要**传买入日: 对 g56 这类
    "D-1 判定" 策略会错取横截面池。

    variant: 调用方**显式声明** bars 的变体 (如 "night_off0"), 进缓存键。`_bars_identity`
             已能自动分辨绝大多数分歧; 本参数留给"内容恰好同签名但语义确实不同"的场景,
             以及给排查留可读线索 (缓存键能直接看出这一档是谁算的)。

    ── 缓存键 = (code, ext名@日期#bars身份#variant) ──────────────────────────
    日期: 防夜侧 (不含 D0) 与盘中 (含合成 D0) 互串。
    bars 身份: 见 `_bars_identity` —— **这一档是 B7 (2026-10-05) 补的**。
    """
    name = (spec.meta or {}).get("ext")
    if not name:
        return {}
    fn = EXT_PROVIDERS.get(name)
    if fn is None:
        raise KeyError(f"{spec.key}: meta.ext={name!r} 未注册 (见 present.register_ext)")
    d = asof_date or (str(bars[-1]["time"])[:10] if bars else "")
    key = f"{name}@{d}#{_bars_identity(bars)}" + (f"#{variant}" if variant else "")
    if cache is not None:
        # 同 (code, ext, 日期, bars身份) 只算一次 —— 多策略/多轮复用同一份特征数组
        return cache.ext(code, key, lambda: fn(spec, code, bars, d, cache))
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
        self._ext_hits = 0
        self._ext_miss = 0

    def ext(self, code: str, name: str, builder) -> Dict[str, Any]:
        """按 (code, ext名@日期#bars身份) 记忆化派生量 —— 多策略共用一次 O(n) 特征计算。

        ★ `name` 必须自带**完整身份**: 日期 + bars 身份 + variant, 由 `build_ext`
          拼好再传进来 (见 `_bars_identity` 的注释 —— 那里解释了为什么旧的
          "(code, 名@日期)" 会串味)。本方法只做查表, 不重造键语义。
        """
        k = (code, name)
        v = self._ext.get(k)
        if v is None:
            # B6/B1#3 (2026-10-05): ext 侧命中率此前**完全无可观测性** —— 而它正是
            # "池归预处理是否真的生效" 的唯一证据 (首拍阻塞 vs 命中)。
            self._ext_miss += 1
            v = self._ext[k] = builder()
        else:
            self._ext_hits += 1
        return v

    def _load(self, code: str) -> List[Dict[str, Any]]:
        if self._loader is not None:
            bars = self._loader(code, self.days)
        else:
            from app.market_cn.auto.core.data.kline import fetch_kline_db
            # M15 (2026-09-28): 必须与 warm() 同锚 (as_of=self.asof)。原实现锚=now,
            # asof 为历史日期时 "warm 过的票"([asof-450d, asof]) 与 "惰性补载的票"
            # ([now-450d, now] 再截 asof) 历史长度不同 → MA/MACD/最小根数门槛、
            # _g56_pool_batch 切片等价性全受影响 (kline._window_bounds A1 同款坑)。
            bars = fetch_kline_db(code, self.days, as_of=self.asof)
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
        """Bars/bars ext 两侧的行踪。

        B1#3 + B6 (2026-10-05): 原先只报 bars 侧, ext 侧**完全黑盒** ⇒ 无法回答
        "g56 的横截面池到底被复用了几次、是否在每轮重算全市场 `_g1_arrays`"。
        这两个数现在出来了: `ext_hit_rate` 低就说明有 ext 在每轮被反复重算。
        """
        tot = self._ext_hits + self._ext_miss
        return {"codes": len(self._bars), "loads": self.loads, "hits": self.hits,
                "ext_codes": len(self._ext),
                "ext_hits": self._ext_hits, "ext_miss": self._ext_miss,
                "ext_hit_rate": round(self._ext_hits / tot, 4) if tot else None}


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


# ================================================================
# find_limit_ups 记忆化 (B3, 2026-10-04)
# ================================================================
# 背景: `find_limit_ups(bars, board_type, market_spec)` 只依赖这三个入参,
#   而 bars 已经经 BarsCache 跨策略共享 —— 但结果没有共享。
#   旧实现每 (策略, 股票) 重扫一遍 O(n) 逐根 is_limit_up:
#   N 策略 × 5000 票 × 300 根, 其中 (N-1)/N 是纯重复。
# 键含 id(market_spec) 与 id(bars): 同一 code 在不同策略下 market_spec 可能不同
#   (core/market.py:get_board_type 的口径来自 spec.board_rules), 不能只按 code 缓存。
# 条目持有 (bars, market_spec) 引用防 id() 复用, 命中前核对 `is`。
# 体积上限 FIFO: 全市场扫描时上限以下不会触发, 命中收益在"同一票多策略"那段。
# ================================================================
_LU_CACHE: Dict[Any, Any] = {}          # (id(bars), board_type, id(spec)) -> (bars, spec, [lu])
_LU_CACHE_CAP = 4096


def _find_limit_ups_cached(bars, board_type, market_spec) -> List[int]:
    key = (id(bars), board_type, id(market_spec))
    hit = _LU_CACHE.get(key)
    if hit is not None and hit[0] is bars and hit[1] is market_spec:
        return hit[2]
    got = find_limit_ups(bars, board_type, market_spec)
    if len(_LU_CACHE) >= _LU_CACHE_CAP:
        for stale in list(_LU_CACHE)[: _LU_CACHE_CAP // 4]:
            _LU_CACHE.pop(stale, None)
    _LU_CACHE[key] = (bars, market_spec, got)
    return got


def _candidate_lus(spec: StrategySpec, bars: List[Dict[str, Any]],
                   board_type: str, i: int) -> List[int]:
    """决策日的 lu_idx 候选 (镜像回测的当日枚举)。

    limit_up (dragon_callback): i 之前的每个历史涨停日都是候选 (升序)。
    day / intraday: 单候选 lu_idx=0 (门表不依赖 lu_idx)。

    ⚠️ 返回的顺序是**升序**且被消费端依赖: `intraday_cycle` 的 lu_map 按此序
    append, 再 `for h_lu in lu_map[...]` 取首个通过者 —— 与回测"升序遍历 lu_idx,
    首个全部门通过者出信号"同源。改这里不能打乱顺序。
    """
    if str(spec.meta.get("enumeration", "limit_up")).lower() == "limit_up":
        return [j for j in _find_limit_ups_cached(bars, board_type, spec.market_spec)
                if j < i]
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


def resume_points_by_key(keys) -> Dict[str, Tuple[Any, ...]]:
    """各策略的断点记忆点声明 —— 声明源是**策略实例**, 不是 yaml 门表。

    ⚠️ 这条容易踩: `specs` 里的 StrategySpec 由 yaml 门表构造, **不带策略类属性**
       ⇒ `collect_points(specs.values())` 恒返回空元组, 通道静默空转。
       必须从 `strategies.get_strategy(key)` 取实例。
    ⚠️ 未声明的策略回落**标准默认点** (Ctx 的 macd/atr/boll/kdj), 见 base.py 约定。
    """
    from app.market_cn.auto.core.runtime.resume import _default_points
    try:
        from app.market_cn.auto import strategies as strat_reg
    except Exception:
        strat_reg = None
    out: Dict[str, Tuple[Any, ...]] = {}
    for k in keys:
        pts = tuple(getattr(strategy_object(k, strat_reg), "resume_points", None) or ())
        out[k] = pts if pts else tuple(_default_points())
    return out


# ================================================================
# 7b. 断点续传: **日线边界**语义 (2026-10-05 简化)
# ================================================================
#: 断点 = 「截至昨日收盘」的状态摘要; 实时侧只保留最后 `K` 根 bar (历史段由断点
#: 代表), 递推类读数改由断点接力。
#:
#: ★★ **`K` 不是本模块的常量, 而是「策略声明的最大值」** (2026-10-05 下沉):
#:    每个策略在 `strategies/<key>.py` 里声明 `warmup` = 自己判定所需的最短日线根数
#:    (`StrategyBase.warmup`, 默认 40; g56 声明 80)。展示层取
#:      K = max(所有启用策略的 warmup)
#:    理由 —— 断点书是**一份**共享产物 (`precompute_night` 产一份 `resume_book`),
#:    它的快照位置只有一个; 而 `BarsCache` 也是全策略共享的一份。任何策略若想保留
#:    比 K 更短的历史, 就得让它的断点快照落在更晚的位置 ⇒ 需要**另一份**书 = 另一趟
#:    全市场递推。那是零和 (省下的递推 = 多花的建书), 不做。所以本模块只承认 K 这一个数,
#:    策略声明的差异**不会**带来每个策略不同的窗口 —— 声明的真正价值在于:
#:      ① 新增/修改策略不需要改 pipeline 任何一个数;
#:      ② `tests/test_warmup_slice.py` 直接读声明做加倍差分 ⇒ 声明与门禁不可能漂移。
#:
#: ⚠️ 收益主要不在"省递推 CPU"(全市场仅 2.3s), 而在**省取数 IO**:
#:    实测 `fetch_klines_batch` 全市场 320 根 12.9s → 80 根 ~3.3s (省 ~9.6s)。
#:    K 越小收益越大, 但**不能小于任一策略的窗口下限**, 否则滑窗读数失真且静默
#:    —— 这正是"声明下沉"要防的事 (以前 K 写死 80, 谁也不知道它对应哪条策略)。
RESUME_KEEP_FALLBACK = 40      #: 策略**没有**声明 warmup 时的兜底 (= StrategyBase.warmup)


def strategy_object(key: str, strat_reg=None):
    """取策略**实例** (类属性声明的唯一可信来源)。

    ⚠️ 这条路一旦失败必须**看得见**: 返回 None 会让调用方静默拿到默认声明
       (40 根窗口 / 默认记忆点) ⇒ 症状是"某个策略的窗口悄悄变小"。故调用方
       遇到 None 应显式计数登记 (见 `resume_window_by_key` 的 `_decl_miss`)。
    """
    if strat_reg is None:
        try:
            from app.market_cn.auto import strategies as strat_reg
        except Exception:
            return None
    try:
        return strat_reg.get_strategy(key)
    except Exception:
        return None


def resume_window_by_key(keys) -> Dict[str, int]:
    """{策略 key: 它声明的窗口根数} —— 只对**数学可启用**断点 (`resume_supported`)
    的策略返回正值; 其余返回 0 表示"不参与取 max"。

    ⚠️ 声明读不到时 (`get_strategy` 失败 / 未注册) 回落 `RESUME_KEEP_FALLBACK`
       而非 0 —— 用 0 会让该策略被排除在本轮 max 之外, 等于偷偷把全局窗口调小,
       是**静默降级**。宁可保守地按默认窗口算, 也不冒"窗口不够导致读数失真"的风险。
    """
    try:
        from app.market_cn.auto import strategies as strat_reg
    except Exception:
        strat_reg = None
    out: Dict[str, int] = {}
    for k in keys:
        obj = strategy_object(k, strat_reg)
        if obj is None:
            # 保守: 用兜底窗口而非 0 (用 0 会让 max 变小 ⇒ 悄悄把全局窗口调小)
            out[k] = RESUME_KEEP_FALLBACK
            continue
        out[k] = _declared_window(obj)
    return out


def _supports_resume(obj, default: bool = True) -> bool:
    """该策略是否**数学可启用**断点 (`StrategyBase.resume_supported`)。

    取不到实例时返回 `default` (保守=True ⇒ 让它的窗口参与取 max)。
    """
    if obj is None:
        return default
    return bool(getattr(obj, "resume_supported", True))


def _declared_window(obj) -> int:
    """读实例声明的窗口根数; 不可启用断点或声明非法一律返回 0 (不参与 max)。"""
    if not _supports_resume(obj):
        return 0
    try:
        w = int(getattr(obj, "warmup", 0) or 0)
    except Exception:
        w = 0
    return w if w > 0 else 0


def resume_window(keys) -> int:
    """本次断点保留的根数 = max(各策略声明) + 兜底。

    ★ 全 0 (没有任何策略参与) 时返回 `RESUME_KEEP_FALLBACK` 而不是 0 ——
      0 会让 `_resume_prefix_bars` 退化成"前缀=全量", 断点落点与实时切片全错位
      且**不报错**。给一个正的兜底值, 语义至少自洽。
    """
    vals = [v for v in resume_window_by_key(keys).values() if v > 0]
    return max(vals) if vals else RESUME_KEEP_FALLBACK


def resume_keys_enabled(specs) -> set:
    """本次可启用断点的策略 key 集合 = 由**策略自己声明**是否支持。

    (`StrategyBase.resume_supported`; 取代旧 pipeline 侧的硬编码 key 集合。)
    ⚠️ 实例取不到时**保守判为可启用** —— 宁可让它走正常的取 max 路径,
       也不要因为一次 import 抖动就悄悄把策略踢出断点。
    """
    try:
        from app.market_cn.auto import strategies as strat_reg
    except Exception:
        strat_reg = None
    out = set()
    for k in specs:
        if _supports_resume(strategy_object(k, strat_reg), default=True):
            out.add(k)
    return out


def _resume_prefix_bars(cache: "BarsCache", code: str, buy_date: str,
                        keep: int) -> List[Dict[str, Any]]:
    """断点**前缀** bars = 夜侧 bars 去掉末 `keep` 根。

    夜算按此取 snapshot (断点状态 = prefix 末根结束时的摘要);
    盘中按**同一口径**切片 (`bars_t[len(bars_t) - keep:]`) ⇒ 天然对齐,
    无需每票每轮再做指纹校验 (除权由预处理重建负责, 见 `build_book` 的校验)。
    """
    b = _night_bars(cache, code, 0, buy_date) or []
    return b[:len(b) - keep] if len(b) > keep else []


def precompute_night(specs: Dict[str, StrategySpec], codes, cache: BarsCache,
                     stock_info: Optional[Dict[str, Any]] = None,
                     buy_date: Optional[str] = None,
                     progress_every: int = 0) -> Dict[str, Any]:
    """T-1 夜预计算: 对每个策略求 night 门 → 明日候选集。

    返回 {"hits": {key: [NightHit]}, "plans": {key: GatePlan}, "stats": {key: StageStats}}

    ── 循环维度: **外层股票、内层策略** (B4, 2026-10-04) ──
    旧实现是外层策略、内层股票, 导致每股的 `_night_bars` / `get_board_type` /
    `find_limit_ups` / `Ctx._ind_cache` 全都拿不到跨策略共享 —— 这是夜算重复计算的
    结构性根因。倒转后这些量天然每股一份。

    ⚠️ `hits[key]` 的**顺序不变** (被消费端依赖, 见 `_candidate_lus` 注释):
       两种循环序下 hits[key] 都是"按 codes 顺序 × lu 升序"展开。
    ⚠️ `stats[key].codes/evaluated/passed` 不变; 只有 `.elapsed` 由"该策略整段墙钟"
       变为"各股票片段累加"(纯展示用, 无消费端依赖)。
    ⚠️ `progress_every` 由"每完成 N 个策略打印"变为"跑完统一打印"(倒转后没有中间完成点)。
    """
    si = stock_info or {}
    plans = {k: plan_gates(s) for k, s in specs.items()}
    need_buy_date = any(p.decision_offset == 0 and p.night for p in plans.values())
    if need_buy_date and not buy_date:
        raise ValueError("存在 偏移=0 的策略有夜门 → 必须传 buy_date 以构造虚拟 D0 bar")

    hits: Dict[str, List[NightHit]] = {k: [] for k in specs}
    stats: Dict[str, StageStats] = {k: StageStats() for k in specs}
    codes = list(codes)

    # 股票级共享量: bars 按 (code, decision_offset) 缓存 (不同策略偏移不同 → 占位 bar 不同),
    # board_type 按 (code, market_spec) 缓存 (market_spec 影响 board_rules 口径)。
    bars_memo: Dict[Any, Any] = {}
    bt_memo: Dict[Any, str] = {}

    for code in codes:
        info = si.get(code)
        for key, spec in specs.items():
            plan = plans[key]
            st = stats[key]
            t0 = time.time()
            min_n = max(cache.min_len, required_min_len(spec))
            bkey = (code, plan.decision_offset)
            bars = bars_memo.get(bkey)
            if bars is None:
                bars = bars_memo.setdefault(
                    bkey, _night_bars(cache, code, plan.decision_offset, buy_date or ""))
            if not bars or len(bars) < min_n:
                st.elapsed += time.time() - t0
                continue
            st.codes += 1
            if not plan.night:
                st.elapsed += time.time() - t0
                continue
            mkey = (code, id(spec.market_spec))
            bt = bt_memo.get(mkey)
            if bt is None:
                bt = bt_memo.setdefault(mkey, get_board_type(code, spec.market_spec))
            i = len(bars) - 1
            ext = build_ext(spec, code, bars, cache=cache)
            for lu in _candidate_lus(spec, bars, bt, i):
                st.evaluated += 1
                ctx = Ctx(bars, i, lu, spec.params, board_type=bt, code=code,
                          stock_info=info, ext=ext, market=spec.market_spec)
                ok, _failed = evaluate_gates(spec, plan.night, ctx)
                if ok:
                    st.passed += 1
                    hits[key].append(NightHit(code=code, lu_idx=lu, board_type=bt))
            st.elapsed += time.time() - t0

    for key, st in stats.items():
        st.elapsed = round(st.elapsed, 2)
    if progress_every:
        for idx, key in enumerate(specs, 1):
            if idx % progress_every == 0:
                print(f"  [night] {key} 通过 {stats[key].passed} "
                      f"用时 {stats[key].elapsed:.1f}s", flush=True)

    # ── 断点续传产出 (2026-10-05 接线) ──────────────────────────────
    # 无感: 只认策略**声明**的 `resume_points`; 声明来自**策略实例**
    # (`strategies.get_strategy(key)`), 不是 yaml 门表 StrategySpec ——
    # 后者不带策略类属性, 传它会静默拿到 0 个记忆点 (通道空转)。
    # 未声明的策略回落**标准默认点** (Ctx 的 macd/atr/boll/kdj)。
    # ⚠️ 断点 = 本轮夜算所见 bars 的末根 (break_date 取首票末根日期, 仅记录)。
    # ── 断点产出: **单一档** (日线边界语义, 2026-10-05 简化) ──
    # 不再按 keep 分档、不再按策略声明 —— 全局一份 ResumeBook, 所有非豁免策略共用。
    by_key = resume_points_by_key(specs.keys())
    enabled = resume_keys_enabled(specs)
    # └ keep = **策略自己声明的窗口**的 max (见 `resume_window` 的长注释)
    keep = resume_window([k for k in specs if k in enabled])
    seen: set = set()
    points_list: List[Any] = []
    for k, ps in by_key.items():
        if k not in enabled:
            continue
        for p in ps:
            if p.key not in seen:
                seen.add(p.key)
                points_list.append(p)
    points = tuple(points_list)
    book = None
    if points and codes:
        try:
            b0 = _resume_prefix_bars(cache, codes[0], buy_date or "", keep)
            bd = str(b0[-1].get("time"))[:10] if b0 else ""
        except Exception:
            bd = ""
        # ★ 除权/数据修正的**唯一处理点**: 预处理在这里一次性比对, 不一致即重建。
        #   实时侧完全不感知除权 (见 intraday_cycle 的 use_resume 分支)。
        book = build_book(codes, lambda c: _resume_prefix_bars(cache, c, buy_date or "", keep),
                          points, bd)
        if progress_every:
            print(f"  [night] 断点续传产出: {len(codes)} 票 × {len(points)} 记忆点, "
                  f"keep={keep}, break_date={bd}", flush=True)

    return {"hits": hits, "plans": plans, "stats": stats,
            "resume_points": points, "resume_points_by_key": by_key,
            "resume_book": book, "resume_keep": keep}


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
                   plugins: Optional[Dict[str, Any]] = None,
                   use_resume: Optional[bool] = None) -> Dict[str, Any]:
    """D0 盘中一轮: 候选集内求 day 门 (+合成 D0 bar) → 命中行。

    ── 断点续传 (2026-10-05 简化: **日线边界**语义) ──────────────────────────
    断点 = 「截至昨日收盘」的状态摘要 (夜算产出)。启用时实时侧只保留最后
    `K = max(各策略声明的 warmup)` 根 bar (数由夜侧算出并随 `resume_keep` 传来),
    历史段由断点代表 ⇒ 递推类读数不再从头重算。

    `use_resume`:
       None / False (默认) → 关闭: bars 全量, `Ctx.resume={}` ⇒ 与不接时逐位一致;
       True                → 启用 (对 `resume_supported=False` 的策略自动不生效)。

    ★★ **实时侧不做任何防御** (2026-10-05 裁定):
       除权、数据修正一律由**预处理** (`precompute_night` → `build_book`) 一次性
       比对并重建; 实时侧只判断"断点有没有、日期对不对", 不做逐票指纹校验
       (旧实现把指纹放在盘中每票每轮 ⇒ 160 次/23.8ms 占 75%, 是净成本的主因)。

    ⚠️ `enumeration=limit_up` 家族**数学上不可启用** (策略侧声明
       `StrategyBase.resume_supported = False`, 见 dragon_callback.py):
       `_candidate_lus` 的候选是「**窗口内**的历史涨停日」且 `lu_idx` 是**绝对索引**
       ⇒ 截窗会削减候选本身 (实测 dragon_callback 465→161), 断点**救不回**。

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
    # ── 断点续传: 夜算产出的**单一** ResumeBook (日线边界语义) ──
    rbook_root = night.get("resume_book") if use_resume else None
    rpoints_all: Tuple[Any, ...] = tuple(night.get("resume_points") or ())
    # 按策略取自己的声明 (各策略声明可能不同, 不能用全局并集)
    rpoints_by_key: Dict[str, Tuple[Any, ...]] = dict(night.get("resume_points_by_key") or {})
    # └ 夜侧算出的窗口 = 各策略 `warmup` 声明的 max; 盘中必须**用同一个数**,
    #   否则两侧落点错位 (夜 vs 日永远对不上) 且不报错。
    resume_keep = int(night.get("resume_keep") or resume_window(resume_keys_enabled(specs)))
    if use_resume and (rbook_root is None or not rpoints_all):
        # 要求压缩却没有断点可用 ⇒ **不做静默降级**, 关闭并计数
        use_resume = False
        skipped.append("use_resume=True 但无断点产出 → 退回全量重算")
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

    # ── B4 (2026-10-05): 循环维度改为 **外层股票、内层策略** ────────────────
    # 原实现外层策略、内层股票 ⇒ 每股的共享量都拿不到跨策略复用:
    #   · `get_board_type(code, ...)`     每策略各算一遍
    #   · `build_ext` → `_g1_arrays`      虽已有 cache, 但首次遍历时每策略各触发一次
    #   · `Ctx` 的指标缓存                依赖 (bars, i) 共享, 倒转后才落在同一票上
    # (夜侧 `precompute_night` 早已倒转, 这是当时没做完的另一半。)
    #
    # ★★ **out[key] 的顺序必须逐项不变** —— 这是本次改造唯一的高危点。做法:
    #     ① 各策略的 pool 统一**按 snapshots 的顺序重排** (plugin shortlist 的构造序不可假设);
    #     ② 外层严格按 snapshots 序遍历, 内层按 specs 序遍历;
    #     ⇒ 任一 key 的输出序列仍 = 沿 snapshots 顺序取其在 pool 中的命中者, 与旧实现同源。
    key_ctx: Dict[str, Dict[str, Any]] = {}
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
        # ★ 按 snapshots 重排 + 只保留确实存在于 snapshots 的 code —— 见上方①
        pool = {c: pool[c] for c in snapshots if c in pool}
        # 本策略是否启用断点: 全局开关 ∩ 非豁免家族 ∩ 夜算确有产出
        rkeep = bool(use_resume) and key in resume_keys_enabled(specs)
        rbook = rbook_root if rkeep else None
        rpts = rpoints_by_key.get(key) or rpoints_all
        if rkeep and not rpts:
            # 该策略没有记忆点声明 ⇒ **不做静默降级**, 关闭并计数
            skipped.append(f"{key}: 无记忆点声明 → 退回全量重算")
            rkeep = False
        key_ctx[key] = {"spec": spec, "plan": plan, "lu_map": lu_map,
                        "pool": pool, "rkeep": rkeep, "rbook": rbook, "rpts": rpts}

    bt_memo: Dict[Any, Any] = {}      # (code, id(market_spec)) -> (market_spec, board_type)
    for code in snapshots:
        snap = snapshots[code]
        for key, KC in key_ctx.items():
            if code not in KC["pool"]:
                continue
            spec, plan = KC["spec"], KC["plan"]
            lu_map = KC["lu_map"]
            rkeep, rbook, rpts = KC["rkeep"], KC["rbook"], KC["rpts"]
            if code not in synth_cache:
                bars = cache.get(code)
                sb = synth_bar(_series(code), trade_date) if trade_date else None
                if sb is None:
                    skipped.append(f"{code}: 无当日快照序列 → 无法合成 D0 bar")
                    synth_cache[code] = []
                    # ⚠️ **必须是 break 不是 continue** (倒转后的新语义): 这里淘汰的是
                    #    "该票没有当日快照序列" —— 与具体策略无关, 所有策略都不可用;
                    #    continue 会变成"跳过本策略继续看下一个策略", 把同一条日志按
                    #    策略数重复 N 遍。
                    break
                synth_cache[code] = bars + [sb]
            bars_t = synth_cache[code]
            if not bars_t or len(bars_t) < required_min_len(spec):
                continue
            # ── 断点续传消费: 截短历史 + 取断点状态 ──
            # ★ 实时侧**不校验指纹**: 除权/修正由预处理重建保证 (见 build_book)。
            #   这里只做"断点有没有、点数够不够"的最简判断, O(1)。
            rstate: Dict[Any, Any] = {}
            if rkeep and len(bars_t) > resume_keep:
                bars_t = bars_t[len(bars_t) - resume_keep:]
                rstate = fetch(rbook, code, rpts, None)
                # ⚠️ 判据**必须无条件** (不能写 `if rstate and ...`):
                #    一旦截短了 bars 而断点不全/全失效, Ctx 会在短窗口上走全量重算
                #    ⇒ 递推初值缺失, 数值全错且**不报错**。宁可回落全量, 不可算错。
                if len(rstate) != len(rpts):
                    bars_t = synth_cache[code]
                    rstate = {}
                    skipped.append(f"{code}: 记忆点不全 → 回落全量")
            i = len(bars_t) - 1
            # board_type 每股每 market_spec 只算一次 (倒转后的跨策略共享收益)
            _mkey = (code, id(spec.market_spec))
            _bt = bt_memo.get(_mkey)
            if _bt is None or _bt[0] is not spec.market_spec:
                _bt = bt_memo[_mkey] = (spec.market_spec,
                                        get_board_type(code, spec.market_spec))
            bt = _bt[1]
            for h_lu in lu_map.get(code, [0]):
                n_eval += 1
                # ⚠️ ext 必须基于**全量** bars: build_ext 的缓存键是 (code, ext名@日期),
                #    **不含 bars 长度** ⇒ 若传截短的 bars_t 且缓存未热, 会把"短窗口特征"
                #    写进缓存并污染后续全量调用 (静默算错)。全量侧算一次即可, 之后命中。
                ctx = Ctx(bars_t, i, h_lu, spec.params, board_type=bt,
                          code=code, stock_info=si.get(code) if hasattr(si, "get") else None,
                          ext=build_ext(spec, code, synth_cache[code], cache=cache),
                          latest=snapshots.get(code),
                          series=_series(code),
                          resume=rstate or None,
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
                 ext: Optional[Dict[str, Any]] = None,
                 cache: Optional["BarsCache"] = None) -> Dict[str, Any]:
    """等价性自检 (单 (code, 决策日 i)): 全量 vs 夜/日分段, 比较选中的 lu_idx 与展示字段。

    分段侧忠实复刻管线口径 (不是"另写一遍近似"):
      夜 = plan.night 门在 night bars 上求值 → 保留**全部**通过者;
      日 = 对夜通过者按**升序**取首个 day 门通过者。
    ext 可预传 (O(n) 特征 / 横截面池) —— 批量自检时避免每股每日重算。
    cache: 可选。给了就让 `n_ext` 也走记忆化 (B7, 2026-10-05) —— 自检对每个决策日 i
           调一次 verify_split, 夜侧 ext 不缓存 ⇒ 每个 i 重算 O(n) 特征。
           ⚠️ **安全前提是 build_ext 的键已含 bars 身份**: `off == 0` 时 n_bars =
           `bars[:-1]+[placeholder_bar]` 与全量 bars 根数/末日期都相同, 键里没有
           `_bars_identity` 就会拿到全量侧算错的 ext 且不报错。
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
    # variant 显式标注"夜侧占位", 与全量侧彻底隔离 (双保险: _bars_identity 已能分辨)
    n_ext = _ext if off <= -1 else build_ext(
        spec, code, n_bars, cache=cache, asof_date=asof, variant="night_off0")
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
        # M18 (2026-09-28): day 门在全量**真实** bars 上求值, ext 必须同源 (_ext 由
        # 真实 bars / 调用方预传构建); 原实现传 n_ext (夜侧占位 D0 bar 构建), 对声明
        # meta.ext 且 offset=0 的策略, 日侧特征来自占位 → 自检产假 ok/假 bad。
        ctx = Ctx(bars, i, lu, spec.params, board_type=bt, code=code,
                  stock_info=stock_info, ext=_ext, market=spec.market_spec)
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
    from app.market_cn.auto.core.runtime.functions import ensure_gate_init
    ensure_gate_init()  # noqa: F401  副作用: 注册门表 DSL 标准库 (gate_stdlib) + 各策略私有门函数
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
                    # cache 一并下传 ⇒ 夜侧 ext (off==0 时) 也走记忆化 (B7)。
                    # ⚠️ 每个 i 的夜侧 bars 长度不同 ⇒ 缓存天然按 i 分份, 不要期望"只算一次";
                    #    本循环是 --verify 小样本自检, codes/window 都有限, 内存可控。
                    r = verify_split(spec, bars, code, i, si.get(code),
                                     ext=ext, cache=cache)
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
