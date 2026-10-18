# -*- coding: utf-8 -*-
"""F12 接线守卫：轮界主动压缩（C2，2026-10-17）。

背景（合并总账 :180 / 主题 C2）：mimo 的 MimocodeAgent 是**模型决定式**压缩
（`_do_compact` 只由 Compact 工具触发，`compaction_threshold_tokens` 仅作为
`<context_usage>` 提示语的分母）⇒ **没有任何自动阈值触发**。于是跨轮时：
turn2 的每一次模型调用都要重发 turn1 的全量（含 4477 字报告）≈20k token/次，
而轮界**无任何主动裁剪**。

F12 修法：在**轮界**（`_run_inner` 里清完瞬态污染之后、进 ReAct 主循环之前）
主动调用 mimo 既有的 `_do_compact()`。它重建
`[system…, boundary, anchor(首轮 user 原始问题，护住注入的金例/资料块), summary]`，
summary 由 `_QD_COMPACT_NOTES` 约束**必须保留**数字+来源、标的代码、已有结论、
未完事项、用户偏好。

铁律（防降智降能力边界）：
  ① 只复用 mimo 既有压缩机件，不另造丢失口径；
  ② 非首轮 + 历史确实超阈值才触发 —— 短对话零行为变化；
  ③ fail-open：压缩失败保留原历史，绝不让整轮崩；
  ④ 可一键回退：`QD_TURN_COMPACT=0`。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
QD_AGENT = BACKEND / "app" / "agent" / "qd_agent.py"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """每个用例从"无 F12 env"的干净态开始。"""
    for var in ("QD_TURN_COMPACT", "QD_TURN_COMPACT_THRESHOLD", "QD_TURN_COMPACT_RATIO"):
        monkeypatch.delenv(var, raising=False)
    yield


def _msg(role: str, content: str = "", **kw) -> dict:
    m = {"role": role, "content": content}
    m.update(kw)
    return m


def _prior_round(big: bool = True) -> list:
    """一段"已完结的工具轮"历史：system + 首轮 user + assistant(tool_call) + tool + assistant。"""
    payload = ("宽表数据。" * 400) if big else "小数据。"
    return [
        _msg("system", "你是量化助手。"),
        _msg("user", "分析600519"),
        _msg("assistant", "", tool_calls=[{
            "id": "c1", "type": "function",
            "function": {"name": "get_realtime_quote", "arguments": "{}"}}]),
        _msg("tool", payload, tool_call_id="c1"),
        _msg("assistant", "结论：估值偏高，观望。"),
    ]


def _stand(messages, *, raise_on_compact: bool = False):
    """BudgetMixin 的最小 stand：只提供 messages / _do_compact / trace。"""
    from app.agent.qd_budgeting import BudgetMixin

    class _S(BudgetMixin):
        def _do_compact(self):  # 替身：计数 + 可选抛错
            self.compact_calls += 1
            if self._raise:
                raise RuntimeError("compaction boom")

    s = _S()
    s.messages = messages
    s.compact_calls = 0
    s._raise = raise_on_compact
    s.trace = None
    return s


# ── ① 阈值口径 ────────────────────────────────────────────────────────

def test_threshold_default_ratio_and_override(monkeypatch):
    """默认按窗口比例（0.30）；显式 env 优先；`QD_TURN_COMPACT=0` 关闭 ⇒ 0。"""
    from app.agent.qd_config import _turn_compact_threshold, _context_window

    assert _turn_compact_threshold() == int(_context_window() * 0.30), \
        "默认阈值必须按上下文窗口比例算出（非硬编码绝对值）"
    monkeypatch.setenv("QD_TURN_COMPACT_THRESHOLD", "5000")
    assert _turn_compact_threshold() == 5000
    monkeypatch.setenv("QD_TURN_COMPACT", "0")
    assert _turn_compact_threshold() == 0, "关闭时必须返回 0（哨兵值）"


# ── ② 触发条件 ────────────────────────────────────────────────────────

def test_fires_on_large_prior_history(monkeypatch):
    """非首轮 + 历史超阈值 ⇒ 主动压缩一次，并返回压缩前 token 估算。"""
    monkeypatch.setenv("QD_TURN_COMPACT_THRESHOLD", "50")
    s = _stand(_prior_round(big=True))
    pre = s._maybe_turn_compact()
    assert pre > 0, "超过阈值时应返回压缩前 token 估算（>0）"
    assert s.compact_calls == 1, "应恰好调用一次 _do_compact"


def test_skips_first_turn(monkeypatch):
    """首轮（无任何 assistant/tool 消息）不压缩 —— 无可压缩的已完结轮。"""
    monkeypatch.setenv("QD_TURN_COMPACT_THRESHOLD", "1")
    s = _stand([_msg("system", "s"), _msg("user", "分析600519" + "x" * 5000)])
    assert s._maybe_turn_compact() == 0
    assert s.compact_calls == 0


def test_skips_when_below_threshold(monkeypatch):
    """历史未超阈值 ⇒ 不压缩（短对话零行为变化）。"""
    monkeypatch.setenv("QD_TURN_COMPACT_THRESHOLD", "100000")
    s = _stand(_prior_round(big=False))
    assert s._maybe_turn_compact() == 0
    assert s.compact_calls == 0


def test_disabled_by_env(monkeypatch):
    """`QD_TURN_COMPACT=0` ⇒ 一键回退，永不压缩。"""
    monkeypatch.setenv("QD_TURN_COMPACT", "0")
    s = _stand(_prior_round(big=True))
    assert s._maybe_turn_compact() == 0
    assert s.compact_calls == 0


def test_fail_open_on_compact_error(monkeypatch):
    """压缩抛异常 ⇒ 吞掉并返回 0，绝不让整轮崩（保留原历史）。"""
    monkeypatch.setenv("QD_TURN_COMPACT_THRESHOLD", "50")
    s = _stand(_prior_round(big=True), raise_on_compact=True)
    assert s._maybe_turn_compact() == 0
    assert s.compact_calls == 1, "既然尝试了就该记到调用（然后被 fail-open 吞掉）"


# ── ③ 接线不变量 ──────────────────────────────────────────────────────

def test_run_inner_calls_turn_compact():
    """AST：`_run_inner` 必须在进主循环前调用 `_maybe_turn_compact`。

    防"好心删回去"——F12 的全部价值就在这个轮界调用点上。
    """
    src = QD_AGENT.read_text(encoding="utf-8")
    tree = ast.parse(src)
    nodes = list(tree.body)
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            nodes.extend(node.body)
    fn = next((n for n in nodes
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "_run_inner"), None)
    assert fn is not None, "qd_agent.py 找不到 _run_inner"
    body = ast.unparse(fn)
    assert "_maybe_turn_compact" in body, \
        "_run_inner 必须调用 _maybe_turn_compact（F12 轮界主动压缩）"
    assert body.index("_maybe_turn_compact") < body.index("super().run("), \
        "轮界压缩必须在 super().run()（ReAct 主循环）之前"
