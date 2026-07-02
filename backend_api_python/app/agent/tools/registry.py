# -*- coding: utf-8 -*-
"""
工具注册中心

扫描 tools/*.py 中的公开函数，自动包装为 Tool 实例，
兼容 Agent Template 的 ToolRegistry + TaskAgent 体系。
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, get_type_hints

from tools.base import Tool, ToolResult

logger = logging.getLogger(__name__)

# ── 类型映射 ──────────────────────────────────────────────────
_TYPE_MAP = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _python_type_to_json(tp) -> Dict[str, Any]:
    """Python 类型 → JSON Schema type。"""
    origin = getattr(tp, "__origin__", None)
    if origin is type(None):
        return {"type": "string"}
    if origin is dict:
        return {"type": "object"}
    if origin is list:
        args = getattr(tp, "__args__", None)
        if args:
            return {"type": "array", "items": _python_type_to_json(args[0])}
        return {"type": "array"}
    import typing
    if origin is typing.Union:
        args = getattr(tp, "__args__", ())
        non_none = [a for a in args if a is not type(None)]
        if non_none:
            return _python_type_to_json(non_none[0])
        return {"type": "string"}
    return {"type": _TYPE_MAP.get(tp, "string")}


def _parse_docstring(doc: str) -> tuple:
    """解析 docstring → (description, {param: description})。"""
    if not doc:
        return "", {}
    lines = doc.strip().split("\n")
    desc_lines, param_descs = [], {}
    in_args = False
    for line in lines:
        stripped = line.strip()
        if stripped.lower().rstrip(":") in ("args", "parameters", "参数"):
            in_args = True
            continue
        if in_args:
            if stripped and not line[0].isspace() and ":" not in stripped:
                in_args = False
                desc_lines.append(line)
                continue
            if stripped and ":" in stripped:
                parts = stripped.split(":", 1)
                pname = parts[0].strip().split("(")[0].strip()
                pdesc = parts[1].strip().strip('"')
                if pname:
                    param_descs[pname] = pdesc
                continue
        if not in_args:
            desc_lines.append(line)
    return "\n".join(desc_lines).strip(), param_descs


def _func_to_tool(func: Callable) -> Tool:
    """将普通函数包装为 Tool 实例。"""
    sig = inspect.signature(func)
    doc = inspect.getdoc(func) or ""
    description, param_descs = _parse_docstring(doc)
    try:
        hints = get_type_hints(func)
    except Exception:
        hints = {}

    properties = {}
    required = []
    for pname, param in sig.parameters.items():
        if pname in ("self", "cls"):
            continue
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            continue
        ptype = hints.get(pname, str)
        prop = _python_type_to_json(ptype)
        if pname in param_descs:
            prop["description"] = param_descs[pname]
        properties[pname] = prop
        if param.default is inspect.Parameter.empty:
            required.append(pname)

    if not description:
        description = doc.split("\n")[0][:500] if doc else func.__name__

    parameters = {"type": "object", "properties": properties}
    if required:
        parameters["required"] = required

    # 动态创建 Tool 子类（在类体中定义 execute，满足 ABC 要求）
    tool_name = func.__name__
    tool_description = description[:1024]
    _fn = func  # 闭包引用

    async def _execute(self, **kwargs):
        sig_inner = inspect.signature(_fn)
        valid = set(sig_inner.parameters.keys())
        filtered = {k: v for k, v in kwargs.items() if k in valid}
        result = _fn(**filtered)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, ToolResult):
            return result
        return ToolResult(success=True, output=result)

    # type() 动态创建，execute 在类体中定义
    _FuncTool = type(tool_name, (Tool,), {
        'name': tool_name,
        'description': tool_description,
        'parameters': parameters,
        'execute': _execute,
        '_original_func': staticmethod(func),
    })

    return _FuncTool()


# ── 跳过的文件名 ──────────────────────────────────────────────
_SKIP_FILES = {"__init__", "base", "registry", "em_utils", "pagination", "screener_config"}


class ToolRegistry:
    """工具注册表 — 扫描 tools/*.py，自动发现并包装为 Tool 实例。"""

    def __init__(self):
        self._tools: Dict[str, Tool] = {}
        self._discovered = False
        self._tools_dir = Path(__file__).parent.resolve()

    def discover(self):
        """扫描 tools 目录，发现所有公开函数并注册为 Tool。"""
        if self._discovered:
            return
        self._discovered = True

        for py_file in sorted(self._tools_dir.glob("*.py")):
            module_name = py_file.stem
            if module_name.startswith("_") or module_name in _SKIP_FILES:
                continue
            try:
                mod = importlib.import_module(f"app.agent.tools.{module_name}")
            except Exception:
                logger.debug("[ToolRegistry] 跳过 %s", module_name, exc_info=True)
                continue

            for attr_name in dir(mod):
                if attr_name.startswith("_"):
                    continue
                obj = getattr(mod, attr_name)
                if not callable(obj) or not inspect.isfunction(obj):
                    continue
                if getattr(obj, "__module__", "") != mod.__name__:
                    continue
                if not inspect.getdoc(obj):
                    continue
                try:
                    tool = _func_to_tool(obj)
                    self._tools[tool.name] = tool
                except Exception as e:
                    logger.debug("[ToolRegistry] 包装 %s 失败: %s", attr_name, e)

        logger.info("[ToolRegistry] 发现 %d 个工具", len(self._tools))

    def add(self, tool: Tool):
        """手动注册 Tool 实例。"""
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def list_tools(self) -> List[str]:
        return sorted(self._tools.keys())

    def get_function_schemas(self) -> List[dict]:
        """返回所有工具的 OpenAI Function Calling Schema（带 type: function 包装）。"""
        return [
            {"type": "function", "function": tool.get_function_schema()}
            for tool in self._tools.values()
        ]

    async def call(self, name: str, **kwargs) -> ToolResult:
        """按名执行工具。"""
        tool = self._tools.get(name)
        if not tool:
            return ToolResult(success=False, error=f"工具不存在: '{name}'")
        return await tool.safe_execute(**kwargs)

    async def call_from_llm_response(self, tool_call: dict) -> ToolResult:
        """从 LLM 的 tool_call 响应中执行工具。"""
        func_info = tool_call.get("function", {})
        name = func_info.get("name", "")
        arguments_str = func_info.get("arguments", "{}")
        try:
            kwargs = json.loads(arguments_str) if isinstance(arguments_str, str) else arguments_str
        except (json.JSONDecodeError, TypeError):
            kwargs = {}
        return await self.call(name, **kwargs)

    def __len__(self):
        return len(self._tools)

    def __contains__(self, name: str):
        return name in self._tools


# ── 模块级单例 ────────────────────────────────────────────────
_registry: Optional[ToolRegistry] = None


def get_local_registry() -> ToolRegistry:
    """获取全局 ToolRegistry 单例。"""
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry


# 便捷引用
registry: ToolRegistry = get_local_registry()
