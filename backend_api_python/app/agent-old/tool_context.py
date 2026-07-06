# -*- coding: utf-8 -*-
"""
Tool Context — 运行时上下文注入。

使用 contextvars + 全局 dict 双写，确保 smolagents 子线程也能读到。

被调用方：
  agent.py → set_tool_context() → 注入 session_id, user_id, domain
  tools/*.py → get_tool_context() → 读取当前上下文

公开接口：
  set_tool_context(ctx: Dict) → None
  get_tool_context() → Dict
  get_session_id() → str
  get_user_id() → int
  get_domain() → str
"""
from __future__ import annotations

import contextvars
import threading
from app.agent.log import logger
from typing import Any, Callable, Dict, Optional

# ContextVar（async 安全）
_tool_context_var: contextvars.ContextVar[Dict[str, Any]] = contextvars.ContextVar(
    "tool_context", default={}
)

# 全局 dict 兜底（smolagents 子线程 contextvars 会丢失）
_tool_context_global: Dict[str, Any] = {}
_global_lock = threading.Lock()


def set_tool_context(ctx: Dict[str, Any]):
    """Set the current tool context. 双写 contextvars + 全局 dict。"""
    _tool_context_var.set(ctx)
    with _global_lock:
        _tool_context_global.clear()
        _tool_context_global.update(ctx)


def get_tool_context() -> Dict[str, Any]:
    """Get the current tool context. 优先 contextvars，兜底全局 dict。"""
    cv = _tool_context_var.get({})
    if cv:
        return cv
    with _global_lock:
        return dict(_tool_context_global)


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
    """Get current progress_callback from context."""
    return get_tool_context().get("progress_callback")


def emit_progress(event: Dict[str, Any]):
    """Emit a progress event via the current callback (if any)."""
    cb = get_tool_context().get("progress_callback")
    if cb:
        try:
            cb(event)
        except Exception:
            logger.warning("progress_callback 抛异常", exc_info=True)
