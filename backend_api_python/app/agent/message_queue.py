# -*- coding: utf-8 -*-
"""
message_queue.py — 统一消息队列

Flask 和 Cron 共用同一个队列 + worker 线程池，
所有消息走同一条链路: submit → queue → worker → agent.chat。

用法：
    from message_queue import submit, init_workers
    init_workers(4)
    future = submit("提醒我上网", session_id="default")
    result = future.result(timeout=300)  # 阻塞等待
"""
from __future__ import annotations

import logging
import queue
import threading
from concurrent.futures import Future
from typing import Optional

logger = logging.getLogger(__name__)

# ── 全局队列 + worker 线程池 ────────────────────────────────

_task_queue: queue.Queue = queue.Queue(maxsize=256)
_workers_started = False
_worker_count = 0

# ── 跨终端立即停止（2026-09-17）──
# 停止指令注册表：session_id → stop 标记。CLI Ctrl+C / Web / API 统一走 request_stop()，
# worker 侧的 step callback 与工具守卫每次检查——命中即中止当前 run。
_stop_flags: set = set()
_stop_lock = threading.Lock()


def request_stop(session_id: str) -> None:
    """请求停止指定会话当前正在执行的任务（幂等，线程安全）。

    任何终端都可以调用：CLI Ctrl+C / Flask 端点 / Cron。停止位在任务开始时清除、
    任务结束后保留至下次 start——worker 侧见 _is_stop_requested。
    """
    if not session_id:
        return
    with _stop_lock:
        _stop_flags.add(str(session_id))
    logger.warning("[MQ] 已请求停止会话 %s 的当前任务", session_id)


def clear_stop(session_id: str) -> None:
    """清除停止位（任务真正开始执行时调用，防止残留位误杀新任务）。"""
    with _stop_lock:
        _stop_flags.discard(str(session_id))


def _is_stop_requested(session_id: str) -> bool:
    with _stop_lock:
        return str(session_id) in _stop_flags


def init_workers(n: int = 4):
    """启动 worker 线程池（幂等，只启动一次）。"""
    global _workers_started, _worker_count
    if _workers_started:
        return
    _workers_started = True
    _worker_count = n
    for i in range(n):
        t = threading.Thread(target=_worker_loop, daemon=True, name=f"mq-worker-{i}")
        t.start()
    logger.info("[MQ] 已启动 %d 个 worker 线程", n)


def submit(
    message: str,
    session_id: str = "default",
    timeout: int = 300,
    event_cb=None,
) -> Future:
    """提交消息到统一队列，返回 Future 用于获取结果。

    Args:
        message: 消息内容
        session_id: 会话 ID
        timeout: 超时秒数

    Returns:
        Future[str]，调用 .result(timeout) 阻塞等待结果
    """
    if not _workers_started:
        init_workers()

    future: Future = Future()
    try:
        # 队列满时快速失败而不是无限挂起调用线程（SSE 请求会被拖死，审计 P2）
        _task_queue.put({
            "message": message,
            "session_id": session_id,
            "timeout": timeout,
            "future": future,
            "event_cb": event_cb,
        }, timeout=5)
    except queue.Full:
        raise RuntimeError("Agent 消息队列已满（256），请稍后重试")
    return future


def _worker_loop():
    """Worker 线程：从队列取消息 → agent.chat → 回写结果。"""
    import asyncio
    from agent import agent

    while True:
        task = _task_queue.get()
        if task is None:  # 毒丸
            break

        future: Future = task["future"]
        if future.cancelled():
            continue
        clear_stop(str(task["session_id"]))  # 新任务开始，清掉该会话残留停止位

        try:
            loop = asyncio.new_event_loop()
            try:
                # event_cb 经 _current_event_cb 生效（chat() 读取，避免改公开签名）。
                # 时序关键（2026-09-11 修复）：协程创建≠执行——若在协程外设置/恢复，
                # finally 会在 run_until_complete 真正执行协程体之前把回调恢复为 None，
                # 过程事件全部丢失。必须在协程体内设置、finally 恢复；
                # 超时取消时 finally 同样执行，不会泄漏。
                _prev_cb = getattr(agent, "_current_event_cb", None)

                # 协作取消（2026-09-17 Ctrl+C 修复）：worker 线程收不到 SIGINT，
                # 主线程只能置 future 取消位；agent.run 的 step callback 是天然检查点——
                # 每个 step 边界看一眼 future.cancelled()，命中即中止 run，
                # 不再让已"中断"的任务继续烧 token（smolagents 每步都会调 callbacks）。
                class _UserInterruptError(RuntimeError):
                    pass

                def _cancel_check(_step_data):
                    # 触发源：会话停止位（任意终端 request_stop；CLI Ctrl+C 也归一到此）。
                    # 不再查 future.cancelled()——Future.cancel 对运行中任务返回 False
                    # 且不置位（concurrent.futures 语义），查它是坏开关。
                    if _is_stop_requested(str(task["session_id"])):
                        raise _UserInterruptError("user interrupted")

                async def _run_with_events():
                    _cb = task.get("event_cb")
                    if _cb is not None:
                        agent._current_event_cb = _cb
                    _prev_cbs = getattr(agent, "_user_step_callbacks", None)
                    agent._user_step_callbacks = list(_prev_cbs or []) + [_cancel_check]

                    # 立即停止（2026-09-17）：工具级中断探针（task_agent._INTERRUPT_CHECKS）
                    # 三个触发源：future 取消位（CLI Ctrl+C）/ 会话停止位（任意终端
                    # request_stop）/ smolagents interrupt_switch（TaskAgent 转发 interrupt()）。
                    # 工具守卫每次工具调用进入前执行探针 → step 内即停，不等步边界。
                    from agents import task_agent as _ta_mod

                    def _interrupt_probe():
                        # 只查停止位 + interrupt_switch（见 _cancel_check 注释：
                        # future.cancelled() 对运行中任务恒 False，是坏开关）
                        if _is_stop_requested(str(task["session_id"])):
                            raise _UserInterruptError("user interrupted")
                        _ca = getattr(agent, "_active_code_agent", None)
                        if _ca is not None and getattr(_ca, "interrupt_switch", False):
                            raise _UserInterruptError("user interrupted")

                    _prev_checks = list(_ta_mod._INTERRUPT_CHECKS)
                    _ta_mod._INTERRUPT_CHECKS.append(_interrupt_probe)
                    try:
                        return await agent.chat(task["message"], session_id=task["session_id"])
                    except _UserInterruptError:
                        raise
                    finally:
                        agent._current_event_cb = _prev_cb
                        agent._user_step_callbacks = _prev_cbs
                        _ta_mod._INTERRUPT_CHECKS[:] = _prev_checks

                resp = loop.run_until_complete(asyncio.wait_for(_run_with_events(), timeout=task["timeout"]))
                future.set_result(resp.content or "")
            finally:
                # 不 close 共享 LLM 客户端（审计 P0-3）：客户端绑定 event loop 且全局共享，
                # 任务级 close 会误杀并发 worker 的在途请求，且下一个任务在新 loop 上复用
                # 已关闭的客户端会随机报 "Event loop is closed"。连接池随进程存活。
                loop.close()
        except (Exception, asyncio.CancelledError) as e:
            if isinstance(e, asyncio.CancelledError):
                logger.warning("[MQ] Worker 任务超时被取消（放行）")
                future.set_result("")
            elif type(e).__name__ == "_UserInterruptError":
                # 用户停止（CLI Ctrl+C / Web request_stop）：安静收尾。
                # set_result('') 让仍阻塞在 result() 的调用方立刻解除（不抛给无人等的场景）。
                logger.warning("[MQ] Worker 任务被用户停止，已中止 agent.run")
                try:
                    future.set_result("")
                except Exception:
                    pass
            else:
                logger.error("[MQ] Worker 异常: %s", e, exc_info=True)
                future.set_exception(e)
