"""chain/baseline_report.py — P1 只读基线报表（方案 §11 P1-3/4/6）。

纯只读层：只 SELECT，不写库、不触发评估、不跑实验。给三个基线视图：
- stratified_results：按 horizon / condition_key / skill / as_of 分层的预测结果；
  报告同时给名义样本与独立交易日簇，不只看总体平均（§6/§9.3）。
- exposure_funnel：曝光漏斗逐级计数（§5.4.3 状态枚举），分母为各级入口。
- connection_baseline：经验↔结果连接率（links / 曝光∩outcome），确认数据完整
  再估效果（P1-6）；当前 links=0 是正常的时间差，不是故障。

字段口径都取自 qd_agent_evaluation_outcomes / injection_events 现有列，不新增表。
DB 不可用时 fail-soft 返回 ok=False + reason，绝不伪造零值当真实基线。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in __import__("sys").path:
    __import__("sys").path.insert(0, ROOT)

from app.agent.chain.evaluation_store import _db, _need_db  # noqa: E402

_DIMS = ("horizon", "condition_key", "skill_id", "as_of")


def _rows(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    with _db() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        out = [dict(r) if not isinstance(r, dict) else r for r in cur.fetchall()]
        cur.close()
    return out


def stratified_results(dim: str = "horizon",
                       canonical_only: bool = True) -> Dict[str, Any]:
    """按单一维度分层出方向成功率、样本数、独立交易日簇、Brier 可用数。"""
    if dim not in _DIMS:
        return {"ok": False, "reason": "unsupported_dim", "dim": dim}
    if not _need_db():
        return {"ok": False, "reason": "disabled_or_schema_unavailable"}
    try:
        where = "WHERE canonical = TRUE" if canonical_only else ""
        sql = """
            SELECT %s AS value,
                   count(*) AS n,
                   count(DISTINCT as_of) AS date_clusters,
                   count(*) FILTER (
                     WHERE verdict IN ('hit','miss','partial')) AS scored,
                   count(*) FILTER (WHERE verdict = 'hit') AS hit,
                   count(*) FILTER (WHERE brier_score IS NOT NULL) AS with_brier
            FROM qd_agent_evaluation_outcomes
            %s
            GROUP BY 1 ORDER BY 1
        """ % (dim, where)
        rows = _rows(sql)
        for r in rows:
            scored = int(r.get("scored") or 0)
            r["direction_success_rate"] = (
                round(int(r.get("hit") or 0) / scored, 4) if scored else None)
            r["brier_coverage"] = (
                round(int(r.get("with_brier") or 0) / int(r.get("n") or 1), 4)
                if r.get("n") else None)
        return {"ok": True, "dim": dim, "rows": rows}
    except Exception as e:
        return {"ok": False, "reason": "%s: %s" % (type(e).__name__, e)}


def exposure_funnel() -> Dict[str, Any]:
    """曝光漏斗按 exposure_stage 枚举分组，另报 decision/bundle 完整度。"""
    if not _need_db():
        return {"ok": False, "reason": "disabled_or_schema_unavailable"}
    try:
        stages = _rows("""
            SELECT exposure_stage AS stage, count(*) AS n
            FROM qd_agent_experience_injection_events
            GROUP BY 1 ORDER BY 2 DESC
        """)
        totals = _rows("""
            SELECT count(*) AS events,
                   count(decision_id) AS with_decision,
                   count(bundle_sha) AS with_bundle,
                   count(*) FILTER (WHERE experiment_arm IS NOT NULL) AS assigned
            FROM qd_agent_experience_injection_events
        """)[0]
        return {"ok": True, "by_stage": stages,
                "events": totals.get("events"),
                "with_decision": totals.get("with_decision"),
                "with_bundle": totals.get("with_bundle"),
                "assigned": totals.get("assigned")}
    except Exception as e:
        return {"ok": False, "reason": "%s: %s" % (type(e).__name__, e)}


def connection_baseline() -> Dict[str, Any]:
    """经验↔结果连接率基线：链接数 / 曝光 decision∩outcome decision。"""
    if not _need_db():
        return {"ok": False, "reason": "disabled_or_schema_unavailable"}
    try:
        sql = """
            SELECT (SELECT count(*) FROM qd_agent_experience_outcome_links) AS links,
                   (SELECT count(*) FROM (
                      SELECT DISTINCT e.decision_id
                      FROM qd_agent_experience_injection_events e
                      JOIN qd_agent_evaluation_outcomes o
                        ON o.decision_id = e.decision_id
                   ) t) AS joinable_decisions,
                   (SELECT count(DISTINCT decision_id)
                      FROM qd_agent_experience_injection_events) AS exposure_decisions,
                   (SELECT count(DISTINCT decision_id)
                      FROM qd_agent_evaluation_outcomes) AS outcome_decisions
        """
        r = _rows(sql)[0]
        j = int(r.get("joinable_decisions") or 0)
        e = int(r.get("exposure_decisions") or 0)
        r["connection_rate"] = round(j / e, 4) if e else None
        r["data_complete"] = j > 0
        r["ok"] = True
        return r
    except Exception as e:
        return {"ok": False, "reason": "%s: %s" % (type(e).__name__, e)}


def full_baseline() -> Dict[str, Any]:
    """汇总三个视图；各子项独立 fail-soft，一项失败不影响其余。"""
    return {
        "stratified": {d: stratified_results(d) for d in _DIMS},
        "funnel": exposure_funnel(),
        "connection": connection_baseline(),
    }
