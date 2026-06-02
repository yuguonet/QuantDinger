# -*- coding: utf-8 -*-
"""
Tool Adapter — converts QuantDinger's dict-based tool definitions
into smolagents Tool subclasses. Also loads built-in, Hub, and MCP tools.
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

    # Build a forward method with explicit parameter names matching inputs,
    # so smolagents validation passes (it checks forward signature vs inputs keys).
    param_names = list(props.keys())
    # Dynamically create a function with the correct signature
    def _make_forward(_fn, _param_names):
        def forward(self, **kwargs):
            return _fn(**kwargs)
        # Build an explicit signature so smolagents can introspect it
        import inspect as _inspect
        params = [_inspect.Parameter("self", _inspect.Parameter.POSITIONAL_OR_KEYWORD)]
        for pname in _param_names:
            params.append(_inspect.Parameter(pname, _inspect.Parameter.KEYWORD_ONLY, default=None))
        forward.__signature__ = _inspect.Signature(params)
        return forward

    # Mark all params as nullable since the forward signature gives them default=None
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
    """Load smolagents built-in tools (search, web, etc.)."""
    tools = []

    # DuckDuckGo search — free, no API key needed
    try:
        from smolagents import DuckDuckGoSearchTool
        tools.append(DuckDuckGoSearchTool())
        logger.info("[ToolAdapter] Loaded DuckDuckGoSearchTool")
    except Exception as e:
        logger.debug("[ToolAdapter] DuckDuckGoSearchTool unavailable: %s", e)

    # Google search — needs SERPAPI_API_KEY or GOOGLE_API_KEY
    try:
        from smolagents import GoogleSearchTool
        tools.append(GoogleSearchTool())
        logger.info("[ToolAdapter] Loaded GoogleSearchTool")
    except Exception as e:
        logger.debug("[ToolAdapter] GoogleSearchTool unavailable: %s", e)

    # Web search (auto-picks available provider)
    try:
        from smolagents import WebSearchTool
        tools.append(WebSearchTool())
        logger.info("[ToolAdapter] Loaded WebSearchTool")
    except Exception as e:
        logger.debug("[ToolAdapter] WebSearchTool unavailable: %s", e)

    # Visit webpage — fetch and extract text from URL
    try:
        from smolagents import VisitWebpageTool
        tools.append(VisitWebpageTool())
        logger.info("[ToolAdapter] Loaded VisitWebpageTool")
    except Exception as e:
        logger.debug("[ToolAdapter] VisitWebpageTool unavailable: %s", e)

    # Wikipedia search
    try:
        from smolagents import WikipediaSearchTool
        tools.append(WikipediaSearchTool())
        logger.info("[ToolAdapter] Loaded WikipediaSearchTool")
    except Exception as e:
        logger.debug("[ToolAdapter] WikipediaSearchTool unavailable: %s", e)

    # UserInputTool — DISABLED: uses input() which blocks on stdin,
    # hanging the agent in web/SSE contexts. The agent should ask
    # clarifying questions in its output text instead (multi-turn chat).
    # from smolagents import UserInputTool
    # tools.append(UserInputTool())

    return tools


# ═══════════════════════════════════════════════════════════════
# 3. Hub & MCP tool loading
# ═══════════════════════════════════════════════════════════════

def _load_hub_tools() -> List[Tool]:
    """Load tools from HuggingFace Hub collections or individual repos.

    Controlled by env vars:
      - SMOLAGENTS_HUB_COLLECTIONS: comma-separated collection slugs
      - SMOLAGENTS_HUB_TOOLS: comma-separated tool repo IDs
    """
    tools = []
    token = os.getenv("HF_TOKEN") or os.getenv("HUGGING_FACE_HUB_TOKEN")

    # Collections
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

    # Individual tools
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


_mcp_collections: list = []  # Keep references alive


def _load_mcp_tools() -> List[Tool]:
    """Load tools from MCP servers.

    Explicit config via env vars:
      - SMOLAGENTS_MCP_CONFIG: path to JSON config file
      - SMOLAGENTS_MCP_SERVERS: inline JSON string
    """
    import json as _json

    tools = []

    # 1. Try explicit config first (inline JSON or file path)
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

    # 2. Auto-detect: DISABLED — stdio-based MCP (npx) is incompatible with
    #    smolagents >=1.25 ToolCollection.from_mcp() which expects HTTP transport.
    #    Users should configure MCP explicitly via SMOLAGENTS_MCP_SERVERS or SMOLAGENTS_MCP_CONFIG.

    # 3. Load MCP servers
    #    In smolagents >=1.25, ToolCollection.from_mcp() is a generator/context manager.
    #    We must use `with` to enter it, and keep the context manager alive so tools persist.
    if config and "mcpServers" in config:
        for server_name, server_params in config["mcpServers"].items():
            try:
                ctx = ToolCollection.from_mcp(server_params, trust_remote_code=True)
                collection = ctx.__enter__()
                tools.extend(collection.tools)
                _mcp_collections.append(ctx)  # keep context manager alive
                logger.info("[ToolAdapter] Loaded MCP server '%s': %d tools", server_name, len(collection.tools))
            except Exception as e:
                logger.warning("[ToolAdapter] Failed to load MCP server '%s': %s", server_name, e)

    return tools


# ═══════════════════════════════════════════════════════════════
# 4. Master loader
# ═══════════════════════════════════════════════════════════════

_tools_cache = None  # type: ignore


def build_all_tools() -> List[Tool]:
    """Load all tools: QuantDinger built-in + smolagents built-in + Hub + MCP.

    Results are cached after the first call to avoid repeated heavy imports.
    """
    global _tools_cache
    if _tools_cache is not None:
        return _tools_cache

    from app.agent.tools.data_tools import DATA_TOOLS
    from app.agent.tools.analysis_tools import ANALYSIS_TOOLS
    from app.agent.tools.search_tools import SEARCH_TOOLS
    from app.agent.tools.market_tools import MARKET_TOOLS
    from app.agent.tools.stock_screener_tools import SCREENER_TOOLS
    from app.agent.tools.backtest_tools import BACKTEST_TOOLS
    from app.agent.tools.indicator_tools import INDICATOR_TOOLS
    from app.agent.tools.trading_tools import TRADING_TOOLS
    from app.agent.tools.screening_tools import SCREENING_TOOLS
    from app.agent.tools.code_workspace_tools import WORKSPACE_TOOLS
    from app.agent.tools.scan_tools import SCAN_TOOLS
    from app.agent.tools.self_modify_tools import SELF_MODIFY_TOOLS

    # 1. QuantDinger tools (dict-based → Tool)
    all_lists = [
        DATA_TOOLS, ANALYSIS_TOOLS, SEARCH_TOOLS, MARKET_TOOLS,
        SCREENER_TOOLS, BACKTEST_TOOLS, INDICATOR_TOOLS, TRADING_TOOLS,
        SCREENING_TOOLS, WORKSPACE_TOOLS,
        SCAN_TOOLS, SELF_MODIFY_TOOLS,
    ]
    tools = []
    for lst in all_lists:
        tools.extend(load_tools_from_module(lst))
    logger.info("[ToolAdapter] QuantDinger tools: %d", len(tools))

    # 2. smolagents built-in tools (search, web, etc.)
    builtin = _load_builtin_tools()
    tools.extend(builtin)

    # 3. Hub tools
    hub = _load_hub_tools()
    tools.extend(hub)

    # 4. MCP tools
    mcp = _load_mcp_tools()
    tools.extend(mcp)

    logger.info("[ToolAdapter] Total tools loaded: %d", len(tools))
    _tools_cache = tools
    return tools
