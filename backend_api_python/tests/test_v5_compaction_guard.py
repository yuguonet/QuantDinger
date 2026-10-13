# -*- coding: utf-8 -*-
"""v5 P1-C2：few-shot 金例在对话压缩后的存活守卫（2026-10-13）

【为什么要有这个文件】
v5 §19.6 已**查证更正**过一个此前的误判：单轮 run 下金例注入块是安全的，
此前「会被 compaction 吞掉」的推断是错的。但**结论写在文档里不算数** ——
mimoagent 是第三方包，一次版本升级就可能改掉重建顺序，而这类失效是**静默**的：
不报错、不崩溃，只是金例悄悄消失 ⇒ 提智链路断在看不见的地方。
本文件把那条结论**钉成可执行的事实**，改坏了就当场红。

【已查证的事实链（勿重复查）】
    qd_service.py:447  注入块拼进**首轮 user 消息**（"\n\n".join(parts) + "[用户问题]…"）
    qd_agent.py:444    class QDAgent(MimocodeAgent) ⇒ 走 **mimocode** 压缩路径，
                       **不是** codex 那条（codex_agent.py:372 用
                       build_local_compacted_history，会按 token 预算**截断** user 消息
                       —— 那条路径才会丢东西，我们没走）
    mimocode_agent.py:233-237
                       compacted_messages = [*system, boundary, anchor, summary]
                       anchor = original_user_issue_anchor_message(messages)
    surgery.py:47-65   anchor 把首轮 user 消息**原样**包进 <original_user_issue>，
                       **不做任何截断**
    surgery.py:49-50   若消息本身已是 anchor 格式则原样返回 ⇒ **二次压缩不会套娃、
                       也不会漂移**到后来的 user 消息

【易错处（改本文件前先读）】
· 金例块的真实前缀是 `【历史相似案例——仅作参考，不要照搬结构】`（全角方头括号），
  **不是** `[参考案例]`。v5-实施计划 C3 原文写的 `[参考案例]` 是笔误，已修正。
  为防止指纹漂移而测试还绿，`test_case_marker_matches_implementation` 会把
  常量与 `case_memory.format_case_injection()` 的真实产出绑定校验。
· 测试 2 是**事实记录**而非缺陷：它断言「第二轮注入块不在 anchor 里」。
  若将来支持多轮注入，这条应随之更新，而不是删掉。
· pytest 下 psycopg2 被 conftest 换成 mock（tests/conftest.py:33-41），
  但 mimoagent 的 surgery 不依赖 DB ⇒ 可以直接 import，不需要 skip。
"""
from __future__ import annotations

import os
import sys

import pytest

# 【2026-10-13·P5】原本靠 sys.path 注入拿 legacy 的顶层 `utils` 包。
# 改造后 mimoagent 是 site-packages 里的独立包，与本仓库 utils 无关，无需注入。
from mimoagent.compaction.surgery import (  # noqa: E402
    COMPACT_ORIGINAL_USER_ISSUE_PREFIX as ANCHOR_PREFIX,
)

# ── 注入块指纹（与实现同步校验，见 test_case_marker_matches_implementation）
CASE_MARK = "【历史相似案例——仅作参考，不要照搬结构】"
REF_MARK = "[参考资料]"


def _msg(role: str, content: str) -> dict:
    return {"role": role, "content": content}


def _rebuild(messages: list, summary: str = "（模拟摘要：这里什么都留不下）"):
    """复刻 mimocode_agent.py:227-237 的重建顺序 —— 不 import agent（避免拉起模型/工具）。"""
    from mimoagent.compaction.surgery import (
        create_compact_boundary_message,
        create_compact_summary_message,
        original_user_issue_anchor_message,
        split_leading_system_messages,
    )

    system_messages, _ = split_leading_system_messages(messages)
    anchor = original_user_issue_anchor_message(messages)
    boundary = create_compact_boundary_message(compact_count=1, pre_tokens=99999)
    out = [*system_messages, boundary]
    if anchor is not None:
        out.append(anchor)
    out.append(create_compact_summary_message(summary))
    return out


def _all_text(messages: list) -> str:
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            c = "".join(str(p.get("text") or p.get("content") or "")
                        if isinstance(p, dict) else str(p) for p in c)
        out.append(str(c or ""))
    return "\n".join(out)


# ═══════════════════════════════════════════════════════════════
#  指纹同步（防止「前缀改了、测试还绿」的假安全）
# ═══════════════════════════════════════════════════════════════

def test_case_marker_matches_implementation():
    """CASE_MARK 必须是 case_memory 真实产出的前缀，否则下面三条守卫全在守一个假指纹。"""
    from app.agent.utils import case_memory as cm

    blk = cm.format_case_injection([{
        "task_summary": "分析 600519 的短线走势",
        "plan_digest": [{"tools": ["market_screener"]}],
        "outcome": {"label": "correct"},
        "failure_modes": [],
    }])
    assert blk, "case_memory.format_case_injection 返回空：无法校验指纹"
    assert blk.startswith(CASE_MARK), (
        f"金例块前缀已变：常量 CASE_MARK={CASE_MARK!r}，\n"
        f"实际产出以 {blk[:40]!r} 开头。\n"
        "请同步更新本文件的 CASE_MARK 与 qd_agent._QD_COMPACT_NOTES 第 6 条 —— "
        "否则压缩守卫守的是一个已经不存在的字符串。"
    )


# ═══════════════════════════════════════════════════════════════
#  守卫：钉住「首轮注入块压缩后存活」
# ═══════════════════════════════════════════════════════════════

def test_first_turn_injection_survives_compaction():
    """★ 核心：首轮 user 消息里的金例块，压缩后必须**逐字**还在（v5 §19.6）。"""
    first_turn = (
        f"{CASE_MARK}\n"
        '- 任务"分析 600519 的短线走势"：T+N 校验正确。工具链骨架：market_screener。\n'
        f"\n{REF_MARK}\n- 某条召回资料\n"
        "\n[用户问题]\n帮我看看 600519"
    )
    messages = [
        _msg("system", "你是量化分析助手"),
        _msg("user", first_turn),
        _msg("assistant", "我先取行情"),
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "function": {"name": "get_kline",
                                                  "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "600519 收盘 1700.00"},
        _msg("user", "再看看资金流"),
    ]
    rebuilt = _rebuild(messages)
    text = _all_text(rebuilt)

    assert CASE_MARK in text, (
        "压缩后金例块丢了：重建历史里找不到「历史相似案例」前缀。\n"
        "这意味着提智链路（case_memory → 首轮注入 → 模型）在长对话中被静默切断，"
        "而模型不会报错。检查 mimocode_agent.py 的重建顺序与 "
        "surgery.original_user_issue_anchor_message 是否仍保留首轮 user 原文。"
    )
    assert REF_MARK in text, "压缩后 [参考资料] 块也丢了（与金例同源，一起守）"
    # 逐字：不能只是"提到了"，必须是完整保留（anchor 不截断）
    assert "600519 的短线走势" in text, (
        "金例的**正文**没保留：可能 anchor 被按 token 截断了。"
        "surgery.create_original_user_issue_anchor_message 不应做任何截断；"
        "若走了 codex_agent 的 build_local_compacted_history（会 _truncate_message_to_tokens）"
        "就会出现这种半截保留。"
    )


def test_second_turn_injection_is_not_anchored():
    """**事实记录**（非缺陷）：第二轮才注入的块**不在** anchor 里，压缩后会丢。

    原因：`original_user_issue_anchor_message` 只取**第一个** user 消息。
    当前 `_prefetch` 在单轮 run 内只调一次 ⇒ 注入总在首轮 ⇒ 不受影响。
    ⚠️ 若将来支持多轮 session 复用 agent 并中途注入，本条应当**更新**而不是删除。
    """
    second_turn_case = f"{CASE_MARK}\n- 任务\"第二轮才注入的金例\"：T+N 校验正确。"
    messages = [
        _msg("system", "你是量化分析助手"),
        _msg("user", "首轮：帮我看看 600519"),   # 首轮**不含**注入块
        _msg("assistant", "好的"),
        _msg("user", "第二轮问题\n\n" + second_turn_case),
    ]
    rebuilt = _rebuild(messages)

    assert CASE_MARK not in _all_text(rebuilt), (
        "第二轮注入的金例竟然存活了 —— 说明 mimo 的 anchor 机制已改为覆盖多条 user 消息。"
        "这是**好消息**，请更新本测试为「多轮注入也安全」，并同步 v5 §19.6 的结论。"
    )


def test_repeat_compaction_keeps_original_anchor():
    """二次压缩时 anchor 不得漂移、不得套娃：金例必须仍是**原封不动**的那一块。

    依据 surgery.py:49-50：输入若已是 anchor 格式则原样返回，不会再加一层前缀。
    """
    case_body = '- 任务"分析 600519 的短线走势"：T+N 校验正确。'
    first_turn = f"{CASE_MARK}\n{case_body}\n\n[用户问题]\n帮我看看 600519"

    once = _rebuild([
        _msg("system", "sys"),
        _msg("user", first_turn),
        _msg("assistant", "取数中"),
    ])
    twice = _rebuild(once + [_msg("assistant", "继续"), _msg("user", "再看资金流")])
    text = _all_text(twice)

    assert CASE_MARK in text and case_body in text, (
        "二次压缩后金例丢了：anchor 漂移到后来的 user 消息了。\n"
        f"重建结果：{text[:300]}"
    )
    # 不套娃：**anchor 前缀**只应出现一次（出现两次说明 anchor 被重复包裹）。
    # ★ 这里守的是 anchor 标签而不是 CASE_MARK —— 套娃叠的是 <original_user_issue>
    #   外层标签，金例正文本身仍只出现一次，数 CASE_MARK 是数不出来的。
    #   （2026-10-13 变异检验发现的断言写错，勿改回去。）
    assert text.count(ANCHOR_PREFIX) == 1, (
        f"anchor 被重复包裹（{ANCHOR_PREFIX} 出现 {text.count(ANCHOR_PREFIX)} 次，应为 1）："
        "surgery.py:49-50 的「已是 anchor 则原样返回」分支失效了，"
        "每压缩一次就多套一层标签，长对话下会白白吃掉窗口。"
    )
