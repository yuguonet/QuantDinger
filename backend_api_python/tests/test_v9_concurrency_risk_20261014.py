# -*- coding: utf-8 -*-
r"""A-01 / A-02：并发与超时风险的**复现断言**（2026-10-14，审计报告 P1 两条）。

⚠ 本文件**不改任何产品行为**，只把风险钉成可执行的证据，供后续出方案时对照：
  · 现状为"不满足"的项用 `xfail(strict=True)` 登记 —— 今天它确实不满足（xfail 通过）；
    哪天真修好了会变 XPASS，strict 会**报错**，逼你回来同步审计报告结论与设计文档，
    避免"改了但没人知道"。
  · 现状为"成立"的项（如停止位不依赖钩子）用普通断言 —— 它们同时**修正了审计报告
    里偏重的措辞**（报告说"停止标记无法再被执行体读取"，实测只有 interrupt_switch
    那条通道断，`_stop_runs` 通道仍活着；A-02 止血①后停止位改为 per-run 粒度）。

A-01（**已修复 + 更正**，2026-10-14）：串行化下沉到 `_run_sync`（T2 工作线程）内用
       **threading.Lock**（`_session_lock()` 唯一入口），覆盖超时孤儿 run（asyncio.Lock
       在 chat 层会在超时取消时提前释放锁）。`test_run_sync_real_serializes_same_session`
       用真实 `_run_sync` 驱动证明：同会话两 run 区间不重叠、跨会话并行。
A-02（**已全收口**，2026-10-14）：① 止血① per-run 停止位（`request_stop_run` 只停自己）；
       ② 方向2 超时保留钩子（`_run_sync` 真正结束时清理）；③ 停止位顺序（`request_stop_run`
       在 `unregister_run_hooks` 之前）——`test_stop_is_flagged_before_hooks_are_unregistered`
       从**假 xfail 转正**（原匹配 bug：查 `request_stop` 而实际是 `request_stop_run`）。
"""
from __future__ import annotations

import asyncio
import ast
import threading
import time
from pathlib import Path

import pytest

from app.agent import message_queue as mq
from app.agent import qd_hooks as hooks
from app.agent.qd_service import QDAgentService

_BACKEND = Path(__file__).resolve().parents[1]
_QS = _BACKEND / "app" / "agent" / "qd_service.py"
_MQ = _BACKEND / "app" / "agent" / "message_queue.py"

_SID = "test.concurrency.A01"


class _FakeAgent:
    """QDAgent 的最小替身：只记录「被构造了几次 / 被并发 run 了几次」。"""

    instances: list = []

    def __init__(self, *a, **kw):
        self.runs = 0
        _FakeAgent.instances.append(self)

    def run(self, task):
        self.runs += 1
        return ("ok", "fake")


# ── A-01：同会话共享同一个 Agent 实例 ──

def test_same_session_gets_the_same_agent_instance(monkeypatch):
    """事实证据：会话级缓存 ⇒ 两个并发请求拿到的是**同一个**实例。"""
    from app.agent import qd_service as qs
    _FakeAgent.instances.clear()
    monkeypatch.setattr(qs, "QDAgent", _FakeAgent)
    svc = QDAgentService(model=object(), memory=None, agent_config={})
    a1 = svc._get_agent(_SID)
    a2 = svc._get_agent(_SID)
    assert a1 is a2, "同会话未复用实例 ⇒ 与审计 A-01 的前提不符，需重查"
    assert len(_FakeAgent.instances) == 1


def test_session_lock_is_per_session_singleton():
    """结构性前提：`_session_lock()` 同会话复用同一把锁、不同会话不同锁。

    同会话不复用 ⇒ 串行化失效（退化成无锁）；不同会话复用 ⇒ 跨会话被误伤串行。
    """
    svc = QDAgentService(model=object(), memory=None, agent_config={})
    l1 = svc._session_lock("x")
    l2 = svc._session_lock("x")
    l3 = svc._session_lock("y")
    assert l1 is l2, "同会话应复用同一把锁（否则串行化失效）"
    assert l1 is not l3, "不同会话必须拿到不同锁（否则跨会话被串行化误伤）"
    # A-01 更正后应为**同步锁**（threading/_thread 的 Lock），不是 asyncio.Lock——
    # 后者会在超时取消（CancelledError）时让 `async with` 提前释放，孤儿 run 逃逸串行化。
    # 注：mimoagent 启动时会把 `threading.Lock` 替换为工厂函数（底层仍返回 _thread.lock），
    # 故不 isinstance 检查，改用 type 的 module 判定，避免 TypeError。
    assert type(l1).__module__ in ("_thread", "threading"), \
        f"A-01 更正后应为同步锁（threading/_thread），实际: {type(l1)}"


def test_run_sync_serializes_same_session_different_parallel():
    """A-01 更正后：串行化在 `_run_sync`（T2 工作线程）内用 threading.Lock 实现，
    覆盖超时孤儿 run（asyncio.Lock 在 chat 层会在取消时提前释放）。本例直接驱动
    `_session_lock` 的 acquire/release 机制（与 `_run_sync` 内部一致），验证同会话
    峰值并发==1、跨会话峰值>=2。
    """
    svc = QDAgentService(model=object(), memory=None, agent_config={})
    global_active = {"n": 0}
    global_max = {"v": 0}
    sess_active: dict = {}
    sess_max: dict = {}

    def body(message, session_id):
        lk = svc._session_lock(session_id)
        lk.acquire()
        try:
            global_active["n"] += 1
            global_max["v"] = max(global_max["v"], global_active["n"])
            sess_active[session_id] = sess_active.get(session_id, 0) + 1
            sess_max[session_id] = max(sess_max.get(session_id, 0), sess_active[session_id])
            time.sleep(0.05)
            sess_active[session_id] -= 1
            global_active["n"] -= 1
            return f"r:{session_id}"
        finally:
            lk.release()

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        futs = [ex.submit(body, f"m{i}", sid)
                for i, sid in enumerate(["sA", "sA", "sB"])]
        for f in futs:
            f.result()

    # 同会话必须串行 ⇒ 峰值并发恰为 1
    assert sess_max["sA"] == 1, f"同会话峰值并发={sess_max['sA']}（应==1，未串行化）"
    # 跨会话必须并行 ⇒ 全局峰值 >= 2
    assert global_max["v"] >= 2, f"跨会话峰值并发={global_max['v']}（应>=2，锁误伤吞吐）"


def test_run_sync_real_serializes_same_session(monkeypatch):
    """真实 `_run_sync` 同会话串行化（A-01 更正：threading.Lock 在 T2 内）。

    隔离 resolver（强制 ImportError ⇒ fail-open），mock `_get_agent`/`_prefetch`，
    用线程池并发调 sA/sA/sB，验证 sA 两 run 区间不重叠（串行）、跨会话并行。
    ★ 有 bite：去掉 `_run_sync` 内的 `lk.acquire()` ⇒ sA 两 run 并发交错 ⇒ 断言失败。
    """
    import sys as _sys
    monkeypatch.setitem(_sys.modules, "app.agent.resolvers.bridge", None)  # 强制 resolver 走 fail-open

    svc = QDAgentService(model=object(), memory=None, agent_config={})
    events: list = []
    ev_lock = threading.Lock()

    def _make_agent(sid):
        class _FA:
            def run(self, task):
                with ev_lock:
                    events.append(("enter", sid, time.perf_counter()))
                time.sleep(0.05)
                with ev_lock:
                    events.append(("exit", sid, time.perf_counter()))
                return ("ok", "fake")
        return _FA()

    svc._get_agent = _make_agent
    # skill_hint（2026-10-17 skill 进预选）：真实 `_prefetch` 多了一个带默认值的
    # 形参，stub 必须跟签名一致——否则测的是"stub 接不住新参数"，不是串行化。
    svc._prefetch = lambda m, sid, ri, skill_hint="": ("task", {"report": {}})

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        futs = [ex.submit(svc._run_sync, f"m{i}", sid)
                for i, sid in enumerate(["sA", "sA", "sB"])]
        for f in futs:
            f.result()

    sa = sorted([e for e in events if e[1] == "sA"], key=lambda x: x[2])
    assert len(sa) == 4, f"sA 事件数异常: {sa}"
    assert (sa[0][0], sa[1][0], sa[2][0], sa[3][0]) == ("enter", "exit", "enter", "exit"), \
        f"sA 两 run 并发交错（未串行化）: {[x[0] for x in sa]}"
    assert sa[1][2] <= sa[2][2] + 1e-6, f"sA 两 run 区间重叠（未串行化）: {sa}"
    # 跨会话并行：sB 区间与 sA 总区间重叠
    sb = [e for e in events if e[1] == "sB"]
    assert len(sb) == 2, f"sB 事件数异常: {sb}"
    assert sb[0][2] < sa[3][2] and sb[1][2] > sa[0][2], "跨会话未并行（per-session 锁误伤吞吐）"


# ── A-02：停止位 vs 钩子注销 ──

def test_stop_flag_is_independent_of_hooks():
    """修正审计措辞：置位后**即使钩子已注销**，停止位仍可读 ⇒ 残留 run 仍能协作自停。

    A-02 止血①后停止位是 per-run 粒度：`request_stop(session)` 只停该会话**当前活跃**
    的 run；故先注册一个活跃 run，再置位，验证「置位」不受「钩子注销」影响。
    """
    sid = "test.concurrency.A02.flag"
    rid = "A02.run.1"
    try:
        mq._register_active(rid, sid)
        mq.request_stop(sid)
        assert mq.is_session_stop_requested(sid) is True
        assert mq.is_stop_requested_run(rid) is True
        hooks.bind_run_session(sid)
        hooks.register_run_hooks(sid, event_cb=None, step_checks=[], probes=[])
        hooks.unregister_run_hooks(sid)          # ← 审计说的"先注销"
        assert mq.is_session_stop_requested(sid) is True, (
            "注销钩子后停止位读不到了 ⇒ 残留 run 彻底停不下来（比审计描述更严重）")
    finally:
        mq._clear_active(rid)
        mq.clear_stop_run(rid)
        hooks.unregister_run_hooks(sid)


def test_interrupt_switch_channel_is_cut_after_unregister():
    """通道二确实断了：`_hooks_now()` 拿不到钩子 ⇒ active_agent / interrupt_switch 失效。"""
    sid = "test.concurrency.A02.hook"
    try:
        hooks.bind_run_session(sid)
        hooks.register_run_hooks(sid, event_cb=lambda *a: None, step_checks=[], probes=[])
        assert hooks._hooks_now(), "注册后应能取到钩子（本用例前提）"
        hooks.unregister_run_hooks(sid)
        assert hooks._hooks_now() == {}, "注销后仍取到钩子 ⇒ 与 A-02 描述不符，需重查"
    finally:
        hooks.unregister_run_hooks(sid)


def test_stop_is_flagged_before_hooks_are_unregistered():
    """A-02 ③ 已修（2026-10-14）：置停止位（`request_stop_run`）在钩子注销
    （`unregister_run_hooks`）**之前**（源码行号顺序）。超时取消路径置位后保留钩子
    （方向2），孤儿 run 的 step 探针能读到停止位协作自停。

    ★ 修了匹配 bug：原测试查 `name == "request_stop"`，而实际调用是 `request_stop_run`
      ⇒ `lines["request_stop"]` 恒空，断言恒因「没找到调用点」而失败（假 xfail，从没真正
      测到顺序）。现改为同时匹配两种名字，才真正校验「置位早于注销」。
    """
    tree = ast.parse(_MQ.read_text(encoding="utf-8"))
    lines = {"unregister": [], "request_stop": []}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        if name == "unregister_run_hooks":
            lines["unregister"].append(node.lineno)
        elif name in ("request_stop", "request_stop_run"):  # 修 bug：实际调用是 request_stop_run
            lines["request_stop"].append(node.lineno)
    assert lines["request_stop"] and lines["unregister"], "没找到两个调用点（结构变了？）"
    assert min(lines["request_stop"]) < min(lines["unregister"]), (
        f"置位({min(lines['request_stop'])}) 晚于注销({min(lines['unregister'])}) ⇒ "
        f"超时后钩子通道已断")
