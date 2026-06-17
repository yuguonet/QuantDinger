# -*- coding: utf-8 -*-
"""
Agent — Nanobot-powered agent for QuantDinger.

薄壳：build_agent_executor() → _AgentExecutor → 委托给 nanobot_bridge。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class AgentResult:
    def __init__(self, success=False, content="", tool_calls_log=None,
                 total_steps=0, total_tokens=0, model="", error=None, charts=None):
        self.success = success
        self.content = content
        self.tool_calls_log = tool_calls_log or []
        self.total_steps = total_steps
        self.total_tokens = total_tokens
        self.model = model
        self.error = error
        self.charts = charts or []


class _AgentExecutor:
    """委托给 nanobot_bridge.NanobotExecutor。"""

    def __init__(self, skills=None, user_id=1, max_steps=10,
                 timeout_seconds=None, model=None, provider=None):
        from app.agent.nanobot_bridge import NanobotExecutor
        self._impl = NanobotExecutor(
            skills=skills, user_id=user_id, max_steps=max_steps,
            timeout_seconds=timeout_seconds, model=model, provider=provider,
        )
        self._current_agent = None
        self._agent_ready_event = self._impl._agent_ready_event

    def chat(self, message, session_id, context=None,
             progress_callback=None, user_id=1) -> AgentResult:
        return self._impl.chat(message, session_id, context, progress_callback, user_id)

    def chat_stream(self, message, session_id, context=None,
                    progress_callback=None, user_id=1):
        yield from self._impl.chat_stream(message, session_id, context, progress_callback, user_id)


def build_agent_executor(skills=None, user_id=1, max_steps=10,
                         timeout_seconds=None, model=None, provider=None,
                         domain=None) -> _AgentExecutor:
    return _AgentExecutor(
        skills=skills, user_id=user_id, max_steps=max_steps,
        timeout_seconds=timeout_seconds, model=model, provider=provider,
    )
