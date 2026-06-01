# -*- coding: utf-8 -*-
"""
Agent Factory — builds configured AgentExecutor instances.

Centralises construction: ToolRegistry (cached), LLM adapter wiring,
skill/strategy injection.

策略来源：指标IDE（IndicatorAnalyzer 沙箱分析 + 真实K线数据）。
"""
from __future__ import annotations

import copy
import logging
import os
from typing import Any, Callable, Dict, List, Optional

from app.agent.tools.registry import ToolRegistry
from app.agent.tools.data_tools import DATA_TOOLS
from app.agent.tools.analysis_tools import ANALYSIS_TOOLS
from app.agent.tools.search_tools import SEARCH_TOOLS
from app.agent.tools.market_tools import MARKET_TOOLS
from app.agent.tools.stock_screener_tools import SCREENER_TOOLS
from app.agent.tools.backtest_tools import BACKTEST_TOOLS
from app.agent.tools.indicator_tools import INDICATOR_TOOLS
from app.agent.tools.trading_tools import TRADING_TOOLS
from app.agent.tools.screening_tools import SCREENING_TOOLS

logger = logging.getLogger(__name__)

# ── Module-level caches ───────────────────────────────────────

_TOOL_REGISTRY: Optional[ToolRegistry] = None


def get_tool_registry() -> ToolRegistry:
    """Return a cached ToolRegistry (built once, shared across requests)."""
    global _TOOL_REGISTRY
    if _TOOL_REGISTRY is not None:
        return _TOOL_REGISTRY

    registry = ToolRegistry()
    all_tools = (
        DATA_TOOLS
        + ANALYSIS_TOOLS
        + SEARCH_TOOLS
        + MARKET_TOOLS
        + SCREENER_TOOLS
        + BACKTEST_TOOLS
        + INDICATOR_TOOLS
        + TRADING_TOOLS
        + SCREENING_TOOLS
    )
    registry.register_many(all_tools)
    _TOOL_REGISTRY = registry
    logger.info("[AgentFactory] ToolRegistry cached (%d tools)", len(registry.list_tools()))
    return _TOOL_REGISTRY


def _get_skill_instructions(
    skills: Optional[List[str]] = None,
    user_id: int = 1,
) -> str:
    """
    从指标IDE加载策略指令。

    通过 IndicatorAnalyzer 沙箱运行用户指标（真实K线数据），
    提取行为统计和回测预览，生成 LLM 可理解的策略上下文。

    Args:
        skills: 指标 ID 列表（字符串形式），None 则加载用户全部指标
        user_id: 用户 ID
    """
    if not skills:
        return ""

    # skills 可能是指标 ID 列表（字符串形式如 ["1", "2"]）
    indicator_ids: Optional[List[int]] = None
    if skills:
        try:
            indicator_ids = [int(s) for s in skills if s.isdigit()]
        except (ValueError, AttributeError):
            indicator_ids = None

    if not indicator_ids and skills:
        # skills 非空但无法解析为 ID，忽略
        return ""

    try:
        from app.services.indicator_analyzer import build_agent_skill_instructions
        return build_agent_skill_instructions(
            user_id=user_id,
            indicator_ids=indicator_ids,
        )
    except Exception as e:
        logger.warning("[AgentFactory] IndicatorAnalyzer unavailable: %s", e, exc_info=True)
        return ""


def _build_call_with_tools_fn(llm_service=None) -> Callable:
    """Build a closure that calls LLM with tools.

    Returns: (messages, tools) -> {"content", "tool_calls", "usage"}
    """
    if llm_service is None:
        from app.services.llm import LLMService
        llm_service = LLMService()

    def call_fn(messages: List[Dict], tools: List[Dict]) -> Dict[str, Any]:
        return llm_service.call_with_tools(
            messages=messages,
            tools=tools,
            temperature=0.3,  # Lower temp for more deterministic tool calling
        )

    return call_fn


def build_agent_executor(
    skills: Optional[List[str]] = None,
    user_id: int = 1,
    llm_service=None,
    max_steps: int = 10,
    timeout_seconds: Optional[float] = None,
):
    """Build and return a configured AgentExecutor.

    Args:
        skills: 指标 ID 列表（字符串形式）。
        user_id: 用户 ID，用于加载该用户的指标策略。
        llm_service: Optional LLMService instance (created if None).
        max_steps: Max LLM round-trips.
        timeout_seconds: Overall timeout budget.

    Returns:
        AgentExecutor ready to call .run() or .chat().
    """
    from app.agent.executor import AgentExecutor

    registry = get_tool_registry()
    skill_instructions = _get_skill_instructions(skills, user_id=user_id)
    call_fn = _build_call_with_tools_fn(llm_service)

    return AgentExecutor(
        tool_registry=registry,
        call_with_tools_fn=call_fn,
        skill_instructions=skill_instructions,
        max_steps=max_steps,
        timeout_seconds=timeout_seconds,
    )
