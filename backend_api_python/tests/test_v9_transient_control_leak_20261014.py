# -*- coding: utf-8 -*-
"""瞬态控制消息**跨轮泄漏**复现与修复守卫（2026-10-14）。

【事故】会话内「现在买什么股?」烧穿 per-run token 预算 → `_account_budget` 抛
`LimitsExceeded(budget_wrapup_hint(...))` → mimo 把这条**控制指令**当 user 消息
留进复用的 `agent.messages`，`qd_service` 又把返回文本当 assistant 落库。下一轮
「分析002866」被"已停止继续探索 / 不要再展开新的取数"带偏 ⇒ 预选虽已点名 10 个
工具却**一个都没调**，直接把全部字段写成「未获取」（status=Idle, steps=1）。

【修法】`qd_config.TRANSIENT_CONTROL_MARKERS` 定义唯一指纹；两处消费：
  · `qd_agent._run_inner` 开轮前 `_purge_transient_control(self.messages)`；
  · `qd_service._should_store_assistant` 决定是否把回复写入会话记忆。

⚠ 指纹漂移 = 守卫失效：改任何控制文案都必须同步本文件（见
`test_markers_cover_all_real_control_texts` 对真实函数的直接取证）。
"""
from __future__ import annotations

import importlib

import pytest


# ── ① 指纹定义本身 ────────────────────────────────────────────────

def _cfg():
    return importlib.import_module("app.agent.qd_config")


def test_markers_cover_all_real_control_texts() -> None:
    """每一个真实控制文案都必须被 `is_transient_control` 认出来。

    这里**不写死字符串**，而是直接构造各来源的真实产出（函数 / 现有文件里的
    字面量），再问断言 —— 文案一改，本测试立刻红，逼你同步指纹。
    """
    from app.agent.utils.budget import budget_wrapup_hint

    cfg = _cfg()
    samples = {
        "budget_wrapup": budget_wrapup_hint("run_token:202346/200000"),
        "truncated": "（本轮达到步数上限被截断——请把问题拆小分步重试）",
        "empty_reply": "（模型未返回文字内容，请重试或换个问法）",
        "empty_nudge": "请用文字直接给出本轮结论（不要再调用工具）。",
        "grounding_rewrite": (
            "【数字溯源拦截】某个数字无法溯源\n请只依据本轮工具输出中的数字重写结论；"
            "无法溯源的数字改为\"未获取\"并说明缺失口径。"),
        "mimo_empty": "Empty assistant response",
    }
    for name, text in samples.items():
        assert cfg.is_transient_control(text), f"未识别瞬态控制文案：{name}"

    # 带首尾空白也必须识别（mimo 返回值可能带换行）
    assert cfg.is_transient_control("  " + samples["budget_wrapup"] + "\n")


def test_normal_text_is_not_flagged() -> None:
    """真实用户提问 / 正常答复绝不能被误判（误判 = 静默丢用户输入）。"""
    cfg = _cfg()
    for text in (
        "",
        "   ",
        None,
        "分析002866",
        "现在买什么股?",
        "## 分析 002866 传艺科技\n收盘价 12.34，量比 1.2。",
        "请用文字直接给出结论",  # 近似但非完整指纹前缀 ⇒ 不误伤
        "本轮已触及成本上限",     # 缺全角括号前缀 ⇒ 不误伤
    ):
        assert not cfg.is_transient_control(text), f"误判普通文本：{text!r}"


# ── ② 会话消息清除（防跨轮泄漏）────────────────────────────────────

def test_purge_removes_prior_turn_control_messages() -> None:
    """`_purge_transient_control` 清掉上一轮控制消息，保留真实对话。"""
    qd_agent = importlib.import_module("app.agent.qd_agent")
    purge = qd_agent._purge_transient_control

    from app.agent.utils.budget import budget_wrapup_hint

    messages = [
        {"role": "system", "content": "你是 A 股研究助手"},
        {"role": "user", "content": "现在买什么股?"},
        # ← 上一轮的预算熔断提示（跨轮污染的主角）
        {"role": "user", "content": budget_wrapup_hint("run_token:202346/200000")},
        {"role": "assistant", "content": "（本轮已触及成本上限：run_token:1/2，已停止继续探索。）\n下面是目前已经取到 / 算出的部分；关键数据若未取到，如实标注为「未获取」，不要编造，也不要再展开新的取数。"},
        # ← 上一轮的空回复轻推轮 / 溯源重写轮 prompt（同样会被 mimo 永久留存）
        {"role": "user", "content": "请用文字直接给出本轮结论（不要再调用工具）。"},
        {"role": "user", "content": "【数字溯源拦截】x\n请只依据本轮工具输出中的数字重写结论。"},
        # 真实对话必须保留
        {"role": "user", "content": "分析002866"},
        {"role": "assistant", "content": "传艺科技属于消费电子板块。"},
        {"role": "tool", "content": "收盘价 12.34"},  # 工具消息不参与（角色不符）
    ]
    removed = purge(messages)
    assert removed == 4, f"应删 4 条瞬态控制消息，实际 {removed}"

    kept = [(m["role"], m["content"]) for m in messages]
    assert ("user", "现在买什么股?") in kept
    assert ("user", "分析002866") in kept
    assert ("assistant", "传艺科技属于消费电子板块。") in kept
    assert ("tool", "收盘价 12.34") in kept
    # 确认污染源全没了
    assert all("已停止继续探索" not in (m.get("content") or "") for m in messages)
    assert all("【数字溯源拦截】" not in (m.get("content") or "") for m in messages)


def test_purge_is_noop_on_clean_history() -> None:
    """干净会话不误删、返回 0（避免每轮白改 messages）。"""
    qd_agent = importlib.import_module("app.agent.qd_agent")
    messages = [
        {"role": "user", "content": "分析002866"},
        {"role": "assistant", "content": "好"},
    ]
    snapshot = [dict(m) for m in messages]
    assert qd_agent._purge_transient_control(messages) == 0
    assert messages == snapshot


def test_next_turn_is_clean_after_budget_hit() -> None:
    """端到端语义：预算熔断轮之后，下一轮 messages 里不再有那条旧指令。

    直接复刻 002866 事故的消息序列（mimo `TerminatingException` 分支
    `add_message('user', str(e))` 的产物），再跑开轮清除。
    """
    qd_agent = importlib.import_module("app.agent.qd_agent")
    from app.agent.utils.budget import budget_wrapup_hint

    hint = budget_wrapup_hint("run_token:202346/200000")
    messages = [
        {"role": "system", "content": "base"},
        {"role": "user", "content": "现在买什么股?"},
        {"role": "user", "content": hint},           # mimo 捕获异常后追加
        {"role": "user", "content": "分析002866"},   # 本轮新的真实问题
    ]
    qd_agent._purge_transient_control(messages)
    contents = [m["content"] for m in messages]
    assert hint not in contents
    assert "分析002866" in contents


# ── ③ 写侧闸门（防入库污染）────────────────────────────────────────

def test_should_store_assistant_blocks_transient_controls() -> None:
    """`_should_store_assistant`：瞬态控制文案不落 assistant 记忆。"""
    qd_service = importlib.import_module("app.agent.qd_service")
    f = qd_service._should_store_assistant
    from app.agent.utils.budget import budget_wrapup_hint

    assert f("传艺科技收盘价 12.34，量比 1.2。") is True
    assert f(budget_wrapup_hint("run_token:1/2")) is False
    assert f("（模型未返回文字内容，请重试或换个问法）") is False
    assert f("（本轮达到步数上限被截断——请把问题拆小分步重试）") is False
    assert f("Empty assistant response") is False
    assert f("") is False
    assert f(None) is False


# ── ④ 接线守卫：run 主干确实调用了清除 ─────────────────────────────

def test_run_inner_invokes_purge() -> None:
    """静态接线：`_run_inner` 必须调用 `_purge_transient_control`。

    纯逻辑测试证明函数对，这条证明它**真的被挂进 run 主干**（否则修了函数
    却没接线，事故照旧）。用 AST 定位 `_run_inner` 函数体，避免文本误匹配。
    """
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "app" / "agent" / "qd_agent.py").read_text(
        encoding="utf-8")
    tree = ast.parse(src)
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_run_inner":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    fn = sub.func
                    name = getattr(fn, "id", None) or getattr(fn, "attr", None)
                    if name:
                        called.add(name)
    assert "_purge_transient_control" in called, (
        "_run_inner 没有调用 _purge_transient_control —— 跨轮防泄漏没接线")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
