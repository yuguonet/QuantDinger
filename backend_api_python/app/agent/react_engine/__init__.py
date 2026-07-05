# -*- coding: utf-8 -*-
"""
react_engine — 内嵌 smolagents ReAct 引擎

直接使用 smolagents 原版代码，已去除所有外部依赖。

提供：
  - Tool: 工具基类
  - ChatMessage: 消息数据类
  - CodeAgent: ReAct 代码执行循环
"""

from .tools import Tool, BaseTool
from .models import ChatMessage
from .agents import CodeAgent, MultiStepAgent
from .memory import AgentMemory, ActionStep, TaskStep, SystemPromptStep, FinalAnswerStep

__all__ = [
    "Tool", "BaseTool",
    "ChatMessage",
    "CodeAgent", "MultiStepAgent",
    "AgentMemory", "ActionStep", "TaskStep", "SystemPromptStep", "FinalAnswerStep",
]
