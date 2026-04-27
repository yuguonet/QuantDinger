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


# ── Dynamic workflow templates (Agent dispatch layer) ─────────

WORKFLOW_TEMPLATES = {
    "analysis": """## 分析工作流程（必须严格按阶段顺序执行，每阶段等工具结果返回后再进入下一阶段）

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

> ⚠️ 每阶段的工具调用必须完整返回结果后，才能进入下一阶段。""",

    "screening": """## 选股筛选工作流程

**第一步 · 条件筛选**
- 使用 `screen_stocks` 按行业、概念、涨跌幅、换手率等条件从全市场筛选候选股

**第二步 · 指标验证**（对候选股逐只执行）
- 使用 `list_indicators` 查看可用指标策略
- 使用 `run_indicator_signal` 对每只候选股执行指标策略，检查是否出现买入信号

**第三步 · 综合推荐**
- 汇总有买入信号的股票，分析信号强度
- 给出推荐列表和理由""",

    "backtest": """## 回测验证工作流程

**第一步 · 发现策略**
- 使用 `list_strategies` 列出用户所有交易策略
- 使用 `list_indicators` 列出可用指标策略

**第二步 · 执行回测**
- 使用 `run_backtest` 对指定策略在指定股票和时间范围内跑回测
- 可以对多个策略/股票组合分别回测做对比

**第三步 · 分析绩效**
- 分析回测结果：收益率、胜率、最大回撤、夏普比率
- 使用 `get_backtest_history` 查看历史回测记录做对比
- 给出策略优化建议""",

    "trading": """## 交易执行工作流程

**第一步 · 确认信号**
- 使用 `get_realtime_quote` 确认当前行情
- 使用 `run_indicator_signal` 确认是否出现交易信号

**第二步 · 确认策略**
- 使用 `list_strategies` 列出可用策略
- 使用 `get_strategy_detail` 确认策略配置

**第三步 · 执行交易**
- 使用 `start_strategy` 启动策略执行
- 使用 `get_strategy_trades` 监控最近交易记录""",

    "full_pipeline": """## 完整量化交易流水线

**第一步 · 选股初筛**
- 使用 `screen_stocks` 从全市场筛选候选股

**第二步 · 指标精筛**
- 使用 `run_indicator_signal` 对候选股执行指标策略
- 筛选出有买入信号的标的

**第三步 · 回测验证**
- 使用 `run_backtest` 对筛选出的标的跑历史回测
- 验证策略在该标的上的历史表现

**第四步 · 交易执行**
- 使用 `start_strategy` 对通过验证的标的启动策略
- 使用 `get_strategy_trades` 监控执行情况""",
}

# Intent keywords -> workflow mapping
_INTENT_KEYWORDS = {
    "screening": ["选股", "筛选", "选股票", "找股票", "初选", "股票池", "screen", "filter", "scan"],
    "backtest": ["回测", "回验", "验证", "历史表现", "过去表现", "backtest", "test"],
    "trading": ["买入", "卖出", "交易", "下单", "启动策略", "执行", "buy", "sell", "trade", "execute"],
    "full_pipeline": ["全流程", "完整流程", "一站式", "从头到尾", "pipeline", "full"],
    "analysis": ["分析", "行情", "走势", "技术面", "基本面", "怎么看", "analyze", "analysis"],
}


def _detect_intent(message: str) -> str:
    """Detect user intent from message text. Returns workflow key."""
    msg_lower = message.lower()
    for intent, keywords in _INTENT_KEYWORDS.items():
        for kw in keywords:
            if kw in msg_lower:
                return intent
    return "analysis"  # default


def build_dynamic_system_prompt(
    user_message: str = "",
    active_skills: str = "",
    language: str = "zh",
    mode: str = "chat",
) -> str:
    """Build a dynamic system prompt based on user intent.

    Args:
        user_message: User's message (for intent detection)
        active_skills: Skill instructions string
        language: Language code
        mode: "dashboard" for structured JSON output, "chat" for free-form
    """
    intent = _detect_intent(user_message)
    workflow = WORKFLOW_TEMPLATES.get(intent, WORKFLOW_TEMPLATES["analysis"])
    language_section = _build_language_section(language)
    skill_section = ""
    if active_skills:
        skill_section = f"## 激活的交易技能\n\n{active_skills}\n"

    tool_catalog = """
## 可用工具分类

**数据工具**: get_realtime_quote, get_daily_history, get_stock_info, get_market_indices, get_sector_rankings
**分析工具**: analyze_trend, calculate_ma, get_volume_analysis, analyze_pattern, get_chip_distribution
**搜索工具**: search_stock_news, search_comprehensive_intel
**选股工具**: screen_stocks, review_stocks_with_indicator, list_user_selection_strategies
**指标工具**: list_indicators, get_indicator_params, run_indicator_signal
**回测工具**: run_backtest, get_backtest_history
**交易工具**: list_strategies, get_strategy_detail, start_strategy, stop_strategy, get_strategy_trades"""

    if mode == "dashboard":
        return _DASHBOARD_PROMPT_TEMPLATE.format(
            workflow=workflow,
            tool_catalog=tool_catalog,
            skill_section=skill_section,
            language_section=language_section,
        )
    else:
        return _CHAT_PROMPT_TEMPLATE.format(
            workflow=workflow,
            tool_catalog=tool_catalog,
            skill_section=skill_section,
            language_section=language_section,
        )


_DASHBOARD_PROMPT_TEMPLATE = """你是一位专业的金融投资分析 Agent，拥有丰富的数据工具和交易技能，负责生成专业的【决策仪表盘】分析报告。

{workflow}
{tool_catalog}
{skill_section}
## 规则

1. **必须调用工具获取真实数据** — 绝不编造数字，所有数据必须来自工具返回结果。
2. **系统化分析** — 严格按工作流程分阶段执行。
3. **输出格式** — 最终响应必须是有效的决策仪表盘 JSON。
4. **风险优先** — 必须排查风险（股东减持、业绩预警、监管问题）。
5. **工具失败处理** — 记录失败原因，使用已有数据继续分析，不重复调用失败工具。

## 输出格式：决策仪表盘 JSON

```json
{{{{
    "stock_name": "股票名称",
    "sentiment_score": 0-100整数,
    "trend_prediction": "强烈看多/看多/震荡/看空/强烈看空",
    "operation_advice": "买入/加仓/持有/减仓/卖出/观望",
    "decision_type": "buy/hold/sell",
    "confidence_level": "高/中/低",
    "dashboard": {{{{
        "core_conclusion": {{{{
            "one_sentence": "一句话核心结论（30字以内）",
            "signal_type": "🟢买入信号/🟡持有观望/🔴卖出信号/⚠️风险警告",
            "position_advice": {{{{"no_position": "空仓者建议", "has_position": "持仓者建议"}}}}
        }}}},
        "data_perspective": {{{{
            "trend_status": {{{{"ma_alignment": "", "is_bullish": true, "trend_score": 0}}}},
            "price_position": {{{{"current_price": 0, "ma5": 0, "ma10": 0, "ma20": 0}}}},
            "volume_analysis": {{{{"volume_ratio": 0, "volume_status": ""}}}},
            "chip_structure": {{{{"profit_ratio": "", "avg_cost": "", "chip_health": ""}}}}
        }}}},
        "intelligence": {{{{
            "latest_news": "",
            "risk_alerts": [],
            "positive_catalysts": []
        }}}},
        "battle_plan": {{{{
            "sniper_points": {{{{"ideal_buy": "", "secondary_buy": "", "stop_loss": "", "take_profit": ""}}}},
            "action_checklist": []
        }}}}
    }}}},
    "analysis_summary": "100字综合分析摘要",
    "risk_warning": "风险提示",
    "buy_reason": "操作理由",
    "technical_analysis": "技术面分析",
    "news_summary": "新闻摘要"
}}}}
```

{language_section}"""


_CHAT_PROMPT_TEMPLATE = """你是一位专业的金融投资分析 Agent，拥有丰富的数据工具和交易技能，负责解答用户的投资问题。

{workflow}
{tool_catalog}
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


# ── Cross-turn tool result helpers ─────────────────────────────

def _extract_tool_data(tool_calls_log: List[Dict], messages: List[Dict]) -> Dict[str, Any]:
    """Extract useful data from tool call results in the message history.

    Returns a dict with the latest results for each tool type.
    """
    data: Dict[str, Any] = {}
    # Walk messages in reverse to find tool results
    tool_name_to_result: Dict[str, Any] = {}
    for msg in reversed(messages):
        if msg.get("role") == "tool":
            name = msg.get("name", "")
            content = msg.get("content", "")
            if name and name not in tool_name_to_result:
                try:
                    parsed = json.loads(content)
                except (json.JSONDecodeError, TypeError):
                    parsed = content
                tool_name_to_result[name] = parsed

    # Keep compact versions of key results
    for tool_name in ("get_realtime_quote", "analyze_trend", "get_volume_analysis",
                       "get_daily_history", "get_chip_distribution", "search_stock_news"):
        if tool_name in tool_name_to_result:
            result = tool_name_to_result[tool_name]
            # Truncate large results
            result_str = json.dumps(result, ensure_ascii=False) if isinstance(result, (dict, list)) else str(result)
            if len(result_str) > 2000:
                result_str = result_str[:2000] + "...(truncated)"
            data[tool_name] = result_str

    return data


def _inject_saved_tool_results(session_id: str, context: Optional[Dict]) -> str:
    """Retrieve saved tool results and format as injectable context text."""
    try:
        store = _conversations._get_store()
        if not store:
            return ""
        saved = store.get_tool_results(session_id)
        if not saved:
            return ""

        stock_code = (context or {}).get("stock_code", "")
        # Try exact match first, then any entry
        results = saved.get(stock_code) or next(iter(saved.values()), {})
        if not results:
            return ""

        lines = []
        tool_labels = {
            "get_realtime_quote": "实时行情",
            "analyze_trend": "技术趋势",
            "get_volume_analysis": "量能分析",
            "get_daily_history": "历史K线",
            "get_chip_distribution": "筹码分布",
            "search_stock_news": "新闻舆情",
        }
        for tool_name, result in results.items():
            label = tool_labels.get(tool_name, tool_name)
            lines.append(f"【{label}】\n{result}")

        return "\n\n".join(lines)
    except Exception:
        return ""


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
        language = (context or {}).get("report_language", "zh")
        system_prompt = build_dynamic_system_prompt(
            user_message=task,
            active_skills=self.skill_instructions,
            language=language,
            mode="dashboard",
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
        language = (context or {}).get("report_language", "zh")
        system_prompt = build_dynamic_system_prompt(
            user_message=message,
            active_skills=self.skill_instructions,
            language=language,
            mode="chat",
        )

        # Build messages: system + history + context injection + new message
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
        ]

        # Inject conversation history
        history = _conversations.get_history(session_id)
        messages.extend(history)

        # Inject previous analysis context if provided
        context_parts: List[str] = []
        if context:
            if context.get("stock_code"):
                context_parts.append(f"股票代码: {context['stock_code']}")
            if context.get("stock_name"):
                context_parts.append(f"股票名称: {context['stock_name']}")
            if context.get("previous_analysis_summary"):
                summary = context["previous_analysis_summary"]
                text = json.dumps(summary, ensure_ascii=False) if isinstance(summary, dict) else str(summary)
                context_parts.append(f"上次分析摘要:\n{text}")

        # Inject saved tool results from previous turns
        saved_tools = _inject_saved_tool_results(session_id, context)
        if saved_tools:
            context_parts.append(f"[历史工具调用结果（可直接引用，无需重复调用）]:\n{saved_tools}")

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

        # Persist tool results for cross-turn reuse
        if result.tool_calls_log:
            try:
                store = _conversations._get_store()
                if store:
                    # Extract key data from tool call results
                    tool_data = _extract_tool_data(result.tool_calls_log, result.messages)
                    if tool_data:
                        stock_code = (context or {}).get("stock_code", "unknown")
                        store.save_tool_results(session_id, {stock_code: tool_data})
            except Exception:
                pass  # Non-critical

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
