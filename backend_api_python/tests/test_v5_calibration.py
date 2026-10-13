"""v5 P3：条件校准与层级池化（TDD，2026-10-13）

## 校准思路（可解释优先，不上一上来就搞逻辑回归）

按条件格做**频率校准**：
    β₀(格) = logit(该格实际胜率) − logit(该格 p_base 均值)
即"把该格的预测概率推到它实际发生的频率上"。可解释、可审计、无需迭代优化，
且天然是**条件化**的（不同市况给不同修正）。

## ★ 样本不足 ⇒ 不出系数（v5 硬约束）

T+3 口径年独立簇 361、条件空间 12 格 ⇒ 每格约 30 簇。
**< MIN_CLUSTERS 的格只出先验（不给 β₀）** ——
给一个基于 8 个样本拟合出来的 β₀，等于拿噪声当信号，
且事后它与"真系数"长得一模一样，无法区分。

## 层级池化

格不足 ⇒ 向**父层**借：
    "up|high"（regime×vol）→ "up|*"（只 regime）→ "*"（全局）
每借一级都要**如实标注**来自哪一层（`source_level`），
否则下游会把"全局先验"当成"该格专属修正"，是**静默放大**。

## τ 季度重估

系数带 `fitted_at`，超过 `QD_CALIB_TAU_DAYS`（默认 90 天）标记 `stale`。
过期**不自动删除**（留着可审计），但 `usable` 置 False。
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest

from app.agent.chain import calibration as cal


def _cell(n_clusters: int, win_rate: float, mean_p_base: float) -> dict:
    return {"n_clusters": n_clusters, "win_rate": win_rate,
            "mean_p_base": mean_p_base}


# ═══════════════════════════════════════════════════════════════
#  ★ 样本不足 ⇒ 不出系数
# ═══════════════════════════════════════════════════════════════

def test_under_min_clusters_gives_no_beta():
    """< MIN_CLUSTERS ⇒ **不给 β₀**（只出先验），不是给个"估估看"的值。"""
    r = cal.fit_cell("up|high", _cell(8, 0.8, 0.5))
    assert r["b0"] is None, "只有 8 簇却给了 β₀ ⇒ 拿噪声当信号"
    assert r["usable"] is False
    assert r["reason"], "不出系数必须说清为什么（否则下游以为是 0）"


def test_enough_clusters_gives_beta():
    r = cal.fit_cell("up|high", _cell(40, 0.7, 0.5))
    assert r["b0"] is not None
    assert r["usable"] is True
    assert r["b0"] > 0, "实际胜率 0.7 > p_base 0.5 ⇒ 应向上修正"


def test_beta_direction_matches_gap():
    """β₀ 方向必须跟随「实际 − 预测」的差（向下修正时是负的）。"""
    up = cal.fit_cell("k", _cell(40, 0.7, 0.5))
    down = cal.fit_cell("k", _cell(40, 0.3, 0.5))
    assert up["b0"] > 0 > down["b0"]


def test_min_clusters_is_env_configurable(monkeypatch):
    monkeypatch.setenv("QD_CALIB_MIN_CLUSTERS", "5")
    assert cal.min_clusters() == 5
    assert cal.fit_cell("k", _cell(6, 0.7, 0.5))["usable"] is True


# ═══════════════════════════════════════════════════════════════
#  层级池化
# ═══════════════════════════════════════════════════════════════

def test_pool_up_on_shortage():
    """本格不足 ⇒ 向父层借，且**如实标注**来源层。"""
    cells = {"up|high": _cell(5, 0.9, 0.5), "up|mid": _cell(50, 0.6, 0.5)}
    r = cal.fit_with_pooling("up|high", cells)
    assert r["source_level"] == "parent", f"应池化到父层，实际 {r}"
    assert r["pooled_from"] == "up|*", f"应记明借自父层键: {r}"
    # 本格簇数**不得**被池化值覆盖（否则看不出本格其实只有 5 簇）
    assert r["n_clusters"] == 5
    assert r["pooled_n_clusters"] >= 50


def test_pool_to_global_when_parent_also_short():
    cells = {"up|high": _cell(5, 0.9, 0.5), "down|low": _cell(60, 0.4, 0.5)}
    r = cal.fit_with_pooling("up|high", cells)
    assert r["source_level"] in ("global", "prior"), f"父层也不足应退到全局: {r}"


def test_no_pooling_when_cell_is_rich():
    cells = {"up|high": _cell(60, 0.7, 0.5), "up|mid": _cell(50, 0.6, 0.5)}
    r = cal.fit_with_pooling("up|high", cells)
    assert r["source_level"] == "cell", f"本格够样本就不该池化: {r}"


def test_pooling_aggregates_siblings():
    """父层 = 同 regime 的**所有**兄弟格（不是随便挑一个）。"""
    cells = {"up|high": _cell(5, 0.9, 0.5), "up|mid": _cell(50, 0.6, 0.5),
             "up|low": _cell(50, 0.5, 0.5)}
    r = cal.fit_with_pooling("up|high", cells)
    assert r["pooled_n_clusters"] >= 100, f"父层应聚合两格(50+50): {r}"


# ═══════════════════════════════════════════════════════════════
#  τ 季度重估
# ═══════════════════════════════════════════════════════════════

def test_fresh_coef_is_usable():
    c = {"b0": 0.1, "fitted_at": datetime.now().isoformat(timespec="seconds")}
    assert cal.is_fresh(c) is True
    assert cal.coef_usable(c) is True


def test_stale_coef_is_not_usable_but_kept():
    """过期 ⇒ `usable=False`，但**不删**（留着可审计）。"""
    old = (datetime.now() - timedelta(days=200)).isoformat(timespec="seconds")
    c = {"b0": 0.1, "fitted_at": old}
    assert cal.is_fresh(c) is False
    assert cal.coef_usable(c) is False
    assert c["b0"] == 0.1, "过期系数不该被删掉（要能审计它曾经存在）"


def test_tau_is_env_configurable(monkeypatch):
    monkeypatch.setenv("QD_CALIB_TAU_DAYS", "30")
    assert cal.tau_days() == 30


# ═══════════════════════════════════════════════════════════════
#  校准报告（可审计）
# ═══════════════════════════════════════════════════════════════

def test_report_lists_every_cell_with_sample_size():
    """报告必须列出**每一格**及其样本数 —— 否则看不出哪些格是空的。"""
    cells = {"up|high": _cell(40, 0.7, 0.5), "down|low": _cell(3, 0.9, 0.5)}
    rep = cal.calibration_report(cells)
    assert set(rep["cells"]) == {"up|high", "down|low"}
    # 本格簇数**如实**显示（3），不被借来的簇数掩盖 ⇒ 一眼看得出哪些格是空的
    assert rep["cells"]["down|low"]["n_clusters"] == 3
    assert rep["cells"]["up|high"]["n_clusters"] == 40
    # 3 簇那格借的是**全局先验**（40+3=43 簇达标），且来源层如实标注 ⇒ 可审计
    assert rep["cells"]["down|low"]["source_level"] == "global"
    assert rep["cells"]["down|low"]["pooled_n_clusters"] == 43
    assert rep["cells"]["up|high"]["source_level"] == "cell"
    assert rep["n_usable"] == 2


def test_report_on_empty_input():
    rep = cal.calibration_report({})
    assert rep["n_usable"] == 0
    assert rep["cells"] == {}
