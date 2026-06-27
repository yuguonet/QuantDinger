# -*- coding: utf-8 -*-
"""
Tool adapter — aggregates tool sources for the agent.

Provides build_all_tools() used by agent_blueprint.py for tool listing.
Combines local registry tools + smolagents Hub tools + MCP tools.
"""
from __future__ import annotations

import logging
from typing import Any, List

logger = logging.getLogger(__name__)


def build_all_tools() -> List[Any]:
    """汇总所有可用工具（本地 + builtin + hub/MCP），返回 smolagents Tool 对象列表。

    依次加载：
      1. 本地 registry 工具（app.agent.tools）
      2. smolagents 内置工具（如 web_search）
      3. Hub/MCP 工具（如可用）

    Returns:
        smolagents Tool 对象列表
    """
    from app.agent.tools.registry import build_smolagent_tools

    tools = build_smolagent_tools()

    # 补充 smolagents 内置工具
    builtins = _load_builtin_tools()
    tools.extend(builtins)

    # Hub/MCP 工具（最佳努力）
    hub_tools = _load_hub_tools()
    tools.extend(hub_tools)

    return tools


def _load_builtin_tools() -> List[Any]:
    """加载 smolagents 内置工具（如 web_search, visit_webpage 等）。"""
    builtin_ids = [
        "web_search",
        "visit_webpage",
        "duckduckgo_search",
        "wikipedia_search",
    ]
    result = []
    for tool_id in builtin_ids:
        try:
            from smolagents import load_tool
            tool = load_tool(tool_id)
            if tool is not None:
                result.append(tool)
        except Exception:
            pass
    return result


def _load_hub_tools() -> List[Any]:
    """加载 smolagents Hub 上托管的工具（最佳努力，忽略失败）。"""
    tools = []
    try:
        from smolagents import load_tool
        hub_ids = [
            # 如需添加 Hub 工具，在此处添加 tool ID
        ]
        for tool_id in hub_ids:
            try:
                tool = load_tool(tool_id)
                if tool is not None:
                    tools.append(tool)
            except Exception:
                logger.debug("[ToolAdapter] Hub 工具 %s 加载失败", tool_id)
    except ImportError:
        pass
    return tools
