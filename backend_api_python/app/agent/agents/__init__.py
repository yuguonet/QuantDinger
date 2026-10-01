"""
Agent 核心层

v2 迁移后仅保留响应契约（AgentBase/AgentResponse）。
执行核为 mimoagent（见 qd_agent.py / qd_service.py），不再导出 TaskAgent。
"""
from agents.base import AgentBase, AgentResponse

__all__ = ["AgentBase", "AgentResponse"]
