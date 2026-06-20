# -*- coding: utf-8 -*-
"""
Local ToolRegistry for agent.

Auto-discovers tool functions from agent/tools/ and wraps them as
smolagents Tool objects via the smolagents `tool` decorator.

Usage:
    from app.agent.tools import registry
    registry.discover()
    tools = build_smolagent_tools({"deny": [...], "domain": ...})
    spec = registry.get("search_stock_by_name")
    spec.fn(stock_code="600066")
"""
from __future__ import annotations

import importlib
import inspect
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from smolagents import tool as smolagents_tool

logger = logging.getLogger(__name__)


class _ToolSpec:
    """薄的 spec 包装，提供 .fn 属性供 skill 脚本调用。"""

    def __init__(self, fn: Callable, name: str, description: str):
        self.fn = fn
        self.name = name
        self.description = description


class ToolRegistry:
    """本地工具注册表 — 扫描 agent/tools/ 并包装为 smolagents Tool 对象。"""

    def __init__(self):
        self._tools: Dict[str, _ToolSpec] = {}
        self._smolagent_tools: Dict[str, Any] = {}  # name → smolagents Tool instance
        self._discovered = False
        self._tools_dir = Path(__file__).parent.resolve()

    def discover(self):
        """扫描 tools 目录，发现所有公开函数并注册。"""
        if self._discovered:
            return
        self._discovered = True

        for py_file in sorted(self._tools_dir.glob("*.py")):
            module_name = py_file.stem
            if module_name.startswith("_"):
                continue

            try:
                mod = importlib.import_module(f"app.agent.tools.{module_name}")
            except Exception:
                logger.debug("[ToolRegistry] 跳过模块 %s: %s", module_name, exc_info=True)
                continue

            for attr_name in dir(mod):
                if attr_name.startswith("_"):
                    continue
                obj = getattr(mod, attr_name)
                if not callable(obj):
                    continue
                # 必须是普通函数（非 class）
                if not inspect.isfunction(obj):
                    continue
                # 必须定义在当前模块（不是导入的）
                if getattr(obj, '__module__', '') != mod.__name__:
                    continue
                doc = inspect.getdoc(obj)
                if not doc:
                    continue
                # 跳过返回 Callable 的装饰器函数（smolagents 不支持）
                hints = inspect.get_annotations(obj, eval_str=False)
                ret = hints.get('return', '')
                if 'Callable' in str(ret):
                    continue

                spec = _ToolSpec(
                    fn=obj,
                    name=attr_name,
                    description=doc.split("\n")[0][:500],
                )
                self._tools[attr_name] = spec

    def _wrap_as_smolagent(self, name: str) -> Any:
        """将指定工具包装为 smolagents Tool（惰性）。"""
        if name in self._smolagent_tools:
            return self._smolagent_tools[name]
        spec = self._tools.get(name)
        if spec is None:
            raise KeyError(f"工具 '{name}' 未注册")
        # smolagents.tool() 要求函数有完整 type hints
        tool_obj = smolagents_tool(spec.fn)
        self._smolagent_tools[name] = tool_obj
        return tool_obj

    def get(self, name: str) -> Optional[_ToolSpec]:
        """获取工具 spec（含 .fn 属性）。"""
        return self._tools.get(name)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools


def build_smolagent_tools(config: Optional[Dict[str, Any]] = None) -> List[Any]:
    """构建 smolagent 兼容工具列表。

    Args:
        config: 可选配置字典，支持:
            - deny: List[str] — 排除的工具名列表
            - domain: str — 领域过滤（当前未实现全部过滤）

    Returns:
        可用于 smolagents CodeAgent/ToolCallingAgent 的工具列表
    """
    registry = ToolRegistry()
    registry.discover()
    config = config or {}
    deny = set(config.get("deny", []) or [])
    domain = config.get("domain", "")

    tools = []
    for name in sorted(registry._tools.keys()):
        if name in deny or name == "final_answer":
            continue
        tool_obj = registry._wrap_as_smolagent(name)
        tools.append(tool_obj)
    return tools


# ── 模块级单例 ──────────────────────────────────────────────────
_registry: Optional[ToolRegistry] = None


def get_local_registry() -> ToolRegistry:
    """获取（或创建）全局 ToolRegistry 单例。"""
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry
