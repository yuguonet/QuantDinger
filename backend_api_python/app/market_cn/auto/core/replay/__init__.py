"""core/replay — 唯一「走历史」的程序（改进方案 §2.3，P1 落地）。

Excel 比喻：本模块 = 「统计/透视表」与「历史小计」共用的那台计算引擎。
  - 回测视图 = replay + TradesCollector
  - 调试视图 = replay + TraceCollector（P2）
  - 生产视图 = 每日 fold 直接产事件，**不经 replay**

三条不变量（违反任意一条即退化成第二份回测）:
  ① **不写第二份编排** —— 折叠一律走 `core.present.runner.fold_range` / `_fold_one`；
  ② **不做判定** —— 入场/出场判定全在策略 `evaluate`，本模块只把事件链配成对；
  ③ **即产即收（流式）** —— 收集器在 fold 推进时消费事件，不落盘后重读。

易错点:
  - `fold_range(stages=None)` 才是回测口径（全链）；默认 `("ready",)` 是生产投影口径。
  - knife/tail 的 evaluate 需要 `ctx["series"]` 快照序列 ⇒ 纯日线 feed 不触发，
    必须走 IntradayFeed（P1.5）。这不是折叠缺陷，是 feed 缺口。
  - **市场门是 fail-closed**：`ctx.mkt_gain` 为 None 时 knife/tail 的门直接拒
    （knife_catch._gates:328）⇒ 回放不供给 mkt_gain = 零 trade 且不报错。
    市场门输入分两层（2026-10-09 评估后收口）：
      ① **逐槽 as-of**（正解）= `load_market_slots()` → `ctx["mkt_series"]`（盘中折叠用，
         与生产 `scan._mkt_gain` 同口径；消除「用收盘门控 14:56 入场」的前视）；
      ② **日频 close 横截面** = `market_gain()` / `load_market_gain()` → `ctx["mkt_gain"]`
         （仅作 ① 缺失时的回退，勿再当主源 —— 见 docs/市场门口径评估_20261009.md）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from app.market_cn.auto.core.present.runner import fold_range
from app.market_cn.auto.core.replay.gate_dbg import GateDebugCollector
from app.market_cn.auto.core.replay.trade_map import build_trade, is_self_closed

#: 未闭合链的收尾原因（对齐旧 backtest_stock 口径，勿改文案——golden 依赖）
REASON_DATA_END = "数据结束平仓"


def market_gain(bars_by_code: dict[str, list[dict]], min_n: int = 50) -> dict[str, float]:
    """批量日线 → {date: 全市场均涨幅%}（横截面，与 `frames.mkt_gain` 同口径）。

    ⚠ **日频口径**（当日 close，全天恒定）。盘中市场门的**正解**是逐槽 as-of
    `load_market_slots()`（`ctx["mkt_series"]`）；本函数只作其缺失时的**回退**
    （用收盘门控 14:56 入场 = 前视 —— 见 docs/市场门口径评估_20261009.md）。

    盘中策略的市场门 ``mkt_gate`` 是 **fail-closed**：``ctx.mkt_gain`` 为 None 时门
    直接返回 None（永不触发）。日线回放没有全市场分钟帧 ⇒ 必须由横截面日线供给，
    否则 knife/tail **零 trade 且不报错**（实测 120 票 × 300 日 = 16098 个 watch，
    零 ready —— 极易被误判为"样本没有信号"）。

    Args:
        min_n: 当日样本数下限；不足则该日不给值（宁可 fail-closed，不伪造市场门）。
    """
    acc: dict[str, list] = {}
    for bars in bars_by_code.values():
        prev_c = None
        for b in bars or ():
            c = float(b.get("close") or 0)
            if prev_c and prev_c > 0 and c > 0:
                d = str(b.get("time"))[:10]
                a = acc.get(d)
                if a is None:
                    acc[d] = [(c / prev_c - 1) * 100, 1]
                else:
                    a[0] += (c / prev_c - 1) * 100
                    a[1] += 1
            prev_c = c
    return {d: round(s / n, 3) for d, (s, n) in acc.items() if n >= min_n}


def load_market_gain(start: str, end: str, *, days: int = 300,
                     min_n: int = 200) -> dict[str, float]:
    """全市场日线均涨幅（单票回放注入 mkt_gain 用；一次批量取数）。

    单票 `replay` 没有横截面可算 ⇒ 走本函数。**失败返回 {}**（市场门随之
    fail-closed，零触发）—— 不静默伪造，与项目「静默降级是头号敌人」一致。
    """
    try:
        from app.market_cn.auto.core.data.hub import all_codes
        from app.market_cn.auto.core.data.window_cache import load_windows
        got = load_windows(list(all_codes() or []), days=days) or {}
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("[replay] 全市场日线取数失败: %s", e)
        return {}
    lo, hi = str(start)[:10], str(end)[:10]
    return market_gain({c: [b for b in bs if lo <= str(b.get("time"))[:10] <= hi]
                        for c, bs in got.items()}, min_n=min_n)


class DailyFeed:
    """日线 feed：bars → 折叠事件流（**全阶段**，不只 ready）。

    ctx_provider: 可选 ``callable(idx, bars) -> dict | None``，给 evaluate 供 ctx
    （intraday 策略的快照序列；纯日线策略不需要）。
    """

    def __init__(self, bars: list[dict], *, ctx_provider: Callable | None = None,
                 lo: int = 0, hi: int | None = None,
                 mkt_map: dict[str, float] | None = None):
        self.bars = bars
        self.lo = lo
        self.hi = hi
        self.mkt_map = mkt_map or {}
        # 默认 ctx：注入 mkt_gain（盘中策略的市场门依赖；日线策略不读，无害）。
        # 显式传入 ctx_provider 优先 —— IntradayFeed 用它补 series。
        self.ctx_provider = ctx_provider or self._ctx_for

    @property
    def exec_basis(self) -> str:
        return "daily"

    def _ctx_for(self, k: int, bars: list[dict]) -> dict:
        """默认 ctx：当日全市场均涨幅（无横截面数据时为 None ⇒ 市场门 fail-closed）。"""
        if not self.mkt_map:
            return {}
        g = self.mkt_map.get(str(bars[k].get("time"))[:10])
        return {} if g is None else {"mkt_gain": g}

    def run(self, strategy, code: str, *, trace_sink=None,
            gate_dbg=None) -> list[tuple[int, object]]:
        """产出 [(bar 下标, Progress)] —— 唯一折叠路径。

        ⚠ 必须 ``stateful=True``：exec/exit 链依赖 prev 传递，stateless（prev=None）
        下策略永远不产 exec —— 回测会静默空转（不报错、零 trade）。

        trace_sink: 注入 ``ctx["_trace"]``（门原因，调试视图）。gate_dbg: 注入
        ``ctx["_gate_dbg"]``（全门向量，门漏斗视图，终态② Step 1）。两者均不影响判定。
        """
        provider = self.ctx_provider
        extra = {}
        if trace_sink is not None:
            extra["_trace"] = trace_sink
        if gate_dbg is not None:
            extra["_gate_dbg"] = gate_dbg
        if extra:
            base = self.ctx_provider

            def provider(idx, bars):
                ctx = base(idx, bars) if base is not None else None
                ctx = dict(ctx) if ctx else {}
                ctx.update(extra)
                return ctx
        return fold_range(strategy, code, self.bars, self.lo, self.hi,
                          stages=None, ctx_provider=provider,
                          stateful=True)

    def last_bar(self):
        return self.bars[-1] if self.bars else None

    def index_of_date(self, date: str) -> int:
        for i, b in enumerate(self.bars):
            if str(b.get("time"))[:10] == str(date)[:10]:
                return i
        return -1


@dataclass
class ReplayResult:
    """一次回放的结果（三视图共用同一结构，各取所需字段）。"""
    code: str
    strategy: str
    trades: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)
    # 门诊断向量 {(code, i): {gate_id: bool}}（终态② Step 1，GateDebugCollector 产出）
    gates: dict = field(default_factory=dict)


class TraceCollector:
    """调试视图收集器：通过 ``ctx["_trace"]`` 注入 TraceSink，收集门原因链。

    P2 落地（改进方案 §2.4/§406）。与 TradesCollector 不同，门原因**不是事件**，
    而是策略在 evaluate 内、通过 ``ctx["_trace"]`` 上的 ``sink.gate(ok, reason)``
    主动打点（P0 已接线 dragon/g56）。本收集器不碰事件流 —— 它只负责：
      ① 提供一个 ``ctx_provider`` 包装，把同一个 sink 注入每次 fold 的 ctx；
      ② 在回放结束后把 sink 的 records 交回 ``ReplayResult.trace``。

    ⚠ 未接 ``_trace`` 打点的策略（knife/tail/break，P3 才抽血）在回放时 records
    为空 —— 这是「未接线」而非「无落选」，调用方须与 TradesCollector 区分：
    门原因缺失 ≠ 零 trade。别把空 trace 当结论。

    用法（replay 已内置，无需手工）:
        tc = TraceCollector(code, strategy)
        res = replay(strategy, code, feed, collectors=[TradesCollector(...), tc])
        res.trace  # [{t, ok, reason, ...}]
    """

    def __init__(self, code: str = "", strategy: str = ""):
        from app.market_cn.auto.core.trace import TraceSink
        self.code = code
        self.strategy = strategy
        self.sink = TraceSink(code=code, strategy=strategy)

    # ---- ctx 注入面（供 DailyFeed / replay 组合）----
    def wrap_ctx_provider(self, base):
        """包一层：在 base(ctx) 结果上注入 ``_trace``（同一 sink，全程复用）。

        base 可为 ``callable(idx, bars)->dict|None`` 或 None。返回新 provider。
        """
        sink = self.sink

        def provider(idx, bars):
            ctx = base(idx, bars) if base is not None else None
            ctx = dict(ctx) if ctx else {}
            ctx["_trace"] = sink
            return ctx
        return provider

    # ---- 结果面 ----
    @property
    def records(self) -> list[dict]:
        return self.sink.records

    def clear(self) -> None:
        self.sink.clear()

    # ---- collectors 契约（no-op；门原因走 ctx["_trace"] 注入，非事件流）----
    def feed(self, idx: int, ev) -> None:
        pass

    def finish(self, feed) -> None:
        pass


class TradesCollector:
    """事件链 → canonical trade（流式：逐事件 feed，结束 finish）。

    链匹配（一次实现，改进方案 §2.3）:
        ready → exec → exit           三段链（dragon/g56）：trade 由 exit 产出
        ready → exec(自带出场)        两段链（knife/tail）：trade 由 exec 产出
    断链规则:
      - exec 且 buyable=False ⇒ gap 越界不入场，链终止（对齐旧 entry_decision 口径）；
      - 回放结束仍未平 ⇒ 末日收盘平仓，原因 `数据结束平仓`（对齐旧 backtest_stock）；
      - 旧链未平时收到新 ready ⇒ 保留旧链、不开新链（对齐旧「持仓去重」语义）。
    """

    def __init__(self, code: str, strategy: str, *, exec_basis: str = "daily"):
        self.code = code
        self.strategy = strategy
        self.exec_basis = exec_basis
        self.trades: list[dict] = []
        self._chain: dict | None = None

    # ---- 流式消费 ----
    def feed(self, idx: int, ev) -> None:
        stage = getattr(ev, "stage", "")
        pl = getattr(ev, "payload", None) or {}
        date = getattr(ev, "date", "")

        if stage == "ready":
            if self._chain is not None:
                return                      # 持仓中不开新链（对齐旧去重口径）
            self._chain = {"ready_date": date, "ready_pl": pl}
            return

        if self._chain is None:
            return                          # 无信号不入场

        if stage == "exec":
            if is_self_closed(pl):          # 两段链：exec 自带出场
                self._emit(exec_date=date, exec_pl=pl, exit_pl=None)
                return
            if pl.get("buyable") is False:  # gap 越界：不入场
                self._chain = None
                return
            self._chain["exec_date"] = date
            self._chain["exec_pl"] = pl
            return

        if stage == "exit":
            self._emit(exec_date=self._chain.get("exec_date"),
                       exec_pl=self._chain.get("exec_pl"),
                       exit_pl=pl)

    # ---- 收尾 ----
    def finish(self, feed: DailyFeed | None = None) -> None:
        """回放结束：未平链按末日收盘平仓（对齐旧口径）。"""
        chain, self._chain = self._chain, None
        if chain is None or "exec_pl" not in chain:
            return                          # 只有 ready / 未入场 ⇒ 不产 trade
        if feed is None or not feed.bars:
            return
        exec_pl = chain["exec_pl"] or {}
        entry_price = float(exec_pl.get("entry_price") or 0)
        if entry_price <= 0:
            return
        ei = feed.index_of_date(exec_pl.get("entry_date") or chain.get("exec_date") or "")
        if ei < 0:
            ei = 0
        last = feed.bars[-1]
        exit_price = float(last.get("close") or 0)
        if exit_price <= 0:
            return
        peak = max((float(b.get("high") or 0) for b in feed.bars[ei:]), default=exit_price)
        self.trades.append(build_trade(
            code=self.code, strategy=self.strategy,
            ready_date=chain.get("ready_date"), ready_pl=chain.get("ready_pl"),
            exec_date=chain.get("exec_date"), exec_pl=exec_pl,
            exit_pl={
                "exit_date": last.get("time"),
                "exit_price": exit_price,
                "exit_day": len(feed.bars) - ei,
                "reason": REASON_DATA_END,
                "return_pct": round((exit_price / entry_price - 1) * 100, 2),
                "peak_return_pct": round((peak / entry_price - 1) * 100, 2),
            },
            exec_basis=self.exec_basis,
        ))

    # ---- 内部 ----
    def _emit(self, *, exec_date, exec_pl, exit_pl) -> None:
        chain, self._chain = self._chain, None
        self.trades.append(build_trade(
            code=self.code, strategy=self.strategy,
            ready_date=chain.get("ready_date") if chain else None,
            ready_pl=chain.get("ready_pl") if chain else None,
            exec_date=exec_date, exec_pl=exec_pl, exit_pl=exit_pl,
            exec_basis=self.exec_basis,
        ))


def replay_batch(strategy, bars_by_code: dict[str, list[dict]], *,
                 collectors: dict[str, list], lo: int = 0,
                 hi: int | None = None, ctx_provider=None) -> dict[str, ReplayResult]:
    """跨票批量回放 —— 需要 ``begin_day`` 横截面池的策略（g56）走本入口。

    与 `DailyRunner.advance_all` 同构（每日 begin_day → 逐票推进），差异只在
    「全历史回放」对「1 日延伸」。推进仍复用 `_fold_one` 唯一定义，prev 语义与
    `_fold_step` 一致（`events[-1]`，无事件则保持）。

    Args:
        bars_by_code: {code: bars}，各票日期可不同长度（停牌天然缺席）。
        collectors:   {code: [collector, ...]}，逐票收集器（流式 feed）。
        ctx_provider: 可选 ``callable(code, date, bars) -> dict``，补 ctx（如快照序列）。
    """
    from app.market_cn.auto.core.present.contract import InsufficientHistory
    from app.market_cn.auto.core.present.runner import _fold_one

    mkt_map = market_gain(bars_by_code)          # 横截面市场门（fail-closed 依赖）

    # TraceSink：若任一票挂了 TraceCollector，则全程注入同一 sink（跨票共享收集）。
    trace_sink = None
    for cl in collectors.values():
        for c in cl:
            if isinstance(c, TraceCollector):
                trace_sink = c.sink
                break
        if trace_sink is not None:
            break

    # 门诊断回调（终态② Step 1）：任一票挂了 GateDebugCollector → 全程注入同一回调。
    # 聚合 key 含 code，跨票共享一个回调不冲突（与 GateCollector 同语义）。
    gate_dbg = None
    for cl in collectors.values():
        for c in cl:
            if isinstance(c, GateDebugCollector):
                gate_dbg = c
                break
        if gate_dbg is not None:
            break

    states: dict[str, dict] = {}
    prevs: dict[str, object] = {}
    start: dict[str, int] = {}
    idx_of: dict[str, dict[str, int]] = {}

    # ---- seed（与 fold_range 同逻辑：起点尽量早，不足则后滑）----
    for code, bars in bars_by_code.items():
        if not bars:
            continue
        h = len(bars) - 1 if hi is None else min(hi, len(bars) - 1)
        j, st = lo, None
        while j <= h:
            try:
                st = strategy.init_state(code, bars[:j])
                break
            except InsufficientHistory:
                j += 1
        if st is None:
            continue
        states[code], prevs[code], start[code] = st, None, j
        idx_of[code] = {str(b.get("time"))[:10]: i for i, b in enumerate(bars)}

    # ---- 日期轴：并集（与 rebuild._date_axis 同口径）----
    axis = sorted({d for m in idx_of.values() for d in m})

    results = {code: ReplayResult(code=code,
                                  strategy=getattr(strategy, "key", "") or "")
               for code in states}
    for date in axis:
        day_bars = {}
        for code in states:
            i = idx_of[code].get(date)
            if i is not None and i >= start[code]:
                day_bars[code] = bars_by_code[code][i]
        if not day_bars:
            continue
        # begin_day 契约（对齐 runner.advance_all:562）：states 与 bars **同键**，
        # 只含当日参与推进的票 —— 停牌缺席的票不进池，也不被池统计。
        day_ctx = strategy.begin_day(
            date, {c: states[c] for c in day_bars}, day_bars) or {}
        for code, bar in day_bars.items():
            ctx = {"_day": day_ctx}
            if date in mkt_map:
                ctx["mkt_gain"] = mkt_map[date]
            if trace_sink is not None:
                ctx["_trace"] = trace_sink
            if gate_dbg is not None:
                ctx["_gate_dbg"] = gate_dbg
            if ctx_provider is not None:
                ctx.update(ctx_provider(code, date, bars_by_code[code]) or {})
            i = idx_of[code][date]
            try:
                st, events, head = _fold_one(strategy, code, states[code], bar, ctx,
                                             prevs[code])
            except InsufficientHistory:
                st, events, head = strategy.step(states[code], bar), [], None
            states[code] = st
            if head is not None:
                prevs[code] = head
            for e in events:
                results[code].events.append(
                    {"idx": i, "stage": getattr(e, "stage", ""),
                     "date": getattr(e, "date", ""),
                     "payload": getattr(e, "payload", None) or {}})
                for c in collectors.get(code) or []:
                    c.feed(i, e)

    # ---- 收尾：未平链按末日平仓 ----
    for code in states:
        feed = DailyFeed(bars_by_code[code], lo=start[code], mkt_map=mkt_map)
        for c in collectors.get(code) or []:
            c.finish(feed)
            if hasattr(c, "trades"):
                results[code].trades = c.trades
            if isinstance(c, TraceCollector):
                results[code].trace = c.records
    if gate_dbg is not None:
        # 跨票共享同一 GateDebugCollector：merged() 后按 code 拆分回各票结果。
        merged = gate_dbg.merged()
        for code in states:
            results[code].gates = {k: v for k, v in merged.items() if k[0] == code}
    return results


def replay(strategy, code: str, feed: DailyFeed, *,
           collectors: list) -> ReplayResult:
    """唯一回放入口：折叠一次，各收集器即产即收。

    collectors 中每个对象需实现 ``feed(idx, ev)`` 与 ``finish(feed)``。
    若含 ``TraceCollector``，其 sink 自动注入 ``ctx["_trace"]``（调试视图）。
    若含 ``GateDebugCollector``，其回调自动注入 ``ctx["_gate_dbg"]``（门漏斗视图）。
    """
    trace_sink = next((c.sink for c in collectors
                       if isinstance(c, TraceCollector)), None)
    gate_dbg = next((c for c in collectors
                     if isinstance(c, GateDebugCollector)), None)
    res = ReplayResult(code=code, strategy=getattr(strategy, "key", "") or "")
    for idx, ev in feed.run(strategy, code, trace_sink=trace_sink, gate_dbg=gate_dbg):
        res.events.append({"idx": idx, "stage": getattr(ev, "stage", ""),
                           "date": getattr(ev, "date", ""),
                           "payload": getattr(ev, "payload", None) or {}})
        for c in collectors:
            c.feed(idx, ev)
    for c in collectors:
        c.finish(feed)
        if hasattr(c, "trades"):
            res.trades = c.trades
        if isinstance(c, TraceCollector):
            res.trace = c.records
        if isinstance(c, GateDebugCollector):
            res.gates = c.merged()
    return res
