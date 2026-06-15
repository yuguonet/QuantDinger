# -*- coding: utf-8 -*-
"""
TracedTool — 包装原始工具，自动记录调用信息到 TraceCollector。

对 agent 透明：工具行为完全不变，只是每次调用自动触发 collector.on_tool_call()。

注意：不能直接继承 smolagents.Tool（其 __init__ 会校验 forward 签名必须和 inputs 一致，
TracedTool 的 forward 用 **kwargs 无法通过校验）。改用 BaseTool.register() 让
isinstance(tool, BaseTool) 检查通过。
"""
from __future__ import annotations

import time
from typing import Any

from smolagents import Tool
from smolagents.tools import BaseTool

from app.agent.trace_collector import TraceCollector


class TracedTool:
    """包装原始工具，自动记录调用信息。"""

    # smolagents 框架附加在所有工具调用上的参数，需剥离后才传给底层工具
    _FRAMEWORK_KWARGS = {"sanitize_inputs_outputs"}

    def __init__(self, original_tool, collector: TraceCollector):
        self._tool = original_tool
        self._collector = collector
        # 保持原始工具的所有属性（smolagents 需要这些）
        self.name = original_tool.name
        self.description = original_tool.description
        self.inputs = getattr(original_tool, 'inputs', {})
        self.output_type = getattr(original_tool, 'output_type', 'text')

    @staticmethod
    def _strip_framework_kwargs(kwargs: dict) -> dict:
        """移除 smolagens 框架附加参数，只保留工具真实参数。"""
        return {k: v for k, v in kwargs.items() if k not in TracedTool._FRAMEWORK_KWARGS}

    def forward(self, *args, **kwargs) -> Any:
        t0 = time.time()
        error = None
        result = None
        try:
            if args:
                # 位置参数调用：将 args 映射到原始工具 forward 的参数名
                import inspect
                sig = inspect.signature(self._tool.forward)
                param_names = [p for p in sig.parameters if p != "self"]
                merged = dict(zip(param_names, args))
                merged.update(kwargs)
                result = self._tool.forward(**self._strip_framework_kwargs(merged))
            else:
                result = self._tool.forward(**self._strip_framework_kwargs(kwargs))
        except Exception as e:
            error = str(e)
            raise
        finally:
            # 构建完整参数记录（合并位置参数和关键字参数）
            if args:
                import inspect
                sig = inspect.signature(self._tool.forward)
                param_names = [p for p in sig.parameters if p != "self"]
                full_kwargs = dict(zip(param_names, args))
                full_kwargs.update(kwargs)
            else:
                full_kwargs = kwargs
            elapsed = (time.time() - t0) * 1000
            self._collector.on_tool_call(
                tool_name=self.name,
                arguments=full_kwargs,
                result=result,
                elapsed_ms=elapsed,
                error=error,
            )
        return result

    def __call__(self, *args, **kwargs) -> Any:
        """让 TracedTool 可以直接像函数一样调用。

        支持两种调用方式（CodeAgent 生成的代码可能用任一种）：
            get_realtime_quote(stock_code="600066")   # 关键字参数
            get_realtime_quote("600066")               # 位置参数
        """
        return self.forward(*args, **kwargs)

    def to_code_prompt(self) -> str:
        """代理原始工具的 to_code_prompt，smolagents Jinja 模板渲染系统提示词时需要。"""
        return self._tool.to_code_prompt()

    def to_tool_calling_prompt(self) -> str:
        """代理原始工具的 to_tool_calling_prompt。"""
        return self._tool.to_tool_calling_prompt()

    def __repr__(self):
        return f"TracedTool({self.name})"


# 让 isinstance(traced_tool, BaseTool) 返回 True，通过 smolagents 的类型检查
BaseTool.register(TracedTool)
