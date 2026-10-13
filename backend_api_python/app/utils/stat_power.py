# -*- coding: utf-8 -*-
"""统计功效估算（MDE / 独立簇）—— 设计文档 v3 §10.2 的可执行实现。

★ 为什么它是地基常量
  1. 条件化预测引擎（v3 下编）的地基：任何「这个条件下策略是不是真的更好」
     的判断，先得回答「样本够不够」。没有它，后面所有 Δp 对比都是在噪声里找信号。
  2. 纯函数、零外部依赖 ⇒ 相关测试不依赖网络，不会因行情接口抖动而假红。
  3. 全仓独一份：app/agent 侧无同名实现，删了即丢能力。

★ 常量来源：v4 实测 → v3 §10 吸收，此处只是把结论做成可调用工具。
  改动必须回写设计文档 §10.2，别就地改数。

★ 位置 2026-10-13 变过一次：原住 `app/agentcore/tools/catalog.py`
  （P16 该目录退役，三件套迁至本文件）。

指令示例：
    from app.utils.stat_power import estimate_mde, estimate_independent_clusters
    estimate_mde(361)      # -> mde_pp ≈ 7.4（v3 §10.2 表内数值）
    estimate_mde(150)      # ⚠️ 错用法：150 是原始样本数，不是独立簇数
    estimate_independent_clusters(150)   # -> 约 4.5，先换算再估 MDE
"""
from __future__ import annotations

import math
from typing import Any

# (z_{0.975} + z_{0.80})^2 = (1.96 + 0.8416)^2 ≈ 7.8489
# 双侧 α=0.05、统计功效 80% 下的两比例检验系数。
# 来源：v3 §10（自 v4 实测论证吸收），改动必须回写文档，别就地改数。
_Z_SUM_SQUARED = 7.8489

# 全体 A 股日收益的平均 pairwise 相关系数（v4 实测）。
# 它决定了"同一天 N 只票"到底算几个独立观察 —— 是全部效能估算的分母。
RHO_BAR = 0.2191

# 单日池化标的数的默认口径（v4 实测样本：150 只/日）。
DEFAULT_POOL_SIZE = 150


def estimate_mde(n_clusters: float, p: float = 0.5) -> dict[str, Any]:
    """估算最小可检测效应（MDE）：给定独立观察数，能分辨多大的胜率差。

    n_clusters 必须填**独立簇数**而不是原始样本数 —— A 股同日标的高度
    相关，150 只/日只约等于 4.5 个独立观察。直接填原始样本数会把
    MDE 算小一个量级，得出"样本够了"的错误结论。

    Args:
        n_clusters: 独立观察（簇）数，可用 estimate_independent_clusters 换算
        p: 基线胜率，默认 0.5（此时方差最大，MDE 最保守）
    """
    if n_clusters <= 0:
        return {"error": "n_clusters 必须为正（独立簇数，非原始样本数）"}
    if not 0.0 < p < 1.0:
        return {"error": "p 必须落在 (0, 1) 区间"}

    variance = p * (1.0 - p)
    mde = math.sqrt(_Z_SUM_SQUARED * variance / n_clusters)
    return {
        "mde": round(mde, 6),
        "mde_pp": round(mde * 100.0, 2),
        "n_clusters": round(n_clusters, 2),
        "p_base": p,
        "basis": "two-proportion z-test, alpha=0.05 (two-sided), power=0.80",
    }


def estimate_independent_clusters(
    n_days: float,
    pool_size: float = DEFAULT_POOL_SIZE,
    rho: float = RHO_BAR,
) -> dict[str, Any]:
    """把"交易日 × 每日标的数"折算成独立观察数（簇）。

    设计效应 DEFF = 1 + (m-1)·ρ：同一天的 m 只票不是 m 个独立证据，
    因为它们共享当日市场共同因子（PC1 解释方差 24.1%）。

    Args:
        n_days: 交易日数
        pool_size: 每日池化标的数
        rho: 同日标的平均相关系数
    """
    if n_days <= 0:
        return {"error": "n_days 必须为正"}
    if pool_size <= 0:
        return {"error": "pool_size 必须为正"}
    if not -1.0 < rho < 1.0:
        return {"error": "rho 必须落在 (-1, 1) 区间"}

    deff = 1.0 + (pool_size - 1.0) * rho
    raw = n_days * pool_size
    clusters = raw / deff
    return {
        "independent_clusters": round(clusters, 2),
        "raw_observations": round(raw, 0),
        "deff": round(deff, 2),
        "per_day": round(pool_size / deff, 3),
        "rho": rho,
    }


def required_days_for_mde(
    target_mde_pp: float,
    pool_size: float = DEFAULT_POOL_SIZE,
    rho: float = RHO_BAR,
    p: float = 0.5,
) -> dict[str, Any]:
    """反解：想分辨 target_mde_pp 个百分点的胜率差，需要攒多少个交易日。

    MDE 公式的逆运算。用途是在开一个条件格之前先算清"这格要养多久"，
    避免开出一批永远达不到支撑度门槛的格子。

    Args:
        target_mde_pp: 目标可分辨的胜率差，单位百分点（如 7.4 表示 7.4pp）
        pool_size: 每日池化标的数
        rho: 同日标的平均相关系数
        p: 基线胜率
    """
    if target_mde_pp <= 0:
        return {"error": "target_mde_pp 必须为正"}
    if not 0.0 < p < 1.0:
        return {"error": "p 必须落在 (0, 1) 区间"}

    delta = target_mde_pp / 100.0
    need_clusters = _Z_SUM_SQUARED * p * (1.0 - p) / (delta * delta)
    deff = 1.0 + (pool_size - 1.0) * rho
    days = need_clusters * deff / pool_size
    return {
        "required_days": math.ceil(days),
        "required_clusters": round(need_clusters, 2),
        "target_mde_pp": target_mde_pp,
        "deff": round(deff, 2),
    }
