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

        try:
            loop = asyncio.new_event_loop()
            try:
                # event_cb 经 _current_event_cb 生效（chat() 读取，避免改公开签名）。
                # 时序关键（2026-09-11 修复）：协程创建≠执行——若在协程外设置/恢复，
                # finally 会在 run_until_complete 真正执行协程体之前把回调恢复为 None，
                # 过程事件全部丢失。必须在协程体内设置、finally 恢复；
                # 超时取消时 finally 同样执行，不会泄漏。
                _prev_cb = getattr(agent, "_current_event_cb", None)

                async def _run_with_events():
                    _cb = task.get("event_cb")
                    if _cb is not None:
                        agent._current_event_cb = _cb
                    try:
                        return await agent.chat(task["message"], session_id=task["session_id"])
                    finally:
                        agent._current_event_cb = _prev_cb

                resp = loop.run_until_complete(asyncio.wait_for(_run_with_events(), timeout=task["timeout"]))
                future.set_result(resp.content or "")
            finally:
                # 不 close 共享 LLM 客户端（审计 P0-3）：客户端绑定 event loop 且全局共享，
                # 任务级 close 会误杀并发 worker 的在途请求，且下一个任务在新 loop 上复用
                # 已关闭的客户端会随机报 "Event loop is closed"。连接池随进程存活。
                loop.close()
        except Exception as e:
            logger.error("[MQ] Worker 异常: %s", e, exc_info=True)
            future.set_exception(e)
