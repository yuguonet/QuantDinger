"""v5 P2：条件化聚合器 + 配对双跑 + 红线守卫（TDD，2026-10-13）

聚合公式（v5）：
    logit(p_final) = logit(p_base) + β₀(condition_key) + Σ βⱼ(condition_key)·eⱼ
    约束：|Δlogit| ≤ LOGIT_DELTA_MAX（默认 0.4），**fail-open**

## ★ 当前状态：骨架，不是成品

`β` 需要**配对样本**才能拟合，而现在**一条配对都还没有**（采集刚启动）。
⇒ 无系数时 `p_final == p_base`（Δ=0）是**正确行为**，不是半成品 ——
   凭空编系数才是灾难（那是在拿噪声当信号，且事后与真系数无法区分）。

## 两条红线

**R1**：LLM **自报概率**一律丢弃并记违规（与 `chain/evidence.py` 同款，此处是聚合侧再拦一道）。
**R2**：`p_base` 路径**禁止**接入 `qd_agent_weights` / `exp_*` 反馈信号 ——
    否则"基线"里混进了它自己的历史战绩，Δp 估计会**自我强化**（越涨越信、越跌越怂）。
    ⇒ 用**源码扫描**守住：aggregator.py 里不得出现这些表名。

## 易错处

· `logit(0)` / `logit(1)` 会炸（除零 / log(0)）⇒ 必须夹到 (eps, 1-eps)。
· 裁剪**必须被记录**（`clipped=True`）—— 静默裁剪会让校准报告以为模型很保守。
· fail-open 指**聚合失败时退回 p_base**，不是退回"随便一个数"。
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from app.agent.chain import aggregator as ag


# ═══════════════════════════════════════════════════════════════
#  数学基础
# ═══════════════════════════════════════════════════════════════

def test_logit_sigmoid_roundtrip():
    for p in (0.1, 0.25, 0.5, 0.75, 0.9):
        assert abs(ag.sigmoid(ag.logit(p)) - p) < 1e-9


def test_logit_handles_degenerate_bounds():
    """p=0 / p=1 **不许炸**（除零 / log(0)），必须夹到内部。"""
    for p in (0.0, 1.0, -0.5, 1.5):
        v = ag.logit(p)
        assert math.isfinite(v), f"logit({p}) 非有限值 ⇒ 下游全 NaN"
    assert math.isfinite(ag.sigmoid(ag.logit(0.0)))


# ═══════════════════════════════════════════════════════════════
#  无系数 ⇒ 不动（不伪造）
# ═══════════════════════════════════════════════════════════════

def test_no_coefs_means_no_change():
    """没有校准系数 ⇒ p_final **必须等于** p_base（Δ=0）。

    凭空编系数才是灾难：那是在拿噪声当信号，且事后与真系数无法区分。
    """
    r = ag.aggregate(p_base=0.55, evidence={"discriminators": ["放量"]},
                     condition_key="up|high", coefs=None)
    assert r["p_final"] == pytest.approx(0.55)
    assert r["delta_logit"] == pytest.approx(0.0)
    assert r["used_coefs"] is False


def test_coefs_are_applied_when_present():
    coefs = {"b0": 0.2, "discriminators": 0.1}
    r = ag.aggregate(p_base=0.5, evidence={"discriminators": ["a", "b"]},
                     condition_key="up|high", coefs=coefs)
    assert r["used_coefs"] is True
    assert r["delta_logit"] > 0
    assert r["p_final"] > 0.5


# ═══════════════════════════════════════════════════════════════
#  裁剪
# ═══════════════════════════════════════════════════════════════

def test_delta_is_clipped():
    """再大的系数也不许把概率推飞 ⇒ 裁剪到 ±LOGIT_DELTA_MAX。"""
    r = ag.aggregate(p_base=0.5, evidence={"discriminators": ["a"] * 50},
                     condition_key="k", coefs={"b0": 99.0, "discriminators": 99.0})
    assert abs(r["delta_logit"]) <= ag.logit_delta_max() + 1e-9
    assert r["clipped"] is True, "裁剪了却没记录 ⇒ 校准报告会误判模型很保守"


def test_unclipped_marks_false():
    r = ag.aggregate(p_base=0.5, evidence={"discriminators": ["a"]},
                     condition_key="k", coefs={"b0": 0.01})
    assert r["clipped"] is False


def test_clip_bound_is_env_configurable(monkeypatch):
    monkeypatch.setenv("QD_AGG_LOGIT_MAX", "0.1")
    assert ag.logit_delta_max() == pytest.approx(0.1)
    r = ag.aggregate(p_base=0.5, evidence={}, condition_key="k", coefs={"b0": 5.0})
    assert abs(r["delta_logit"]) <= 0.1 + 1e-9


# ═══════════════════════════════════════════════════════════════
#  fail-open
# ═══════════════════════════════════════════════════════════════

def test_fail_open_returns_p_base():
    """聚合失败 ⇒ **退回 p_base**（不是随便一个数，也不是 0.5）。"""
    r = ag.aggregate(p_base=0.62, evidence={"discriminators": ["a"]},
                     condition_key="k", coefs={"b0": "不是数字"})
    assert r["p_final"] == pytest.approx(0.62)
    assert r["fail_open"] is True
    assert r["reason"]


def test_invalid_p_base_fails_open():
    for bad in (None, "x", float("nan")):
        r = ag.aggregate(p_base=bad, evidence={}, condition_key="k")
        assert r["fail_open"] is True, f"p_base={bad!r} 应 fail-open"
        assert r["p_final"] is None or 0.0 <= r["p_final"] <= 1.0


# ═══════════════════════════════════════════════════════════════
#  ★ 红线 R1（聚合侧再拦一道）
# ═══════════════════════════════════════════════════════════════

def test_r1_llm_probability_rejected():
    """把 LLM 自报概率塞进来当 p_base ⇒ 必须拒绝（fail-open + 记违规）。"""
    r = ag.aggregate(p_base=0.5, evidence={"p_up": 0.8, "discriminators": ["a"]},
                     condition_key="k")
    viol = r.get("violations") or []
    assert any("R1" in str(v) for v in viol), f"R1 未触发: {viol}"
    assert "0.8" not in str({k: v for k, v in r.items() if k != "violations"})


# ═══════════════════════════════════════════════════════════════
#  ★ 红线 R2（源码级）
# ═══════════════════════════════════════════════════════════════

def test_r2_aggregator_never_reads_feedback_tables():
    """aggregator.py **不得**出现 `qd_agent_weights` / `exp_` —— 否则 p_base
    混进自己的历史战绩，Δp 估计会自我强化（越涨越信、越跌越怂）。"""
    import re
    src = Path(ag.__file__).read_text(encoding="utf-8", errors="replace")
    # 去掉 docstring 再查 —— 文档里**点名**"禁止接入 X"是说明禁令，不是违反禁令。
    # （只去 `"""..."""` 块；单行 # 注释里也允许提，一并去掉）
    code = re.sub(r'""".*?"""', "", src, flags=re.S)
    code = "\n".join(ln for ln in code.splitlines()
                     if not ln.lstrip().startswith("#"))
    for bad in ("qd_agent_weights", "exp_cond_calib", "exp_pair"):
        assert bad not in code, (
            f"aggregator 代码里出现 {bad} ⇒ 违反红线 R2"
            f"（p_base 不得接入反馈信号）")


# ═══════════════════════════════════════════════════════════════
#  配对双跑留痕
# ═══════════════════════════════════════════════════════════════

@pytest.fixture()
def _tmpdir(tmp_path, monkeypatch):
    monkeypatch.setenv("QD_PAIR_DIR", str(tmp_path))


def test_record_pair_and_load(_tmpdir):
    ag.record_pair(claim_id=1, p_base=0.5, p_final=0.56,
                   condition_key="up|high", as_of="2026-10-12")
    rows = ag.load_pairs("2026-10-12")
    assert len(rows) == 1
    assert rows[0]["p_base"] == pytest.approx(0.5)
    assert rows[0]["delta_p"] == pytest.approx(0.06, abs=1e-6)


def test_record_pair_is_idempotent(_tmpdir):
    """同 (as_of, claim_id) 覆盖 —— 补跑不重复计票（与 G24 同款纪律）。"""
    for p in (0.55, 0.60):
        ag.record_pair(claim_id=1, p_base=0.5, p_final=p,
                       condition_key="k", as_of="2026-10-12")
    rows = ag.load_pairs("2026-10-12")
    assert len(rows) == 1
    assert rows[0]["p_final"] == pytest.approx(0.60)


def test_pair_carries_condition_key(_tmpdir):
    """配对记录必须带 condition_key —— 否则事后无法按条件格校准。"""
    ag.record_pair(claim_id=2, p_base=0.5, p_final=0.5,
                   condition_key="down|low", as_of="2026-10-12")
    assert ag.load_pairs("2026-10-12")[0]["condition_key"] == "down|low"
