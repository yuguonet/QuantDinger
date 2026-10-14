# -*- coding: utf-8 -*-
r"""A-04：运行记录里的用户输入脱敏（2026-10-14，审计报告 A-04）。

背景：`app/agent/traces/*.jsonl` 是 Git 跟踪文件，`user_input` / `query`
**明文落盘**（抽样："分析雪天盐业"）。报告标的 P2，但它是**保留面最大**的一类数据
（用户输入会进 JSONL + `qd_agent_traces` 表 + 评测侧车）。

契约：**用户输入只在 `utils.mask.mask_user_text` 这一个入口脱敏**，
tracing 与 preselect 两个 sink 共用它 ⇒ 不允许出现"半边脱敏"
（漏一个出口 = 等于没脱）。口径开关 `QD_TRACE_MASK_USER_INPUT`（默认开）。

★ 反向对照不可省：只测"敏感串被抹"的话，一个 `return "***"` 的假实现也能全绿
  ⇒ 必须同时证明**正常问句零改动**（脱敏不能毁掉 preselect 的离线共现原料）。
"""
from __future__ import annotations

import json
import os

import pytest

from app.agent.utils import tracing as tracing_mod
from app.agent.preselect import preselect_stats as ps_mod
from app.agent.utils.mask import mask_user_text, user_input_mask_enabled


PHONE = "13812345678"
ID_CARD = "11010119900307123X"
NORMAL_Q = "分析雪天盐业，看看 MACD 和筹码分布"


# ── 脱敏规则本身 ──

def test_mask_user_text_masks_phone():
    out = mask_user_text(f"联系电话 {PHONE} 帮我看下")
    assert PHONE not in out
    assert "138****78" in out          # 手机号按前 3 后 2 保留，仍可定位


def test_mask_user_text_masks_id_card():
    assert ID_CARD not in mask_user_text(f"身份证 {ID_CARD}")


def test_mask_user_text_masks_api_key_kv():
    out = mask_user_text("api_key=abcdef123456 查一下")
    assert "abcdef123456" not in out


def test_mask_user_text_keeps_normal_question():
    """反向对照：正常问句必须原样保留（否则 preselect 共现权重原料被毁）。"""
    assert mask_user_text(NORMAL_Q) == NORMAL_Q


def test_mask_user_text_is_env_toggleable(monkeypatch):
    monkeypatch.setenv("QD_TRACE_MASK_USER_INPUT", "0")
    assert user_input_mask_enabled() is False
    assert mask_user_text(f"电话 {PHONE}") == f"电话 {PHONE}"
    monkeypatch.setenv("QD_TRACE_MASK_USER_INPUT", "1")
    assert user_input_mask_enabled() is True
    assert PHONE not in mask_user_text(f"电话 {PHONE}")


# ── sink 一：tracing（JSONL 的 user_input + DB 的 user_query 两列）──

def test_trace_masks_user_input_in_run_start_event():
    rec = tracing_mod.AgentTraceRecorder(
        agent_type="TaskAgent", session_id="s1",
        user_input=f"我的电话 {PHONE}，{NORMAL_Q}")
    payload = rec.events[0]["payload"]
    assert PHONE not in payload["user_input"]
    assert NORMAL_Q in payload["user_input"]      # 正常问句保留


def test_trace_user_input_attr_is_masked():
    """`self.user_input` 同时供 finish() 写两处 DB 列（input_params.user_query
    与 user_query）⇒ 必须在入口处就脱敏，不能只脱事件。"""
    rec = tracing_mod.AgentTraceRecorder(
        agent_type="TaskAgent", session_id="s1",
        user_input=f"api_key=abcdef123456 {NORMAL_Q}")
    assert "abcdef123456" not in rec.user_input
    assert NORMAL_Q in rec.user_input


# ── sink 二：preselect（pairs JSONL 的 query + 内存样本的 q）──

def test_preselect_pairs_query_is_masked(monkeypatch, tmp_path):
    target = tmp_path / "pairs.jsonl"
    monkeypatch.setenv("QD_PRESELECT_PAIRS_FILE", str(target))
    res = ps_mod.record_task_calls(f"电话 {PHONE} {NORMAL_Q}", ["get_realtime_quote"])
    assert res is not None
    line = target.read_text(encoding="utf-8").strip().splitlines()[-1]
    rec = json.loads(line)
    assert PHONE not in rec["query"]
    assert NORMAL_Q in rec["query"]


def test_preselect_empty_face_sample_is_masked():
    ps_mod._SAMPLES.clear()
    ps_mod.record_empty_face(query=f"身份证 {ID_CARD}", route="no_pick")
    q = ps_mod._SAMPLES[-1]["q"]
    assert ID_CARD not in q
    ps_mod._SAMPLES.clear()


# ── 结构守卫：两个 sink 必须共用同一个入口 ──

def test_both_sinks_share_the_same_mask_entry():
    """★ 防「半边脱敏」：tracing 与 preselect 必须是同一个函数对象。

    各自 `from ..utils.mask import mask_text` 的话，开关对某一路失效，
    而两路都写 `traces/` ⇒ 一半脱了等于没脱。
    """
    assert tracing_mod._mask_user_text is ps_mod._mask_user_text
    assert tracing_mod._mask_user_text is mask_user_text


def test_no_sink_calls_raw_mask_text_directly():
    """源码层守卫：sink 里不许出现绕过开关的裸 `mask_text(` 调用。"""
    import ast
    for rel in ("app/agent/utils/tracing.py", "app/agent/preselect/preselect_stats.py"):
        src = (os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/" + rel)
        with open(src, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        bad = [n.lineno for n in ast.walk(tree)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == "mask_text"]
        assert not bad, f"{rel} 第 {bad} 行绕过 mask_user_text 直接调 mask_text"
