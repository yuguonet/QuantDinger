"""ide/evaluate.py — 门表策略加载与回测编排 (M1 原型 → M2 泛化)。

核心：把 strategies/*.py 插件的"信号判定"替换为门表求值；回测编排按 `meta.enumeration`
分派枚举方式（limit_up=遍历涨停日 / day=逐日），入场/出场则由 **entry_modes / exit_modes**
按 YAML 的 `entry.mode` / `exit.mode` 选择 —— 成交语义只实现一份（共用 core/exec.py）。
从而产出与 python 参考版**逐笔等价**的 trades。

as-of 守护：Ctx 只暴露 ≤ 决策日 i 的数据（见 functions.Ctx）；门表表达式经
expr.static_asof_check 加载期静态校验 + 运行时偏移强制，双重杜绝未来函数。

诊断钩子（M6，2026-09-20）：`GateEvaluator(gate_dbg=...)` 可选传入回调后，每次门求值
**额外**算一遍全门布尔向量并回调 `gate_dbg(phase, code, i, lu_idx, {gate_id: bool})`；
`gate_dbg=None`（生产默认）时走原短路主路径，**零额外开销、判定语义完全不变**。供
`tools/explain.py` 采集门漏斗（门表路径此前无采集通道，`failed` 只留首个失败门）。
"""

from __future__ import annotations

import ast
import functools
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import yaml

from app.market_cn.auto.adapters.markets.registry import load_market, require_runnable
from app.market_cn.auto.core.filters import unified_prefilter
from app.market_cn.auto.core.market import (
    MarketSpec, find_limit_ups, get_board_name, get_board_type, is_limit_up,
)
from app.market_cn.auto.core.entry_modes import resolve_entry
from app.market_cn.auto.core.exit_modes import run_exit
from app.market_cn.auto.core.runtime.expr import ExprError, evaluate, static_asof_check
from app.market_cn.auto.core.runtime.functions import Ctx, OFFSET_FUNCS, build_funcs, offset_funcs
import app.market_cn.auto.core.runtime.strategy_funcs  # 副作用: 注册 v1/relay3 专属函数 (register_function)
from app.market_cn.auto.core.runtime.strategy_funcs import bk_struct, break_features, relay3_features

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
    entry: Dict[str, Any]
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
        key=key, meta=doc.get("meta", {}), entry=doc.get("entry", {}),
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
    funcs = build_funcs(ctx, spec.func_names)
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
    funcs = build_funcs(ctx, spec.func_names)
    for g in gates:
        try:
            if not evaluate(g.expr, spec.params, funcs):
                return False, [g.id]
        except ExprError:
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
            except ExprError:
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
                           params: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """资格门（与 lu_idx 无关）。返回 (全过, 失败门id列表)。

        短路求值: 首个失败门即返回 (布尔结论等价, failed 仅保留拦截门; 见 evaluate_gates)。
        gate_dbg 非 None 时额外回调全门向量 (phase="qualify")。
        """
        gates = self.spec.prefilter_gates()
        ctx = self._ctx(bars, i, 0, params)
        funcs = build_funcs(ctx, self.spec.func_names)
        if self.gate_dbg is not None:
            vec = self._vector(gates, params, funcs)
            self.gate_dbg("qualify", self.code, i, 0, vec)
            return self._first_false(gates, vec)
        for g in gates:
            try:
                if not evaluate(g.expr, params, funcs):
                    return False, [g.id]
            except ExprError:
                return False, [g.id]
        return True, []

    def evaluate_decision(self, bars: List[Dict[str, Any]], i: int, lu_idx: int,
                          params: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """判定门（依赖 lu_idx）。返回 (全过, 失败门id列表)。短路求值 (同 evaluate_gates)。
        gate_dbg 非 None 时额外回调全门向量 (phase="decision")。
        """
        gates = self.spec.decision_gates()
        ctx = self._ctx(bars, i, lu_idx, params)
        funcs = build_funcs(ctx, self.spec.func_names)
        if self.gate_dbg is not None:
            vec = self._vector(gates, params, funcs)
            self.gate_dbg("decision", self.code, i, lu_idx, vec)
            return self._first_false(gates, vec)
        for g in gates:
            try:
                if not evaluate(g.expr, params, funcs):
                    return False, [g.id]
            except ExprError:
                return False, [g.id]
        return True, []

    def evaluate_all(self, bars: List[Dict[str, Any]], i: int,
                     params: Dict[str, Any], ctx: Optional[Ctx] = None) -> Tuple[bool, List[str]]:
        """一次性求所有启用门（v1/relay3/break 等无 lu_idx 依赖策略）。返回 (全过, 失败门id列表)。

        短路求值 (同 evaluate_gates)。ctx 可选：调用方若需复用同一 Ctx（命中结构/指标
        记忆化），可外部构造并传入。gate_dbg 非 None 时额外回调全门向量 (phase="all")。
        """
        if ctx is None:
            ctx = self._ctx(bars, i, 0, params)
        funcs = build_funcs(ctx, self.spec.func_names)
        gates = self.spec.enabled_gates
        if self.gate_dbg is not None:
            vec = self._vector(gates, params, funcs)
            self.gate_dbg("all", self.code, i, 0, vec)
            return self._first_false(gates, vec)
        for g in gates:
            try:
                if not evaluate(g.expr, params, funcs):
                    return False, [g.id]
            except ExprError:
                return False, [g.id]
        return True, []


# ================================================================
# 回测编排（枚举分派：limit_up / day；day 再按 meta.day_flow 分派信号字段口径）
# ================================================================
def run_backtest(bars: List[Dict[str, Any]], code: str, spec: StrategySpec,
                 stock_info: Optional[Dict[str, Any]] = None,
                 use_prefilter: bool = True,
                 gate_dbg: Optional[Any] = None) -> List[Dict[str, Any]]:
    """门表策略全历史回测入口；按 spec.meta["enumeration"] 分派枚举方式。

    - "limit_up"（默认，dragon_callback）：枚举候选日 → 遍历 lu_idx → 收盘买入。
    - "day"（v1 / relay3 / break）：逐日候选 → D0 信号 → 次日开盘入场。
    入场/出场由 entry_modes / exit_modes 按 YAML 选择 → 逐笔等价于对应 python 参考版。
    gate_dbg: M6 诊断回调 (None=零开销); 非 None 时每次门求值回调全门向量, 供 explain 采集。
    """
    board_type = get_board_type(code, spec.market_spec)
    n = len(bars)
    ev = GateEvaluator(spec, board_type, code=code, stock_info=stock_info, gate_dbg=gate_dbg)
    enumeration = str(spec.meta.get("enumeration", "limit_up")).lower()
    if enumeration == "day":
        flow = str(spec.meta.get("day_flow", "v1")).lower()
        if flow == "relay3":
            return _run_backtest_day_relay3(bars, code, spec, ev, board_type, stock_info, use_prefilter)
        if flow == "break":
            return _run_backtest_day_break(bars, code, spec, ev, board_type, stock_info, use_prefilter)
        if flow == "g56":
            return _run_backtest_day_g56(bars, code, spec, ev, board_type, stock_info, use_prefilter)
        return _run_backtest_day_v1(bars, code, spec, ev, board_type, stock_info, use_prefilter)
    return _run_backtest_limit_up(bars, code, spec, ev, board_type, stock_info, use_prefilter)


# ================================================================
# 枚举方式 A：limit_up（dragon_callback，反转日收盘买入）
# 逐字镜像 dragon_callback.backtest_stock（见 M1 验收）
# ================================================================
def _run_backtest_limit_up(bars, code, spec, ev, board_type, stock_info, use_prefilter):
    """门表版龙回头全历史回测，返回 trades 列表（与 dragon_callback.backtest_stock 逐笔等价）。

    编排逐字镜像 backtest_stock：枚举候选日 → 廉价预筛 → 资格门 → 遍历 lu_idx 取首个通过
    → 去重±4 → unified_prefilter → 入场=entry_modes(close) → 出场=exit_modes(exit.mode)。
    """
    n = len(bars)
    if n < 5:
        return []
    lu_all = find_limit_ups(bars, board_type, spec.market_spec)
    pd_min = int(spec.params["min_pullback_days"])
    pd_max = int(spec.params["max_pullback_days"])
    params = spec.params
    trades: List[Dict[str, Any]] = []
    used_ranges: List[Tuple[int, int]] = []

    for i in range(2, n - 1):
        # 廉价预筛（数学必要条件超集；与 backtest_stock 同源）
        if not any(pd_min + 1 <= i - j <= pd_max + 1 for j in lu_all):
            continue
        # 资格门（与 lu_idx 无关）
        ok, _ = ev.evaluate_prefilter(bars, i, params)
        if not ok:
            continue
        # 遍历 lu_idx（升序；首个通过即出信号 —— 与 scan_signals 同语义）
        lu_cands = [j for j in lu_all if j < i]
        chosen = None
        for lu_idx in lu_cands:
            ok, _ = ev.evaluate_decision(bars, i, lu_idx, params)
            if ok:
                chosen = lu_idx
                break
        if chosen is None:
            continue
        lu_idx = chosen
        # 去重（±4 天内跳过）
        skip = False
        for (s, e) in used_ranges:
            if abs(i - s) <= 4 or abs(i - e) <= 4:
                skip = True
                break
        if skip:
            continue
        used_ranges.append((lu_idx, i))
        # U1~U4 预过滤（锚定涨停日）
        if use_prefilter and lu_idx > 0:
            ok, _ = unified_prefilter(bars, lu_idx, code, stock_info, spec.market_spec)
            if not ok:
                continue
        # 入场 = D0(反转日)收盘价
        d0 = bars[i]
        d1 = bars[i + 1]
        entry_price = float(d0["close"] or 0)
        if entry_price <= 0:
            continue
        result = run_exit(spec.exit.get("mode", "combo"), bars=bars, entry_idx=i,
                          entry_price=entry_price, code=code, board_type=board_type,
                          params=spec.params, diag={})
        if not result:
            continue
        # 信号附带字段（与 _signal_to_legacy_dict + scan_signals.extra 完全一致）
        d_prev = bars[i - 1]
        d_prev2 = bars[i - 2]
        prev_chg = (float(d_prev["close"]) / float(d_prev2["close"]) - 1) * 100 \
            if float(d_prev2.get("close") or 0) > 0 else 0.0
        prev_vol = float(d_prev["volume"]) / float(d_prev2["volume"]) \
            if float(d_prev2.get("volume") or 0) > 0 else 0.0
        sig = {
            "code": code,
            "board": get_board_name(code, spec.market_spec),
            "path": spec.key,
            "path_label": spec.meta.get("name", spec.key),
            "lu_date": bars[lu_idx]["time"],
            "pullback_days": (i - 1) - lu_idx,
            "signal_date": d0["time"],
            "signal_chg": round(prev_chg, 2),
            "signal_vol_r": round(prev_vol, 2),
            "signal_price": round(entry_price, 3),
            "entry_vol_r": round(prev_vol, 2),
            "buy_mode": "signal_close",
        }
        trades.append({
            **sig,
            "entry_date": d0["time"],
            "entry_price": round(entry_price, 3),
            "buy_mode": "signal_close",
            "d1_gap": round((float(d1["open"]) / entry_price - 1) * 100, 2),
            "d1_change": round((float(d1["close"]) / entry_price - 1) * 100, 2),
            **result,
        })
    return trades


# ================================================================
# 枚举方式 B：day（逐日候选 → D0 信号 → 次日开盘入场）
#   day_flow=v1     : 镜像 v1.backtest_stock（见 tmp/_v1_equivalence.py 验收）
#   day_flow=relay3 : 镜像 relay3.backtest_stock（见 tmp/_relay3_equivalence.py 验收）
# ================================================================
def _run_backtest_day_v1(bars, code, spec, ev, board_type, stock_info, use_prefilter):
    """门表版 V1 全历史回测，返回 trades 列表（与 v1.backtest_stock 逐笔等价）。

    编排逐字镜像 v1.backtest_stock：逐日候选判定（门表求值）→ U1~U4 锚定 D0 →
    入场=entry_modes(open + gap 过滤) → 出场=exit_modes(v1_combo，含 V1 动量 D2 清仓)。
    v1 原版无去重 ±4（本路径不施加）；起点/最小长度由 meta.day_start / day_min_n 声明。
    """
    n = len(bars)
    if n < int(spec.meta.get("day_min_n", 30)):
        return []
    _p = spec.params
    trades: List[Dict[str, Any]] = []

    for i in range(int(spec.meta.get("day_start", 25)), n - 1):
        # D0 逐日判定（门表一次性求所有门，与 scan_signals 同一逻辑）
        ok, _ = ev.evaluate_all(bars, i, _p)
        if not ok:
            continue

        # U1~U4（信号日 D0 锚定，v1 prefilter_anchor='signal'）
        if use_prefilter:
            ok, _ = unified_prefilter(bars, i, code, stock_info, spec.market_spec)
            if not ok:
                continue

        # 入场 = entry_modes（open + gap 过滤；阈值来自 YAML entry 块，可引用 params 名）
        d0 = bars[i]
        site, _reason = resolve_entry(spec.entry, bars, i, board_type, _p)
        if site is None:
            continue
        entry_idx = site["entry_idx"]
        entry_price = site["entry_price"]
        entry_date = site["entry_date"]
        d1_gap = site["diag"]["d1_gap"]
        d1_change = site["diag"]["d1_change"]

        # 信号附字段（与 _signal_to_legacy_dict + backtest_stock 完全一致）
        d_1 = bars[i - 1]
        d_2 = bars[i - 2]
        ret_20d = (float(d0["close"]) / float(bars[i - 20]["close"]) - 1) * 100
        d_1_change = (float(d_1["close"]) / float(d_2["close"]) - 1) * 100
        circ = float((stock_info or {}).get("circ_shares") or 0)
        total = float((stock_info or {}).get("total_shares") or 0)
        sig = {
            "code": code,
            "board": get_board_name(code, spec.market_spec),
            "path": "v1",
            "path_label": "V1",
            "d0_date": d0["time"],
            "d0_close": round(float(d0["close"]), 3),
            "ret_20d": round(ret_20d, 2),
            "d_1_change": round(d_1_change, 2),
            "turnover_anchor": round(float(d0["volume"]) / circ * 100, 2) if circ > 0 else None,
            "turnover_anchor_total": round(float(d0["volume"]) / total * 100, 2) if total > 0 else None,
            "buy_mode": "next_open",
        }

        # 出场 = exit_modes（v1_combo；成交语义见 core/exec.py，只此一份）
        d1 = bars[i + 1]
        diag = dict(site["diag"])
        diag["d1_limit_up"] = is_limit_up(float(d1["close"]), float(d0["close"]),
                                        board_type, spec.market_spec)
        bt = run_exit(spec.exit.get("mode", "v1_combo"), bars=bars, entry_idx=entry_idx,
                      entry_price=entry_price, code=code, board_type=board_type,
                      params=_p, diag=diag)
        if not bt:
            continue

        trades.append({
            **sig,
            "entry_date": entry_date,
            "entry_price": round(entry_price, 3),
            "buy_mode": "next_open",
            "d1_change": round(d1_change, 2),
            "d1_gap": round(d1_gap, 2),
            "intraday": round(d1_change - d1_gap, 2),
            **bt,
        })
    return trades


def _run_backtest_day_relay3(bars, code, spec, ev, board_type, stock_info, use_prefilter):
    """门表版 relay3(3板接力) 全历史回测，返回 trades 列表（与 relay3.backtest_stock 逐笔等价）。

    编排逐字镜像 relay3.backtest_stock：逐日 D0 判定（门表: 非北交所 + 恰3连板 + MA多头）
    → U1~U4 锚定 D0 涨停日 → 入场=entry_modes(open, gap -2~9%) → 出场=exit_modes(relay3_s4)。
    参考版 scan_signals 要求 len(bars[:i+1])>=67 → meta.day_start=66 精确复现该下界。
    """
    n = len(bars)
    if n < int(spec.meta.get("day_min_n", 5)):
        return []
    _p = spec.params
    trades: List[Dict[str, Any]] = []

    for i in range(int(spec.meta.get("day_start", 66)), n - 1):
        # D0 逐日判定（门表一次性求所有门，与 relay3.scan_signals 同一逻辑）
        ok, _ = ev.evaluate_all(bars, i, _p)
        if not ok:
            continue

        # U1~U4（锚定 D0 涨停日；relay3 prefilter_anchor='limit_up'）
        if use_prefilter:
            ok, _ = unified_prefilter(bars, i, code, stock_info, spec.market_spec)
            if not ok:
                continue

        # 入场 = entry_modes（open + gap 过滤）
        site, _reason = resolve_entry(spec.entry, bars, i, board_type, _p)
        if site is None:
            continue

        # 出场 = exit_modes（relay3_s4 日线近似；成交语义见 core/exec.py）
        result = run_exit(spec.exit.get("mode", "relay3_s4"), bars=bars,
                          entry_idx=site["entry_idx"], entry_price=site["entry_price"],
                          code=code, board_type=board_type, params=_p, diag=site["diag"])
        if not result or result.get("open"):
            continue  # 参考版跳过开放持仓（数据不足, 只统计已平仓）

        # 信号展示字段（镜像 relay3 calc_features: board_height/ma_bull/lu_vol_ratio/rsi）
        feats = relay3_features(Ctx(bars, i, lu_idx=0, params=_p, board_type=board_type, code=code,
                                    market=spec.market_spec))
        trades.append({
            "code": code,
            "board": get_board_type(code, spec.market_spec),
            "path": spec.key,
            "path_label": spec.meta.get("name", spec.key),
            "signal_date": bars[i]["time"],
            "entry_date": site["entry_date"],
            "entry_price": round(site["entry_price"], 3),
            "buy_mode": "next_open",
            "d1_gap": round(site["diag"]["d1_gap"], 2),
            "lu_date": bars[i]["time"],
            "board_height": feats.get("board_height"),
            "ma_bull": feats.get("ma_bull"),
            "lu_vol_ratio": feats.get("lu_vol_ratio"),
            "rsi": feats.get("rsi"),
            **result,
        })
    return trades


# ================================================================
# 枚举方式 B / day_flow=break（断板接力，D0 确认日 → 次日开盘入场）
# 逐字镜像 break_buy.BreakStrategy.backtest_stock（见 tmp/_break_equivalence.py 验收）
# ================================================================
def _run_backtest_day_break(bars, code, spec, ev, board_type, stock_info, use_prefilter):
    """门表版 break(断板接力) 全历史回测，返回 trades 列表（与 break_buy.backtest_stock 逐笔等价）。

    编排逐字镜像 backtest_stock：确认日必为非涨停 → 廉价预筛(窗口内有涨停) → 门表求值
    → 去重 (streak_start, break_date)（**在 U1~U4 之前**，与参考版同序）→ U1~U4 锚定 D0
    → 入场=entry_modes(open, 无 gap 过滤) → 出场=exit_modes(break_combo)。
    起点 i=4 / 最小长度 6 由 meta.day_start / day_min_n 声明。
    """
    n = len(bars)
    if n < int(spec.meta.get("day_min_n", 6)):
        return []
    _p = spec.params
    max_break_gap = int(_p.get("max_break_gap", 5))
    lu_all = find_limit_ups(bars, board_type, spec.market_spec)
    lu_set = set(lu_all)
    trades: List[Dict[str, Any]] = []
    used = set()

    for i in range(int(spec.meta.get("day_start", 4)), n - 1):
        # 确认日必为非涨停日（断板期最后一天）
        if is_limit_up(float(bars[i]["close"]), float(bars[i - 1]["close"]),
                       board_type, spec.market_spec):
            continue
        # 廉价预过滤：断板期结束于 i → 必存在距 i 不超过 max_break_gap 的涨停日
        if not any(j in lu_set for j in range(max(1, i - max_break_gap), i)):
            continue
        # 逐日候选判定（门表一次性求所有门；Ctx 复用给 bk_struct，命中记忆化）
        ctx = Ctx(bars, i, lu_idx=0, params=_p, board_type=board_type,
                  code=code, stock_info=stock_info, market=spec.market_spec)
        ok, _ = ev.evaluate_all(bars, i, _p, ctx=ctx)
        if not ok:
            continue
        s = bk_struct(ctx)
        if not s:
            continue
        # 去重: 同一连板起点+断板日只取一次（在 U1~U4 之前，与参考版同序）
        key = (s["streak_start_date"], s["break_date"])
        if key in used:
            continue
        used.add(key)

        # U1~U4（锚定确认日 D0；信号日 prefilter_anchor='signal'）
        if use_prefilter:
            ok, _ = unified_prefilter(bars, i, code, stock_info, spec.market_spec)
            if not ok:
                continue

        # 入场 = entry_modes（open，break 无 gap 过滤）
        site, _reason = resolve_entry(spec.entry, bars, i, board_type, _p)
        if site is None:
            continue

        # 出场 = exit_modes（break_combo 收盘价口径）
        result = run_exit(spec.exit.get("mode", "break_combo"), bars=bars,
                          entry_idx=site["entry_idx"], entry_price=site["entry_price"],
                          code=code, board_type=board_type, params=_p, diag=site["diag"])
        if not result:
            continue

        # 信号展示字段（镜像 _signal_to_legacy_dict + scan_signals.extra）
        feats = break_features(ctx, stock_info=stock_info)
        d1 = bars[i + 1]
        prev_close = float(bars[i]["close"])
        trades.append({
            "code": code,
            "board": get_board_name(code, spec.market_spec),
            "path": "break_buy",
            "path_label": spec.meta.get("name", spec.key),
            "mode": "streak_break",
            "streak_len": feats.get("streak_len"),
            "streak_start": feats.get("streak_start"),
            "streak_end": feats.get("streak_end"),
            "break_date": feats.get("break_date"),
            "signal_date": bars[i]["time"],
            "break_days": feats.get("break_days"),
            "break_chg": feats.get("break_chg"),
            "break_gap": feats.get("break_gap"),
            "break_vol_r": feats.get("break_vol_r"),
            "confirm_chg": feats.get("confirm_chg"),
            "confirm_gap": feats.get("confirm_gap"),
            "pre20_gain": feats.get("pre20_gain"),
            "ma_bull": feats.get("ma_bull"),
            "turnover_anchor": feats.get("turnover_anchor"),
            "turnover_sig": feats.get("turnover_sig"),
            "turnover_anchor_total": feats.get("turnover_anchor_total"),
            "turnover_sig_total": feats.get("turnover_sig_total"),
            "entry_price": round(float(site["entry_price"]), 3),
            "buy_mode": "next_open",
            "entry_date": site["entry_date"],
            "d1_change": round((float(d1["close"]) / float(d1["open"]) - 1) * 100, 2)
            if float(d1["open"]) > 0 else 0,
            "d1_gap": round((float(d1["open"]) / prev_close - 1) * 100, 2)
            if prev_close > 0 else 0,
            "intraday": round((float(d1["close"]) - float(d1["open"])) / prev_close * 100, 2)
            if prev_close > 0 else 0,
            **result,
        })
    return trades


# ================================================================
# 枚举方式 B / day_flow=g56（五重共振，D-1 信号 → D0 开盘入场 + 锁仓去重）
# 逐字镜像 g56.G56Strategy.backtest_stock（见 tmp/_g56_equivalence.py 验收）
# ================================================================
def _run_backtest_day_g56(bars, code, spec, ev, board_type, stock_info, use_prefilter):
    """门表版 g56(五重共振) 全历史回测，返回 trades 列表（与 g56.backtest_stock 逐笔等价）。

    编排逐字镜像 backtest_stock：北交所/长度早返回 → 特征 O(n) 一次预计算 → 横截面池聚合
    → 逐日 s（信号日 k=s-1）→ 锁仓去重（s <= last_exit_idx 跳过，**在门表之前**）→ 门表求值
    → 入场=entry_modes(open + gap_max 分板块) → 出场=exit_modes(g56_no_trail 无追踪 7d/-8%)
    → 锁仓至退出日。g56 无 U1~U4（use_unified_prefilter=False）。
    起点/终点由 meta.day_start(68) / day_end(9) 声明（镜像 range(68, n-9)）。
    """
    from app.market_cn.auto.strategies.g56 import _ensure_pool_daily, _g1_arrays

    if str(code).startswith(("8", "4", "92")) or len(bars) < 68:
        return []
    _p = spec.params
    n = len(bars)
    # 特征一次预计算（镜像修复① O(n^2)→O(n)）；池按 pool_target 跨股缓存复用
    ext = {"g56_feats": _g1_arrays(bars),
           "g56_pool": _ensure_pool_daily(str(bars[-1]["time"])[:10])}
    trades: List[Dict[str, Any]] = []
    last_exit_idx = -1

    for s in range(int(spec.meta.get("day_start", 68)), n - int(spec.meta.get("day_end", 9))):
        if float(bars[s].get("open") or 0) <= 0 or s <= last_exit_idx:
            continue                        # 锁仓去重（镜像修复②：未退出前不重复入场）
        i = s - 1                           # 信号日 D-1
        ctx = Ctx(bars, i, lu_idx=0, params=_p, board_type=board_type, code=code,
                  stock_info=stock_info, ext=ext, market=spec.market_spec)
        ok, _ = ev.evaluate_all(bars, i, _p, ctx=ctx)
        if not ok:
            continue

        # 入场 = entry_modes（open + gap_max；gap >= 涨停幅度 → 一字/触板不可买）
        site, _reason = resolve_entry(spec.entry, bars, i, board_type, _p)
        if site is None:
            continue

        # 出场 = exit_modes（g56_no_trail，无追踪 7d/-8%）
        result = run_exit(spec.exit.get("mode", "g56_no_trail"), bars=bars,
                          entry_idx=site["entry_idx"], entry_price=site["entry_price"],
                          code=code, board_type=board_type, params=_p, diag=site["diag"])
        if not result:
            continue

        sig = build_signal(ctx, spec)
        ed = s + int(result["exit_day"]) - 1
        trades.append({
            "code": code,
            "board": board_type,
            "strategy": spec.key,
            "signal_date": str(bars[i]["time"])[:10],
            "entry_date": str(bars[s]["time"])[:10],
            "entry_price": round(float(bars[s]["open"]), 3),
            "entry_gap": round(float(site["diag"]["d1_gap"]), 2),
            "exit_date": str(bars[ed]["time"])[:10]
            if 0 < int(result["exit_day"]) and ed < n else None,
            "exit_price": result["exit_price"],
            "exit_day": result["exit_day"],
            "return_pct": result["return_pct"],
            "peak_return_pct": result["peak_return_pct"],
            **sig,
            "buy_mode": "next_open",
        })
        last_exit_idx = ed
    return trades
