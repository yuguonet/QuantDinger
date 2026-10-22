# -*- coding: utf-8 -*-
"""evidence — EV-1 证据向量（v5 §C3P 第②段的产出契约，2026-10-13）

## EV-1 契约

    ev_ver / condition_key / subject / claims / discriminators / falsifiers
    / evidence_strength / provenance{prompt_sha, producer_ver, evidence_class,
                                     as_of, bar_freq}

## ★ 无人值守下的安全策略（改动前必读）

本模块**不改 `qd_agent_cases` 表结构**（加列是不可逆 DDL，用户不在场不做），
EV-1 先落 **JSONL 文件**（与 D1 情境帧同款：先落盘、后进统计）。
契约稳定且样本攒够后，再决定要不要入表 —— 那时 `append_evidence` 换成 INSERT 即可，
上层调用方不用改。

## ★ 不伪造原则（本模块最重要的纪律）

`discriminators` / `falsifiers` 需要 **LLM 证据生成**，而当前**没有**该接线。
⇒ 留 `[]` 是**正确行为**，并如实标 `evidence_class="weak"`。
**绝不能**填 `"待补充"` / `"无"` 之类占位符假装有了 —— 那会让下游把
「没有判别特征」误读成「判别特征说明此处不适用」，是**静默污染**，
且事后无法与"真的没有"区分。

## 条件空间封顶（v5 统计功效约束）

T+3 口径年独立簇 361 ⇒ `max_cells = floor(361/30) = 12` 格。
默认只用 `regime × vol_bucket` 两维（3×3=**9** 格 ≤ 12）；
加开 `theme_heat` 变 27 格 ⇒ **超预算**，`assert_cell_budget()` 必须如实报错。

## 红线

**R1**：LLM **自报概率**一律丢弃并记违规。
理由：自报概率与 `p_base` 口径不一致，混进条件校准会让 Δp 估计整个失真。
`_strip_llm_probability()` 是唯一的执行点。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..log import logger

from . import label_spec
from ._jsonl import read_jsonl

EV_VER = "ev1"
PRODUCER_VER = "ev1-producer"

# ── 条件键维度 ────────────────────────────────────────────────────────
# 各维度的**取值基数**（用于算条件空间大小，不是取值列表）
_DIM_CARD: Dict[str, int] = {"regime": 3, "vol_bucket": 3, "theme_heat": 3}
_DEFAULT_DIMS = ("regime", "vol_bucket")


def _dims() -> List[str]:
    raw = os.getenv("QD_EV_KEY_DIMS", "").strip()
    if not raw:
        return list(_DEFAULT_DIMS)
    out = [d.strip() for d in raw.split(",") if d.strip() in _DIM_CARD]
    return out or list(_DEFAULT_DIMS)


def max_cells() -> int:
    """条件空间上限（格）。默认 12 —— v5：T+3 口径年 361 独立簇 / 每格至少 30 簇。"""
    try:
        return int(os.getenv("QD_EV_MAX_CELLS", "").strip() or 12)
    except (TypeError, ValueError):
        return 12


def cell_count() -> int:
    """当前维度组合出的格数（含 `unknown` 兜底 +1 维？不含 —— 见下）。"""
    n = 1
    for d in _dims():
        n *= _DIM_CARD[d]
    return n


def assert_cell_budget() -> tuple:
    """(是否合规, 当前格数, 上限)。超预算**如实返回 False**，不静默、不崩。"""
    cnt, cap = cell_count(), max_cells()
    ok = cnt <= cap
    if not ok:
        logger.warning("[Evidence] 条件空间 %d 格超出预算 %d 格 ⇒ 每格样本不足，"
                       "条件校准不可信（请收窄 QD_EV_KEY_DIMS）", cnt, cap)
    return ok, cnt, cap


def condition_key(subject: Optional[str], frame: Optional[Dict[str, Any]]) -> str:
    """情境键。**确定性**（同输入必同输出），否则条件校准无从配对。

    取不到情境 ⇒ 标 `unknown`（**不猜** —— 猜出来的条件键比没有更糟：
    它会把样本分到错误的格子里，且事后无法识别）。
    """
    parts: List[str] = []
    for d in _dims():
        v = (frame or {}).get(d)
        parts.append(str(v) if v else "unknown")
    return "|".join(parts)


# ── 红线 R1：LLM 自报概率 ─────────────────────────────────────────────
_P_KEYS = ("p_up", "p_down", "p_up_pct", "probability", "prob", "confidence_llm",
           "llm_confidence", "p_llm", "win_prob", "胜率", "概率")


def _strip_llm_probability(extra: Optional[Dict[str, Any]]) -> tuple:
    """丢弃 LLM 自报概率（R1）。返回 (保留下来的字段, 违规记录列表)。"""
    if not extra:
        return {}, []
    keep: Dict[str, Any] = {}
    viol: List[str] = []
    for k, v in extra.items():
        kl = str(k).strip().lower()
        if kl in _P_KEYS or kl.endswith("_p") and kl != "p":
            viol.append(f"R1: 丢弃 LLM 自报概率字段 {k}")
            continue
        keep[k] = v
    if viol:
        logger.warning("[Evidence] R1 触发：%s", viol)
    return keep, viol


# ── 证据强度：随内容计算（**不是常量**）──────────────────────────────
def _evidence_strength(claims: Sequence[Any], discriminators: Sequence[Any],
                       falsifiers: Sequence[Any], has_frame: bool) -> float:
    s = 0.2
    if claims:
        s += 0.2
    if discriminators:
        s += 0.2
    if falsifiers:
        s += 0.3
    if has_frame:
        s += 0.1
    return round(min(s, 1.0), 3)


def _evidence_class(discriminators: Sequence[Any], falsifiers: Sequence[Any]) -> str:
    """有证伪条件才算 `strong` —— 可证伪性是证据质量的硬指标（v5 §证伪 gate）。"""
    if falsifiers:
        return "strong"
    if discriminators:
        return "medium"
    return "weak"


def _prompt_sha() -> str:
    """取当前 prompt 指纹（复用 `utils.tracing._repro_meta`，与留痕同一口径）。"""
    try:
        from ..utils.tracing import _repro_meta
        meta = _repro_meta()
        sha = meta.get("prompt_sha") or {}
        return json.dumps(sha, ensure_ascii=False, sort_keys=True)[:200]
    except Exception as e:
        logger.debug("[Evidence] prompt_sha 取不到: %s", e)
        return ""


def build_evidence(*, subject: Optional[str] = None,
                   as_of: Optional[str] = None,
                   claims: Optional[Sequence[Any]] = None,
                   discriminators: Optional[Sequence[Any]] = None,
                   falsifiers: Optional[Sequence[Any]] = None,
                   frame: Optional[Dict[str, Any]] = None,
                   extra: Optional[Dict[str, Any]] = None,
                   producer_ver: str = PRODUCER_VER) -> Dict[str, Any]:
    """构造一条 EV-1 证据向量（纯函数，便于单测）。"""
    _claims = list(claims or [])
    _disc = list(discriminators or [])
    _fals = list(falsifiers or [])
    keep, viol = _strip_llm_probability(extra)

    _as_of = str(as_of or (frame or {}).get("as_of")
                 or datetime.now().date().isoformat())[:10]
    return {
        "ev_ver": EV_VER,
        "condition_key": condition_key(subject, frame),
        "subject": str(subject or ""),
        "as_of": _as_of,
        "claims": _claims,
        "discriminators": _disc,          # ★ 没有就是 [] —— 不许填占位符
        "falsifiers": _fals,
        "evidence_strength": _evidence_strength(_claims, _disc, _fals, bool(frame)),
        "provenance": {
            "prompt_sha": _prompt_sha(),
            "producer_ver": producer_ver,
            "evidence_class": _evidence_class(_disc, _fals),
            "as_of": _as_of,
            "bar_freq": label_spec.bar_freq(),
        },
        "extra": keep,
        "violations": viol,
        "built_at": datetime.now().isoformat(timespec="seconds"),
    }


# ── 落盘（JSONL，先落盘后进统计）─────────────────────────────────────
def _ev_dir() -> str:
    return os.getenv("QD_EV_DIR") or str(
        Path(__file__).resolve().parents[3] / "data" / "evidence")


def evidence_path(as_of: str) -> Path:
    return Path(_ev_dir()) / f"evidence_{str(as_of)[:7]}.jsonl"


def append_evidence(ev: Dict[str, Any]) -> Path:
    """落盘一条证据。**同一 (as_of, subject) 覆盖**（幂等，补跑不重复计票）。"""
    key = (str(ev.get("as_of") or "")[:10], str(ev.get("subject") or ""))
    path = evidence_path(key[0])
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [r for r in read_jsonl(path)
            if (str(r.get("as_of") or "")[:10], str(r.get("subject") or "")) != key]
    rows.append(ev)
    rows.sort(key=lambda r: (str(r.get("as_of") or ""), str(r.get("subject") or "")))
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in rows)
        + ("\n" if rows else ""),
        encoding="utf-8")
    return path


def load_evidence(as_of: Optional[str] = None) -> List[Dict[str, Any]]:
    d = Path(_ev_dir())
    if not d.exists():
        return []
    if as_of:
        k = str(as_of)[:10]
        return [r for r in read_jsonl(evidence_path(k))
                if str(r.get("as_of") or "")[:10] == k]
    out: List[Dict[str, Any]] = []
    for p in sorted(d.glob("evidence_*.jsonl")):
        out.extend(read_jsonl(p))
    return out
