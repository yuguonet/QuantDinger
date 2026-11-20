"""chain/experience_effect_report.py — 方案 §5.4.6 逐经验效果报告（只读）。

把已有机制层连接成一份可审计的经验效果视图：覆盖漏斗、预注册状态、功效审计、
档位决策留痕与逐经验维度。只 SELECT，不写库、不触发评估、不升档、不碰 skill。

硬纪律：无受控对照时不报净超额收益与风险差；无标定概率时不报 Brier 差；
输出端引用只做逐字短语命中的弱信号，明确 causal=False / drives_promotion=False；
数据不足一律「证据不足」，绝不称「有效」或「有害」。阈值走 env，DB 异常
fail-soft 返回 ok=False + reason。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Sequence

from . import baseline_report, consumption_policy, evaluation_store, replay

OUTPUT_REFERENCE_SEMANTICS = "weak_signal_verbatim_phrase"
INSUFFICIENT = "证据不足"


def _min_phrase() -> int:
    try:
        return max(4, int(os.getenv("QD_OUTPUT_REF_MIN_PHRASE", "12") or 12))
    except (TypeError, ValueError):
        return 12


def _rows(sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
    with evaluation_store._db() as conn:
        cur = conn.cursor()
        cur.execute(sql, tuple(params))
        out = [dict(r) if not isinstance(r, dict) else r for r in cur.fetchall()]
        cur.close()
    return out


def _j(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _phrases(excerpt: Any, minimum: int) -> List[str]:
    text = str(excerpt or "")
    out: List[str] = []
    for piece in re.split(r"[\n\r；;，,。|、]+", text):
        phrase = piece.strip(" \t-*_#`>\"'[]{}()")
        if len(phrase) >= minimum and not phrase.isdigit():
            out.append(phrase)
    return out


def output_reference_ratio(events: Sequence[Dict[str, Any]],
                            answers: Dict[int, str],
                            contents: Dict[str, Dict[str, Any]],
                            experience_id: str) -> Dict[str, Any]:
    """逐字短语命中率；只作弱信号，不参与任何升档或因果判定。"""
    minimum = _min_phrase()
    phrases: List[str] = []
    for piece in _phrases((contents.get(experience_id) or {}).get("excerpt"),
                          minimum):
        if piece not in phrases:
            phrases.append(piece)
    matched, denom = 0, 0
    for event in events:
        ids = _j(event.get("experience_ids")) or []
        if experience_id not in [str(x) for x in ids]:
            continue
        answer = answers.get(int(event.get("decision_id") or 0))
        if not answer:
            continue
        denom += 1
        if any(p in answer for p in phrases):
            matched += 1
    return {"matched": matched, "denominator": denom,
            "ratio": round(matched / denom, 4) if denom and phrases else None,
            "semantics": OUTPUT_REFERENCE_SEMANTICS, "causal": False,
            "drives_promotion": False,
            "reason": "" if denom and phrases else "no_answer_or_no_phrase"}


def _ledger() -> Dict[str, Any]:
    try:
        rows = _rows("""
            SELECT decision_sha, experiment, experience_id, from_mode, to_mode,
                   verdict, reason, evidence, config_sha, actor, created_at
              FROM qd_agent_consumption_decisions
             ORDER BY created_at DESC LIMIT 20
        """)
        return {"ok": True, "rows": rows}
    except Exception as e:
        return {"ok": False, "rows": [],
                "reason": "%s: %s" % (type(e).__name__, e)}


def _registration() -> Dict[str, Any]:
    from . import experiment_config as _cfg
    cfg = _cfg.load_config()
    return {k: _cfg.registration_status(cfg, k)
            for k in _cfg.EXPERIMENT_KEYS}


def _rollout(request_id: str) -> Dict[str, Any]:
    from . import rollout_policy as _rp
    try:
        return _rp.effective_mode("observe", version="v1",
                                  request_id=request_id, domain="finance",
                                  cfg=_rp.load_config())
    except Exception as e:
        return {"ok": False, "consumption_mode": "observe",
                "reason": "%s: %s" % (type(e).__name__, e)}


def _power() -> Dict[str, Any]:
    from . import power_analysis as _pa
    out: Dict[str, Any] = {}
    for experiment in ("feedback_iteration", "experience_injection"):
        try:
            out[experiment] = _pa.audit_from_config(experiment)
        except Exception as e:
            out[experiment] = {"ok": False, "verdict": "undecidable",
                               "reason": "%s: %s" % (type(e).__name__, e)}
    return out


def report(experience_id: Optional[str] = None, *,
           limit: Optional[int] = None) -> Dict[str, Any]:
    """按方案 §5.4.6 输出逐经验效果报告；数据不足保持显式空值与原因。"""
    if not evaluation_store._need_db():
        return {"ok": False, "reason": "disabled_or_schema_unavailable"}
    try:
        where = "WHERE experience_id = %s" if experience_id else ""
        params: List[Any] = [experience_id] if experience_id else []
        sql = """
            SELECT experience_id, experience_version, source_kind, condition_key,
                   normalized_content, evidence_status, consumption_mode,
                   registered_at, source_available_at
              FROM qd_agent_experiences
        """ + where + " ORDER BY registered_at DESC"
        if limit is not None:
            sql += " LIMIT %s"
            params.append(int(limit))
        experiences = _rows(sql, params)
        if not experiences:
            return {"ok": True, "experiences": [], "reason": "no_experience"}

        ids = [str(r["experience_id"]) for r in experiences]
        events = _rows("""
            SELECT event_id, decision_id, experience_ids, exposure_items,
                   token_budget, created_at
              FROM qd_agent_experience_injection_events
             ORDER BY created_at DESC
        """)
        decisions = sorted({int(e["decision_id"]) for e in events
                            if e.get("decision_id")})
        answers: Dict[int, str] = {}
        if decisions:
            rows = _rows("""
                SELECT id, answer FROM qd_agent_decisions WHERE id = ANY(%s)
            """, (decisions,))
            answers = {int(r["id"]): str(r.get("answer") or "")
                       for r in rows if r.get("answer")}
        links = _rows("""
            SELECT l.event_id, l.experience_id, l.eligible_for_attribution,
                   l.exclusion_reason, o.verdict, o.brier_score, o.as_of,
                   o.subject_kind, o.subject
              FROM qd_agent_experience_outcome_links l
              LEFT JOIN qd_agent_evaluation_outcomes o
                ON o.outcome_id = l.outcome_id
             WHERE l.experience_id = ANY(%s)
        """, (ids,))
        stats = _rows("""
            SELECT experience_id, confidence_interval, effective_weight,
                   prediction_success_rate, last_matured_at
              FROM qd_agent_experience_stats WHERE experience_id = ANY(%s)
        """, (ids,))
        stat_by_id = {str(r["experience_id"]): r for r in stats}

        contents = {str(r["experience_id"]): _j(r.get("normalized_content")) or {}
                    for r in experiences}
        result: List[Dict[str, Any]] = []
        for exp in experiences:
            eid = str(exp["experience_id"])
            own_events = [e for e in events
                          if eid in [str(x) for x in (_j(e.get("experience_ids")) or [])]]
            injected = 0
            dropped = 0
            for event in own_events:
                listed = [str(x) for x in (_j(event.get("experience_ids")) or [])]
                if listed and listed[0] != eid:
                    dropped += 1
                for item in (_j(event.get("exposure_items")) or []):
                    if (str(item.get("experience_id") or "") == eid
                            and item.get("injected") is True):
                        injected += 1
            answered = [e for e in own_events if e.get("decision_id")
                        and answers.get(int(e["decision_id"]))]
            own_links = [l for l in links if str(l.get("experience_id") or "") == eid]
            mature = [l for l in own_links
                      if l.get("eligible_for_attribution")
                      and str(l.get("verdict") or "") in ("hit", "miss", "partial")]
            excluded = [str(l.get("exclusion_reason"))
                        for l in own_links
                        if l.get("eligible_for_attribution") is False
                        and l.get("exclusion_reason")]
            hits = sum(1 for l in mature if l.get("verdict") == "hit")
            misses = sum(1 for l in mature if l.get("verdict") == "miss")
            with_brier = [l for l in mature if l.get("brier_score") is not None]
            st = stat_by_id.get(eid, {})
            pit_reasons: Dict[str, int] = {}
            pit_ok_ids = set()
            for event in own_events:
                ok, reason = replay.pit_available(exp, event.get("created_at"))
                if ok:
                    pit_ok_ids.add(str(event.get("event_id") or ""))
                else:
                    pit_reasons[reason] = pit_reasons.get(reason, 0) + 1
            qualified = [e for e in answered
                         if str(e.get("event_id") or "") in pit_ok_ids]
            result.append({
                "experience_id": eid,
                "version": str(exp.get("experience_version") or ""),
                "source_kind": str(exp.get("source_kind") or ""),
                "evidence_status": str(exp.get("evidence_status") or ""),
                "consumption_mode": str(exp.get("consumption_mode") or ""),
                "condition_key": str(exp.get("condition_key") or ""),
                "exposures": len(own_events),
                "actual_injections": injected,
                "answer_available_exposures": len(answered),
                "qualified_exposures": len(qualified),
                "matured_attributable": len(mature),
                "excluded_reasons": sorted(set(excluded)),
                "output_reference_ratio": output_reference_ratio(
                    own_events, answers, contents, eid),
                "net_excess_return_delta": None,
                "net_excess_return_reason": "no_controlled_comparison_arm",
                "risk_delta": None,
                "risk_delta_reason": "no_controlled_comparison_arm",
                "brier_delta": None,
                "brier_delta_reason": (
                    "no_calibrated_probability" if not with_brier
                    else "no_controlled_comparison_arm"),
                "direction_success_rate": (
                    round(hits / (hits + misses), 4) if hits + misses else None),
                "confidence_interval": st.get("confidence_interval"),
                "date_clusters": len({str(l.get("as_of") or "")
                                      for l in mature if l.get("as_of")}),
                "stock_clusters": len({str(l.get("subject") or "")
                                       for l in mature if l.get("subject")}),
                "token_delta": None,
                "token_delta_reason": "no_baseline_arm",
                "latency_delta": None,
                "latency_delta_reason": "no_baseline_arm",
                "pit_violations": sum(pit_reasons.values()),
                "pit_reasons": pit_reasons,
                "attribution_dropped_exposures": dropped,
                "attribution_policy": "first_experience_id_only",
                "effective_weight": st.get("effective_weight"),
                "verdict_label": INSUFFICIENT,
            })

        return {"ok": True, "experiences": result,
                "coverage": evaluation_store.coverage_funnel(),
                "funnel": baseline_report.exposure_funnel(),
                "registration": _registration(),
                "power": _power(),
                "ledger": _ledger(),
                "consumption_policy_enabled": consumption_policy.enabled(),
                "rollout": _rollout(
                    str(events[0].get("event_id") or "") if events else ""),
                "attribution_note": (
                    "production link 只取 experience_ids[0]；"
                    "attribution_dropped_exposures 统计因此未归因的曝光数")}
    except Exception as e:
        return {"ok": False, "reason": "%s: %s" % (type(e).__name__, e)}
