# -*- coding: utf-8 -*-
"""
Agent Factory — builds configured AgentExecutor instances.

Centralises construction: ToolRegistry (cached), LLM adapter wiring,
skill/strategy injection.
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

logger = logging.getLogger(__name__)

# ── Module-level caches ───────────────────────────────────────

_TOOL_REGISTRY: Optional[ToolRegistry] = None
_STRATEGIES_CACHE: Optional[List[Dict]] = None


def get_tool_registry() -> ToolRegistry:
    """Return a cached ToolRegistry (built once, shared across requests)."""
    global _TOOL_REGISTRY
    if _TOOL_REGISTRY is not None:
        return _TOOL_REGISTRY

    registry = ToolRegistry()
    all_tools = DATA_TOOLS + ANALYSIS_TOOLS + SEARCH_TOOLS + MARKET_TOOLS
    registry.register_many(all_tools)
    _TOOL_REGISTRY = registry
    logger.info("[AgentFactory] ToolRegistry cached (%d tools)", len(registry.list_tools()))
    return _TOOL_REGISTRY


def _get_skill_instructions(skills: Optional[List[str]] = None) -> str:
    """Load YAML strategy instructions for activated skills."""
    if not skills:
        return ""

    global _STRATEGIES_CACHE
    if _STRATEGIES_CACHE is None:
        try:
            from app.services.strategy_loader import load_strategies
            _STRATEGIES_CACHE = load_strategies()
        except Exception as e:
            logger.warning("[AgentFactory] Strategy loader unavailable: %s", e)
            _STRATEGIES_CACHE = []

    sections = []
    for sid in skills:
        strat = next((s for s in _STRATEGIES_CACHE if s.get("id") == sid), None)
        if not strat:
            continue
        instructions = strat.get("instructions", "")
        if not instructions:
            continue
        sections.append(
            f"### 当前激活策略：{strat['name']}（{sid}）\n{instructions}"
        )

    if sections:
        return (
            "\n## 当前激活的分析策略\n"
            "用户已选择以下分析策略，请严格按照策略框架执行分析：\n\n"
            + "\n\n".join(sections)
        )
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
    llm_service=None,
    max_steps: int = 10,
    timeout_seconds: Optional[float] = None,
):
    """Build and return a configured AgentExecutor.

    Args:
        skills: Skill/strategy IDs to activate.
        llm_service: Optional LLMService instance (created if None).
        max_steps: Max LLM round-trips.
        timeout_seconds: Overall timeout budget.

    Returns:
        AgentExecutor ready to call .run() or .chat().
    """
    from app.agent.executor import AgentExecutor

    registry = get_tool_registry()
    skill_instructions = _get_skill_instructions(skills)
    call_fn = _build_call_with_tools_fn(llm_service)

    return AgentExecutor(
        tool_registry=registry,
        call_with_tools_fn=call_fn,
        skill_instructions=skill_instructions,
        max_steps=max_steps,
        timeout_seconds=timeout_seconds,
    )
