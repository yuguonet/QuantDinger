# -*- coding: utf-8 -*-
"""calibration — 条件校准与层级池化（v5 P3，2026-10-13）

## 校准思路（可解释优先，不上来就搞逻辑回归）

按条件格做**频率校准**：
    β₀(格) = logit(该格实际胜率) − logit(该格 p_base 均值)
即"把该格的预测概率推到它实际发生的频率上"。
可解释、可审计、无需迭代优化，且天然是**条件化**的（不同市况给不同修正）。

## ★ 样本不足 ⇒ 不出系数（v5 硬约束，改本文件时先读）

T+3 口径年独立簇 361、条件空间 12 格 ⇒ 每格约 30 簇。
**< `min_clusters()` 的格只出先验（不给 β₀）**。
给一个基于 8 个样本拟合的 β₀ = 拿噪声当信号，
且事后它与"真系数"长得一模一样，无法区分 —— 这是本项目最贵的一类错误。

## 层级池化

格不足 ⇒ 向**父层**借：
    "up|high"（regime×vol）→ "up|*"（只 regime）→ "*"（全局）
每借一级都**如实标注** `source_level` / `pooled_from`，
否则下游会把"全局先验"当成"该格专属修正" ⇒ **静默放大**。

## τ 季度重估

系数带 `fitted_at`，超过 `tau_days()`（默认 90）标 `stale`。
过期**不自动删除**（留着可审计），但 `usable=False`。

## 独立簇 vs 名义样本

★ `n_clusters` 是**独立簇数**，不是名义样本数 —— 150 只/日因同日相关，
DEFF_day=33.6 ⇒ 只折约 4.5 个独立观察。用名义样本数当 n 会**严重高估功效**。
"""
from __future__ import annotations

import math
import os
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from ..log import logger

CALIB_VER = "cal1"

_EPS = 1e-6


def _env_int(name: str, default: int) -> int:
    try:
        v = int(os.getenv(name, "").strip() or default)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def min_clusters() -> int:
    """每格至少需要的**独立簇**数（v5：361 簇 / 12 格 ≈ 30）。"""
    return _env_int("QD_CALIB_MIN_CLUSTERS", 30)


def tau_days() -> int:
    """系数有效期（天），季度重估。"""
    return _env_int("QD_CALIB_TAU_DAYS", 90)


def _logit(p: Any) -> Optional[float]:
    try:
        v = float(p)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    v = min(max(v, _EPS), 1.0 - _EPS)
    return math.log(v / (1.0 - v))


def fit_cell(condition_key: str, cell: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """拟合单格的 β₀。样本不足 ⇒ **b0=None**（不是给个估估值）。

    Args:
        cell: {"n_clusters": int, "win_rate": float, "mean_p_base": float}
    """
    out: Dict[str, Any] = {
        "condition_key": condition_key, "b0": None, "usable": False,
        "n_clusters": int((cell or {}).get("n_clusters") or 0),
        "source_level": "prior", "pooled_from": None, "reason": "",
        "calib_ver": CALIB_VER,
    }
    if not cell:
        out["reason"] = "该格无数据"
        return out
    n = out["n_clusters"]
    if n < min_clusters():
        out["reason"] = (f"独立簇不足：{n} < {min_clusters()} ⇒ 只出先验"
                         f"（给基于 {n} 簇的 β₀ 等于拿噪声当信号）")
        return out
    lw, lp = _logit(cell.get("win_rate")), _logit(cell.get("mean_p_base"))
    if lw is None or lp is None:
        out["reason"] = "胜率或 p_base 均值不可用"
        return out
    out.update(b0=round(lw - lp, 6), usable=True, source_level="cell",
               fitted_at=datetime.now().isoformat(timespec="seconds"))
    return out


def _parent_key(ck: str) -> Optional[str]:
    """条件键 → 父层键：`"up|high"` ⇒ `"up|*"`。单段键没有父层。"""
    parts = str(ck or "").split("|")
    if len(parts) < 2:
        return None
    return "|".join([parts[0]] + ["*"] * (len(parts) - 1))


def _aggregate(cells: Dict[str, Dict[str, Any]], keys: list) -> Dict[str, Any]:
    """把若干格**按簇数加权**聚合成一格（不是简单平均 —— 簇数不同权重不同）。"""
    tot_n = sum(int((cells.get(k) or {}).get("n_clusters") or 0) for k in keys)
    if tot_n <= 0:
        return {"n_clusters": 0, "win_rate": None, "mean_p_base": None}
    w = sum((int((cells.get(k) or {}).get("n_clusters") or 0)
             * float((cells.get(k) or {}).get("win_rate") or 0.0)) for k in keys)
    p = sum((int((cells.get(k) or {}).get("n_clusters") or 0)
             * float((cells.get(k) or {}).get("mean_p_base") or 0.0)) for k in keys)
    return {"n_clusters": tot_n, "win_rate": w / tot_n, "mean_p_base": p / tot_n}


def fit_with_pooling(condition_key: str,
                     cells: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """带层级池化的拟合：本格 → 父层 → 全局。

    ★ 每借一级都如实标注 `source_level` / `pooled_from` ——
      否则下游会把"全局先验"当成"该格专属修正"，是**静默放大**。
    """
    r = fit_cell(condition_key, cells.get(condition_key))
    # ★ `n_clusters` **始终是本格自己的**簇数；用于拟合的簇数另记 `pooled_n_clusters`。
    #   混在一起会让报告看不出"本格其实只有 3 簇"（把借来的当成自己的 = 静默放大）。
    own_n = r["n_clusters"]
    if r["usable"]:
        r["pooled_n_clusters"] = own_n
        return r

    pk = _parent_key(condition_key)
    if pk:
        sibs = [k for k in cells if _parent_key(k) == pk]
        if sibs:
            agg = _aggregate(cells, sibs)
            pr = fit_cell(pk, agg)
            if pr["usable"]:
                _pn = int(pr["n_clusters"])
                pr.update(source_level="parent", pooled_from=pk, sources=sibs,
                          condition_key=condition_key,
                          n_clusters=own_n, pooled_n_clusters=_pn,
                          reason=(f"本格仅 {own_n} 簇 < {min_clusters()}"
                                  f" ⇒ 池化到父层 {pk}（{_pn} 簇）"))
                return pr

    agg_all = _aggregate(cells, list(cells))
    gr = fit_cell("*", agg_all)
    _gn = int(gr["n_clusters"])
    gr.update(source_level="global" if gr["usable"] else "prior",
              pooled_from="*", sources=list(cells), condition_key=condition_key,
              n_clusters=own_n, pooled_n_clusters=_gn,
              reason=(f"本格与父层均不足 ⇒ 退到全局（{_gn} 簇）"
                      + ("" if gr["usable"] else "；全局仍不足 ⇒ 只出先验")))
    return gr


def is_fresh(coef: Optional[Dict[str, Any]]) -> bool:
    """系数是否在 τ 有效期内。缺 `fitted_at` 视为**不新鲜**（无法证明新鲜）。"""
    if not coef:
        return False
    ts = str(coef.get("fitted_at") or "").strip()
    if not ts:
        return False
    try:
        t = datetime.fromisoformat(ts[:19])
    except (ValueError, TypeError):
        return False
    return (datetime.now() - t) <= timedelta(days=tau_days())


def coef_usable(coef: Optional[Dict[str, Any]]) -> bool:
    """可用 = 有 b0 且未过期。**不删过期系数**（留着可审计）。"""
    return bool(coef) and coef.get("b0") is not None and is_fresh(coef)


def calibration_report(cells: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """出校准报告。**列出每一格**及其样本数（否则看不出哪些格是空的）。"""
    out: Dict[str, Any] = {
        "calib_ver": CALIB_VER,
        "min_clusters": min_clusters(),
        "tau_days": tau_days(),
        "cells": {},
        "n_cells": len(cells or {}),
        "n_usable": 0,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    for ck in sorted(cells or {}):
        r = fit_with_pooling(ck, cells)
        out["cells"][ck] = r
        if coef_usable(r):
            out["n_usable"] += 1
    if out["n_usable"] == 0:
        logger.info("[Calibration] 暂无任何条件格达到 %d 簇 ⇒ 全部只出先验",
                    min_clusters())
    return out
