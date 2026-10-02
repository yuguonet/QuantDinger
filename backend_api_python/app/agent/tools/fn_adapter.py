# -*- coding: utf-8 -*-
"""FnToolAdapter — 把现有"裸函数工具"零改动适配成 mimoagent BaseTool。

迁移边界（v2 大包大揽版）：tools/ 整层保留，函数体一行不动，只换外包装。

契约对账（§7.21 三对账，全部沿用现行约定）：
  1. name/description/parameters ← func_to_openai_schema(func)
     （docstring 的 Returns: 段是返回结构契约真源，2026-09-21 收束后不再采样）
  2. 返回值序列化 ← ToolResult.to_str()（现行 token 最省格式：扁平 dict 逐行
     k:v、表形 TSV、嵌套缩进 JSON）
  3. requires_confirm ← 签名带 `confirm` 参数的工具（trading_tools 人工确认闸），
     由 QDAgent 的 ActionInterceptor 消费；函数内 confirm 硬闸保留（双保险）

幻觉参数容忍：模型多给的未知参数不炸——无 **kwargs 的函数静默丢弃未知键并在
metadata 留痕（延续旧系统"幻觉调用纠正"精神）。
"""
from __future__ import annotations

import asyncio
import inspect
from typing import Any, Callable

try:  # 生产运行时（backend_api_python 为根）
    from tools.base import ToolResult, func_to_openai_schema
except ImportError:  # app/agent 直接在 sys.path 时（cli/测试路径）
    from tools.base import ToolResult, func_to_openai_schema

from mimoagent.tools.base import BaseTool, ToolOutput


class FnToolAdapter(BaseTool):
    """裸函数 → mimoagent BaseTool 薄壳。"""

    def __init__(self, func: Callable, *, domain: str = "common", config: dict | None = None):
        super().__init__(config)
        self._func = func
        self._domain = domain
        self._schema = func_to_openai_schema(func)
        # func_to_openai_schema 返回 OpenAI 完整信封 {"type":"function","function":{...}}
        self._fn = self._schema.get("function") or self._schema
        sig = inspect.signature(func)
        self._params = list(sig.parameters)
        self._accepts_var_kw = any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
        # 人工确认闸标记：签名带 confirm 参数（trading_tools 约定）
        self.requires_confirm = "confirm" in sig.parameters

    @property
    def name(self) -> str:
        return self._fn["name"]

    @property
    def description(self) -> str:
        return self._fn.get("description") or (self._func.__doc__ or "").strip().split("\n")[0]

    @property
    def domain(self) -> str:
        return self._domain

    def get_function_parameters(self) -> dict:
        return self._fn.get("parameters") or {"type": "object", "properties": {}}

    def execute(self, params: Any, context: dict | None = None) -> ToolOutput:
        kwargs = dict(params or {})
        # 元工具契约（2026-10-01）：签名里带 `_context` 的工具会自动拿到执行上下文
        # （含 env/model/**当前 agent**）。下划线开头 ⇒ func_to_openai_schema 不写入
        # schema，模型既看不到也传不了，只有本适配器能注入 —— 工具发现类元工具
        # （search_tools/activate_tools）靠它把"激活"登记回当前会话的 agent。
        if "_context" in self._params:
            kwargs["_context"] = context
        dropped: list[str] = []
        if not self._accepts_var_kw:
            for key in list(kwargs):
                if key not in self._params:
                    dropped.append(key)
                    kwargs.pop(key)
        metadata: dict[str, Any] = {"tool": self.name, "domain": self._domain}
        if dropped:
            metadata["dropped_params"] = dropped
        try:
            result = self._func(**kwargs)
            if inspect.isawaitable(result):
                result = asyncio.run(result)
        except Exception as e:  # 工具失败→观察值，不终止循环（mimoagent 约定）
            return ToolOutput(
                output=f"Error: {self.name} 执行失败: {e}",
                success=False,
                metadata=metadata,
            )
        return self._to_output(result, metadata)

    @staticmethod
    def _to_output(result: Any, metadata: dict) -> ToolOutput:
        if isinstance(result, ToolResult):
            text = result.to_str()
            success = bool(result.success)
            if not success and result.error:
                text = f"[工具执行失败] {result.error}"
        elif isinstance(result, dict) and set(result.keys()) == {"error"}:
            # 金融工具失败约定：{"error": ...}
            text = f"Error: {result['error']}"
            success = False
        else:
            text = ToolResult(output=result).to_str()
            success = True
        return ToolOutput(output=text, success=success, metadata=metadata)
