"""test_trace.py — 门原因通道 TraceSink 单元门禁（改进方案 §2.4，P0）。

验三件事：
  1. TraceSink 收集/JSONL 导出/清空的基本契约（一行一记录，透传 ok）；
  2. dragon evaluate 接线：ctx["_trace"] 存在时门原因进 sink（含落选原因）；
  3. **零行为差异**：带 sink 与不带 sink 的 fold 事件流逐位一致（P0 的核心约束）。

易错点:
  - fuzz 数据产不出信号/门调用 ⇒ [] == [] 假绿；必须用 crafted_bars（有意外板 31
    的落选记录可断言）。
  - 断言落选记录要认 `ok is False` 而非 `reason` 非空（reason 文案可变）。
"""

from __future__ import annotations

import json

from app.market_cn.auto.core.present.contract import DayInput, InsufficientHistory
from app.market_cn.auto.core.trace import TraceSink
from app.market_cn.auto.strategies.dragon_callback import DragonCallbackStrategy

from .test_dragon import CODE, crafted_bars


def _fold(strategy, bars, ctx=None):
    """最小 stateless fold（同 fold_range 折叠序，可注入 ctx）。"""
    state, j = None, 0
    while j < len(bars):
        try:
            state = strategy.init_state(CODE, bars[:j])
            break
        except InsufficientHistory:
            j += 1
    assert state is not None, "seed 失败"
    evs = []
    for k in range(j, len(bars)):
        evs.extend(strategy.evaluate(state, DayInput(CODE, bars[k], ctx), None) or [])
        state = strategy.step(state, bars[k])
    return evs


def test_sink_records_and_jsonl(tmp_path):
    sink = TraceSink(code="600000", strategy="t")
    sink.begin_day("2026-01-05")
    assert sink.gate(False, "门2 未过", cand="2025-12-01") is False   # 透传
    assert sink.gate(True, None) is True
    sink.note("no_signal")
    assert len(sink.records) == 3
    assert sink.records[0] == {"t": "2026-01-05", "ok": False, "reason": "门2 未过",
                               "code": "600000", "cand": "2025-12-01"}
    lines = sink.to_jsonl().strip().splitlines()
    assert [json.loads(x)["reason"] for x in lines] == ["门2 未过", None, None]
    # dump 追加写 + 自动建目录
    p = str(tmp_path / "sub" / "t.jsonl")
    assert sink.dump_jsonl(p) == 3
    assert sink.dump_jsonl(p) == 3
    with open(p, encoding="utf-8") as f:
        assert len(f.read().strip().splitlines()) == 6
    sink.clear()
    assert sink.records == [] and sink.to_jsonl() == ""


def test_dragon_evaluate_writes_trace():
    """ctx['_trace'] 存在 → 门原因进 sink（含落选）；crafted_bars 的意外板必出落选记录。"""
    s = DragonCallbackStrategy()
    bars = crafted_bars()
    sink = TraceSink(code=CODE, strategy=s.key)
    _fold(s, bars, ctx={"_trace": sink})
    assert sink.records, "dragon 门调用未写入 trace（接线失效）"
    assert any(r["ok"] is False for r in sink.records), \
        "无落选记录 —— crafted_bars 的意外板 31 应产生 gate(False, ...)"

def test_trace_sink_zero_behavior_diff():
    """带 sink 与不带 sink 的 fold 事件流逐位一致（P0 核心约束：通道只旁路收集）。"""
    s = DragonCallbackStrategy()
    bars = crafted_bars()
    evs_plain = _fold(s, bars, ctx=None)
    evs_trace = _fold(s, bars, ctx={"_trace": TraceSink(code=CODE)})
    key = lambda e: (e.stage, e.date, sorted(e.payload.items()), e.next_realtime)
    assert [key(e) for e in evs_plain] == [key(e) for e in evs_trace]


# ================================================================
# P2 — TraceCollector + probe 适配器
# ================================================================
def test_trace_collector_wraps_ctx_provider():
    """TraceCollector.wrap_ctx_provider 在 base ctx 上注入 _trace（同 sink 复用）。"""
    from app.market_cn.auto.core.replay import TraceCollector
    tc = TraceCollector(code=CODE, strategy="dragon")

    def base(idx, bars):
        return {"mkt_gain": 1.5}

    provider = tc.wrap_ctx_provider(base)
    ctx = provider(0, [])
    assert ctx["mkt_gain"] == 1.5          # base 字段保留
    assert ctx["_trace"] is tc.sink         # 注入的是同一个 sink
    assert provider(1, [])["_trace"] is tc.sink   # 全程复用，非每次新建

    # base=None 也可包装
    ctx2 = tc.wrap_ctx_provider(None)(0, [])
    assert ctx2 == {"_trace": tc.sink}


def test_trace_collector_collector_contract():
    """TraceCollector 满足 replay 的 collectors 契约（feed/finish 为 no-op）。"""
    from app.market_cn.auto.core.replay import TraceCollector
    tc = TraceCollector(code=CODE, strategy="dragon")
    tc.feed(0, object())     # 不抛
    tc.finish(None)          # 不抛
    assert tc.records == []  # 门原因走 ctx 注入，不经事件流


def test_probe_adapter_double_writes_sink():
    """probe.Probe 旧 API 落盘 JSONL 的同时双写 TraceSink（P2 适配器）。"""
    from app.market_cn.auto.probe import Probe, DayTrace
    dt = DayTrace()
    dt.trace("prefilter", code="000001")
    assert len(dt.items) == 1
    assert len(dt.sink.records) == 1          # 双写

    import tempfile
    d = tempfile.mkdtemp()
    pr = Probe("dragon", tag="t", out_dir=d)
    pr.trace("step1", code="x")
    pr.sample(code="x", d0_date="2026-10-07")
    pr.shell("ctx", foo=1)
    assert pr.counts == {"trace": 1, "sample": 1, "shell": 1}
    assert len(pr.sink.records) == 3          # 双写同源
    pr.close()
    with open(pr.path, encoding="utf-8") as f:
        lines = [json.loads(x) for x in f.read().strip().splitlines()]
    # JSONL 落盘格式不变（kind/strategy/ts + 载荷），rule_audit 消费面零破坏
    assert [x["kind"] for x in lines] == ["trace", "sample", "shell"]
