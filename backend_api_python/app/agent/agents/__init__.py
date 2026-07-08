"""
Agent 核心层

提供 TaskAgent（统一的任务型 Agent，支持 direct/execute 两种阶段类型）。
"""
from agents.base import AgentBase, AgentResponse
from agents.task_agent import TaskAgent

__all__ = ["AgentBase", "AgentResponse", "TaskAgent"]
