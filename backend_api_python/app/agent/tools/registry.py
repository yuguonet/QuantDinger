# -*- coding: utf-8 -*-
"""
Tool Registry — decorator-based self-registration for QuantDinger tools.

Usage:
    from app.agent.tools.registry import tool, registry

    @tool(description="根据中文名称搜索股票代码", category="名称查询")
    def search_stock_by_name(keyword: str, market: str = "CNStock", limit: int = 10):
        ...

    # Auto-discover all tools in the package
    registry.discover()

    # Build smolagents Tool list with optional policy filtering
    tools = registry.build({"deny": ["shell_exec"]})
"""
from __future__ import annotations

import inspect
import logging
import pkgutil
import importlib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, get_type_hints

logger = logging.getLogger(__name__)

# ── Type mapping: Python type → smolagents/OpenAI type string ──
_TYPE_MAP = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    dict: "object",
    list: "array",
}

# Handle generic aliases (Dict[str, Any], List[Dict], etc.)
try:
    from typing import _GenericAlias
    def _is_generic(tp, base):
        return isinstance(tp, _GenericAlias) and tp.__origin__ is base
except ImportError:
    def _is_generic(tp, base):
        return False


def _python_type_to_str(tp) -> str:
    """Convert a Python type annotation to smolagents type string."""
    if tp is inspect.Parameter.empty:
        return "string"
    # Direct match
    for base, name in _TYPE_MAP.items():
        if tp is base:
            return name
    # Generic aliases: Dict[str, Any] → "object", List[...] → "array"
    if _is_generic(tp, dict):
        return "object"
    if _is_generic(tp, list):
        return "array"
    # String type name fallback
    name = getattr(tp, "__name__", str(tp)).lower()
    return _TYPE_MAP.get(tp, "string")


# ═══════════════════════════════════════════════════════════════
# ToolSpec — lightweight tool metadata container
# ═══════════════════════════════════════════════════════════════

@dataclass
class ToolSpec:
    """Registered tool metadata, convertible to smolagents Tool."""
    fn: Callable
    name: str
    description: str
    category: str = ""
    output_type: str = "string"
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_smolagents_tool(self):
        """Convert to a smolagents Tool subclass instance."""
        from smolagents import Tool

        sig = inspect.signature(self.fn)
        try:
            hints = get_type_hints(self.fn)
        except Exception:
            hints = {}

        # Build smolagents inputs dict from function signature
        inputs = {}
        for pname, param in sig.parameters.items():
            tp = hints.get(pname, param.annotation)
            type_str = _python_type_to_str(tp)
            desc = ""
            # Try to extract from docstring (Google-style)
            desc = _extract_param_desc(self.fn, pname)
            inputs[pname] = {"type": type_str, "description": desc}
            if param.default is not inspect.Parameter.empty:
                inputs[pname]["nullable"] = True

        param_names = list(sig.parameters.keys())

        def _make_forward(_fn, _param_names):
            def forward(self, **kwargs):
                return _fn(**kwargs)
            params = [inspect.Parameter("self", inspect.Parameter.POSITIONAL_OR_KEYWORD)]
            for pname in _param_names:
                params.append(inspect.Parameter(pname, inspect.Parameter.KEYWORD_ONLY, default=None))
            forward.__signature__ = inspect.Signature(params)
            return forward

        tool_class = type(
            f"Tool_{self.name}",
            (Tool,),
            {
                "name": self.name,
                "description": self.description,
                "inputs": inputs,
                "output_type": self.output_type,
                "forward": _make_forward(self.fn, param_names),
            },
        )
        return tool_class()


def _extract_param_desc(fn: Callable, param_name: str) -> str:
    """Extract parameter description from Google-style docstring.

    Looks for lines like:
        keyword: 搜索关键词（中文股票名称等）
    """
    doc = inspect.getdoc(fn) or ""
    in_args = False
    for line in doc.split("\n"):
        stripped = line.strip()
        # Section headers: "Args:", "Arguments:", "Parameters:"
        if stripped.lower().rstrip(":") in ("args", "arguments", "parameters"):
            in_args = True
            continue
        # New section ends args
        if in_args and stripped and not stripped[0].isspace() and stripped.endswith(":"):
            break
        if in_args and ":" in stripped:
            name_part, _, desc_part = stripped.partition(":")
            if name_part.strip().split()[0] == param_name:
                # Handle "name (type): description" format
                desc = desc_part.strip()
                if not desc and "(" in name_part:
                    desc = stripped
                return desc
    return ""


# ═══════════════════════════════════════════════════════════════
# ToolRegistry — central registry
# ═══════════════════════════════════════════════════════════════

class ToolRegistry:
    """Central registry for @tool-decorated functions.

    Lifecycle:
        1. Modules define @tool(...) decorated functions
        2. registry.discover() imports all modules in the tools package → triggers registration
        3. registry.build(config) applies policy filters and returns smolagents Tool list
    """

    def __init__(self):
        self._tools: Dict[str, ToolSpec] = {}
        self._discovered = False

    def register(self, fn: Callable, name: str, description: str,
                 category: str = "", output_type: str = "string", **meta):
        """Register a tool function. Called by the @tool decorator."""
        spec = ToolSpec(
            fn=fn, name=name, description=description,
            category=category, output_type=output_type, meta=meta,
        )
        self._tools[name] = spec

    def discover(self, package: str = "app.agent.tools"):
        """Import all modules in the package to trigger @tool registrations."""
        if self._discovered:
            return
        pkg = importlib.import_module(package)
        for importer, mod_name, is_pkg in pkgutil.iter_modules(
            getattr(pkg, "__path__", [])
        ):
            if mod_name.startswith("_"):
                continue
            try:
                importlib.import_module(f"{package}.{mod_name}")
            except Exception as e:
                logger.warning("[ToolRegistry] Failed to import %s.%s: %s", package, mod_name, e)
        self._discovered = True
        logger.info("[ToolRegistry] Discovered %d tools from %s", len(self._tools), package)

    def build(self, config: Dict = None) -> List:
        """Build smolagents Tool list with optional policy filtering.

        config keys:
            allow: list[str] — if set, only these tools are included
            deny: list[str] — these tools are excluded
        """
        config = config or {}
        allow = set(config.get("allow", []))
        deny = set(config.get("deny", []))

        tools = []
        for spec in self._tools.values():
            if deny and spec.name in deny:
                continue
            if allow and spec.name not in allow:
                continue
            try:
                tools.append(spec.to_smolagents_tool())
            except Exception as e:
                logger.warning("[ToolRegistry] Failed to build tool '%s': %s", spec.name, e)
        return tools

    @property
    def categories(self) -> Dict[str, List[str]]:
        """Return {category: [tool_names]} mapping."""
        cats: Dict[str, List[str]] = {}
        for spec in self._tools.values():
            cat = spec.category or "其他"
            cats.setdefault(cat, []).append(spec.name)
        return cats

    @property
    def all_names(self) -> List[str]:
        return list(self._tools.keys())

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def __len__(self):
        return len(self._tools)

    def __contains__(self, name: str):
        return name in self._tools


# ── Global singleton ──
registry = ToolRegistry()


# ═══════════════════════════════════════════════════════════════
# @tool decorator
# ═══════════════════════════════════════════════════════════════

def tool(
    description: str,
    name: str = "",
    category: str = "",
    output_type: str = "string",
    **meta,
):
    """Decorator to register a function as a QuantDinger tool.

    Usage:
        @tool(description="搜索股票", category="名称查询")
        def search_stock_by_name(keyword: str, market: str = "CNStock"):
            ...

    The decorated function remains callable as normal — the decorator
    only registers it in the global registry, it does NOT wrap it.
    """
    def decorator(fn: Callable) -> Callable:
        tool_name = name or fn.__name__
        registry.register(
            fn=fn,
            name=tool_name,
            description=description,
            category=category,
            output_type=output_type,
            **meta,
        )
        return fn  # Unwrapped — function stays directly callable
    return decorator
