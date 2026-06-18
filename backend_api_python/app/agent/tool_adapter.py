# -*- coding: utf-8 -*-
"""
Tool Adapter — 工具适配器。

将 QuantDinger 的 @tool 装饰器注册的函数转换为 smolagents Tool 子类。
同时加载 smolagents 内置工具、Hub 工具、MCP 工具。

被调用方：
  agent.py → build_all_tools() → get_smolagent() 构建工具列表
  agent.py → _build_managed_agents() → 构建子 Agent 的工具列表

公开接口：
  build_all_tools() → List[Tool]（所有工具）
  load_tools_from_module(module) → List[Tool]（从模块加载旧式 dict 工具）
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from smolagents import Tool, ToolCollection

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 1. Dict-based → smolagents Tool converter
# ═══════════════════════════════════════════════════════════════

def _make_tool_class(
    name: str,
    description: str,
    fn: callable,
    parameters: dict,
    required: list,
) -> type:
    """Dynamically create a smolagents Tool subclass from a dict-based tool spec."""
    props = parameters.get("properties", {})
    inputs = {}
    for param_name, param_schema in props.items():
        param_type = param_schema.get("type", "string")
        type_map = {
            "string": "string", "integer": "integer", "number": "number",
            "boolean": "boolean", "object": "object", "array": "array",
        }
        inputs[param_name] = {
            "type": type_map.get(param_type, "string"),
            "description": param_schema.get("description", ""),
        }
        if param_name not in required and "default" in param_schema:
            inputs[param_name]["nullable"] = True

    output_type = "string"
    try:
        import inspect
        sig = inspect.signature(fn)
        ret = sig.return_annotation
        if ret is not inspect.Parameter.empty:
            if ret in (dict, Dict, Dict[str, Any]):
                output_type = "object"
            elif ret in (list, List, List[Dict]):
                output_type = "object"
    except Exception:
        pass

    param_names = list(props.keys())

    def _make_forward(_fn, _param_names):
        def forward(self, **kwargs):
            return _fn(**kwargs)
        import inspect as _inspect
        params = [_inspect.Parameter("self", _inspect.Parameter.POSITIONAL_OR_KEYWORD)]
        for pname in _param_names:
            params.append(_inspect.Parameter(pname, _inspect.Parameter.KEYWORD_ONLY, default=None))
        forward.__signature__ = _inspect.Signature(params)
        return forward

    for pname in inputs:
        inputs[pname]["nullable"] = True

    tool_class = type(
        f"Tool_{name}",
        (Tool,),
        {
            "name": name,
            "description": description,
            "inputs": inputs,
            "output_type": output_type,
            "forward": _make_forward(fn, param_names),
        },
    )
    return tool_class


def load_tools_from_module(tool_list: list) -> List[Tool]:
    """Convert a list of dict-based tool specs into smolagents Tool instances."""
    tools = []
    for spec in tool_list:
        try:
            cls = _make_tool_class(
                name=spec["name"],
                description=spec["description"],
                fn=spec["fn"],
                parameters=spec.get("parameters", {"type": "object", "properties": {}}),
                required=spec.get("required", []),
            )
            tools.append(cls())
        except Exception as e:
            logger.warning("[ToolAdapter] Failed to wrap tool '%s': %s", spec.get("name", "?"), e)
    return tools


# ═══════════════════════════════════════════════════════════════
# 2. Built-in smolagents tools
# ═══════════════════════════════════════════════════════════════

def _load_builtin_tools() -> List[Tool]:
    tools = []

    try:
        from smolagents import DuckDuckGoSearchTool
        tools.append(DuckDuckGoSearchTool())
        logger.info("[ToolAdapter] Loaded DuckDuckGoSearchTool")
    except Exception as e:
        logger.debug("[ToolAdapter] DuckDuckGoSearchTool unavailable: %s", e)

    try:
        from smolagents import GoogleSearchTool
        tools.append(GoogleSearchTool())
        logger.info("[ToolAdapter] Loaded GoogleSearchTool")
    except Exception as e:
        logger.debug("[ToolAdapter] GoogleSearchTool unavailable: %s", e)

    try:
        from smolagents import WebSearchTool
        tools.append(WebSearchTool())
        logger.info("[ToolAdapter] Loaded WebSearchTool")
    except Exception as e:
        logger.debug("[ToolAdapter] WebSearchTool unavailable: %s", e)

    try:
        from smolagents import VisitWebpageTool
        tools.append(VisitWebpageTool())
        logger.info("[ToolAdapter] Loaded VisitWebpageTool")
    except Exception as e:
        logger.debug("[ToolAdapter] VisitWebpageTool unavailable: %s", e)

    try:
        from smolagents import WikipediaSearchTool
        tools.append(WikipediaSearchTool())
        logger.info("[ToolAdapter] Loaded WikipediaSearchTool")
    except Exception as e:
        logger.debug("[ToolAdapter] WikipediaSearchTool unavailable: %s", e)

    return tools


# ═══════════════════════════════════════════════════════════════
# 3. Hub & MCP tool loading
# ═══════════════════════════════════════════════════════════════

def _load_hub_tools() -> List[Tool]:
    tools = []
    token = os.getenv("HF_TOKEN") or os.getenv("HUGGING_FACE_HUB_TOKEN")

    collections = os.getenv("SMOLAGENTS_HUB_COLLECTIONS", "").strip()
    if collections:
        for slug in collections.split(","):
            slug = slug.strip()
            if not slug:
                continue
            try:
                collection = ToolCollection.from_hub(slug, token=token, trust_remote_code=True)
                tools.extend(collection.tools)
                logger.info("[ToolAdapter] Loaded Hub collection '%s': %d tools", slug, len(collection.tools))
            except Exception as e:
                logger.warning("[ToolAdapter] Failed to load Hub collection '%s': %s", slug, e)

    tool_repos = os.getenv("SMOLAGENTS_HUB_TOOLS", "").strip()
    if tool_repos:
        for repo_id in tool_repos.split(","):
            repo_id = repo_id.strip()
            if not repo_id:
                continue
            try:
                tool = Tool.from_hub(repo_id, token=token, trust_remote_code=True)
                tools.append(tool)
                logger.info("[ToolAdapter] Loaded Hub tool '%s'", repo_id)
            except Exception as e:
                logger.warning("[ToolAdapter] Failed to load Hub tool '%s': %s", repo_id, e)

    return tools


_mcp_collections: list = []


def _load_mcp_tools() -> List[Tool]:
    import json as _json

    tools = []
    mcp_config_raw = os.getenv("SMOLAGENTS_MCP_SERVERS", "").strip()
    mcp_config_path = os.getenv("SMOLAGENTS_MCP_CONFIG", "").strip()

    config = None
    if mcp_config_raw:
        try:
            config = _json.loads(mcp_config_raw)
        except _json.JSONDecodeError as e:
            logger.warning("[ToolAdapter] Invalid SMOLAGENTS_MCP_SERVERS JSON: %s", e)
    elif mcp_config_path:
        try:
            with open(mcp_config_path) as f:
                config = _json.load(f)
        except Exception as e:
            logger.warning("[ToolAdapter] Failed to read MCP config '%s': %s", mcp_config_path, e)

    if config and "mcpServers" in config:
        for server_name, server_params in config["mcpServers"].items():
            try:
                ctx = ToolCollection.from_mcp(server_params, trust_remote_code=True)
                collection = ctx.__enter__()
                tools.extend(collection.tools)
                _mcp_collections.append(ctx)
                logger.info("[ToolAdapter] Loaded MCP server '%s': %d tools", server_name, len(collection.tools))
            except Exception as e:
                logger.warning("[ToolAdapter] Failed to load MCP server '%s': %s", server_name, e)

    return tools


# ═══════════════════════════════════════════════════════════════
# 4. Master loader (registry-based)
# ═══════════════════════════════════════════════════════════════

# Legacy excluded tool names — migrated from hardcoded set.
# Prefer using registry.build({"deny": [...]}) from config instead.
_EXCLUDED_TOOL_NAMES = {
    "screen_stocks", "smart_screen",
    "get_stock_fund_flow", "batch_get_stock_fund_flow",
    "get_dragon_tiger_stocks", "get_dragon_tiger_by_stock",
    "get_hot_rank_stocks", "get_zt_pool_stocks",
    "get_limit_down_stocks", "get_broken_board_stocks",
}

_tools_cache = None  # type: ignore
_tools_cache_time = 0  # epoch seconds
_TOOLS_CACHE_TTL = int(os.getenv("TOOLS_CACHE_TTL", "300"))  # 5 minutes default


def _load_quantdinger_tools(config: Dict = None) -> List[Tool]:
    """Load QuantDinger tools via registry (@tool decorators).

    All tools are registered through the @tool decorator in registry.py.
    Legacy _TOOLS lists have been fully migrated.
    """
    from app.agent.tools.registry import registry

    # Discover @tool-decorated functions (also imports all tool modules)
    registry.discover()

    # Build tools with policy filtering
    registry_config = dict(config or {})
    if _EXCLUDED_TOOL_NAMES:
        extra_deny = set(registry_config.get("deny", []))
        extra_deny.update(_EXCLUDED_TOOL_NAMES)
        registry_config["deny"] = list(extra_deny)
    tools = registry.build(registry_config)
    logger.info("[ToolAdapter] Registry tools: %d", len(tools))

    return tools


def build_all_tools(config: Dict = None) -> List[Tool]:
    """Load all tools: QuantDinger built-in + smolagents built-in + Hub + MCP.

    Results are cached with TTL to allow periodic refresh.

    Args:
        config: Optional policy config for registry.build():
            {"allow": [...], "deny": [...]}
    """
    global _tools_cache, _tools_cache_time
    import time as _time
    now = _time.time()
    if _tools_cache is not None and (now - _tools_cache_time) < _TOOLS_CACHE_TTL:
        return _tools_cache

    # 1. QuantDinger tools (registry + legacy fallback)
    tools = _load_quantdinger_tools(config)
    logger.info("[ToolAdapter] QuantDinger tools: %d", len(tools))

    # 2. smolagents built-in tools
    tools.extend(_load_builtin_tools())

    # 3. Hub tools
    tools.extend(_load_hub_tools())

    # 4. MCP tools
    tools.extend(_load_mcp_tools())

    logger.info("[ToolAdapter] Total tools loaded: %d", len(tools))
    _tools_cache = tools
    _tools_cache_time = _time.time()
    return tools
