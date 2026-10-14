# -*- coding: utf-8 -*-
"""A-02 止血①：停止位改 per-run 粒度（消除"误杀邻居"）。

RED→GREEN 驱动：本文件断言的是**新 API** 行为（`request_stop_run` /
`is_stop_requested_run` / `request_stop(session)` 落到 run 粒度 / `submit` 分配
`run_id`）。在 `message_queue.py` 改完之前，import 与函数引用会失败 ⇒ RED。

关键不变量：
1. 同会话并发的两个 run，停掉其中一个**绝不能**让另一个也停（误杀邻居）。
2. 用户显式停会话 ⇒ 停掉该会话**当前活跃**的所有 run（不含已结束/未开始的）。
3. worker 内部检查点（step check + 探针）按 run_id 查停止位，不是 session。
4. 超时分支只停**自己的** run，不是整会话。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.agent import message_queue as mq  # noqa: E402

MQ = Path(__file__).resolve().parents[1] / "app" / "agent" / "message_queue.py"
SRC = MQ.read_text(encoding="utf-8")


# ── 行为层：注册表逻辑（这是"误杀邻居"的真正修复点）─────────────────

def test_request_stop_run_is_independent_of_other_runs_same_session():
    mq._stop_runs.clear(); mq._active.clear()  # 幂等：清掉历史残留位
    mq._register_active("runA", "sess1")
    mq._register_active("runB", "sess1")
    try:
        mq.request_stop_run("runA")
        assert mq.is_stop_requested_run("runA") is True
        assert mq.is_stop_requested_run("runB") is False  # ★ 邻居安然无恙
    finally:
        mq._clear_active("runA")
        mq._clear_active("runB")


def test_request_stop_session_stops_all_active_runs_of_that_session():
    mq._stop_runs.clear(); mq._active.clear()  # 幂等：清掉历史残留位
    mq._register_active("runA", "sess1")
    mq._register_active("runB", "sess1")
    mq._register_active("runC", "sess2")
    try:
        mq.request_stop("sess1")
        assert mq.is_stop_requested_run("runA") is True
        assert mq.is_stop_requested_run("runB") is True
        assert mq.is_stop_requested_run("runC") is False  # 别的会话不受影响
    finally:
        mq._clear_active("runA")
        mq._clear_active("runB")
        mq._clear_active("runC")


def test_clear_stop_run_removes_flag():
        mq._stop_runs.clear(); mq._active.clear()  # 幂等：清掉历史残留位
        mq._register_active("runX", "sess1")
        try:
            mq.request_stop_run("runX")
            assert mq.is_stop_requested_run("runX") is True
            mq.clear_stop_run("runX")
            assert mq.is_stop_requested_run("runX") is False
        finally:
            mq._clear_active("runX")


def test_is_session_stop_requested_reflects_only_active_runs():
    mq._stop_runs.clear(); mq._active.clear()  # 幂等：清掉历史残留位
    # 没有活跃 run ⇒ 不算被停止（避免把已结束会话误判为"仍在停"）
    assert mq.is_session_stop_requested("sessZ") is False
    mq._register_active("runA", "sessZ")
    try:
        mq.request_stop_run("runA")
        assert mq.is_session_stop_requested("sessZ") is True
        mq._clear_active("runA")  # run 结束、清 active ⇒ 不应再报停止
        assert mq.is_session_stop_requested("sessZ") is False
    finally:
        mq._clear_active("runA")


def test_concurrent_session_stop_does_not_kill_finished_runs():
    mq._stop_runs.clear(); mq._active.clear()  # 幂等：清掉历史残留位
    # 一个会话里 run 顺序跑：run1 完成清 active，run2 开始；对会话置停只影响活跃 run2
    mq._register_active("run1", "sess1")
    mq._clear_active("run1")
    mq._register_active("run2", "sess1")
    try:
        mq.request_stop("sess1")
        assert mq.is_stop_requested_run("run2") is True
    finally:
        mq._clear_active("run2")


# ── 结构层：worker 接线与 API 形态（保证"修的是这里"）─────────────────

def test_submit_assigns_run_id_to_task():
    tree = ast.parse(SRC)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "submit":
            body = ast.get_source_segment(SRC, node) or ""
            if '"run_id"' in body or "'run_id'" in body:
                found = True
    assert found, "submit 未给 task 分配 run_id（per-run 停止的前提）"


def test_worker_checks_run_level_stop_not_session():
    assert "is_stop_requested_run" in SRC, "worker 仍在用 session 级停止位（会误杀邻居）"
    assert 'task["run_id"]' in SRC, "worker 未把 run_id 传给停止检查"
    # 允许保留 request_stop(session)（用户停会话），但 worker 内部检查点必须是 run 粒度。


def test_timeout_handler_stops_own_run_only():
    tree = ast.parse(SRC)
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is not None:
            if "CancelledError" in ast.unparse(node.type):
                target = node
    assert target is not None, "未找到 CancelledError 分支"

    def _name(c):
        return getattr(c.func, "attr", getattr(c.func, "id", ""))

    calls = [c for c in ast.walk(target) if isinstance(c, ast.Call)
             and _name(c) in ("request_stop", "request_stop_run")]
    assert calls, "超时分支未调用 request_stop/request_stop_run"
    # 必须是 request_stop_run（per-run），绝不能残留 session 级 request_stop（会误杀邻居）
    run_calls = [c for c in calls if _name(c) == "request_stop_run"]
    assert run_calls, "超时分支未用 request_stop_run（仍可能用 session 级 request_stop 误杀邻居）"
    sess_calls = [c for c in calls if _name(c) == "request_stop"]
    assert not sess_calls, f"超时分支残留 session 级 request_stop：{sess_calls}"
    args_src = [ast.unparse(c.args[0]) if c.args else "" for c in run_calls]
    assert all("run_id" in a for a in args_src), (
        f"超时分支的 request_stop_run 参数不是 run_id（会误杀邻居）：{args_src}")


def test_request_stop_session_uses_active_mapping():
    tree = ast.parse(SRC)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "request_stop")
    body = ast.get_source_segment(SRC, fn) or ""
    assert "_active" in body, "request_stop(session) 未通过 _active 映射定位 run（会误杀/漏杀）"
    assert "request_stop_run" in body or "_stop_runs" in body, \
        "request_stop(session) 未落到 run 粒度"
