# -*- coding: utf-8 -*-
"""
Tool Context — inject runtime context (session_id, user_id, progress_callback, etc.)

Uses contextvars for thread-safe, async-safe context propagation.
"""
from __future__ import annotations

import contextvars
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
    return _tool_context.get({}).get("session_id", "")


def get_user_id() -> int:
    """Get current user_id from context."""
    return _tool_context.get({}).get("user_id", 1)


def get_progress_callback() -> Optional[Callable]:
    """Get current progress_callback from context (for real-time streaming)."""
    return _tool_context.get({}).get("progress_callback")


def emit_progress(event: Dict[str, Any]):
    """Emit a progress event via the current callback (if any)."""
    cb = _tool_context.get({}).get("progress_callback")
    if cb:
        try:
            cb(event)
        except Exception:
            pass
