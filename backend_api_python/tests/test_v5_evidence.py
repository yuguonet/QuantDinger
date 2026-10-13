"""v5 P1-2：EV-1 证据向量契约守卫（TDD，2026-10-13）

EV-1 契约（v5 §C3P 第②段产出）：
    ev_ver / condition_key / subject / claims / discriminators / falsifiers
    / evidence_strength / provenance{prompt_sha, producer_ver, evidence_class, as_of}

**无人值守下的安全策略（重要，改动前先读）**：
    本批**不改 `qd_agent_cases` 表结构**（加列是不可逆 DDL，用户不在场不做），
    EV-1 先落 **JSONL 文件**（与 D1 情境帧同款策略：先落盘、后进统计）。
    等契约稳定、样本攒够后再决定要不要入表。

**★ 不伪造原则（本文件最重要的守卫）**：
    `discriminators` / `falsifiers` 需要 LLM 生成，当前**没有** LLM 证据生成接线。
    ⇒ 留空是**正确行为**，并如实标 `evidence_class="weak"`。
    绝不能填占位符（如 `"待补充"` / `"无"`）假装有了 —— 那会让下游校准把
    「没有判别特征」误读成「判别特征说明这里不适用」，是**静默污染**。

**条件空间封顶（v5 §统计功效）**：
    T+3 口径年独立簇 361，`max_cells = floor(361/30) = 12` 格。
    默认只用 `regime × vol_bucket` 两维（3×3=**9** 格 ≤ 12）；
    加开 `theme_heat` 会变 27 格 ⇒ **超预算**，须如实报错（不静默）。
    这条把 v5 的统计功效约束变成了**可执行断言**。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.chain import evidence as ev


@pytest.fixture()
def _tmpdir(tmp_path, monkeypatch):
    monkeypatch.setenv("QD_EV_DIR", str(tmp_path))


# ═══════════════════════════════════════════════════════════════
#  契约完整性
# ═══════════════════════════════════════════════════════════════

def test_evidence_has_all_contract_fields():
    e = ev.build_evidence(subject="600519", claims=[{"claim_type": "direction"}])
    for k in ("ev_ver", "condition_key", "subject", "claims", "discriminators",
              "falsifiers", "evidence_strength", "provenance"):
        assert k in e, f"EV-1 契约缺字段 {k}：实际 {sorted(e)}"
    for k in ("prompt_sha", "producer_ver", "evidence_class", "as_of"):
        assert k in e["provenance"], f"provenance 缺 {k}"
    assert e["ev_ver"] == ev.EV_VER


def test_evidence_bar_freq_is_reserved():
    """provenance 带 `bar_freq`（拍板 2 预留字段），默认 `1d`。"""
    e = ev.build_evidence(subject="600519")
    assert e["provenance"]["bar_freq"] == "1d"


# ═══════════════════════════════════════════════════════════════
#  ★ 不伪造
# ═══════════════════════════════════════════════════════════════

def test_empty_discriminators_marked_weak():
    """没有判别特征 ⇒ `evidence_class="weak"`，**不许填占位符**。"""
    e = ev.build_evidence(subject="600519", claims=[{"claim_type": "direction"}])
    assert e["discriminators"] == []
    assert e["falsifiers"] == []
    assert e["provenance"]["evidence_class"] == "weak"
    # 反例：任何"看似填了"的占位内容都该被拒绝
    for v in (*e["discriminators"], *e["falsifiers"]):
        assert str(v).strip() not in ("待补充", "无", "N/A", "-", ""), "发现占位符"


def test_having_falsifiers_upgrades_class():
    """有 falsifier ⇒ 证据等级提升（弱 → 中/强），说明分类**随内容**变而非写死。"""
    weak = ev.build_evidence(subject="600519", claims=[{"claim_type": "direction"}])
    strong = ev.build_evidence(
        subject="600519", claims=[{"claim_type": "direction"}],
        discriminators=["放量突破前高"], falsifiers=["次日跌破 5 日线"])
    assert weak["provenance"]["evidence_class"] != strong["provenance"]["evidence_class"]
    assert strong["evidence_strength"] > weak["evidence_strength"]


def test_evidence_strength_is_computed_not_constant():
    """evidence_strength 必须随内容变化（写死常量 = 假指标）。"""
    vals = set()
    for kw in ({}, {"claims": [{"claim_type": "direction"}]},
               {"discriminators": ["a"]}, {"falsifiers": ["b"]},
               {"discriminators": ["a"], "falsifiers": ["b"]}):
        vals.add(ev.build_evidence(subject="600519", **kw)["evidence_strength"])
    assert len(vals) >= 4, f"evidence_strength 几乎不随内容变化（只有 {sorted(vals)}）"
    assert all(0.0 <= v <= 1.0 for v in vals), "evidence_strength 必须落在 0~1"


# ═══════════════════════════════════════════════════════════════
#  ★ 红线 R1：LLM 自报概率一律丢弃
# ═══════════════════════════════════════════════════════════════

def test_llm_self_reported_probability_is_dropped():
    """R1：任何 LLM 自报概率字段必须被**丢弃并记违规**，不得进入证据向量。"""
    e = ev.build_evidence(
        subject="600519",
        extra={"p_up": 0.73, "confidence_llm": 0.9, "probability": 0.6})
    # ★ 排除 `violations`：违规记录**必须**留字段名才谈得上可追溯，
    #   所以只检查载荷本体有没有漏进去。
    blob = str({k: v for k, v in e.items() if k != "violations"})
    for bad in ("0.73", "0.9", "0.6", "p_up", "confidence_llm"):
        assert bad not in blob, f"自报概率 {bad} 漏进了证据向量（违反 R1）"
    viol = e.get("violations") or []
    assert any("R1" in str(v) for v in viol), f"丢弃了却没记违规（无法追溯）: {viol}"


def test_clean_extra_is_kept():
    """非概率类的额外字段**不应**被误杀（R1 只针对自报概率）。"""
    e = ev.build_evidence(subject="600519", extra={"source_tool": "agent_get_kline"})
    assert e.get("extra", {}).get("source_tool") == "agent_get_kline"
    assert not (e.get("violations") or [])


# ═══════════════════════════════════════════════════════════════
#  条件键 + 条件空间封顶
# ═══════════════════════════════════════════════════════════════

def test_condition_key_is_deterministic():
    """同输入必须同输出（否则无法配对，条件校准失去意义）。"""
    frame = {"regime": "up", "vol_bucket": "high", "theme_heat": "hot"}
    a = ev.condition_key("600519", frame)
    b = ev.condition_key("600519", dict(frame))
    assert a == b
    assert "up" in a and "high" in a


def test_condition_key_without_frame_is_unknown():
    """拿不到情境 ⇒ 标 `unknown`，**不许猜**（猜出来的条件键比没有更糟）。"""
    assert "unknown" in ev.condition_key("600519", None)


def test_cell_budget_default_is_within_limit():
    """默认维度组合数必须 ≤ max_cells（v5：T+3 口径 12 格）。"""
    ok, cnt, cap = ev.assert_cell_budget()
    assert ok, f"默认条件空间 {cnt} 格超出预算 {cap} 格 ⇒ 每格样本不足，校准不可信"
    assert cnt <= cap


def test_cell_budget_overrun_is_reported(monkeypatch):
    """开第三维会超预算 ⇒ 必须**如实报错**，不是静默接受。"""
    monkeypatch.setenv("QD_EV_KEY_DIMS", "regime,vol_bucket,theme_heat")
    ok, cnt, cap = ev.assert_cell_budget()
    assert not ok and cnt > cap, f"超预算没被报出来：cnt={cnt} cap={cap}"


# ═══════════════════════════════════════════════════════════════
#  落盘
# ═══════════════════════════════════════════════════════════════

def test_append_and_load(_tmpdir):
    e = ev.build_evidence(subject="600519", claims=[{"claim_type": "direction"}])
    p = ev.append_evidence(e)
    assert Path(p).exists()
    rows = ev.load_evidence()
    assert len(rows) == 1 and rows[0]["subject"] == "600519"


def test_same_key_rerun_is_idempotent(_tmpdir):
    """同一 (as_of, subject) 重跑 ⇒ 覆盖，不重复追加（补跑不重复计票）。"""
    for _ in range(2):
        ev.append_evidence(ev.build_evidence(
            subject="600519", as_of="2026-10-12",
            claims=[{"claim_type": "direction"}]))
    assert len(ev.load_evidence()) == 1


def test_different_subjects_both_kept(_tmpdir):
    for s in ("600519", "000001"):
        ev.append_evidence(ev.build_evidence(subject=s, as_of="2026-10-12"))
    assert len(ev.load_evidence()) == 2
