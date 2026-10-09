"""ide/evaluate.py — 门表策略加载与回测编排 (M1 原型 → M2 泛化)。

核心：把 strategies/*.py 插件的"信号判定"替换为门表求值（单日判定入口 `scan_day`），
出场由 **exit_modes** 按 YAML 的 `exit.mode` 派发（dragon 已接线，break/g56 待扩适配器契约）；
入场**不走分派** —— 由各策略折叠 `step` 内部状态机产出（`core/entry_modes.py` 已于 2026-10-09 退役）。
全历史回测编排（原 `run_backtest`，链 A：门表 runner + 独立出场编排）已于终态② Step 3
（2026-10-09）退役 —— 回测主路径收敛到事件流折叠（core/backtest.run_all →
StrategyBase.backtest_stock 薄壳 → core.replay），判定引擎单源。

as-of 守护：Ctx 只暴露 ≤ 决策日 i 的数据（见 functions.Ctx）；门表表达式经
expr.static_asof_check 加载期静态校验 + 运行时偏移强制，双重杜绝未来函数。

诊断钩子（M6，2026-09-20）：`GateEvaluator(gate_dbg=...)` 可选传入回调后，每次门求值
**额外**算一遍全门布尔向量并回调 `gate_dbg(phase, code, i, lu_idx, {gate_id: bool})`；
`gate_dbg=None`（生产默认）时走原短路主路径，**零额外开销、判定语义完全不变**。供
`tools/explain.py` 采集门漏斗（门表路径此前无采集通道，`failed` 只留首个失败门）。

分层 (2026-09-28): 本文件曾是「门表求值 + 5 份策略专属回测编排」的混合体 (840 行,
其中 415 行 = v1/relay3/break/g56/limit_up 的逐字镜像编排, 占 49%)。编排已**纯搬运**
回 strategies/<key>.py 并经 core/runtime/flows 自注册; 本文件只留门表加载/求值 + 分派。
终态② Step 3 (2026-10-09): 再删去 `run_backtest` 分派入口与 5 份 day_flow runner 注册
(回测主路径已收敛到事件流折叠), 本文件只剩门表加载/求值 + 单日判定分派 (`scan_day`)。
"""

from __future__ import annotations

import ast
import functools
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import yaml

from app.market_cn.auto.adapters.markets.registry import load_market, require_runnable
from app.market_cn.auto.core.market import MarketSpec, get_board_type
from app.market_cn.auto.core.runtime.expr import ExprError, evaluate, static_asof_check
from app.market_cn.auto.core.runtime.functions import (
    AsOfViolation, Ctx, build_funcs, offset_funcs, ensure_gate_init,
)
from app.market_cn.auto.core.runtime.flows import (
    UnknownFlow, get_scan_one,
)
ensure_gate_init()  # 副作用: 注册门表 DSL 标准库 (gate_stdlib) + 各策略私有门函数 (autodiscover)

from app.utils.logger import get_logger

logger = get_logger(__name__)

# 门求值异常去重告警 (A8): (gate_id, 异常类名) → 出现次数。回测逐 bar 循环里
# 同一门反复炸同一异常, 首次 WARNING、后续 DEBUG, 兼顾暴露与日志量。
_GATE_ERR_SEEN: Dict[Tuple[str, str], int] = {}


def _gate_error(gate_id, e) -> None:
    """门求值异常 → 记日志, 调用方视作该门未过 (模块契约)。

    A8 (2026-09-28): 原实现只捕 ExprError —— AsOfViolation (参数化正偏移的
    运行期 _check_k 兜底)、TypeError (注册偏移函数缺参)、KeyError /
    ZeroDivisionError (策略私有门函数内部异常) 全部未捕获, 直接炸掉整轮回测 /
    夜间预计算 (pipeline.py 无外层 try)。一条脏表达式/脏数据最多损失一个门,
    不再打断整策略全市场回测。AsOfViolation 是真未来函数信号, 恒 WARNING 暴露。
    """
    key = (str(gate_id), type(e).__name__)
    _GATE_ERR_SEEN[key] = _GATE_ERR_SEEN.get(key, 0) + 1
    n = _GATE_ERR_SEEN[key]
    if isinstance(e, AsOfViolation):
        logger.warning("[gate:%s] 未来函数违规(运行期 as-of 正偏移), 门按未过处理: %s", gate_id, e)
    elif n == 1:
        logger.warning("[gate:%s] 求值异常(首次), 门按未过处理: %s: %s",
                       gate_id, type(e).__name__, e)
    else:
        logger.debug("[gate:%s] 求值异常(第%d次): %s: %s", gate_id, n, type(e).__name__, e)
# 2026-09-28 分层改造: core 不再惰性 import strategies.* 取特征 (**层反转**), 也不再内嵌
# 任何策略专属编排 —— 编排由 strategies/<key>.py 经 core/runtime/flows 自注册 (见下)。
from app.market_cn.auto.core._paths import STRATEGY_DIR as _STRATEGY_DIR


# ================================================================
# 数据结构
# ================================================================
@dataclass
class Gate:
    id: str
    name: str
    role: str
    needs_d0: int
    enabled: int
    expr: str


@dataclass
class StrategySpec:
    key: str
    meta: Dict[str, Any]
    exit: Dict[str, Any]
    params: Dict[str, Any]
    signal: Dict[str, Any] = field(default_factory=dict)
    gates: List[Gate] = field(default_factory=list)
    # 本策略所属市场的 MarketSpec（由 meta.market 解析，见 load_strategy）。core 各处
    # （Ctx / 枚举 / 入场出场 / 展示）**只经此字段**取市场规则，不写死任何市场常量。
    market_spec: Optional[MarketSpec] = None
    # 展示/门表补足信息（表头中文名等，来自 meta 或 MarketSpec）
    market_key: str = "A"
    # 本策略所有门/信号表达式引用到的函数名并集 (加载期算一次; 见 _referenced_funcs)。
    # 供 build_funcs 只绑定用得到的函数 —— 求值热点里每次构造 ~50 项函数字典是显著开销。
    func_names: Any = None

    @property
    def enabled_gates(self) -> List[Gate]:
        return [g for g in self.gates if g.enabled]

    def prefilter_gates(self) -> List[Gate]:
        """资格门 (qualify)：与 lu_idx 无关，每决策日只算一次。"""
        return [g for g in self.enabled_gates if g.role == "qualify"]

    def decision_gates(self) -> List[Gate]:
        """判定门 (required)：依赖 lu_idx，每个候选涨停日都要算。"""
        return [g for g in self.enabled_gates if g.role == "required"]


@functools.lru_cache(maxsize=256)
def _referenced_funcs(exprs: Tuple[str, ...]) -> frozenset:
    """表达式集合里所有 `Call(名)` 的函数名并集 (纯函数, 按表达式元组缓存)。

    只用于 build_funcs 的"少绑一点"优化。**正确性依赖一个不变式**: 传入的集合是策略
    *全部*表达式 (enabled_gates + signal.fields) 的引用名并集, 因而对任意门子集
    (prefilter/decision/night/day) 都是**超集** —— 不会漏绑。若某名字被漏绑, 求值会
    抛 ExprError, 而 `evaluate_gates` 把 ExprError 视作"门未过" → **静默丢信号**, 故
    调用方必须用并集而非逐门子集。解析失败 → 返回 None (调用方绑全量, 安全回退)。
    """
    from app.market_cn.auto.core.runtime.expr import parse as _parse
    names = set()
    for e in exprs:
        if not e:
            continue
        try:
            tree = _parse(e)
        except Exception:
            return None            # 保守: 解析异常 → 不做过滤 (绑全量)
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                names.add(n.func.id)
    return frozenset(names)


# ================================================================
# 加载
# ================================================================
def load_strategy(key: str) -> StrategySpec:
    """从 strategies/<key>.yaml 加载策略；并对门表做静态 as-of 校验。"""
    path = os.path.join(_STRATEGY_DIR, f"{key}.yaml")
    with open(path, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    gates = [
        Gate(id=g["id"], name=g.get("name", g["id"]), role=g.get("role", "required"),
              needs_d0=int(g.get("needs_d0", 0)), enabled=int(g.get("enabled", 1)),
              expr=g["expr"])
        for g in (doc.get("gates") or [])
    ]
    spec = StrategySpec(
        key=key, meta=doc.get("meta", {}),
        exit=doc.get("exit", {}), params=doc.get("params", {}),
        signal=doc.get("signal", {}) or {}, gates=gates,
    )
    # 市场适配（架构 §9）: 策略只写 `meta.market: <key>`, 其余维度由 MarketSpec 决定。
    # 未声明 → A（基线）。数据源未接的市场在此 **fail-fast**, 绝不静默按 A 股口径跑。
    spec.market_key = str(spec.meta.get("market", "A") or "A")
    spec.market_spec = require_runnable(load_market(spec.market_key))
    spec.func_names = _referenced_funcs(_spec_exprs(spec))
    _static_asof(spec)
    return spec


def _spec_exprs(spec: StrategySpec) -> Tuple[str, ...]:
    """策略内所有被求值的表达式 (门 + 信号字段) —— 供函数名引用分析与缓存键使用。"""
    exprs = [g.expr for g in spec.enabled_gates]
    for fs in (spec.signal.get("fields") or {}).values():
        exprs.append(fs if isinstance(fs, str) else fs.get("expr", ""))
    return tuple(exprs)


def _static_asof(spec: StrategySpec) -> None:
    problems = []
    for g in spec.enabled_gates:
        for (fname, off) in static_asof_check(g.expr, offset_funcs()):
            problems.append(f"{g.id}:{fname}({off}) 正偏移=未来函数")
    for name, fs in (spec.signal.get("fields") or {}).items():
        expr = fs if isinstance(fs, str) else fs.get("expr", "")
        for (fname, off) in static_asof_check(expr, offset_funcs()):
            problems.append(f"signal.{name}:{fname}({off}) 正偏移=未来函数")
    if problems:
        raise ExprError("门表存在未来函数:\n  " + "\n  ".join(problems))


# ================================================================
# 信号字段（signal.fields: 门通过后，用同一 Ctx 求值的展示字段）
# ----------------------------------------------------------------
# 门表只判"是否放行"; 信号展示字段 (extra/trade 字段) 由 `signal.fields` 声明 ——
# 每项 {expr, fmt}；fmt ∈ none / r1..r6 (round) / int。None 值原样透传 (与参考版
# "None if ... else round(...)" 同语义)。这样门表无需 python 即可复现参考版 trades。
# ================================================================
_FMT_FUNCS = {f"r{n}": (lambda v, _n=n: round(v, _n)) for n in range(1, 7)}
_FMT_FUNCS["int"] = lambda v: int(round(v))


def _apply_fmt(v: Any, fmt: Optional[str]) -> Any:
    if v is None:
        return None
    if not fmt or fmt == "none":
        return v
    fn = _FMT_FUNCS.get(str(fmt))
    if fn is None:
        raise ExprError(f"signal.fields 未知 fmt={fmt!r} (支持: none/r1..r6/int)")
    return fn(v)


def build_signal(ctx: Ctx, spec: StrategySpec) -> Dict[str, Any]:
    """按 spec.signal.fields 求值出信号展示字段 dict（门通过后调用）。"""
    fields = spec.signal.get("fields") or {}
    if not fields:
        return {}
    funcs = build_funcs(ctx, spec.key, spec.func_names)
    out: Dict[str, Any] = {}
    for name, fs in fields.items():
        if isinstance(fs, str):
            expr, fmt = fs, None
        else:
            expr, fmt = fs.get("expr", ""), fs.get("fmt")
        out[name] = _apply_fmt(evaluate(expr, spec.params, funcs), fmt)
    return out


def evaluate_gates(spec: StrategySpec, gates: List[Gate], ctx: Ctx) -> Tuple[bool, List[str]]:
    """对给定门子集在**指定 Ctx** 上求值（盘中通道用：qualify 门用快照 Ctx / required 门用日线 Ctx）。

    与 GateEvaluator.evaluate_* 同语义（任一门失败/求值异常 → 拦截），但允许调用方复用
    已构造好的 Ctx（携带 latest/series/mkt_gain 等盘中上下文）。

    **短路求值**：首个失败门即返回。布尔结论与"求全部门再取与"完全等价 (门的顺序无关,
    因为 and 满足结合/交换律)；`failed` 只保留**触发拦截的那一个门 id** —— 该列表历来
    只作诊断 (所有调用方要么丢弃, 要么写入 detail), 不参与任何判定。展示管线的夜间枚举
    是热点 (全市场 × 每票每个 lu ≠ 数万次), 逐门求全的年代价是其主要开销。
    """
    funcs = build_funcs(ctx, spec.key, spec.func_names)
    for g in gates:
        try:
            if not evaluate(g.expr, spec.params, funcs):
                return False, [g.id]
        except Exception as e:      # A8: 求值异常一律视作该门未过 (原只捕 ExprError)
            _gate_error(g.id, e)
            return False, [g.id]
    return True, []


# ================================================================
# 门表求值
# ================================================================
class GateEvaluator:
    """给定 (bars, i, lu_idx)，对门表逐门求值。board_type/code/stock_info 供 is_limit_up、is_bse、
    换手率等口径使用（stock_info 为静态股本元数据，非时序）。"""

    def __init__(self, spec: StrategySpec, board_type: str = "main", code: str = "",
                 stock_info: Optional[Dict[str, Any]] = None,
                 gate_dbg: Optional[Any] = None):
        self.spec = spec
        self.board_type = board_type
        self.code = code
        self.stock_info = stock_info
        # M6 诊断钩子: 非 None 时每次门求值额外回调全门向量 (生产 None=零开销)。
        self.gate_dbg = gate_dbg

    def _vector(self, gates: List[Gate], params: Dict[str, Any],
                funcs: Dict[str, Any]) -> Dict[str, bool]:
        """求全门布尔向量（**不短路**）；求值异常视为该门未过（与短路判定同语义）。

        仅诊断路径调用（gate_dbg 非 None）；生产路径不进此函数。
        """
        out: Dict[str, bool] = {}
        for g in gates:
            try:
                out[g.id] = bool(evaluate(g.expr, params, funcs))
            except Exception as e:  # A8: 求值异常一律视作该门未过 (原只捕 ExprError)
                _gate_error(g.id, e)
                out[g.id] = False
        return out

    @staticmethod
    def _first_false(gates: List[Gate], vec: Dict[str, bool]) -> Tuple[bool, List[str]]:
        """全门向量 → 短路结论 (首个未过门 id)。与逐门短路求值布尔等价。"""
        for g in gates:
            if not vec.get(g.id, False):
                return False, [g.id]
        return True, []

    def _ctx(self, bars: List[Dict[str, Any]], i: int, lu_idx: int,
             params: Dict[str, Any]) -> Ctx:
        return Ctx(bars, i, lu_idx=lu_idx, params=params, board_type=self.board_type,
                   code=self.code, stock_info=self.stock_info, market=self.spec.market_spec)

    def evaluate_prefilter(self, bars: List[Dict[str, Any]], i: int,
                           params: Dict[str, Any],
                           ctx: Optional[Ctx] = None) -> Tuple[bool, List[str]]:
        """资格门（与 lu_idx 无关）。返回 (全过, 失败门id列表)。

        短路求值: 首个失败门即返回 (布尔结论等价, failed 仅保留拦截门; 见 evaluate_gates)。
        gate_dbg 非 None 时额外回调全门向量 (phase="qualify")。

        ctx 可选：调用方若需复用外部构造的 Ctx（携带 ext 注入冻结值等盘中上下文），
        可传入 —— 与 evaluate_all 的 ctx 语义一致。
        """
        gates = self.spec.prefilter_gates()
        if ctx is None:
            ctx = self._ctx(bars, i, 0, params)
        funcs = build_funcs(ctx, self.spec.key, self.spec.func_names)
        if self.gate_dbg is not None:
            vec = self._vector(gates, params, funcs)
            self.gate_dbg("qualify", self.code, i, 0, vec)
            return self._first_false(gates, vec)
        for g in gates:
            try:
                if not evaluate(g.expr, params, funcs):
                    return False, [g.id]
            except Exception as e:  # A8: 求值异常一律视作该门未过 (原只捕 ExprError)
                _gate_error(g.id, e)
                return False, [g.id]
        return True, []

    def evaluate_decision(self, bars: List[Dict[str, Any]], i: int, lu_idx: int,
                          params: Dict[str, Any],
                          ctx: Optional[Ctx] = None) -> Tuple[bool, List[str]]:
        """判定门（依赖 lu_idx）。返回 (全过, 失败门id列表)。短路求值 (同 evaluate_gates)。
        gate_dbg 非 None 时额外回调全门向量 (phase="decision")。

        ctx 可选：调用方若需复用外部构造的 Ctx（携带 ext 注入冻结值等盘中上下文），
        可传入 —— 与 evaluate_all 的 ctx 语义一致。
        """
        gates = self.spec.decision_gates()
        if ctx is None:
            ctx = self._ctx(bars, i, lu_idx, params)
        funcs = build_funcs(ctx, self.spec.key, self.spec.func_names)
        if self.gate_dbg is not None:
            vec = self._vector(gates, params, funcs)
            self.gate_dbg("decision", self.code, i, lu_idx, vec)
            return self._first_false(gates, vec)
        for g in gates:
            try:
                if not evaluate(g.expr, params, funcs):
                    return False, [g.id]
            except Exception as e:  # A8: 求值异常一律视作该门未过 (原只捕 ExprError)
                _gate_error(g.id, e)
                return False, [g.id]
        return True, []

    def evaluate_all(self, bars: List[Dict[str, Any]], i: int,
                     params: Dict[str, Any], ctx: Optional[Ctx] = None) -> Tuple[bool, List[str]]:
        """一次性求所有启用门（break/g56/dragon 等无 lu_idx 依赖策略）。返回 (全过, 失败门id列表)。

        短路求值 (同 evaluate_gates)。ctx 可选：调用方若需复用同一 Ctx（命中结构/指标
        记忆化），可外部构造并传入。gate_dbg 非 None 时额外回调全门向量 (phase="all")。
        """
        if ctx is None:
            ctx = self._ctx(bars, i, 0, params)
        funcs = build_funcs(ctx, self.spec.key, self.spec.func_names)
        gates = self.spec.enabled_gates
        if self.gate_dbg is not None:
            vec = self._vector(gates, params, funcs)
            self.gate_dbg("all", self.code, i, 0, vec)
            return self._first_false(gates, vec)
        for g in gates:
            try:
                if not evaluate(g.expr, params, funcs):
                    return False, [g.id]
            except Exception as e:  # A8: 求值异常一律视作该门未过 (原只捕 ExprError)
                _gate_error(g.id, e)
                return False, [g.id]
        return True, []


# ================================================================
# 单日判定（scan_day = 生产链入口；全历史回测编排已退役 2026-10-09 终态② Step 3）
# ----------------------------------------------------------------
# 本文件曾另含 run_backtest（门表策略**全历史回测**编排分派，链 A：按 meta.enumeration
# 分派 limit_up / day，day 再按 meta.day_flow 分派）。2026-09-28 分层改造把 5 份策略专属
# 编排纯搬运回 strategies/<key>.py 并经 core/runtime/flows 自注册。
# 终态② Step 3：回测主路径已完全收敛到事件流折叠（core/backtest.run_all →
# StrategyBase.backtest_stock 薄壳 → core.replay），判定引擎只剩折叠一处 ⇒
# run_backtest 与 flows 的 _FLOWS 半边（day_flow runner 表）一并退役。
# ================================================================

def scan_day(spec, bars, code, target, board_type=None, stock_info=None) -> list:
    """门表单日判定 → list[Signal]（生产链切门表引擎的入口）。

    复用 `load_strategy` 的门表 + `GateEvaluator` 单日求门；经 flows 单日判定注册表分派
    到策略模块（`register_scan_one` 登记的 fn，**只做单日判定**：不做出场模拟、不去重
    —— 去重是回测/写库层的事，生产单日口径与 `scan_days(lo=hi=target)` 一致）。

    返回 list[Signal]（空表=当日无信号）；intraday 策略不经此入口（走主干折叠）。
    """
    if str(spec.meta.get("enumeration", "")).lower() == "intraday":
        raise UnknownFlow("intraday 策略走主干折叠（IntradayFeed + core.replay），不经 scan_day")
    from app.market_cn.auto.core.market import get_board_type
    board_type = board_type or get_board_type(code, spec.market_spec)
    ev = GateEvaluator(spec, board_type, code=code, stock_info=stock_info)
    i = None
    for k, b in enumerate(bars):
        if str(b.get("time", ""))[:10] == target:
            i = k
            break
    if i is None:
        return []
    enumeration = str(spec.meta.get("enumeration", "")).lower()
    flow = str(spec.meta.get("day_flow", "")).lower() if enumeration == "day" else ""
    fn = get_scan_one(enumeration, flow)
    sig = fn(spec, ev, bars, i, board_type, stock_info)
    return [sig] if sig is not None else []


# ================================================================
# 编排下沉 + 退役声明 (2026-09-28 分层 / 2026-10-09 终态② Step 3)
# ----------------------------------------------------------------
# 2026-09-28: 本文件曾内嵌 5 份策略专属回测编排 (415 行 / 840 行 = 49%)，已**纯搬运**
#   回各自策略模块 (签名与语义逐字不变)，core 只保留门表加载/求值 + 通用分派。
#   逐笔等价回归见 analysis_output/auto架构分层_20260928.md。
# 2026-10-09 终态② Step 3: 回测主路径收敛到事件流折叠后，链 A 的 `run_backtest` 与
#   flows 的 `_FLOWS` 半边 (day_flow runner 表) 一并退役。判定引擎只剩一处 ——
#   strategies/<key>.py 的折叠契约 (init_state/evaluate/step)，经 core.replay 驱动。
#   仍存续的登记点 (生产单日判定，保留):
#     strategies/break.py            register_scan_one("day", "break", ...)
#     strategies/g56.py              register_scan_one("day", "g56", ...)
#     strategies/dragon_callback.py  register_scan_one("limit_up", "", ...)
# ================================================================
