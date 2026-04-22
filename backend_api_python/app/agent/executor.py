# -*- coding: utf-8 -*-
"""
AgentExecutor — ReAct agent with tool calling.

Builds system prompts, manages conversation, delegates to run_agent_loop.
Mirrors the architecture from daily_stock_analysis's executor.py.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from app.agent.runner import (
    RunLoopResult,
    run_agent_loop,
    parse_dashboard_json,
)
from app.agent.tools.registry import ToolRegistry
from app.utils.logger import get_logger

logger = get_logger(__name__)

# ── Startup dependency check ─────────────────────────────────
try:
    import json_repair  # noqa: F401
except ImportError:
    logger.warning("json_repair not installed — dashboard JSON parsing will be less resilient. "
                   "Install with: pip install json-repair>=0.15.0")

# ── Token budget ─────────────────────────────────────────────
CONTEXT_TOKEN_BUDGET = int(os.getenv("AGENT_CONTEXT_TOKEN_BUDGET", "12000"))


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~3 chars per token for mixed zh/en."""
    if not text:
        return 0
    return max(1, len(text) // 3)


def _truncate_context_if_needed(messages: List[Dict], budget: int = CONTEXT_TOKEN_BUDGET) -> List[Dict]:
    """Keep total message list under token budget by trimming oldest messages.

    Always preserves: system message (index 0) + last user message (tail).
    Trims from the middle (conversation history).
    """
    total = sum(_estimate_tokens(m.get("content", "") or "") for m in messages)
    if total <= budget:
        return messages

    if len(messages) <= 2:
        for m in messages:
            c = m.get("content") or ""
            if len(c) > 2000:
                m["content"] = c[:2000] + "\n... (truncated)"
        return messages

    system_msg = messages[0]
    tail_msg = messages[-1]
    middle = messages[1:-1]

    total = (_estimate_tokens(system_msg.get("content", "") or "") +
             _estimate_tokens(tail_msg.get("content", "") or ""))
    kept_middle = []
    for m in reversed(middle):
        t = _estimate_tokens(m.get("content", "") or "")
        if total + t > budget:
            break
        total += t
        kept_middle.append(m)

    kept_middle.reverse()
    result = [system_msg] + kept_middle + [tail_msg]
    logger.info("Context trimmed: %d messages -> %d (est. %d tokens)", len(messages), len(result), total)
    return result


# ── Agent result ──────────────────────────────────────────────

@dataclass
class AgentResult:
    success: bool = False
    content: str = ""
    dashboard: Optional[Dict[str, Any]] = None
    tool_calls_log: List[Dict[str, Any]] = field(default_factory=list)
    total_steps: int = 0
    total_tokens: int = 0
    provider: str = ""
    model: str = ""
    error: Optional[str] = None


# ── System prompts ────────────────────────────────────────────

AGENT_SYSTEM_PROMPT = """你是一位专业的金融投资分析 Agent，拥有数据工具和交易技能，负责生成专业的【决策仪表盘】分析报告。

## 工作流程（必须严格按阶段顺序执行，每阶段等工具结果返回后再进入下一阶段）

**第一阶段 · 行情与K线**（首先执行）
- `get_realtime_quote` 获取实时行情
- `get_daily_history` 获取历史K线

**第二阶段 · 技术与筹码**（等第一阶段结果返回后执行）
- `analyze_trend` 获取技术指标
- `get_chip_distribution` 获取筹码分布（仅A股）
- `get_volume_analysis` 分析量能

**第三阶段 · 情报搜索**（等前两阶段完成后执行）
- `search_stock_news` 搜索最新资讯、减持、业绩预告等风险信号

**第四阶段 · 生成报告**（所有数据就绪后，输出完整决策仪表盘 JSON）

> ⚠️ 每阶段的工具调用必须完整返回结果后，才能进入下一阶段。禁止将不同阶段的工具合并到同一次调用中。
{skill_section}
## 规则

1. **必须调用工具获取真实数据** — 绝不编造数字，所有数据必须来自工具返回结果。
2. **系统化分析** — 严格按工作流程分阶段执行。
3. **输出格式** — 最终响应必须是有效的决策仪表盘 JSON。
4. **风险优先** — 必须排查风险（股东减持、业绩预警、监管问题）。
5. **工具失败处理** — 记录失败原因，使用已有数据继续分析，不重复调用失败工具。

## 输出格式：决策仪表盘 JSON

```json
{{
    "stock_name": "股票名称",
    "sentiment_score": 0-100整数,
    "trend_prediction": "强烈看多/看多/震荡/看空/强烈看空",
    "operation_advice": "买入/加仓/持有/减仓/卖出/观望",
    "decision_type": "buy/hold/sell",
    "confidence_level": "高/中/低",
    "dashboard": {{
        "core_conclusion": {{
            "one_sentence": "一句话核心结论（30字以内）",
            "signal_type": "🟢买入信号/🟡持有观望/🔴卖出信号/⚠️风险警告",
            "position_advice": {{"no_position": "空仓者建议", "has_position": "持仓者建议"}}
        }},
        "data_perspective": {{
            "trend_status": {{"ma_alignment": "", "is_bullish": true, "trend_score": 0}},
            "price_position": {{"current_price": 0, "ma5": 0, "ma10": 0, "ma20": 0}},
            "volume_analysis": {{"volume_ratio": 0, "volume_status": ""}},
            "chip_structure": {{"profit_ratio": "", "avg_cost": "", "chip_health": ""}}
        }},
        "intelligence": {{
            "latest_news": "",
            "risk_alerts": [],
            "positive_catalysts": []
        }},
        "battle_plan": {{
            "sniper_points": {{"ideal_buy": "", "secondary_buy": "", "stop_loss": "", "take_profit": ""}},
            "action_checklist": []
        }}
    }},
    "analysis_summary": "100字综合分析摘要",
    "risk_warning": "风险提示",
    "buy_reason": "操作理由",
    "technical_analysis": "技术面分析",
    "news_summary": "新闻摘要"
}}
```

{language_section}"""

CHAT_SYSTEM_PROMPT = """你是一位专业的金融投资分析 Agent，拥有数据工具和交易技能，负责解答用户的股票投资问题。

## 分析工作流程（必须严格按阶段执行）

当用户询问某支股票时，按以下阶段顺序调用工具：

**第一阶段 · 行情与K线**
- `get_realtime_quote` + `get_daily_history`

**第二阶段 · 技术与筹码**
- `analyze_trend` + `get_chip_distribution` + `get_volume_analysis`

**第三阶段 · 情报搜索**
- `search_stock_news`

**第四阶段 · 综合分析**
- 基于数据输出投资建议

> ⚠️ 禁止将不同阶段的工具合并到同一次调用中。
{skill_section}
## 规则
1. **必须调用工具获取真实数据** — 绝不编造数字。
2. **自由对话** — 根据用户问题组织语言回答，不需要输出 JSON。
3. **风险优先** — 必须排查风险。

{language_section}"""


def _build_language_section(language: str) -> str:
    if str(language or "").lower().startswith("en"):
        return "\n## Output Language\n- Reply in English.\n- All JSON values in English.\n"
    return "\n## 输出语言\n- 使用中文回答。\n- 所有面向用户的文本值使用中文。\n"


# ── In-memory conversation manager (delegates to SessionStore) ──

class _ConversationManager:
    """Conversation history backed by SessionStore (Redis or memory). Thread-safe."""

    def __init__(self, max_turns: int = 20):
        self._max_turns = max_turns
        self._local_fallback: Dict[str, List[Dict]] = {}
        self._lock = threading.Lock()

    def _get_store(self):
        try:
            from app.agent.session_store import get_session_store
            return get_session_store()
        except Exception:
            return None

    def get_history(self, session_id: str) -> List[Dict]:
        store = self._get_store()
        if store:
            return store.get_history(session_id)
        with self._lock:
            return list(self._local_fallback.get(session_id, []))

    def add_message(self, session_id: str, role: str, content: str):
        store = self._get_store()
        if store:
            store.add_message(session_id, role, content, max_turns=self._max_turns)
            return
        with self._lock:
            if session_id not in self._local_fallback:
                self._local_fallback[session_id] = []
            self._local_fallback[session_id].append({"role": role, "content": content})
            max_msgs = self._max_turns * 2
            if len(self._local_fallback[session_id]) > max_msgs:
                self._local_fallback[session_id] = self._local_fallback[session_id][-max_msgs:]

    def clear(self, session_id: str):
        store = self._get_store()
        if store:
            store.clear_history(session_id)
            return
        with self._lock:
            self._local_fallback.pop(session_id, None)


_conversations = _ConversationManager()


# ── AgentExecutor ─────────────────────────────────────────────

class AgentExecutor:
    """ReAct agent loop with tool calling.

    Usage::

        executor = AgentExecutor(tool_registry, call_with_tools_fn)
        result = executor.run("分析股票 600519")
    """

    def __init__(
        self,
        tool_registry: ToolRegistry,
        call_with_tools_fn: Callable,
        skill_instructions: str = "",
        max_steps: int = 10,
        timeout_seconds: Optional[float] = None,
    ):
        self.tool_registry = tool_registry
        self.call_with_tools_fn = call_with_tools_fn
        self.skill_instructions = skill_instructions
        self.max_steps = max_steps
        self.timeout_seconds = timeout_seconds

    def run(self, task: str, context: Optional[Dict[str, Any]] = None) -> AgentResult:
        """Execute the agent loop for analysis (dashboard JSON output)."""
        skill_section = ""
        if self.skill_instructions:
            skill_section = f"## 激活的交易技能\n\n{self.skill_instructions}\n"

        language = (context or {}).get("report_language", "zh")
        system_prompt = AGENT_SYSTEM_PROMPT.format(
            skill_section=skill_section,
            language_section=_build_language_section(language),
        )

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": self._build_analysis_user_message(task, context)},
        ]

        return self._run_loop(messages, parse_dashboard=True)

    def chat(
        self,
        message: str,
        session_id: str,
        context: Optional[Dict[str, Any]] = None,
        progress_callback: Optional[Callable] = None,
    ) -> AgentResult:
        """Execute the agent loop for free-form chat."""
        skill_section = ""
        if self.skill_instructions:
            skill_section = f"## 激活的交易技能\n\n{self.skill_instructions}\n"

        language = (context or {}).get("report_language", "zh")
        system_prompt = CHAT_SYSTEM_PROMPT.format(
            skill_section=skill_section,
            language_section=_build_language_section(language),
        )

        # Build messages: system + history + context injection + new message
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
        ]

        # Inject conversation history
        history = _conversations.get_history(session_id)
        messages.extend(history)

        # Inject previous analysis context if provided
        if context:
            context_parts = []
            if context.get("stock_code"):
                context_parts.append(f"股票代码: {context['stock_code']}")
            if context.get("stock_name"):
                context_parts.append(f"股票名称: {context['stock_name']}")
            if context.get("previous_analysis_summary"):
                summary = context["previous_analysis_summary"]
                text = json.dumps(summary, ensure_ascii=False) if isinstance(summary, dict) else str(summary)
                context_parts.append(f"上次分析摘要:\n{text}")
            if context_parts:
                ctx_msg = "[系统提供的历史分析上下文]\n" + "\n".join(context_parts)
                messages.append({"role": "user", "content": ctx_msg})
                messages.append({"role": "assistant", "content": "好的，我已了解该股票的历史分析数据。请告诉我你想了解什么？"})

        messages.append({"role": "user", "content": message})

        # Persist user message
        _conversations.add_message(session_id, "user", message)

        result = self._run_loop(messages, parse_dashboard=False, progress_callback=progress_callback)

        # Persist assistant reply
        if result.success:
            _conversations.add_message(session_id, "assistant", result.content)
        else:
            _conversations.add_message(session_id, "assistant",
                                        f"[分析失败] {result.error or '未知错误'}")
        return result

    def _run_loop(
        self,
        messages: List[Dict[str, Any]],
        parse_dashboard: bool,
        progress_callback: Optional[Callable] = None,
    ) -> AgentResult:
        # Enforce token budget before sending to LLM
        messages = _truncate_context_if_needed(messages)

        loop_result = run_agent_loop(
            messages=messages,
            tool_registry=self.tool_registry,
            call_with_tools_fn=self.call_with_tools_fn,
            max_steps=self.max_steps,
            progress_callback=progress_callback,
            max_wall_clock_seconds=self.timeout_seconds,
        )

        model_str = loop_result.model

        if parse_dashboard and loop_result.success:
            dashboard = parse_dashboard_json(loop_result.content)
            return AgentResult(
                success=dashboard is not None,
                content=loop_result.content,
                dashboard=dashboard,
                tool_calls_log=loop_result.tool_calls_log,
                total_steps=loop_result.total_steps,
                total_tokens=loop_result.total_tokens,
                provider=loop_result.provider,
                model=model_str,
                error=None if dashboard else "Failed to parse dashboard JSON",
            )

        return AgentResult(
            success=loop_result.success,
            content=loop_result.content,
            tool_calls_log=loop_result.tool_calls_log,
            total_steps=loop_result.total_steps,
            total_tokens=loop_result.total_tokens,
            provider=loop_result.provider,
            model=model_str,
            error=loop_result.error,
        )

    def _build_analysis_user_message(self, task: str, context: Optional[Dict] = None) -> str:
        """Build the initial user message for analysis."""
        parts = [task]
        if context:
            if context.get("stock_code"):
                parts.append(f"\n股票代码: {context['stock_code']}")
            if context.get("report_type"):
                parts.append(f"报告类型: {context['report_type']}")
            language = context.get("report_language", "zh")
            if str(language).lower().startswith("en"):
                parts.append("输出语言: English")
            else:
                parts.append("输出语言: 中文")

            # Inject pre-fetched context to avoid redundant tool calls
            if context.get("realtime_quote"):
                parts.append(f"\n[系统已获取的实时行情]\n{json.dumps(context['realtime_quote'], ensure_ascii=False)}")
            if context.get("chip_distribution"):
                parts.append(f"\n[系统已获取的筹码分布]\n{json.dumps(context['chip_distribution'], ensure_ascii=False)}")
            if context.get("news_context"):
                parts.append(f"\n[系统已获取的新闻与舆情]\n{context['news_context']}")

        parts.append("\n请使用可用工具获取缺失的数据，然后以决策仪表盘 JSON 格式输出分析结果。")
        return "\n".join(parts)
