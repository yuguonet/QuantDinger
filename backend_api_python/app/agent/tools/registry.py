# -*- coding: utf-8 -*-
"""
Tool Registry — register, discover, and execute agent tools.

Each tool is a plain function decorated with metadata (name, description,
parameters schema).  The registry converts them into OpenAI function-calling
format and dispatches execution by name.
"""
from __future__ import annotations

import inspect
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ToolSpec:
    """Internal representation of a registered tool."""
    name: str
    description: str
    parameters: Dict[str, Any]          # JSON Schema for parameters
    fn: Callable
    required_params: List[str] = field(default_factory=list)


class ToolRegistry:
    """Central registry for agent tools. Thread-safe."""

    def __init__(self):
        self._tools: Dict[str, ToolSpec] = {}
        self._lock = threading.Lock()

    # ── registration ─────────────────────────────────────────

    def register(self, fn: Callable, *, name: str = None,
                 description: str = None,
                 parameters: Dict[str, Any] = None,
                 required: List[str] = None) -> None:
        """Register a tool function.

        If *description* / *parameters* are not given, they are extracted
        from the function's docstring and type hints (best-effort).
        """
        tool_name = name or fn.__name__
        spec = ToolSpec(
            name=tool_name,
            description=description or (inspect.getdoc(fn) or "").strip(),
            parameters=parameters or {"type": "object", "properties": {}},
            fn=fn,
            required_params=required or [],
        )
        with self._lock:
            self._tools[tool_name] = spec

    def register_many(self, tools: List[Dict[str, Any]]) -> None:
        """Batch register tools from dicts with keys: fn, name, description, parameters, required."""
        for t in tools:
            self.register(
                fn=t["fn"],
                name=t.get("name"),
                description=t.get("description", ""),
                parameters=t.get("parameters", {}),
                required=t.get("required", []),
            )

    # ── discovery ─────────────────────────────────────────────

    def list_tools(self) -> List[str]:
        with self._lock:
            return list(self._tools.keys())

    def get_spec(self, name: str) -> Optional[ToolSpec]:
        with self._lock:
            return self._tools.get(name)

    def to_openai_tools(self) -> List[Dict[str, Any]]:
        """Convert all registered tools to OpenAI function-calling format."""
        with self._lock:
            tools_snapshot = list(self._tools.values())
        tools = []
        for spec in tools_snapshot:
            tools.append({
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            })
        return tools

    # ── execution ─────────────────────────────────────────────

    def execute(self, name: str, **kwargs) -> Any:
        """Execute a tool by name.  Raises KeyError if not found."""
        with self._lock:
            spec = self._tools.get(name)
        if spec is None:
            return {"error": f"Unknown tool: {name}", "retriable": False}
        t0 = time.time()
        try:
            result = spec.fn(**kwargs)
            dur = round(time.time() - t0, 2)
            logger.info("Tool '%s' completed in %.2fs", name, dur)
            return result
        except Exception as e:
            dur = round(time.time() - t0, 2)
            logger.error("Tool '%s' failed after %.2fs: %s", name, dur, e, exc_info=True)
            return {"error": str(e), "retriable": True}
