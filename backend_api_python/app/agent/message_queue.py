# -*- coding: utf-8 -*-
"""
message_queue.py — 统一消息队列

Flask 和 Cron 共用同一个队列 + worker 线程池，
所有消息走同一条链路: submit → queue → worker → agent.chat。

用法：
    from app.agent.message_queue import submit, init_workers
    init_workers(4)
    future = submit("提醒我上网", session_id="default")
    result = future.result(timeout=300)  # 阻塞等待
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import uuid
from concurrent.futures import Future, InvalidStateError
from typing import Optional

logger = logging.getLogger(__name__)


def _settle(future: Future, result=None, exc: Optional[BaseException] = None) -> None:
    """给 Future 落终态，**永不抛**（worker 线程保活的前提）。

    concurrent.futures 语义：Future 只能落一次终态，对已取消/已完成的 Future
    再 set_result / set_exception 抛 `InvalidStateError`。worker 里这个异常会
    一路逃出 `while True` ⇒ **worker 线程永久死亡**（消费者悄悄少一个，队列
    堆积且不报错）。典型触发：调用方 `cancel()` 后 worker 仍跑完去 set_result。
    """
    if future is None or future.cancelled() or future.done():
        return
    try:
        if exc is not None:
            future.set_exception(exc)
        else:
            future.set_result(result)
    except InvalidStateError:
        pass  # 竞态下终态已被别处落下，目的已达成


# ── F4 续跑凭据识别（2026-09-28 修 E7 读侧）────────────────
# 已文档化事故：resume.py 落队的凭据 JSON 此前被 worker 当普通用户文本
# 跑全新任务（finalize_failed 时 nodes.py 主动 enqueue），产出垃圾回复。
# 修法：worker 取到消息先识别 type=agent_resume，改写成「续跑上下文
# prompt」重新 plan——兑现 resume.py 文件头「worker 从下一 phase
# 重新 plan-续跑」的设计语义。不做 state 级重建（_code_agent 不可序列化）。
def _rewrite_resume_message(message: str) -> str:
    """agent_resume 凭据 JSON → 续跑上下文 prompt；普通消息原样返回（零开销）。"""
    s = (message or "").strip()
    if not (s.startswith("{") and s.endswith("}")):
        return message
    try:
        cred = json.loads(s)
    except Exception:
        return message
    if not isinstance(cred, dict) or cred.get("type") != "agent_resume":
        return message
    task = str(cred.get("task") or cred.get("effective_input")
               or cred.get("user_input") or "")
    done_text = str(cred.get("completed_phases_text") or "").strip()
    reason = str(cred.get("reason") or "budget_or_timeout")
    done_n = len(cred.get("phase_results") or [])
    parts = [
        f"[续跑] 上一轮任务因 {reason} 中断，已完成 {done_n} 个阶段，请继续完成剩余工作。",
        f"原任务：{task}",
    ]
    if done_text:
        parts.append("已完成阶段摘要（结论可直接引用，勿重复执行）：\n" + done_text)
    parts.append("请基于以上进度重新规划缺口部分并执行，最终给出完整结论。")
    logger.info("[MQ] 识别 agent_resume 凭据（session=%s, 已完成 %d 阶段），改写为续跑 prompt",
                cred.get("session_id"), done_n)
    return "\n\n".join(parts)


# ── 全局队列 + worker 线程池 ────────────────────────────────

_task_queue: queue.Queue = queue.Queue(maxsize=256)
_workers_started = False
_worker_count = 0

# ── 跨终端立即停止（2026-09-17 / A-02 止血① 2026-10-14）──
# 停止位是 **run 粒度**（不是 session 粒度）：每个 submit 分配一个 run_id，停止位
# 登记在 run_id 上。原因——同会话可并发多个 run（多标签页 / cron 多 job），若按
# session 登记，一个 run 超时 `request_stop(session)` 会把同会话**其他正常运行的
# run 一并误杀**（审计 A-02「误杀邻居」）。
# 映射：_active[run_id] = session_id（worker 拉到任务时登记、结束时注销），供
# `request_stop(session)` 把"用户停会话"翻译成"停该会话当前所有活跃 run"——
# 已结束 / 尚未开始的 run 不受影响（既不误杀邻居，也不会因新任务启动把停止位误留）。
_stop_runs: set = set()              # 已请求停止的 run_id
_active: dict = {}                  # run_id -> session_id（当前正在执行的任务）
_stop_lock = threading.Lock()


def request_stop(session_id: str) -> None:
    """停止指定会话**当前正在执行**的所有任务（按 run 粒度，互不误杀）。

    任何终端都可以调用：CLI Ctrl+C / Flask 端点 / Cron。把 session 翻译成当前活跃
    run 后登记停止位；已结束或尚未开始的 run 不受影响。
    """
    if not session_id:
        return
    with _stop_lock:
        targets = [rid for rid, sid in _active.items() if sid == str(session_id)]
        _stop_runs.update(targets)
    logger.warning("[MQ] 已请求停止会话 %s 的当前任务（run=%s）", session_id, targets)


def request_stop_run(run_id: str) -> None:
    """停止单个 run（worker 超时分支用：只停自己的 run，不波及其他）。"""
    if not run_id:
        return
    with _stop_lock:
        _stop_runs.add(str(run_id))


def clear_stop_run(run_id: str) -> None:
    """清除单个 run 的停止位（任务真正开始执行时调用，防止残留位误杀新任务）。"""
    with _stop_lock:
        _stop_runs.discard(str(run_id))


def is_stop_requested_run(run_id: str) -> bool:
    with _stop_lock:
        return str(run_id) in _stop_runs


def is_session_stop_requested(session_id: str) -> bool:
    """是否有**活跃** run 属于该会话且已被请求停止（供 CLI 轮询判定）。

    已结束 run 已注销 active ⇒ 返回 False，避免把历史停止位误判为"仍在停"。
    """
    sid = str(session_id)
    with _stop_lock:
        return any(s == sid and rid in _stop_runs for rid, s in _active.items())


def _register_active(run_id: str, session_id: str) -> None:
    with _stop_lock:
        _active[str(run_id)] = str(session_id)


def _clear_active(run_id: str) -> None:
    with _stop_lock:
        _active.pop(str(run_id), None)


def init_workers(n: int = 4):
    """启动 worker 线程池（幂等，只启动一次）。"""
    global _workers_started, _worker_count
    if _workers_started:
        return
    _workers_started = True
    _worker_count = n
    for i in range(n):
        t = threading.Thread(target=_worker_loop_guarded, daemon=True,
                             name=f"mq-worker-{i}")
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
    run_id = uuid.uuid4().hex  # per-run 标识（A-02 止血①：停止位按 run 粒度，互不误杀）
    try:
        # 队列满时快速失败而不是无限挂起调用线程（SSE 请求会被拖死，审计 P2）
        _task_queue.put({
            "message": message,
            "session_id": session_id,
            "run_id": run_id,
            "timeout": timeout,
            "future": future,
            "event_cb": event_cb,
        }, timeout=5)
    except queue.Full:
        raise RuntimeError("Agent 消息队列已满（256），请稍后重试")
    return future


def _worker_loop_guarded():
    """Worker 线程入口：保活壳。

    `_worker_loop` 只在收到毒丸时 return；任何从任务处理中逃逸的异常都会走到
    这里并**重启消费循环**。不这么做的话，一次偶发异常（例如对已取消 Future
    的二次 set_result）就会让该 worker 线程永久消失——4 个消费者变 3 个、2 个…
    表现为"队列悄悄堆积"，日志里只有一条 traceback，极易误判为"agent 卡死"。
    """
    while True:
        try:
            _worker_loop()
            return          # 正常退出只可能是毒丸
        except BaseException as e:      # noqa: BLE001 - 保活优先
            logger.error("[MQ] Worker 循环异常，重启消费循环(线程保活): %s",
                         e, exc_info=True)


def _worker_loop():
    """Worker 线程：从队列取消息 → agent.chat → 回写结果。"""
    import asyncio
    from .agent import agent

    while True:
        task = _task_queue.get()
        if task is None:  # 毒丸
            break

        future: Future = task["future"]
        # 声明"开始执行"（concurrent.futures 契约，C1）：
        #   ① 此后调用方 cancel() 恒返回 False，且**不会**把 future 置为 CANCELLED
        #      ⇒ 运行中的任务不会被"取消"掉，后续 set_result 也不会撞
        #      InvalidStateError（那是 worker 线程死亡的直接原因）；
        #   ② 若调用方在排队期间就已取消 ⇒ 返回 False，直接丢弃不跑（省一次 LLM）。
        try:
            started = future.set_running_or_notify_cancel()
        except RuntimeError:
            started = False     # 终态/CANCELLED_AND_NOTIFIED 等非常态
        if not started:
            continue
        _register_active(str(task["run_id"]), str(task["session_id"]))
        clear_stop_run(str(task["run_id"]))  # 新任务开始，清掉该 run 残留停止位
        # F4 续跑凭据识别（修 E7）：agent_resume JSON → 续跑上下文 prompt，
        # 普通消息原样透传（详见 _rewrite_resume_message 注释块）。
        task["message"] = _rewrite_resume_message(str(task.get("message") or ""))

        try:
            loop = asyncio.new_event_loop()
            try:
                # event_cb/step_checks/中断探针统一走 task_agent 会话钩子表
                # （2026-09-24 提智 0.7 并发槽位收编：旧 _current_event_cb 全局槽 +
                # save/restore 模式多 worker 互踩，审计 B1）。

                # 协作取消（2026-09-17 Ctrl+C 修复）：worker 线程收不到 SIGINT，
                # 主线程只能置 future 取消位；agent.run 的 step callback 是天然检查点——
                # 每个 step 边界看一眼 future.cancelled()，命中即中止 run，
                # 不再让已"中断"的任务继续烧 token（smolagents 每步都会调 callbacks）。
                class _UserInterruptError(RuntimeError):
                    pass

                def _cancel_check(_step_data):
                    # 触发源：本 run 的停止位（任意终端 request_stop；CLI Ctrl+C 也归一到此）。
                    # 不再查 future.cancelled()——Future.cancel 对运行中任务返回 False
                    # 且不置位（concurrent.futures 语义），查它是坏开关。
                    # ★ A-02 止血①：按 run_id 查，不是 session——同会话并发的
                    #   另一个 run 不会被"误杀邻居"。
                    if is_stop_requested_run(str(task["run_id"])):
                        raise _UserInterruptError("user interrupted")

                async def _run_with_events():
                    # 2026-09-24 提智 0.7：并发槽位收编——原 _current_event_cb /
                    # _user_step_callbacks / _INTERRUPT_CHECKS 均“赋值 + finally 恢复”，
                    # 多 worker 并发互踩（A 收尾抹掉 B 的注册 / 陈旧探针串会话误杀，审计 B1）。
                    # 现按 session 注册到 task_agent 会话钩子表 + 线程局部绑定当前 run。
                    from . import qd_agent as _ta_mod
                    _sid = str(task["session_id"])
                    _ta_mod.bind_run_session(_sid)

                    # 立即停止（2026-09-17）：工具级中断探针，触发源——会话停止位
                    # （任意终端 request_stop，CLI Ctrl+C 归一到此）/ smolagents
                    # interrupt_switch。工具守卫每次工具调用进入前执行探针 → step 内即停。
                    def _interrupt_probe():
                        # 只查本 run 停止位 + interrupt_switch
                        # （future.cancelled() 对运行中任务恒 False）
                        if is_stop_requested_run(str(task["run_id"])):
                            raise _UserInterruptError("user interrupted")
                        _ca = _ta_mod._hooks_now().get("active_agent")
                        if _ca is not None and getattr(_ca, "interrupt_switch", False):
                            raise _UserInterruptError("user interrupted")

                    _ta_mod.register_run_hooks(
                        _sid,
                        event_cb=task.get("event_cb"),
                        step_checks=[_cancel_check],
                        probes=[_interrupt_probe],
                    )
                    try:
                        return await agent.chat(task["message"], session_id=task["session_id"])
                    except _UserInterruptError:
                        raise
                    finally:
                        # A-02 ③（2026-10-14）：**先置停止位、再处理钩子**（顺序，双保险）。
                        # 超时取消（CancelledError）：置停止位让孤儿 run 协作自停，并**保留**钩子
                        # （方向2，`_run_sync` 真正结束时清理，不在此注销）；正常/用户停止路径：
                        # 照常注销（避免泄漏）。
                        # ★ 顺序保证：`request_stop_run` 必须出现在 `unregister_run_hooks` 之前，
                        #   否则孤儿 run 在钩子断掉后才收到停止位 ⇒ 读不到、停不下来（A-02 缺口）。
                        import sys
                        if isinstance(sys.exc_info()[1], asyncio.CancelledError):
                            request_stop_run(str(task["run_id"]))   # ← 先置停止位
                        else:
                            _ta_mod.unregister_run_hooks(_sid)          # ← 仅非超时路径注销

                resp = loop.run_until_complete(asyncio.wait_for(_run_with_events(), timeout=task["timeout"]))
                _settle(future, result=resp.content or "")
            finally:
                # 不 close 共享 LLM 客户端（审计 P0-3）：客户端绑定 event loop 且全局共享，
                # 任务级 close 会误杀并发 worker 的在途请求，且下一个任务在新 loop 上复用
                # 已关闭的客户端会随机报 "Event loop is closed"。连接池随进程存活。
                loop.close()
        except (Exception, asyncio.CancelledError) as e:
            if isinstance(e, asyncio.CancelledError):
                logger.warning("[MQ] Worker 任务超时被取消（放行）")
                # C2：wait_for 超时只是"不再等"，协程可能仍卡在不可取消的同步 IO 里
                # 继续烧 token、继续写记忆/决策库（孤儿 run）。置**本 run** 停止位 ⇒
                # 残留 run 在下一个 step / 工具守卫探针检查点协作自停；钩子由它自己
                # 的 finally 收尾（此处不反注册，否则会切断尚在途的事件回调）。
                # ★ A-02 止血①：只停自己的 run（request_stop_run），**绝不**波及同会话
                #   其他 run（旧 request_stop(session) 会误杀邻居）。
                try:
                    request_stop_run(str(task["run_id"]))
                except Exception as se:
                    logger.warning("[MQ] 超时后置停止位失败: %s: %s",
                                   type(se).__name__, se)
                _settle(future, result="")
            elif type(e).__name__ == "_UserInterruptError":
                # 用户停止（CLI Ctrl+C / Web request_stop）：安静收尾。
                # set_result('') 让仍阻塞在 result() 的调用方立刻解除（不抛给无人等的场景）。
                logger.warning("[MQ] Worker 任务被用户停止，已中止 agent.run")
                _settle(future, result="")
            else:
                logger.error("[MQ] Worker 异常: %s", e, exc_info=True)
                _settle(future, exc=e)
        finally:
            # 任务彻底结束（正常/超时/中断/异常）注销本 run 的活跃登记（A-02 止血①）。
            # 已结束 run 注销后，is_session_stop_requested 才能正确回落为 False，
            # 且不会被"历史停止位"误判为仍在停。
            _clear_active(str(task["run_id"]))
