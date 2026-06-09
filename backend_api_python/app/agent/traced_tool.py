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

    def __init__(self, original_tool, collector: TraceCollector):
        self._tool = original_tool
        self._collector = collector
        # 保持原始工具的所有属性（smolagents 需要这些）
        self.name = original_tool.name
        self.description = original_tool.description
        self.inputs = getattr(original_tool, 'inputs', {})
        self.output_type = getattr(original_tool, 'output_type', 'text')

    def forward(self, **kwargs) -> Any:
        t0 = time.time()
        error = None
        result = None
        try:
            result = self._tool.forward(**kwargs)
        except Exception as e:
            error = str(e)
            raise
        finally:
            elapsed = (time.time() - t0) * 1000
            self._collector.on_tool_call(
                tool_name=self.name,
                arguments=kwargs,
                result=result,
                elapsed_ms=elapsed,
                error=error,
            )
        return result

    def __repr__(self):
        return f"TracedTool({self.name})"


# 让 isinstance(traced_tool, BaseTool) 返回 True，通过 smolagents 的类型检查
BaseTool.register(TracedTool)
