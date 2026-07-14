# -*- coding: utf-8 -*-
"""
LLM 多模型抽象层

默认使用 QuantDinger 的 LLMService（读取 backend_api_python/.env），
保留原始 DashScope / OpenAI 实现以兼容独立运行场景。

使用方式：
    from app.agent.llm import create_llm, QDToolAdapter, QDSkillAdapter

    llm = create_llm()
    tools = QDToolAdapter()
    skills = QDSkillAdapter()
"""

from .base import LLMBase, LLMResponse, ChatMessage
from .factory import LLMFactory, create_llm, list_providers, register_provider

try:
    from .qd_llm import QDLLM
except ImportError:
    QDLLM = None

try:
    from .qd_tools import QDToolAdapter, run_with_tools
except ImportError:
    QDToolAdapter = None
    run_with_tools = None

try:
    from .qd_skills import QDSkillAdapter
except ImportError:
    QDSkillAdapter = None

__all__ = [
    "LLMBase",
    "LLMResponse",
    "ChatMessage",
    "LLMFactory",
    "create_llm",
    "list_providers",
    "register_provider",
    "QDLLM",
    "QDToolAdapter",
    "QDSkillAdapter",
    "run_with_tools",
]
