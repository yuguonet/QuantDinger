# -*- coding: utf-8 -*-
"""
Tool Context — 运行时上下文注入。

使用 contextvars 实现线程安全、异步安全的上下文传播。
工具执行时可读取 session_id / user_id / progress_callback / domain 等信息。

被调用方：
  agent.py → set_tool_context() → 注入 session_id, user_id, domain
  tools/*.py → get_tool_context() → 读取当前上下文

公开接口：
  set_tool_context(ctx: Dict) → None
  get_tool_context() → Dict
  get_tool_context_value(key, default) → Any
"""
from __future__ import annotations

import contextvars
from app.agent.log import logger
from typing import Any, Callable, Dict, Optional

# Context variable for current tool call context
_tool_context: contextvars.ContextVar[Dict[str, Any]] = contextvars.ContextVar(
    "tool_context", default={}
)
def set_tool_context(ctx: Dict[str, Any]):
    """Set the current tool context (called before agent loop)."""
    _tool_context.set(ctx)
def get_tool_context() -> Dict[str, Any]:
    """Get the current tool context."""
    return _tool_context.get({})
def get_session_id() -> str:
    """Get current session_id from context."""
    return get_tool_context().get("session_id", "")
def get_user_id() -> int:
    """Get current user_id from context."""
    return get_tool_context().get("user_id", 1)
def get_domain() -> str:
    """Get current domain from context."""
    return get_tool_context().get("domain", "default")
def get_progress_callback() -> Optional[Callable]:
    """Get current progress_callback from context (for real-time streaming)."""
    return get_tool_context().get("progress_callback")
def emit_progress(event: Dict[str, Any]):
    """Emit a progress event via the current callback (if any)."""
    cb = get_tool_context().get("progress_callback")
    if cb:
        try:
            cb(event)
        except Exception:
            logger.warning("progress_callback 抛异常", exc_info=True)
