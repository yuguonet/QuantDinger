# -*- coding: utf-8 -*-
"""F16 契约：`ToolResult` 只管「给模型的文本」，结构化数据归 `metadata`。

【病灶 · 实测 2026-10-17】
  · `ToolResult`（`tools/_base.py`）只有 `success/output/error` 三字段，**无
    `metadata` 槽**；`output: Any` 同时承载结构化数据，再由 `to_str()` 在**同一
    字段**上转成给模型的文本 —— 「给 LLM 的文本」与「给审计的结构化事实」被混在
    一起，没有边界。
  · 上游侧则**已经分栏**：`_fn_adapter.py` 返回
    `ToolOutput(output=text, metadata={tool,domain,dropped_params})`，我方
    `ToolResult` 没跟上 ⇒ 两套契约并行，**trace 记的 ≠ 模型拿到的**。
  · `_fn_adapter._to_output` 的 else 兜底（`tools/_fn_adapter.py:105`）是
    **101 个工具的唯一执行出口**，动它就是动全站 ⇒ 必须先锁「文本逐字等价」。

【修法】① `ToolResult` 增 `metadata: dict = field(default_factory=dict)`，
`to_str()` **绝不序列化 metadata**；② 新增工厂 `ToolResult.from_raw(result,
metadata=None)`；③ `_fn_adapter._to_output` 改用它，**输出文本逐字不变**。

【tracing 分工】两套事件流各管各的，不得互相越界：
  · `utils/tracing.AgentTraceRecorder.record()` —— 审计树事件流（含 qd_agent_traces 落库）；
  · `audit/trace_adapter.TraceAdapter.emit()` —— qd_traces 事件摘取，**只写 JSONL、不碰库**。
"""
from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
AGENT = BACKEND / "app" / "agent"

from app.agent.tools._base import ToolResult
from app.agent.tools._fn_adapter import FnToolAdapter

# 覆盖所有序列化分支：标量 / None / bool / 纯键值 / 含 null / 列表 / 表（list[dict]）/ 深嵌套 / 空
_SAMPLES = [
    1, 1.5, "s", None, True, False,
    {"a": 1},
    {"a": 1, "b": None},
    [1, 2, 3],
    [{"x": 1}, {"x": 2}],
    {"nested": {"k": [1, {"z": 2}]}},
    {},
    [],
]


def _legacy_text(result):
    """**改造前的口径**（oracle）：直接构造 ToolResult 再 to_str。

    这条路径的构造语义不因 F16 而变（metadata 缺省空 dict），故可作冻结 oracle。
    """
    return ToolResult(output=result).to_str()


# ─────────────────────────────────── ToolResult 契约

def test_toolresult_has_metadata_field_default_empty():
    """`metadata` 必须是独立字段、默认空 dict（且是 dataclass 字段，可 asdict）。"""
    names = {f.name for f in dataclasses.fields(ToolResult)}
    assert "metadata" in names, f"ToolResult 缺 metadata 槽（现字段：{names}）"
    d = dataclasses.asdict(ToolResult())
    assert d["metadata"] == {}, d
    assert set(d) == {"success", "output", "error", "metadata"}, d


def test_metadata_is_not_serialized_into_text():
    """`to_str()` 是「给 LLM 的文本」，**绝不能**把 metadata 混进去。"""
    plain = ToolResult(output={"a": 1})
    with_meta = ToolResult(output={"a": 1}, metadata={"secret_marker": "ZZZ"})
    assert with_meta.to_str() == plain.to_str()
    assert "ZZZ" not in with_meta.to_str()
    assert "secret_marker" not in with_meta.to_str()


def test_from_raw_splits_text_and_metadata():
    """`from_raw` 把结构化数据放 output、审计事实放 metadata，success 默认 True。"""
    r = ToolResult.from_raw({"k": 1}, {"tool": "t", "domain": "common"})
    assert r.output == {"k": 1}
    assert r.metadata == {"tool": "t", "domain": "common"}
    assert r.success is True
    r2 = ToolResult.from_raw([1, 2])
    assert r2.output == [1, 2] and r2.metadata == {}


@pytest.mark.parametrize("sample", _SAMPLES)
def test_from_raw_text_equals_legacy(sample):
    """★ 等价性红线：`from_raw(x).to_str()` 与改造前 `ToolResult(output=x).to_str()`
    **逐字相同**（这是敢动 101 个工具唯一出口的前提）。"""
    assert ToolResult.from_raw(sample).to_str() == _legacy_text(sample)


# ─────────────────────────────────── 适配器出口（:105 改点）

def test_adapter_output_text_unchanged():
    """★ 直接对唯一的执行出口断言：文本与改造前口径逐字一致。"""
    adapter = FnToolAdapter(lambda: None, domain="common")
    md = {"tool": "demo", "domain": "common"}
    for sample in _SAMPLES:
        out = adapter._to_output(sample, md)
        assert out.output == _legacy_text(sample), sample
        assert out.success is True
        assert out.metadata == md, "适配器自带 metadata 必须原样带出"


def test_adapter_carries_result_metadata():
    """工具返回带 metadata 的 ToolResult 时，结构化事实必须并入出站 metadata。"""
    adapter = FnToolAdapter(lambda: None, domain="common")
    r = ToolResult(output={"a": 1}, metadata={"rows": 5, "source": "db"})
    out = adapter._to_output(r, {"tool": "demo", "domain": "common"})
    assert out.output == _legacy_text({"a": 1})
    assert out.metadata["rows"] == 5 and out.metadata["source"] == "db"
    assert out.metadata["tool"] == "demo"


def test_adapter_failed_result_targets_error_text():
    """失败分支的文本口径不变，且 metadata 照旧带出。"""
    adapter = FnToolAdapter(lambda: None, domain="common")
    out = adapter._to_output(ToolResult(success=False, error="boom"), {"tool": "demo"})
    assert out.output == "[工具执行失败] boom"
    assert out.success is False


def test_adapter_error_dict_unchanged():
    adapter = FnToolAdapter(lambda: None, domain="common")
    out = adapter._to_output({"error": "bad"}, {"tool": "demo"})
    assert out.output == "Error: bad" and out.success is False


# ─────────────────────────────────── tracing 两流分工契约

def _module_names(tree):
    mods = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.append(node.module)
    return mods


def test_trace_adapter_does_not_touch_db():
    """`TraceAdapter` 只「摘事件」写 JSONL，**不碰库**（落 qd_traces 是另一条支链的活）。"""
    src = (AGENT / "audit" / "trace_adapter.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    db_mods = [m for m in _module_names(tree)
               if m.startswith(("psycopg", "psycopg2", "sqlalchemy"))
               or m.startswith(("app.services", "app.config", "app.db"))]
    assert not db_mods, f"TraceAdapter 不该引入 DB 依赖：{db_mods}"
    execs = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("execute", "executemany", "commit"):
            execs.append(node.lineno)
    assert not execs, f"TraceAdapter 出现了 DB 执行调用：{execs}"


def test_tracing_two_streams_naming_contract():
    """两条事件流各用各的入口名，不许互相冒充（record vs emit）。"""
    from app.agent.audit.trace_adapter import TraceAdapter
    from app.agent.utils.tracing import AgentTraceRecorder

    assert hasattr(TraceAdapter, "emit")
    assert not hasattr(TraceAdapter, "record"), "TraceAdapter 不是审计树记录器，不该有 record"
    assert hasattr(AgentTraceRecorder, "record")
    assert not hasattr(AgentTraceRecorder, "emit"), "AgentTraceRecorder 不该冒充 TraceAdapter"


def test_trace_collector_names_real_recorder_class():
    """`trace_collector.py` 里对上游记录器的称呼必须是真实类名 `AgentTraceRecorder`。

    ★ 历史误称：注释把它写成不存在的 `utils/tracing.AgentTracer`，追代码的人会被
      带沟里（同名类在全仓根本不存在）。
    """
    src = (AGENT / "trace_collector.py").read_text(encoding="utf-8")
    assert "AgentTracer" not in src, "trace_collector 仍把记录器误称为不存在的 AgentTracer"
    assert "AgentTraceRecorder" in src
