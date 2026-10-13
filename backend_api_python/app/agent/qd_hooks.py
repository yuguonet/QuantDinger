# -*- coding: utf-8 -*-
"""会话钩子表 —— run 生命周期内的外部注入点（事件回调 / 每步检查 / 中断探针）。

从 `qd_agent.py` 拆出（2026-10-13 P3 拆分）。承载原先混在执行核里的钩子注册与
线程局部绑定。

【为什么用线程局部 + 全局表两层】
run 跑在 `to_thread` 出来的工作线程里，而注册者是请求线程 ⇒ **同一个
session_id 可能并发**、且线程会复用（线程池）。若只存 session_id 到线程局部，
换线程后就读不到注册内容 —— `qd_service.py:451` 在 to_thread 之后显式重绑
（`bind_run_session(session_id)`）就是补这一环。

【对外 API 名】与退役的 `agents/task_agent.py` 保持一致，`message_queue` 只需
换 import 行。三个语义：
  · event_cb    —— 过程事件（→ SSE 推送给前端）
  · step_checks —— 每步开头检查，**可抛错中止**（异常原样上抛，由调用方收口）
  · probes      —— 工具派发前中断探针（命中转 `_ProbeInterrupt` 终止 run）

易错点：`_hooks_now()` 返回的是**表内字典本身**（不是拷贝），
`run()` 往里写 `active_agent` 就是靠这个。别改成返回拷贝，否则
`run()` 的 `finally` 清理会失效（active_agent 永远在表里）。
"""
from __future__ import annotations

import threading

from mimoagent.agents.base import TerminatingException


class _ProbeInterrupt(TerminatingException):
    """探针命中（用户停止）→ 终止 run，状态名即类名。

    ★ 类名即契约：`qd_service.py:496` 用 `status == "_ProbeInterrupt"` 匹配，
    改名必须同步改那里。
    """


_HOOKS: dict = {}          # session_id -> {event_cb, step_checks, probes, active_agent}
_HOOKS_LOCK = threading.Lock()
_HOOKS_LOCAL = threading.local()


def bind_run_session(session_id: str) -> None:
    """把当前线程绑定到 session_id（换线程后必须重新绑）。"""
    _HOOKS_LOCAL.session_id = str(session_id)


def register_run_hooks(session_id: str, *, event_cb=None, step_checks=None, probes=None) -> None:
    with _HOOKS_LOCK:
        _HOOKS[str(session_id)] = {
            "event_cb": event_cb,
            "step_checks": list(step_checks or ()),
            "probes": list(probes or ()),
        }


def unregister_run_hooks(session_id: str) -> None:
    with _HOOKS_LOCK:
        _HOOKS.pop(str(session_id), None)


def _hooks_now() -> dict:
    """当前线程绑定的钩子；未绑定 → 空字典（所有钩子点自然降级为 no-op）。"""
    sid = getattr(_HOOKS_LOCAL, "session_id", None)
    if sid is None:
        return {}
    with _HOOKS_LOCK:
        return _HOOKS.get(sid) or {}
