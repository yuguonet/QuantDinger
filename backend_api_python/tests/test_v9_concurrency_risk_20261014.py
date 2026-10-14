# -*- coding: utf-8 -*-
r"""A-01 / A-02：并发与超时风险的**复现断言**（2026-10-14，审计报告 P1 两条）。

⚠ 本文件**不改任何产品行为**，只把风险钉成可执行的证据，供后续出方案时对照：
  · 现状为"不满足"的项用 `xfail(strict=True)` 登记 —— 今天它确实不满足（xfail 通过）；
    哪天真修好了会变 XPASS，strict 会**报错**，逼你回来同步审计报告结论与设计文档，
    避免"改了但没人知道"。
  · 现状为"成立"的项（如停止位不依赖钩子）用普通断言 —— 它们同时**修正了审计报告
    里偏重的措辞**（报告说"停止标记无法再被执行体读取"，实测只有 interrupt_switch
    那条通道断，`_stop_runs` 通道仍活着；A-02 止血①后停止位改为 per-run 粒度）。

A-01：`qd_service.py:307` 的 `self._lock` 只包住 `_get_agent()` 的查找/创建，
       `chat()` 里 `await asyncio.to_thread(self._run_sync, ...)`（:569）**在锁外**
       ⇒ 同会话并发请求会在两个线程里跑**同一个 QDAgent 实例**。
A-02：`wait_for` 超时 → 内层 `finally: unregister_run_hooks(_sid)`（:295-296）**先**
       执行 → 外层 except CancelledError 才 `request_stop_run(run_id)`（:321，A-02 止血①
       后只停**自己的 run**，不波及其他 run / 会话）。
"""
from __future__ import annotations

import ast
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


@pytest.mark.xfail(
    strict=True,
    reason="A-01 已知缺口：run 路径（`chat` / `_run_sync`）没有被会话级锁保护，"
           "锁只包住 `_get_agent` 的查找/创建 ⇒ 同会话并发会交错改写 run 状态。"
           "若本用例 XPASS，说明已做串行化 ⇒ 必须回来改审计报告结论与设计文档。",
)
def test_run_path_is_guarded_by_the_session_lock():
    raw = _QS.read_text(encoding="utf-8")
    tree = ast.parse(raw)
    # ★ 必须 ast.walk 全树：`chat` / `_run_sync` 是 **QDAgentService 的方法**，
    #   不是模块顶层函数。只扫 `tree.body` 会得到 None ⇒ 断言因"函数没找到"而失败，
    #   看起来是 xfail，实际**根本没在测锁**（变异 M1 就是这么溜过去的）。
    fns = {n.name: n for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    guarded = []
    for name in ("chat", "_run_sync"):
        fn = fns.get(name)
        assert fn is not None, f"未找到 {name}（源码结构变了？需同步本守卫）"
        src = ast.get_source_segment(raw, fn) or ""
        assert src, f"取不到 {name} 的源码"
        guarded.append("self._lock" in src)
    # ★ 用 any 不用 all：run 路径**任一层**持有会话锁就足以串行化
    #   （锁在 `chat` 这一层已经把 `_run_sync` 罩住了）。
    #   写 all 会漏掉"只在 chat 加锁"这种正当修法 —— 变异 M1-A01-fixed 实测
    #   就是这么从台账底下溜过去的（加了锁却仍 xfail ⇒ 台账形同虚设）。
    assert any(guarded), f"run 路径未持会话锁：chat={guarded[0]} _run_sync={guarded[1]}"


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


@pytest.mark.xfail(
    strict=True,
    reason="A-02 已知缺口：置停止位发生在注销钩子**之后**（unregister 在内层 finally，"
           "request_stop 在外层 except）。若本用例 XPASS，说明顺序已修正 ⇒ "
           "必须回来改审计报告结论与本文件头注释。",
)
def test_stop_is_flagged_before_hooks_are_unregistered():
    tree = ast.parse(_MQ.read_text(encoding="utf-8"))
    lines = {"unregister": [], "request_stop": []}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        if name == "unregister_run_hooks":
            lines["unregister"].append(node.lineno)
        elif name == "request_stop":
            lines["request_stop"].append(node.lineno)
    assert lines["request_stop"] and lines["unregister"], "没找到两个调用点（结构变了？）"
    assert min(lines["request_stop"]) < min(lines["unregister"]), (
        f"置位({min(lines['request_stop'])}) 晚于注销({min(lines['unregister'])}) ⇒ "
        f"超时后钩子通道已断")
