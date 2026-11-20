"""chain/extraction_funnel.py — §6.3 经验提取与供给八级漏斗（纯函数）。

逐级计数：抽出→结构化→可算→去重→candidate→validated→检索命中→组合增益。
每级入口/通过/拒绝/拒绝原因分布，留存率分母为各级入口。可算级复用
condition_eval.structural_check 与 applicability.needs_data_disposition，越界 key 与
结构非法分开计数；白名单覆盖率单列（可计算候选占全部抽出候选比）。末级组合增益
是 §5.4.5/§9 实验量，仅标注为漏斗终点、留存率置 None，不作诊断比率。

不碰 DB/LLM、不写库、无生产调用方；validated 只认调用方传入的 evidence_status，
不由 LLM 自证引用产生。阈值/枚举走配置，docstring 为工具契约。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Sequence

from . import applicability as _app
from . import condition_eval as _ce

STAGES = ("extracted", "structured", "computable", "deduped",
          "candidate", "validated", "retrieval_hit", "portfolio_gain")
LABELS = {
    "extracted": "抽出", "structured": "结构化", "computable": "可算",
    "deduped": "去重", "candidate": "candidate", "validated": "validated",
    "retrieval_hit": "检索命中", "portfolio_gain": "组合增益",
}
_VALIDATED = "validated"
_CANDIDATE_STATES = ("candidate", "validated")


def _ratio(passed: int, entered: int) -> Optional[float]:
    return round(passed / entered, 4) if entered else None


def _dedup_key(item: Dict[str, Any]) -> Optional[str]:
    for k in ("content_sha", "experience_id"):
        v = item.get(k)
        if v:
            return "%s:%s" % (k, str(v))
    return None


def build_funnel(candidates: Sequence[Dict[str, Any]],
                 cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """按八级逐层过滤；返回逐级计数、留存率、拒绝原因与白名单覆盖率。"""
    if cfg is None:
        try:
            cfg = _ce.load_config()
        except (OSError, ValueError):
            return {"ok": False, "reason": "whitelist_config_unavailable"}
    if not isinstance(cfg, dict) or cfg.get("schema_version") != _ce.SCHEMA_VERSION \
            or not isinstance(cfg.get("keys"), dict) or not isinstance(cfg.get("ops"), list):
        return {"ok": False, "reason": "invalid_whitelist_config"}
    rows: List[Dict[str, Any]] = []
    pool: List[Dict[str, Any]] = [c for c in candidates if isinstance(c, dict)]

    def step(stage: str, predicate) -> None:
        nonlocal pool
        entered = len(pool)
        reasons: Dict[str, int] = {}
        nxt: List[Dict[str, Any]] = []
        for item in pool:
            ok, reason = predicate(item)
            if ok:
                nxt.append(item)
            else:
                reasons[reason] = reasons.get(reason, 0) + 1
        rows.append({"stage": stage, "label": LABELS[stage],
                     "entered": entered, "passed": len(nxt),
                     "rejected": entered - len(nxt),
                     "retention": _ratio(len(nxt), entered),
                     "reject_reasons": dict(sorted(reasons.items()))})
        pool = nxt

    step("extracted", lambda it: (True, "") if it.get("extracted") else (False, "not_extracted"))
    step("structured", lambda it: (True, "") if it.get("structured") else (False, "not_structured"))

    def computable(it: Dict[str, Any]):
        cond = it.get("condition")
        disp = _app.needs_data_disposition(
            cond, cfg, registered_at=it.get("registered_at"), now=it.get("now"))
        action = disp.get("action")
        if action == _app.KEEP:
            return True, ""
        return False, disp.get("reason") or action or "not_computable"

    step("computable", computable)

    seen: set = set()

    def dedup(it: Dict[str, Any]):
        key = _dedup_key(it)
        if key is None:
            return True, ""
        if key in seen:
            return False, "duplicate"
        seen.add(key)
        return True, ""

    step("deduped", dedup)
    step("candidate", lambda it: (True, "") if it.get("evidence_status") in _CANDIDATE_STATES
         else (False, str(it.get("evidence_status") or "missing_status")))
    step("validated", lambda it: (True, "") if it.get("evidence_status") == _VALIDATED
         else (False, "not_validated"))
    step("retrieval_hit", lambda it: (True, "") if it.get("retrieval_hit") else (False, "not_retrieved"))

    entered_gain = len(pool)
    with_gain = [it for it in pool if isinstance(it.get("portfolio_gain"), (int, float))
                 and not isinstance(it.get("portfolio_gain"), bool)]
    rows.append({"stage": "portfolio_gain", "label": LABELS["portfolio_gain"],
                 "entered": entered_gain, "passed": len(with_gain),
                 "rejected": entered_gain - len(with_gain),
                 "retention": None,
                 "reject_reasons": {"no_gain_value": entered_gain - len(with_gain)}
                 if entered_gain - len(with_gain) else {},
                 "note": "experimental_endpoint_not_diagnostic"})

    extracted = [c for c in candidates if isinstance(c, dict) and c.get("extracted")]
    computed_ok = sum(
        1 for c in extracted
        if (lambda s: s.get("atoms", 0) > 0 and s.get("out_of_whitelist", 0) == 0
            and s.get("invalid", 0) == 0)(_ce.structural_check(c.get("condition"), cfg)))
    out_of = sum(_ce.structural_check(c.get("condition"), cfg).get("out_of_whitelist", 0)
                 for c in extracted)
    comp_reasons = next((r["reject_reasons"] for r in rows if r["stage"] == "computable"), {})
    return {"ok": True, "stages": STAGES, "rows": rows,
            "whitelist_coverage": _ratio(computed_ok, len(extracted)),
            "whitelist_coverage_detail": {"computable": computed_ok,
                                          "extracted": len(extracted),
                                          "out_of_whitelist_atoms": out_of},
            "out_of_whitelist_rejects": comp_reasons.get("key_not_in_whitelist", 0),
            "needs_data_pending": comp_reasons.get("pending_whitelist", 0)}
