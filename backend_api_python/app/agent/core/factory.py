# -*- coding: utf-8 -*-
"""
Agent Factory — builds configured AgentExecutor instances.

Uses self-mounting tool discovery: tools/__init__.py auto-scans for TOOL_SPEC
in each tool file. No manual imports needed — just drop a .py with TOOL_SPEC.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from app.agent.core.registry import ToolRegistry

logger = logging.getLogger(__name__)

# ── Module-level caches ───────────────────────────────────────

_TOOL_REGISTRY: Optional[ToolRegistry] = None


def get_tool_registry() -> ToolRegistry:
    """Return a cached ToolRegistry (built once, shared across requests).

    Tools are auto-discovered from app.agent.tools — each .py file with a
    TOOL_SPEC list is picked up automatically.
    """
    global _TOOL_REGISTRY
    if _TOOL_REGISTRY is not None:
        return _TOOL_REGISTRY

    from app.agent.tools import discover_tools

    registry = ToolRegistry()
    all_tools = discover_tools()
    registry.register_many(all_tools)
    _TOOL_REGISTRY = registry
    logger.info("[AgentFactory] ToolRegistry cached (%d tools): %s",
                len(registry.list_tools()), registry.list_tools())
    return _TOOL_REGISTRY


def _get_skill_instructions(
    skills: Optional[List[str]] = None,
    user_id: int = 1,
) -> str:
    """Load indicator skill instructions from IndicatorAnalyzer.

    Args:
        skills: Indicator ID list (strings like ["1", "2"]).
        user_id: User ID.
    """
    if not skills:
        return ""

    indicator_ids: Optional[List[int]] = None
    if skills:
        try:
            indicator_ids = [int(s) for s in skills if s.isdigit()]
        except (ValueError, AttributeError):
            indicator_ids = None

    if not indicator_ids and skills:
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
            temperature=0.3,
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
        skills: Indicator ID list (strings).
        user_id: User ID for loading indicator strategies.
        llm_service: Optional LLMService instance (created if None).
        max_steps: Max LLM round-trips.
        timeout_seconds: Overall timeout budget.

    Returns:
        AgentExecutor ready to call .run() or .chat().
    """
    from app.agent.core.executor import AgentExecutor

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
