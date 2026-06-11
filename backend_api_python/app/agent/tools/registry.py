# -*- coding: utf-8 -*-
"""
Tool Registry — @tool 装饰器自注册 + 自动发现 + smolagents Tool 构建。

生命周期：
  1. 各工具模块用 @tool(...) 装饰器注册函数（data_tools.py, analysis_tools.py 等）
  2. registry.discover() 导入 tools/ 包下所有模块，触发注册
  3. registry.build(config) 过滤 + 转换为 smolagents Tool 列表

架构分层（layer 参数）：
  显示层 — 图表/可视化输出
  数据层 — 名称查询、行情数据获取
  分析层 — 技术分析、指标策略、情报搜索
  决策层 — 选股、回测
  执行层 — 交易
  支撑层 — 工作区、自修改

领域过滤（domain 参数）：
  ["finance"]  — 仅金融分析
  ["coding"]   — 仅代码开发
  []           — 通用工具（所有领域可用，优先级较低）

被调用方：
  tool_adapter.py → build_all_tools() → registry.discover() + registry.build()
  agent.py → _generate_tool_catalog() → registry.layered_categories

公开接口：
  registry.discover(package) → None
  registry.build(config) → List[Tool]（config: allow/deny/domain）
  registry.get(name) → Optional[ToolSpec]
  registry.all_names → List[str]
  registry.categories → Dict[str, List[str]]
  registry.layered_categories → Dict[str, Dict[str, List[str]]]
  @tool(description, name, category, layer, domain, output_type, **meta) → decorator
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


def _is_optional(tp) -> bool:
    """Check if a type is Optional[X] (i.e. Union[X, None])."""
    import typing
    origin = getattr(tp, "__origin__", None)
    if origin is typing.Union:
        return type(None) in tp.__args__
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
    layer: str = ""          # 架构分层：显示层/数据层/分析层/决策层/执行层/支撑层
    domain: List[str] = field(default_factory=list)  # 领域标签：["finance"] / ["coding"] / ["finance","coding"] / []=通用
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
            # Mark nullable when the original param has a default or is Optional
            is_optional = _is_optional(tp)
            has_default = param.default is not inspect.Parameter.empty
            if has_default or is_optional:
                inputs[pname]["nullable"] = True

        param_names = list(sig.parameters.keys())

        def _make_forward(_fn):
            def forward(self, **kwargs):
                return _fn(**kwargs)
            params = [inspect.Parameter("self", inspect.Parameter.POSITIONAL_OR_KEYWORD)]
            for pname in param_names:
                orig = sig.parameters[pname]
                # Preserve original default; use None only if the param is optional or has a default
                if orig.default is not inspect.Parameter.empty:
                    default = orig.default
                else:
                    default = inspect.Parameter.empty
                params.append(inspect.Parameter(pname, inspect.Parameter.KEYWORD_ONLY, default=default))
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
                "forward": _make_forward(self.fn),
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
                 category: str = "", layer: str = "", domain: List[str] = None,
                 output_type: str = "string", **meta):
        """Register a tool function. Called by the @tool decorator."""
        spec = ToolSpec(
            fn=fn, name=name, description=description,
            category=category, layer=layer, domain=domain or [],
            output_type=output_type, meta=meta,
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
            domain: str — if set, filter by domain:
                - tools with matching domain tag → included
                - tools with domain=[] (universal) → included (lower priority)
                - tools with non-matching domain → excluded
        """
        config = config or {}
        allow = set(config.get("allow", []))
        deny = set(config.get("deny", []))
        domain = config.get("domain", "")

        tools = []
        for spec in self._tools.values():
            if deny and spec.name in deny:
                continue
            if allow and spec.name not in allow:
                continue
            # Domain filtering
            if domain:
                if spec.domain and domain not in spec.domain:
                    continue  # 工具指定了领域但不匹配 → 排除
                # spec.domain 为空（通用）或包含当前领域 → 保留
            try:
                tools.append(spec.to_smolagents_tool())
            except Exception as e:
                logger.warning("[ToolRegistry] Failed to build tool '%s': %s", spec.name, e)

        # 排序：领域匹配的工具在前，通用工具在后
        if domain:
            tools.sort(key=lambda t: 0 if self._tools.get(t.name) and domain in self._tools[t.name].domain else 1)

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
    def layered_categories(self) -> Dict[str, Dict[str, List[str]]]:
        """Return {layer: {category: [tool_names]}} mapping."""
        layers: Dict[str, Dict[str, List[str]]] = {}
        for spec in self._tools.values():
            layer = spec.layer or "未分层"
            cat = spec.category or "其他"
            layers.setdefault(layer, {}).setdefault(cat, []).append(spec.name)
        return layers

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
    layer: str = "",
    domain: List[str] = None,
    output_type: str = "string",
    **meta,
):
    """Decorator to register a function as a QuantDinger tool.

    Usage:
        @tool(description="搜索股票", category="名称查询", layer="数据层", domain=["finance"])
        def search_stock_by_name(keyword: str, market: str = "CNStock"):
            ...

    layer: 架构分层，可选值:
        显示层 — 图表/可视化输出
        数据层 — 名称查询、行情数据获取
        分析层 — 技术分析、指标策略、情报搜索
        决策层 — 选股、回测
        执行层 — 交易
        支撑层 — 工作区、自修改

    domain: 领域标签，可选值:
        ["finance"] — 仅金融分析
        ["coding"]  — 仅代码开发
        ["finance", "coding"] — 多领域
        [] 或 None — 通用工具（所有领域可用，优先级较低）

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
            layer=layer,
            domain=domain,
            output_type=output_type,
            **meta,
        )
        return fn  # Unwrapped — function stays directly callable
    return decorator
