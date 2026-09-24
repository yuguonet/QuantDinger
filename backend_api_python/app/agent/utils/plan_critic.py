# -*- coding: utf-8 -*-
"""Plan Critic + Best-of-N 选优（提智方案 二波 B1-B，2026-09-24，署名：OpenClaw agent）。

来源：`agent_tizhi_final_plan_20260923.md` §三 二波 B1（LLM plan critic + Best-of-N）
+ §四 冲突裁决 #5（N=2 起步，方差大才升 N=3+选优）。

职责：
  · **critique()**：对单个候选规划做"三问"审查（哪步会断 / 验收无法判定 / 预算失配），
    输出 `{fatal, warnings, score}`。只挑缺陷、**不改写计划**。
  · **select_best()**：Best-of-N 选优——fatal=0 取 score 最高；全有 fatal 取 fatal 最少
    （带回炉）；score 缺失/并列时按"最小工具面、最小步数"破平（防宏大计划）。
  · **format_defects()**：把缺陷整理成回炉反馈（只列缺陷，不给改法——回炉 prompt 的
    "只修复列出的问题"约束由调用方拼接）。

设计点：
  · **确定性优先**：能被 plan_linter（R1~R4）确定性查出的缺陷不劳 LLM；critic 只兜
    确定性查不了的（逻辑断点/验收不可判定/预算失配）——提智原则 1。
  · **fail-open**：critic 调用失败/输出损坏 ⇒ 该候选 score=None、fatal=[]，**不阻断**
    规划主链（与 plan_linter 同策略）；但错误必须留痕（warnings 带 reason），不静默。
  · **选优是纯函数**：候选进、(index, reason) 出，不触 LLM/DB，可单测、可审计。
  · parse 失败的候选在选优里等价于"1 条最重 fatal"（内容不可用），N=2 时另一候选
    自然胜出——顺带兑现裁决"顺带防 planner JSON 格式损坏"。

易错点：
  · `score` 是 critic 自报的 0~10 分，**不可信值要夹紧**（LLM 可能回 100/字符串）；
  · 选优破平用的"最小工具面/最小步数"在候选的**原始承诺**上统计（含幽灵名——偏保守，
    只影响并列破平，不改工具面；工具面的规格化/裁剪由 plan_linter 在选优后统一做）；
  · critique 的输入必须是**规格化前的原始 plan JSON 摘要**（含 planner 全部承诺），
    传规格化后的版本会把"planner 承诺了不存在的工具"这类缺陷洗掉。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "critique", "select_best", "format_defects", "summarize_plan",
    "critic_enabled", "CritiqueResult",
]

# ── 运维开关（env=运维可调，配置面收敛两层原则）─────────────────────────────
# AGENT_PLAN_CRITIC=0 关闭 LLM critic（选优退化为确定性破平规则）。
# 关闭后 Best-of-N 仍在（纯确定性选优）；彻底单候选需 AGENT_PLAN_BEST_OF_N=1。


def critic_enabled() -> bool:
    """LLM plan critic 开关（默认开）。"""
    return (os.getenv("AGENT_PLAN_CRITIC", "1") or "1").strip().lower() \
        not in ("0", "false", "no", "off")


# 三问 prompt（草稿原文出自用户方案 §三 三、LLM critic 层；措辞冻结，改动先评审）
CRITIC_SYSTEM_PROMPT = (
    "你是研究规划审稿人。给定任务、工具清单（只有这些可用，没有其他）和候选计划。"
    "只挑会导致执行失败或结论不可靠的缺陷：\n"
    "1.哪一步会因数据缺失或工具不支持而断掉？\n"
    "2.哪一步的验收标准实际无法判定？\n"
    "3.步数与预算是否和任务规模失配？\n"
    '输出 JSON：{"fatal": ["缺陷，至多3条"], "warnings": ["提醒，至多3条"], "score": 0-10}。'
    "不要改写计划本身。\n"
    "评分维度（score 综合以下各点，5 分为合格）：\n"
    "- 致命缺陷数量（fatal 越多分越低；有 fatal 最高 4 分）\n"
    "- **最小工具面、最小步数**：工具面越小、步数越少且仍能完成任务，分越高"
    "（宏大计划扣分）\n"
    "- 阶段间依赖是否闭合、验收标准是否可判定"
)


class CritiqueResult:
    """critique() 的结构化结果（纯数据）。"""

    __slots__ = ("fatal", "warnings", "score", "error")

    def __init__(self, fatal: Optional[List[str]] = None,
                 warnings: Optional[List[str]] = None,
                 score: Optional[float] = None,
                 error: str = "") -> None:
        self.fatal: List[str] = list(fatal or [])
        self.warnings: List[str] = list(warnings or [])
        self.score: Optional[float] = score
        self.error: str = error

    @property
    def fatal_count(self) -> int:
        return len(self.fatal)

    def to_trace(self) -> dict:
        return {"fatal": self.fatal, "warnings": self.warnings,
                "score": self.score, "error": self.error}


def _clamp_score(v) -> Optional[float]:
    """critic 自报分数不可信：数值化 + 夹紧 [0,10]；坏值返回 None（=无信息）。"""
    try:
        s = float(v)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(10.0, s))


def summarize_plan(plan: Dict[str, Any]) -> str:
    """候选计划 → critic 输入摘要（保留 planner 全部承诺，含幽灵工具名——见头部易错点）。

    刻意只留结构与承诺，不留长篇 task 正文（三问只关心"做什么/用什么/怎么验收"）；
    task 正文由 critique() 在 user 消息里另附全文。
    """
    import json
    phases = []
    for p in (plan.get("phases") or []):
        if not isinstance(p, dict):
            continue
        phases.append({
            "name": p.get("name"), "goal": str(p.get("goal") or "")[:300],
            "tools": p.get("tools"), "acceptance": p.get("acceptance"),
            "step_budget": p.get("step_budget"), "barrier": p.get("barrier"),
            "replan": p.get("replan"),
        })
    slim = {
        "task": str(plan.get("task") or "")[:1200],
        "selected_skill": plan.get("selected_skill"),
        "selected_domain": plan.get("selected_domain"),
        "step_budget": plan.get("step_budget"),
        "tools": plan.get("tools"),
        "phases": phases,
    }
    return json.dumps(slim, ensure_ascii=False, default=str)


async def critique(llm, user_input: str, plan_json_text: str,
                   tools_hint: str = "") -> CritiqueResult:
    """对单个候选规划做三问审查（fail-open：异常/坏输出不阻断主链）。

    Args:
        llm: LLMBase（复用规划器同一实例——"同模型不同 prompt 即可"，用户方案原文）。
        user_input: 用户任务原文。
        plan_json_text: summarize_plan() 的输出。
        tools_hint: 本轮真实可用工具名清单文本，供"工具不支持"判据。

    Returns:
        CritiqueResult。error 非空 = critic 未生效（调用失败/输出损坏），
        此时 fatal 恒为空、score 恒为 None，选优自动退化为确定性破平。
    """
    from llm.base import ChatMessage
    from utils.json_parser import safe_parse_json

    user_prompt = (
        "【任务】\n" + str(user_input or "")[:1500] + "\n\n"
        "【可用工具清单（只有这些可用，没有其他）】\n"
        + (tools_hint or "(未提供)")[:2000]
        + "\n\n【候选计划】\n" + str(plan_json_text or "")[:3000]
        + "\n\n只输出 JSON，不要其他文字。"
    )
    try:
        resp = await llm.generate(messages=[
            ChatMessage(role="system", content=CRITIC_SYSTEM_PROMPT),
            ChatMessage(role="user", content=user_prompt),
        ])
    except Exception as e:
        return CritiqueResult(error="critic 调用失败: %s" % e,
                              warnings=["plan_critic 不可用（已降级为确定性选优）"])

    raw = (getattr(resp, "content", "") or "").strip()
    data = safe_parse_json(raw, default={})
    if not isinstance(data, dict) or not data:
        return CritiqueResult(
            error="critic 输出不可解析",
            warnings=["plan_critic 输出损坏（已降级为确定性选优）：%s" % raw[:80]])

    fatal = [str(x)[:200] for x in (data.get("fatal") or []) if str(x).strip()][:3]
    warns = [str(x)[:200] for x in (data.get("warnings") or []) if str(x).strip()][:3]
    return CritiqueResult(fatal=fatal, warnings=warns,
                          score=_clamp_score(data.get("score")))


# ═══════════════════════════════════════════════════════════════
#  Best-of-N 选优（纯函数）
# ═══════════════════════════════════════════════════════════════

def _tool_face_size(cand: Dict[str, Any]) -> int:
    """候选的工具面大小（规格化后：phases 白名单并集 或 顶层点名单）。"""
    phases = cand.get("phases") or []
    if phases:
        n = 0
        seen = set()
        for p in phases:
            for t in (p.get("tools") or []):
                if t not in seen:
                    seen.add(t)
                    n += 1
        return n
    return len(cand.get("plan_tools") or [])


def _step_size(cand: Dict[str, Any]) -> int:
    """候选的步数规模（多阶段=各阶段预算之和；单段=step_budget）。"""
    phases = cand.get("phases") or []
    if phases:
        return sum(int(p.get("step_budget") or 0) or 1 for p in phases)
    return int(cand.get("step_budget") or 0)


def _effective_fatals(cand: Dict[str, Any]) -> List[str]:
    """候选的等价 fatal 列表：parse 失败 = 1 条最重 fatal + critic fatal。"""
    if not cand.get("parsed"):
        return ["plan JSON 解析失败（内容不可用）"]
    return list((cand.get("critic") or CritiqueResult()).fatal)


def select_best(candidates: Sequence[Dict[str, Any]]) -> Tuple[int, str]:
    """Best-of-N 选优（用户方案 §三 三：fatal=0 取 score 最高；全 fatal 取 fatal 最少
    带回炉；评分含"最小工具面、最小步数"防宏大计划——破平规则兑现后者）。

    候选 dict 约定键：parsed(bool) / phases / plan_tools / step_budget /
    critic(CritiqueResult|None) / lint(LintReport|None)。

    Returns:
        (index, reason)。reason 进 trace，选优全程可审计。
    """
    if not candidates:
        return -1, "无候选"
    if len(candidates) == 1:
        return 0, "单候选"

    rows = []
    for i, c in enumerate(candidates):
        crit = c.get("critic") or CritiqueResult()
        fatals = _effective_fatals(c)
        rows.append({
            "i": i, "fatal": len(fatals), "parsed": bool(c.get("parsed")),
            "score": crit.score,          # None = critic 无信息
            "face": _tool_face_size(c),
            "steps": _step_size(c),
            "gran": len(c.get("granularity") or []),   # R3 粒度信号越少越好
            "lint_warn": len((c.get("lint").warnings if c.get("lint") else []) or []),
        })

    clean = [r for r in rows if r["fatal"] == 0]
    if clean:
        # fatal=0 → score 最高；score 缺失视为 5.0（中性，不让无信息候选吃亏/占便宜）
        clean.sort(key=lambda r: (-(r["score"] if r["score"] is not None else 5.0),
                                  r["face"], r["steps"], r["gran"], r["lint_warn"], r["i"]))
        top = clean[0]
        reason = ("fatal=0 按 score=%.1f 选优（工具面 %d / 步数 %d / 粒度信号 %d）"
                  % (top["score"] if top["score"] is not None else 5.0,
                     top["face"], top["steps"], top["gran"]))
        return top["i"], reason

    # 全有 fatal → 取 fatal 最少者带回炉。破平键首放 `parsed`（未解析内容不可用，
    # 不许靠"最小工具面 0"之类的破平赢过可用候选——实测 bug，2026-09-24 修）。
    rows.sort(key=lambda r: (r["fatal"], not r["parsed"],
                             r["face"], r["steps"], r["gran"], r["i"]))
    top = rows[0]
    return top["i"], "全候选有 fatal（%d 条最少），带回炉（工具面 %d / 步数 %d）" % (
        top["fatal"], top["face"], top["steps"])


def format_defects(cand: Dict[str, Any]) -> str:
    """把选中候选的缺陷整理成回炉反馈（只列缺陷不给改法，调用方拼接 REPAIR 约束）。

    缺陷源：critic fatal + R3 粒度信号（cand["granularity"]）+ lint 告警（若有）。
    """
    lines: List[str] = []
    for f in _effective_fatals(cand):
        lines.append("- " + f)
    for hint in (cand.get("granularity") or []):
        lines.append("- [粒度] " + str(hint))
    lint = cand.get("lint")
    for w in (getattr(lint, "warnings", None) or [])[:3]:
        lines.append("- [检查] " + w)
    return "\n".join(lines)
