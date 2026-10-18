# -*- coding: utf-8 -*-
"""user_query 纯度守卫（2026-10-17，F23 发现）。

【病灶】F9 把 per-turn 动态上下文（`[当前时间]` + `[工具地图]`）前置进本轮 task
（`qd_tool_face._dynamic_context_text`）。但 `qd_agent._run_inner` 里
`_make_collector(task)` 被排在 `_dynamic_context_text(task)` **之后** ⇒ collector
拿到的 user_query 带上了注入头。

实测证据（`app/agent/traces/preselect_pairs.jsonl`，298 条）：F9 落地后新增的 42 条
全部以 `[当前时间]` 开头（qd_smoke 直调路径不经 `_prefetch`，故没有
`[用户问题]` 标记可剥），而 F9 之前的 256 条干净。

【影响面】`collector.user_query` 是两条下游的唯一来源：
  ① `qd_agent_traces.user_query`（人工复盘"用户到底问了什么"）；
  ② `preselect_stats.record_task_calls` → pairs JSONL（共现权重训练数据 + held-out 召回）。
⇒ 污染会静默劣化预选权重与召回归因。

【根因修法】让 `_make_collector` 在 `_dynamic_context_text` **之前** 拿到原始 task
（一行重排，零净增行）。`trace.run_start` 仍用 F9 后的 task（审计"模型看到了什么"）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agent.qd_aftercare import AftercareMixin  # noqa: E402


class _S(AftercareMixin):
    """只挂 AftercareMixin —— `_make_collector` 不依赖其它 mixin 的实例状态。"""


# ── ① 行为：带 [用户问题] 标记的 prefetch task → 剥出用户原话 ──────────────
def test_make_collector_strips_prefetch_marker():
    s = _S()
    task = ("[当前时间] 2026-10-17 10:00:00\n[工具地图]\n[必注入…]\n  x\n\n"
            "[相关技能：auto-stock-analyze]\n\n[用户问题]\n茅台今天多少钱")
    c = s._make_collector(task)
    assert c is not None
    assert c.user_query == "茅台今天多少钱"


# ── ② 行为：朴素 task → 原样（无标记可剥，不误伤）─────────────────────────
def test_make_collector_keeps_plain_task():
    s = _S()
    c = s._make_collector("茅台今天多少钱")
    assert c is not None
    assert c.user_query == "茅台今天多少钱"


# ── ③ 源码顺序：collector 必须先于动态上下文前置 ─────────────────────────
def test_run_inner_orders_collector_before_dynamic_context():
    src = (ROOT / "app" / "agent" / "qd_agent.py").read_text(encoding="utf-8")
    i_col = src.index("_make_collector(task)")
    i_dyn = src.index("_dynamic_context_text(task)")
    assert i_col < i_dyn, (
        "`_make_collector(task)` 必须在 `_dynamic_context_text(task)` 之前，"
        "否则 [当前时间]/[工具地图] 注入头会污染 collector.user_query")
