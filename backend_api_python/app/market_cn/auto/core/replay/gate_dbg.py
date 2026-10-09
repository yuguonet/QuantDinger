"""core/replay/gate_dbg.py — 门诊断视图收集器（终态② Step 1）。

让事件流折叠路径（replay→evaluate）也能产出门诊断（``gate_dbg`` 全门布尔向量），
使 ``tools/explain.py`` 的漏斗/rule_audit 能在 replay 路径上复现 —— 消除
「链 A run_backtest 独占门诊断」的依赖。

聚合语义与原 ``tools/explain.GateCollector`` 逐位一致（该器已随 Step 3 退役）:
  - day 类策略（break/g56）：每天 fire ``all`` 一次。
  - limit_up 类（dragon）：每天 fire ``qualify`` 一次 + 每个 lu 候选 fire ``decision``。
  - ``merged()`` → ``{(code, i): {gate_id: bool}}``（decision 保留最后一次）。

回调签名 ``(phase, code, i, lu_idx, vec)`` 由策略在 evaluate 内经
``GateEvaluator(gate_dbg=ctx["_gate_dbg"])`` 自动触发，``i`` 为**绝对索引**
（策略侧已对齐链 A 口径），与原 ``GateCollector.merged()`` 的聚合 key 一致。

用法（与 TraceCollector 同构，replay 已内置注入）:
    gc = GateDebugCollector()
    feed = DailyFeed(bars, ctx_provider=gc.wrap_ctx_provider(base_provider))
    res = replay(strategy, code, feed, collectors=[TradesCollector(...), gc])
    res.gates  # {(code, i): {gate_id: bool}}
"""
from __future__ import annotations


class GateDebugCollector:
    """门诊断收集器：``ctx["_gate_dbg"]`` 注入的回调，聚合全门布尔向量。"""

    def __init__(self, code: str = ""):
        self.code = code
        self.by_key = {}       # (code, i) -> {phase: vec}

    def __call__(self, phase, code, i, lu_idx, vec):
        d = self.by_key.setdefault((code, i), {})
        d[phase] = dict(vec)

    def merged(self):
        """→ {(code, i): {gate_id: bool}}（仅当日至少 fire 过一次的键；与 GateCollector 同义）。"""
        out = {}
        for key, d in self.by_key.items():
            if "all" in d:
                out[key] = dict(d["all"])
                continue
            v = {}
            if "qualify" in d:
                v.update(d["qualify"])
            if "decision" in d:
                v.update(d["decision"])
            out[key] = v
        return out

    # ---- ctx 注入面（供 DailyFeed / replay 组合）----
    def wrap_ctx_provider(self, base):
        """包一层：在 base(ctx) 结果上注入 ``_gate_dbg``（同一回调，全程复用）。"""
        def provider(idx, bars):
            ctx = base(idx, bars) if base is not None else None
            ctx = dict(ctx) if ctx else {}
            ctx["_gate_dbg"] = self
            return ctx
        return provider

    def clear(self) -> None:
        self.by_key.clear()

    # ---- collectors 契约（no-op；门向量走 ctx["_gate_dbg"] 注入，非事件流）----
    def feed(self, idx: int, ev) -> None:
        pass

    def finish(self, feed) -> None:
        pass
