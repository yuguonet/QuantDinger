"""v5 P1-3：注入块升级为「金例 + 判别特征检查清单」（TDD，2026-10-13）

**为什么升级**：原注入块给的是**结论**（"T+N 校验正确"）+ 工具链骨架。
模型拿到结论只会**照抄结构**，而 v5 要的是**条件化预测力** ——
该给的是「当时**凭什么**这么判」（可迁移的判别特征），而不是「当时判对了」。

**★ 向后兼容（改本文件前必读）**：
`tests/test_v5_compaction_guard.py` 断言注入块 **startswith(CASE_MARK)**，
且 C3 的 `_QD_COMPACT_NOTES` 第 6 条按该指纹点名保护。
⇒ 升级**只能往后加内容，不能动开头那行**，否则压缩守卫与摘要指令同时失效。

**★ 不伪造（与 EV-1 同款纪律）**：
判别特征来自 LLM 证据生成，当前没有该接线 ⇒ 案例里**没有**就是没有。
此时如实写「尚未记录判别特征」，**不许**填占位符 ——
否则模型会把「没记录」误读成「此处不适用」。

**evidence 存哪**：`outcome.evidence`（jsonb 子键），**不加列**。
`backfill_by_root` 用 `jsonb_set(outcome,'{label}',...)` 改 label，不受影响。
"""
from __future__ import annotations

from app.agent.utils import case_memory as cm

CASE_MARK = "【历史相似案例——仅作参考，不要照搬结构】"


def _case(**kw) -> dict:
    base = {
        "task_summary": "分析 600519 走势",
        "plan_digest": [{"tools": ["agent_get_kline"]}],
        "outcome": {"label": "correct"},
        "failure_modes": [],
    }
    base.update(kw)
    return base


def _with_ev(disc=None, fals=None, ck="up|high") -> dict:
    return _case(outcome={"label": "correct", "evidence": {
        "ev_ver": "ev1", "condition_key": ck,
        "discriminators": list(disc or []), "falsifiers": list(fals or [])}})


# ═══════════════════════════════════════════════════════════════
#  向后兼容
# ═══════════════════════════════════════════════════════════════

def test_fingerprint_is_preserved():
    """★ 压缩守卫 + 摘要指令都依赖这个前缀 ⇒ 升级**不得**动它。"""
    blk = cm.format_case_injection([_with_ev(["放量突破"])])
    assert blk, "注入块为空（C2 守卫会因无法校验指纹而失败）"
    assert blk.startswith(CASE_MARK), f"金例块前缀被改:\n{blk[:60]}"


def test_legacy_case_shape_still_works():
    """没有 evidence 的老案例**必须**还能渲染（向后兼容，不许抛异常）。"""
    blk = cm.format_case_injection([_case()])
    assert blk.startswith(CASE_MARK)
    assert "600519" in blk


def test_empty_returns_empty():
    assert cm.format_case_injection([]) == ""
    assert cm.format_case_injection(None) == ""


# ═══════════════════════════════════════════════════════════════
#  判别特征检查清单
# ═══════════════════════════════════════════════════════════════

def test_discriminators_are_injected():
    blk = cm.format_case_injection([_with_ev(["放量突破前高", "资金净流入"])])
    assert "放量突破前高" in blk and "资金净流入" in blk


def test_falsifiers_are_injected():
    """证伪条件必须出现 —— 这是 v5「证伪 gate」在注入侧的落地。"""
    blk = cm.format_case_injection([_with_ev(["放量突破"], ["次日跌破 5 日线"])])
    assert "次日跌破 5 日线" in blk


def test_condition_key_is_injected():
    """适用市况必须出现 —— 条件化预测力的**核心**：知道这条经验在哪种市况成立。"""
    blk = cm.format_case_injection([_with_ev(["a"], ck="down|low")])
    assert "down|low" in blk


def test_checklist_can_be_turned_off():
    """开关存在（部分场景只想要原版）。关掉后不得出现清单。"""
    blk = cm.format_case_injection([_with_ev(["放量突破"])], with_checklist=False)
    assert "放量突破" not in blk
    assert blk.startswith(CASE_MARK)


# ═══════════════════════════════════════════════════════════════
#  ★ 不伪造
# ═══════════════════════════════════════════════════════════════

def test_absence_is_stated_not_faked():
    """没有判别特征 ⇒ **如实说明**，不许填占位符假装有了。"""
    blk = cm.format_case_injection([_with_ev([], [])])
    for fake in ("待补充", "N/A", "暂无（不适用）"):
        assert fake not in blk, f"出现占位符 {fake}"
    assert "尚未记录" in blk or "未记录" in blk, (
        f"没有判别特征时应当如实说明（否则模型会误以为「不适用」）:\n{blk}")


def test_no_evidence_is_not_treated_as_no_discriminator_needed():
    """整条案例没有 evidence 时，同样如实标注，而不是干脆不提。"""
    blk = cm.format_case_injection([_case()])
    assert "尚未记录" in blk or "未记录" in blk


# ═══════════════════════════════════════════════════════════════
#  既有语义保护
# ═══════════════════════════════════════════════════════════════

def test_incorrect_case_gives_no_fix_path():
    """失败案例只给坑、**绝不附修复路径** —— 这条既有纪律不得被升级破坏。"""
    blk = cm.format_case_injection([_case(outcome={"label": "incorrect"},
                                          failure_modes=["数据缺失导致误判"])])
    assert "数据缺失导致误判" in blk
    assert "工具链骨架" not in blk, "失败案例不该给工具链（那是修复路径）"


# ═══════════════════════════════════════════════════════════════
#  outcome 组装（evidence 存 outcome.evidence，不加列）
# ═══════════════════════════════════════════════════════════════

def test_outcome_carries_evidence():
    ev = {"ev_ver": "ev1", "discriminators": ["a"]}
    out = cm._build_outcome(confidence=0.6, evidence=ev)
    assert out["label"] == "pending"
    assert out["confidence"] == 0.6
    assert out["evidence"] == ev


def test_outcome_without_evidence_has_no_empty_shell():
    """没有 evidence 就**不要**塞空壳 —— 空壳会让「没采集」看起来像"采集了但为空"。"""
    assert "evidence" not in cm._build_outcome(confidence=None, evidence=None)
    assert "evidence" not in cm._build_outcome(confidence=None, evidence={})
