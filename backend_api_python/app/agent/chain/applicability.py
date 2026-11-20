"""chain/applicability.py — 情境匹配与不可计算候选处置（方案 §8.4②/①附）。

①附 needs_data：condition 引用越界 key 或运行时不可计算的候选，不当永久
candidate 僵尸——返回处置建议（reject / pending_whitelist + 到期 retired），
时限走 env，不新增第五种 evidence_status。

② applicability：把经验四段式的 condition 求值成 applicable/not_applicable/
unknown（复用 condition_eval），检索先按 condition 过滤、再按证据强度排序；
unknown 与"存储故障"分开、且**不得当作 applicable**，也不复用旧快照。

硬纪律：纯逻辑层，不碰 DB/LLM、不做实际检索、不接生产调用方；时间基准由
调用方显式传入（无时间词不猜当前时间）；所有阈值/时限走 env 或配置。
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import condition_eval as _ce

NEEDS_DATA = "needs_data"
REJECT = "reject"
PENDING_WHITELIST = "pending_whitelist"
RETIRE = "retire"
KEEP = "keep"

_STORAGE_ERROR = "storage_error"


def _pending_days() -> int:
    try:
        return max(1, int(os.getenv("QD_NEEDS_DATA_PENDING_DAYS", "30") or 30))
    except (TypeError, ValueError):
        return 30


def _as_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def needs_data_disposition(cond: Any, cfg: Dict[str, Any],
                           *, registered_at: Any = None,
                           now: Any = None) -> Dict[str, Any]:
    """不可计算候选的处置：可计算→keep；越界 key→待扩展或到期退役；其余→reject。"""
    try:
        stat = _ce.structural_check(cond, cfg)
    except Exception:
        return {"action": REJECT, "reason": "structural_error",
                "out_of_whitelist": 0, "invalid": 0,
                "pending_days": _pending_days(), "due_date": None}
    out_of = int(stat.get("out_of_whitelist", 0))
    invalid = int(stat.get("invalid", 0))
    result = {"action": KEEP, "reason": "", "out_of_whitelist": out_of,
              "invalid": invalid, "pending_days": _pending_days(),
              "due_date": None}

    if out_of > 0:
        start, today = _as_date(registered_at), _as_date(now)
        result["action"] = PENDING_WHITELIST
        result["reason"] = "key_not_in_whitelist"
        if start and today:
            due = start + timedelta(days=_pending_days())
            result["due_date"] = due.isoformat()
            if today >= due:
                result["action"] = RETIRE
                result["reason"] = "pending_expired"
        return result

    if invalid > 0 or stat.get("atoms", 0) == 0:
        result["action"] = REJECT
        result["reason"] = "invalid_or_no_atom"
    return result


def evaluate(cond: Any, ctx: Optional[Dict[str, Any]], cfg: Dict[str, Any],
             *, collector: Optional[Callable[[str], Optional[dict]]] = None
             ) -> Dict[str, Any]:
    """单条经验的 applicability 三值求值；存储故障单列，绝不吞成 unknown。"""
    if collector is not None:
        try:
            frame = collector("frame")
        except Exception:
            return {"verdict": _ce.UNKNOWN, "status": _STORAGE_ERROR,
                    "reason": "frame_collector_error"}
        if frame is None:
            return {"verdict": _ce.UNKNOWN, "status": _ce.UNKNOWN,
                    "reason": "frame_unavailable"}
        merged = dict(frame or {})
        for k in ("subject", "horizon", "prediction_target", "direction"):
            if ctx and k in ctx and k not in merged:
                merged[k] = ctx[k]
        ctx = merged
    return {"verdict": _ce.eval_condition(cond, ctx or {}, cfg)["verdict"],
            "status": "ok", "reason": ""}


def filter_and_rank(items: Sequence[Dict[str, Any]], ctx: Optional[Dict[str, Any]],
                    cfg: Dict[str, Any], *,
                    weight_of: Callable[[Dict[str, Any]], float]) -> Dict[str, Any]:
    """检索过滤：先按 condition 求值过滤（unknown 不进 applicable），再按证据强度排序。"""
    applicable: List[Dict[str, float]] = []
    counts = {_ce.APPLICABLE: 0, _ce.NOT_APPLICABLE: 0, _ce.UNKNOWN: 0,
              _STORAGE_ERROR: 0}
    for item in items:
        r = evaluate(item.get("condition"), ctx, cfg,
                     collector=item.get("collector"))
        status = r["status"]
        if status == _STORAGE_ERROR:
            counts[_STORAGE_ERROR] += 1
            continue
        verdict = r["verdict"]
        counts[verdict] = counts.get(verdict, 0) + 1
        if verdict == _ce.APPLICABLE:
            applicable.append({"item": item, "weight": float(weight_of(item))})
    applicable.sort(key=lambda e: (-e["weight"], id(e["item"])))
    return {"selected": [e["item"] for e in applicable], "counts": counts,
            "unknown_not_applied": counts[_ce.UNKNOWN] + counts[_STORAGE_ERROR]}
