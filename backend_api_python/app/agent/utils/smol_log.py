# -*- coding: utf-8 -*-
"""app/agent/utils/smol_log.py — smolagents 红字错误 → 结构化 error/warn

问题（用户裁定 2026-09-25）：smolagents 出错只打 **bold red** rich 文本
（`AgentLogger.log_error`），既不是 logger.error 也不进 trace 计数 ⇒ 校验环
「错误流量」静默。本模块把红字错误**接管为结构化事件**：

  - `install_structured_logger(agent)`：替换/包装 agent.logger
  - `record_sml_error(msg, kind)`：进 ring buffer + logger.error + 可选 trace
  - `drain_recent_errors()`：自证仪表（原则 7：校验环流量可观测）

纪律：只依赖 smolagents.monitoring 契约，不 import app.agent 业务。
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Any, Deque, Dict, List, Optional

_lock = threading.Lock()
_recent: Deque[Dict[str, Any]] = deque(maxlen=200)
_counters = {"error": 0, "warn": 0}


def record_sml_error(msg: str, *, kind: str = "error", where: str = "") -> Dict[str, Any]:
    """登记一条 smolagents 错误/警告（结构化，不再只靠红字）。"""
    text = str(msg or "").strip()
    k = "warn" if kind == "warn" else "error"
    item = {"kind": k, "msg": text[:500], "where": where}
    with _lock:
        _recent.append(item)
        _counters[k] = _counters.get(k, 0) + 1
    try:
        from app.utils.logger import get_logger
        lg = get_logger("smolagents")
        if k == "error":
            lg.error("[smol.error]%s %s", f" {where}" if where else "", text[:500])
        else:
            lg.warning("[smol.warn]%s %s", f" {where}" if where else "", text[:500])
    except Exception:
        pass
    return item


def drain_recent_errors() -> List[Dict[str, Any]]:
    with _lock:
        return list(_recent)


def error_counters() -> Dict[str, int]:
    with _lock:
        return dict(_counters)


def reset_counters() -> None:
    with _lock:
        _counters.clear()
        _recent.clear()


class StructuredAgentLogger:
    """包装 smolagents AgentLogger：红字错误双写（rich + 结构化）。"""

    def __init__(self, inner: Any = None):
        self._inner = inner

    def log_error(self, error_message: str) -> None:
        record_sml_error(error_message, kind="error", where="AgentLogger.log_error")
        if self._inner is not None:
            try:
                self._inner.log_error(error_message)
            except Exception:
                pass

    def log(self, *args: Any, **kwargs: Any) -> None:
        level = kwargs.get("level")
        # 只把 **真正的错误级** 变成结构化 error；Final answer / rich Group 不是错误
        try:
            from smolagents.monitoring import LogLevel
            is_err = level is not None and int(level) == int(LogLevel.ERROR)
        except Exception:
            is_err = level is not None and int(level) >= 2
        if is_err:
            text = " ".join(str(a) for a in args if not type(a).__name__.endswith("Group"))
            if text.strip():
                record_sml_error(text, kind="error", where="AgentLogger.log")
        if self._inner is not None:
            try:
                return self._inner.log(*args, **kwargs)
            except Exception:
                return None
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def install_structured_logger(agent: Any) -> Any:
    """给 agent 装上结构化 logger（有 logger 槽则包装，没有则装壳）。"""
    try:
        inner = getattr(agent, "logger", None)
        agent.logger = StructuredAgentLogger(inner)
    except Exception:
        try:
            agent.logger = StructuredAgentLogger(None)
        except Exception:
            pass
    return agent


def extract_step_error(memory_step: Any) -> Optional[Dict[str, Any]]:
    """从 ActionStep 抽出 error 并登记（红字错误的孪生采集点）。"""
    err = getattr(memory_step, "error", None)
    if not err:
        return None
    msg = getattr(err, "message", None) or str(err)
    etype = type(err).__name__
    item = record_sml_error(f"{etype}: {msg}", kind="error", where="ActionStep.error")
    item["error_type"] = etype
    return item
