# -*- coding: utf-8 -*-
"""超预算软收尾(P0) + 观测噪声修复(P1/P2) 的复现与守卫（2026-10-14）。

【P0 事故】会话内「现在买什么股?」烧穿 per-run token 预算 →
`qd_budgeting._account_budget` 抛 `LimitsExceeded(budget_wrapup_hint(...))`；
mimo `BaseAgent.run` 把提示串当本轮 answer 返回，而成本计数器**故意不归零**
⇒ 任何重试轮一进 after_step 立刻再次超限 ⇒ 用户只拿到控制文案，
前面多步已取到的 screener 数据**全被丢弃**（trace: run_token:234895/200000, 10 步零结论）。
修法：`_run_inner` 识别 `is_budget_wrapup` → `_budget_summary_turn`
（绕过主循环、清空工具面、一次纯文字收尾，把已取数据变成部分结论）。

【P1】`audit.trace_adapter.on_step` 旧实现每步遍历 `msgs[-8:]`，同一条 assistant
消息在后续多步被反复 emit（同一 tool_call_id 出现在 step1/2/3）⇒ 工具调用被放大数倍。
修法：游标只摘本步新增消息。

【P2a】`qd_aftercare._plan_lint` 在正常 Idle 收尾也告警"计划未清账"（模型常把 plan
当 TODO、不回写 completed）⇒ 噪声。修法：仅异常收尾（截断/熔断/溯源失败/空回复）告警。

【P2b】`preselect_stats.record_empty_face`：① 闸门(route=gate)有意跳过却计入
empty_rate 分母；② 无最小样本下限 ⇒ 打招呼 1/1=100% 即 WARNING。修法：gate 单列不计、
加最小样本下限。

⚠ 指纹/接线漂移即红：改任何控制文案或收尾路径都需同步本文件。
"""
from __future__ import annotations

import ast
import importlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest


# ── P0-a：熔断提示指纹判定 ─────────────────────────────────────────

def test_is_budget_wrapup_detects_only_budget_hint() -> None:
    cfg = importlib.import_module("app.agent.qd_config")
    from app.agent.utils.budget import budget_wrapup_hint

    hint = budget_wrapup_hint("run_token:234895/200000")
    assert cfg.is_budget_wrapup(hint)
    assert cfg.is_budget_wrapup("  " + hint + "\n")
    # 其它瞬态控制消息**不是**预算熔断（走不同处理路径）
    assert not cfg.is_budget_wrapup("（本轮达到步数上限被截断——请把问题拆小分步重试）")
    assert not cfg.is_budget_wrapup("【数字溯源拦截】x")
    assert not cfg.is_budget_wrapup("Empty assistant response")
    assert not cfg.is_budget_wrapup("请用文字直接给出本轮结论（不要再调用工具）。")
    # 普通文本不误伤
    assert not cfg.is_budget_wrapup("分析002866")
    assert not cfg.is_budget_wrapup("")
    assert not cfg.is_budget_wrapup(None)
    # 预算前缀同时属于瞬态控制（可被下一轮 purge 清掉，不污染）
    assert cfg.is_transient_control(hint)


# ── P0-b：软收尾实现 ───────────────────────────────────────────────

class _FakeModel:
    def __init__(self, content: str = "部分结论：已取到 A=1，B 未获取。", boom: bool = False):
        self.content = content
        self.boom = boom
        self.calls: list = []

    def query(self, messages, **kwargs):
        self.calls.append((list(messages), dict(kwargs)))
        if self.boom:
            raise RuntimeError("model down")
        return {"content": self.content}


def _agent_stand(model) -> object:
    """构造一个**不走 __init__** 的 QDAgent 替身（免模型/环境冷启动）。

    `get_model_query_kwargs(no_tools=True)` 走 ToolFaceMixin 真实现（直接返回 {}），
    因此只需给它 messages / model / msg_path 三个属性。
    """
    qd_agent = importlib.import_module("app.agent.qd_agent")
    obj = qd_agent.QDAgent.__new__(qd_agent.QDAgent)
    obj.messages = []
    obj.model = model
    obj.msg_path = None  # base.add_message 会读它
    return obj


def test_budget_summary_turn_produces_partial_answer() -> None:
    from app.agent.utils.budget import budget_wrapup_hint

    hint = budget_wrapup_hint("run_token:234895/200000")
    model = _FakeModel()
    obj = _agent_stand(model)
    obj.messages = [
        {"role": "system", "content": "base"},
        {"role": "user", "content": "现在买什么股?"},
        {"role": "assistant", "content": "调用了 market_screener"},
        {"role": "tool", "content": "候选：603727 601808"},
        {"role": "user", "content": hint},          # mimo 终止时追加的控制消息
    ]
    status, text = obj._budget_summary_turn(hint)

    assert status == "budget_wrapped"
    assert text == model.content, "应把模型的部分结论作为最终答复"
    # 收尾查询**必须清空工具面**（否则又会取数、又超预算）
    assert model.calls and model.calls[0][1] == {}, model.calls
    # 控制消息被摘掉；收尾 prompt 与答复进了上下文
    assert hint not in [m.get("content") for m in obj.messages]
    assert any(m["role"] == "user" and "软收尾" in (m.get("content") or "") for m in obj.messages)
    assert obj.messages[-1]["role"] == "assistant"
    assert obj.messages[-1]["content"] == model.content
    # 收尾 prompt 前缀命中瞬态指纹 ⇒ 下一轮会被 purge，不跨轮污染
    prompt = next(m["content"] for m in obj.messages
                  if m["role"] == "user" and "软收尾" in (m.get("content") or ""))
    cfg = importlib.import_module("app.agent.qd_config")
    assert cfg.is_transient_control(prompt)


def test_budget_summary_turn_falls_back_when_empty() -> None:
    from app.agent.utils.budget import budget_wrapup_hint

    hint = budget_wrapup_hint("run_token:1/2")
    obj = _agent_stand(_FakeModel(content=""))
    obj.messages = [{"role": "user", "content": hint}]
    status, text = obj._budget_summary_turn(hint)
    assert status == "truncated" and text == hint


def test_budget_summary_turn_falls_back_on_error() -> None:
    from app.agent.utils.budget import budget_wrapup_hint

    hint = budget_wrapup_hint("run_token:1/2")
    obj = _agent_stand(_FakeModel(boom=True))
    obj.messages = [{"role": "user", "content": hint}]
    status, text = obj._budget_summary_turn(hint)
    assert status == "truncated" and text == hint


# ── P0-c：_run_inner 接线守卫 ──────────────────────────────────────

def _run_inner_called_names() -> set:
    src = (Path(__file__).resolve().parents[1] / "app" / "agent" / "qd_agent.py").read_text(
        encoding="utf-8")
    tree = ast.parse(src)
    names: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_run_inner":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    fn = sub.func
                    nm = getattr(fn, "id", None) or getattr(fn, "attr", None)
                    if nm:
                        names.add(nm)
    return names


def test_run_inner_wires_budget_wrapup() -> None:
    names = _run_inner_called_names()
    assert "is_budget_wrapup" in names, "预算熔断判定没接线到 run 主干"
    assert "_budget_summary_turn" in names, "预算软收尾没接线到 run 主干"


# ── P0-d：no_tools 清空工具面 ──────────────────────────────────────

def test_get_model_query_kwargs_no_tools_is_empty() -> None:
    qd_tool_face = importlib.import_module("app.agent.qd_tool_face")
    obj = SimpleNamespace()
    assert qd_tool_face.ToolFaceMixin.get_model_query_kwargs(obj, no_tools=True) == {}


# ── P1：trace 工具事件不重复上报 ───────────────────────────────────

def test_trace_on_step_does_not_reemit_history(tmp_path) -> None:
    from app.agent.audit.trace_adapter import TraceAdapter

    ta = TraceAdapter(out_dir=tmp_path)
    msgs = [{"role": "system", "content": "base"}]
    agent = SimpleNamespace(
        messages=msgs,
        tool_registry=SimpleNamespace(get_function_definitions=lambda: []),
        config=SimpleNamespace(system_template=""),
        model=None,
    )
    ta.run_start(agent, "task")

    msgs.append({"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "function": {"name": "get_realtime_quote", "arguments": "{}"}}]})
    msgs.append({"role": "tool", "name": "get_realtime_quote",
                 "tool_call_id": "c1", "content": "ok"})
    ta.on_step(agent, 1)

    msgs.append({"role": "assistant", "content": "", "tool_calls": [
        {"id": "c2", "function": {"name": "technical_analysis", "arguments": "{}"}}]})
    msgs.append({"role": "tool", "name": "technical_analysis",
                 "tool_call_id": "c2", "content": "ok"})
    ta.on_step(agent, 2)

    recs = [json.loads(l) for l in ta.path.read_text(encoding="utf-8").splitlines()]
    tool_calls = [r for r in recs if r["event"] == "tool_call"]
    assert [r["tool_call_id"] for r in tool_calls] == ["c1", "c2"], \
        f"历史工具调用被重复上报：{[r['tool_call_id'] for r in tool_calls]}"
    assert [r["step"] for r in tool_calls] == [1, 2], "步号归属错乱"
    assert len([r for r in recs if r["event"] == "tool_result"]) == 2


# ── P2a：plan-lint 仅异常收尾告警 ──────────────────────────────────

def _plan_stand() -> SimpleNamespace:
    qd_agent = importlib.import_module("app.agent.qd_agent")

    class _T:
        def __init__(self):
            self.events = []

        def emit(self, event, **f):
            self.events.append((event, f))

    s = SimpleNamespace()
    s.trace = _T()
    s.current_plan = [{"step": "看技术面", "status": "in_progress"}]
    s.plan_explanation = ""
    s._agent_cls = qd_agent.QDAgent
    return s


def test_plan_lint_warns_only_on_abnormal_status(caplog) -> None:
    qd_aftercare = importlib.import_module("app.agent.qd_aftercare")

    # 正常收尾：落 trace 事件，但**不**刷 WARNING
    normal = _plan_stand()
    with caplog.at_level(logging.WARNING, logger="app.agent.qd_aftercare"):
        normal._agent_cls._plan_lint(normal, "Idle")
    assert normal.trace.events and normal.trace.events[0][0] == "plan_lint"
    assert normal.trace.events[0][1]["status"] == "Idle"
    assert not [r for r in caplog.records if "plan-lint" in r.getMessage()], \
        "正常收尾不该告警（噪声）"

    # 异常收尾：必须 WARNING（真信号）
    caplog.clear()
    abnormal = _plan_stand()
    with caplog.at_level(logging.WARNING, logger="app.agent.qd_aftercare"):
        abnormal._agent_cls._plan_lint(abnormal, "budget_wrapped")
    assert [r for r in caplog.records if "plan-lint" in r.getMessage()], \
        "异常收尾必须告警"
    assert abnormal.trace.events and abnormal.trace.events[0][0] == "plan_lint"


# ── P2b：preselect-empty 统计口径 ──────────────────────────────────

def test_preselect_gate_not_counted_in_empty_rate() -> None:
    ps = importlib.import_module("app.agent.preselect.preselect_stats")
    ps.reset()
    try:
        ps.record_empty_face(query="你好，介绍一下自己", route="gate")
        assert ps.empty_rate() == 0.0, "闸门有意跳过不该计入空域率"
        rep = ps.report()
        assert rep["total"] == 0 and rep["gate_skip"] == 1
    finally:
        ps.reset()


def test_preselect_empty_face_no_warning_on_single_sample(caplog) -> None:
    ps = importlib.import_module("app.agent.preselect.preselect_stats")
    ps.reset()
    try:
        with caplog.at_level(logging.WARNING,
                             logger="app.agent.preselect.preselect_stats"):
            ps.record_empty_face(query="帮我看看", route="no_pick")
        assert not [r for r in caplog.records if "preselect-empty-face" in r.getMessage()], \
            "单样本(1/1=100%)不该 WARNING（无最小样本下限）"
        assert ps.empty_rate() > 0  # 但仍计数
    finally:
        ps.reset()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
