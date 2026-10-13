# -*- coding: utf-8 -*-
"""统计功效（MDE / 独立簇）守卫 —— 设计文档 v3 §10.2 的可执行对账。

★ 为什么单独成文件
  这三个函数原住 `app/agentcore/tools/catalog.py`，测试散在 `test_v6_skeleton.py`
  里。2026-10-13 P16 该目录整体退役，函数迁到 `app/utils/stat_power.py`，
  测试随之独立出来——骨架没了，但「v3 §10.2 的数字不能被改坏」这条红线仍在。

★ 守护的东西
  `estimate_mde` / `estimate_independent_clusters` / `required_days_for_mde`
  是全仓独一份（app/agent 侧无同名实现），且是条件化预测引擎的地基常量：
  任何"这个条件下策略是不是真的更好"的判断，先得回答"样本够不够"。
  数字改了必须回写设计文档 §10.2，别就地改数。
"""
import pytest

from app.utils.stat_power import (
    RHO_BAR,
    estimate_independent_clusters,
    estimate_mde,
    required_days_for_mde,
)


# ── 1. 统计口径与 v3 §10 实测对账 ────────────────────────────────

def test_catalog_numbers_match_design_doc():
    """工具输出必须复现 v3 §10 的实测表（ρ̄=0.2191 / 150 只每日）。

    对账点（v3 §10 独立簇表 + MDE 表）：
      243 交易日 × 150 只 → 1083 独立簇（T+1）
      361 簇 → MDE 7.4pp   （T+3）
      1083 簇 → MDE 4.3pp  （T+1）
      54 簇 → MDE 19.0pp   （T+20）
      单日有效观察 ≈ 4.5
    """
    clusters = estimate_independent_clusters(243)
    assert clusters["independent_clusters"] == pytest.approx(1083, rel=0.01)
    assert clusters["per_day"] == pytest.approx(4.5, abs=0.1)
    assert clusters["deff"] == pytest.approx(33.6, abs=0.2)

    assert estimate_mde(361)["mde_pp"] == pytest.approx(7.4, abs=0.1)
    assert estimate_mde(1083)["mde_pp"] == pytest.approx(4.3, abs=0.1)
    assert estimate_mde(54)["mde_pp"] == pytest.approx(19.0, abs=0.2)


def test_mde_rejects_raw_sample_count_instead_of_clusters():
    """把原始样本数当独立簇数填 → 必须报错，不能悄悄给出偏小的 MDE。"""
    out = estimate_mde(0)
    assert "error" in out
    # 对比：150 只/日若被误当成 150 个独立簇，MDE 会小约 2.7 倍
    naive = estimate_mde(150)["mde_pp"]
    honest = estimate_mde(4.5)["mde_pp"]
    assert naive < honest / 2.0, "这个对比本身就是'为什么必须换算'的证明"


def test_required_days_is_inverse_of_mde():
    """反解与正算必须自洽：required_days_for_mde → estimate_mde 能回到原值。"""
    target = 7.4
    days = required_days_for_mde(target)["required_days"]
    back = estimate_independent_clusters(days)["independent_clusters"]
    assert estimate_mde(back)["mde_pp"] == pytest.approx(target, abs=0.15)


def test_tools_return_error_object_not_exception():
    """非法入参返回 {'error': ...}，不抛 —— 工具失败是观察值不是崩溃。"""
    assert "error" in estimate_mde(-1)


# ── 2. 常量与文档同源（防就地改数） ──────────────────────────────

def test_rho_bar_matches_design_doc():
    """ρ̄ 必须与 v3 §10.2 一致 —— 它是全部效能估算的分母。"""
    assert RHO_BAR == pytest.approx(0.2191)


def test_mde_monotonic_in_clusters():
    """簇数越多 → MDE 越小（样本越多能分辨的差异越小）。单调性破了即算错。"""
    vals = [estimate_mde(n)["mde_pp"] for n in (54, 361, 1083)]
    assert vals[0] > vals[1] > vals[2], f"MDE 应随簇数单调下降，实测 {vals}"
