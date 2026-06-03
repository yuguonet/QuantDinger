# -*- coding: utf-8 -*-
"""
Agent module — smolagents-based ReAct Tool-Calling Agent for QuantDinger.

Replaces the previous custom executor/runner with HuggingFace smolagents.
"""
from app.agent.agent import build_agent_executor, get_smolagent
from app.agent.session_store import get_session_store
from app.agent.tool_context import get_tool_context, set_tool_context
from app.agent.router import SemanticIntentRouter, build_default_routes
