# -*- coding: utf-8 -*-
"""会话钩子表 —— run 生命周期内的外部注入点（事件回调 / 每步检查 / 中断探针）。

从 `qd_agent.py` 拆出（2026-10-13 P3 拆分）。承载原先混在执行核里的钩子注册与
线程局部绑定。

【为什么用 ContextVar + 全局表两层（F7，2026-10-17 由 threading.local 改）】
run 跑在 `to_thread` 出来的工作线程里，而注册者是请求线程 ⇒ **同一个
session_id 可能并发**、且线程会复用（线程池）。若只存 session_id 到线程局部
（threading.local），线程复用时可能残留**上一次**的 session_id（张冠李戴）。
`ContextVar` 配合 `asyncio.to_thread` 的 `copy_context()`：绑定随上下文进工作
线程、每次调用都是新副本 ⇒ 不串号、并发任务间天然隔离。全局表仅作「活跃会话
索引」（按 session_id 存钩子字典）；`bind_run_session(session_id)` 仍保留，
用于换线程/换任务后重新绑定。

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

import contextvars
import threading
from typing import Optional

from mimoagent.agents.base import TerminatingException


class _ProbeInterrupt(TerminatingException):
    """探针命中（用户停止）→ 终止 run，状态名即类名。

    ★ 类名即契约：`qd_service.py:496` 用 `status == "_ProbeInterrupt"` 匹配，
    改名必须同步改那里。
    """


_HOOKS: dict = {}          # session_id -> {event_cb, step_checks, probes, active_agent}
_HOOKS_LOCK = threading.Lock()
# 当前上下文的会话绑定（F7）：ContextVar 而非 threading.local。
#   · `asyncio.to_thread` 每次调用 `copy_context()` ⇒ 绑定随上下文进工作线程，
#     线程池复用不残留上一次的 session_id；
#   · 并发任务/协程间天然隔离，`set` 不跨任务泄漏。
# 钩子字典本身仍按 session_id 存 `_HOOKS`（"活跃会话索引"）—— `_hooks_now()`
# 用当前 session_id 去查，`unregister_run_hooks` 注销后立即查不到（语义同改造前）。
_RUN_SESSION: contextvars.ContextVar = contextvars.ContextVar(
    "qd_hooks_session", default=None)


def bind_run_session(session_id: str) -> None:
    """把当前上下文绑定到 session_id（换线程/换任务后必须重新绑）。"""
    _RUN_SESSION.set(str(session_id))


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
    """当前上下文绑定的钩子；未绑定 → 空字典（所有钩子点自然降级为 no-op）。"""
    sid = _RUN_SESSION.get()
    if sid is None:
        return {}
    with _HOOKS_LOCK:
        return _HOOKS.get(sid) or {}


def current_session_id() -> Optional[str]:
    """当前上下文绑定的 session_id（未绑定 → None）。

    F7：对外暴露绑定读取入口，替代原先外部直接读 `_HOOKS_LOCAL.session_id`
    （threading.local 私有属性）。调用方自行决定 None 的回落（如 qd_aftercare
    用 "default"）。
    """
    return _RUN_SESSION.get()
