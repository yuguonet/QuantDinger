# -*- coding: utf-8 -*-
"""aggregator — 条件化聚合器（v5 P2，2026-10-13）

## 公式

    logit(p_final) = logit(p_base) + β₀(condition_key) + Σ βⱼ(condition_key)·eⱼ
    约束：|Δlogit| ≤ LOGIT_DELTA_MAX（默认 0.4，env `QD_AGG_LOGIT_MAX` 可配）
    失败：**fail-open**（退回 `p_base`）

## ★ 当前状态：骨架，不是成品（改本文件前必读）

`β` 需要**配对样本**才能拟合，而现在**一条配对都还没有**（采集刚启动）。
⇒ **无系数时 `p_final == p_base`（Δ=0）是正确行为**。
凭空编系数才是灾难：那是在拿噪声当信号，且事后与真系数无法区分。

`p_base` 由**调用方传入**（冻结基线模型的输出）。本模块**不负责计算 p_base** ——
职责单一，也避免把反馈信号混进基线（见红线 R2）。

## 两条红线

**R1**：LLM **自报概率**一律丢弃并记违规（`chain/evidence.py` 已拦一道，
这里在**聚合侧再拦一道** —— 纵深防御，因为聚合是概率的最后一道关口）。

**R2**：`p_base` 路径**禁止**接入 `qd_agent_weights` / `exp_*` 反馈信号。
理由：若"基线"里混进了它自己的历史战绩，Δp 估计会**自我强化**
（越涨越信、越跌越怂），条件校准得到的将是"回声"而不是"增量"。
⇒ 本模块源码里**不得出现**这些表名（由 `test_r2_aggregator_never_reads_feedback_tables`
扫描守护，注释里说明禁令是允许的）。

## 易错处

· `logit(0)` / `logit(1)` 会炸（除零 / log(0)）⇒ 夹到 `(eps, 1-eps)`。
· 裁剪**必须被记录**（`clipped=True`）—— 静默裁剪会让校准报告误判"模型很保守"。
· fail-open 指**退回 p_base**，不是退回 0.5，也不是随便一个数。
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..log import logger

AGG_VER = "agg1"

_EPS = 1e-6


def logit_delta_max() -> float:
    """Δlogit 上限。默认 0.4（p=0.5 附近约 ±10pp）—— 单条证据不许把概率推飞。"""
    try:
        v = float(os.getenv("QD_AGG_LOGIT_MAX", "").strip() or 0.4)
    except (TypeError, ValueError):
        v = 0.4
    return v if v > 0 else 0.4


def logit(p: Any) -> float:
    """概率 → logit。**边界夹紧**（p=0/1 会除零或 log(0)）。"""
    try:
        v = float(p)
    except (TypeError, ValueError):
        raise ValueError(f"p 不是数字: {p!r}")
    if not math.isfinite(v):
        raise ValueError(f"p 非有限值: {p!r}")
    v = min(max(v, _EPS), 1.0 - _EPS)
    return math.log(v / (1.0 - v))


def sigmoid(x: float) -> float:
    try:
        if x >= 0:
            return 1.0 / (1.0 + math.exp(-x))
        e = math.exp(x)
        return e / (1.0 + e)
    except (OverflowError, TypeError, ValueError):
        return 1.0 if (isinstance(x, (int, float)) and x > 0) else 0.0


# ── 红线 R1：聚合侧再拦一道 ─────────────────────────────────────────
_P_KEYS = ("p_up", "p_down", "p_up_pct", "probability", "prob", "confidence_llm",
           "llm_confidence", "p_llm", "win_prob", "胜率", "概率")


def _scan_r1(evidence: Optional[Dict[str, Any]]) -> List[str]:
    viol: List[str] = []
    for k in (evidence or {}):
        if str(k).strip().lower() in _P_KEYS:
            viol.append(f"R1: 聚合侧拒绝 LLM 自报概率字段 {k}")
    if viol:
        logger.warning("[Aggregator] R1 触发：%s", viol)
    return viol


def aggregate(p_base: Any, evidence: Optional[Dict[str, Any]] = None,
              condition_key: Optional[str] = None,
              coefs: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """条件化聚合：p_base + 证据 → p_final。

    Returns:
        {"p_base","p_final","delta_logit","clipped","used_coefs",
         "fail_open","reason","condition_key","violations","agg_ver"}
        ★ `fail_open=True` 时 `p_final == p_base`（不是 0.5，不是随便一个数）。
    """
    out: Dict[str, Any] = {
        "p_base": None, "p_final": None, "delta_logit": 0.0, "clipped": False,
        "used_coefs": bool(coefs), "fail_open": False, "reason": "",
        "condition_key": condition_key or "", "violations": [], "agg_ver": AGG_VER,
    }
    viol = _scan_r1(evidence)
    out["violations"] = viol

    # ── p_base 校验（非法 ⇒ fail-open）
    try:
        pb = float(p_base)
        if not math.isfinite(pb) or pb < 0.0 or pb > 1.0:
            raise ValueError(f"p_base 越界: {p_base!r}")
    except (TypeError, ValueError) as e:
        out.update(p_final=None, fail_open=True, reason=f"p_base 不可用：{e}")
        logger.warning("[Aggregator] fail-open（p_base=%r）: %s", p_base, e)
        return out
    out["p_base"] = pb

    try:
        delta = 0.0
        if coefs:
            # ★ 系数**必须**是数字：非数字 ⇒ **报错走 fail-open**，不是静默当 0。
            #   静默当 0 的危险在于「β 真的拟合成 0」与「β 配置损坏」表现完全一样，
            #   事后无从区分 —— 这正是本项目反复踩的"静默漂移"。
            delta += _coef(coefs, "b0")
            ev = evidence or {}
            # 判别特征按**条数**计入（当前无向量化证据分，先给可解释的一阶项）
            disc = [d for d in (ev.get("discriminators") or []) if str(d).strip()]
            fals = [f for f in (ev.get("falsifiers") or []) if str(f).strip()]
            if disc:
                delta += _coef(coefs, "discriminators") * len(disc)
            if fals:
                # 证伪条件出现 ⇒ 证据**减弱**（v5 证伪 gate：可证伪性降低确定度）
                delta += _coef(coefs, "falsifiers") * len(fals)
            # 证据强度是**证据内容**（不是配置），宽松处理：取不到当 0
            delta += _coef(coefs, "evidence_strength") * _as_float(
                ev.get("evidence_strength"), default=0.0)

        cap = logit_delta_max()
        if abs(delta) > cap:
            delta = math.copysign(cap, delta)
            out["clipped"] = True
        out["delta_logit"] = round(delta, 6)
        out["p_final"] = round(sigmoid(logit(pb) + delta), 6)
    except Exception as e:
        out.update(p_final=pb, fail_open=True, reason=f"聚合异常：{e}")
        logger.warning("[Aggregator] fail-open（退回 p_base）: %s", e)
    return out


def _coef(coefs: Dict[str, Any], key: str) -> float:
    """取系数（**严格**）：键不存在 ⇒ 0.0；值非数字 ⇒ **抛错**（走 fail-open）。

    ★ 键不存在 = 该项不启用，当 0 合理；
      值非数字 = **配置损坏**，必须暴露 —— 否则与"真的拟合成 0"无法区分。
    """
    if key not in coefs or coefs[key] is None:
        return 0.0
    v = coefs[key]
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"系数 {key} 非数字: {v!r}") from None
    if not math.isfinite(f):
        raise ValueError(f"系数 {key} 非有限值: {v!r}")
    return f


def _as_float(v: Any, default: float = 0.0) -> float:
    """宽松转 float。**非数字一律 default**（不抛、不兜底成"看起来合理"的数）。"""
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


# ═══════════════════════════════════════════════════════════════
#  配对双跑留痕（Δp estimand 的原料）
# ═══════════════════════════════════════════════════════════════
def _pair_dir() -> str:
    return os.getenv("QD_PAIR_DIR") or str(
        Path(__file__).resolve().parents[3] / "data" / "pairs")


def pair_path(as_of: str) -> Path:
    return Path(_pair_dir()) / f"pairs_{str(as_of)[:7]}.jsonl"


def _read(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            out.append(json.loads(s))
        except (ValueError, TypeError):
            continue
    return out


def record_pair(claim_id: Any, p_base: Any, p_final: Any,
                condition_key: Optional[str] = None,
                as_of: Optional[str] = None,
                extra: Optional[Dict[str, Any]] = None) -> Path:
    """记一条配对（p_base vs p_final）。

    ★ 为什么必须同时记两个：Δp 的 estimand 是 `sign(Δp)` 与 `sign(y)` 的一致性。
      只记 p_final 就无从判断"条件化到底有没有带来增量"。
    ★ 同 (as_of, claim_id) **覆盖**（补跑不重复计票，与 G24 同款纪律）。
    """
    _as_of = str(as_of or datetime.now().date().isoformat())[:10]
    rec = {
        "as_of": _as_of,
        "claim_id": claim_id,
        "p_base": _as_float(p_base, default=None) if p_base is not None else None,
        "p_final": _as_float(p_final, default=None) if p_final is not None else None,
        "delta_p": (round(_as_float(p_final) - _as_float(p_base), 6)
                    if p_base is not None and p_final is not None else None),
        "condition_key": condition_key or "",
        "agg_ver": AGG_VER,
        "extra": dict(extra or {}),
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
    }
    key = (_as_of, str(claim_id))
    path = pair_path(_as_of)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [r for r in _read(path)
            if (str(r.get("as_of") or "")[:10], str(r.get("claim_id"))) != key]
    rows.append(rec)
    rows.sort(key=lambda r: (str(r.get("as_of") or ""), str(r.get("claim_id"))))
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in rows)
        + ("\n" if rows else ""),
        encoding="utf-8")
    return path


def load_pairs(as_of: Optional[str] = None) -> List[Dict[str, Any]]:
    d = Path(_pair_dir())
    if not d.exists():
        return []
    if as_of:
        k = str(as_of)[:10]
        return [r for r in _read(pair_path(k)) if str(r.get("as_of") or "")[:10] == k]
    out: List[Dict[str, Any]] = []
    for p in sorted(d.glob("pairs_*.jsonl")):
        out.extend(_read(p))
    return out
