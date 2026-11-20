"""chain/promotion_runner.py — 升档驱动：经验档位自动判定的证据计算与周期执行。

【职责（§8.2/§9.3 自动生效链的执行入口）】
盘后 EvalWorker 在 outcome/link/stats 落库后调用 run_promotion_check：
按经验从 links+outcomes 计算 experience_injection 实验的证据
（注入臂 vs 未注入对照臂的方向成功率差 + 95% CI + 独立决策簇数），
再交 consumption_policy.apply_decision → experiment_config.decide_mode 判定。
判定与写档全部在既有链内（fail-closed），本模块只算证据、不直接改档。

【对照臂定义（experience_injection）】
注入臂 = 该经验被注入（links 关联）的成熟方向 outcome，按 decision 聚成簇；
对照臂 = 同时间窗内、未注入该经验的 decision 的成熟方向 outcome。
"未注入该经验"以当时落库的曝光事件 exposure_items 为准（PIT 安全）：
其 shadow 项不含该 experience_id 即视为对照；按日轮转在同窗内天然产生对照。
时间窗取注入臂 decision 的 created_at 范围，削减时期偏差。
簇内成功率 = 簇内 (hit=1, miss=0) 均值；臂均值差 ±1.96×Wald SE 为 95% CI。

【look 纪律】looks = look_milestones 中 ≤ n_clusters 的里程碑数（alpha-spending
风格），不是每日运行次数——盘后每天跑一次检查不会吃掉 max_looks 配额。

【边界】feedback_iteration 实验的对照臂构成未定稿，本模块不计算不驱动；
证据不足（簇数 < n_min）时不调 apply_decision、不留痕，避免台账日刷。
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_EXPERIMENT = "experience_injection"
_Z95 = 1.96


def _rates_by_decision(conn, where: str, params: tuple) -> List[float]:
    cur = conn.cursor()
    cur.execute("""
        SELECT o.decision_id,
               avg(CASE WHEN o.verdict = 'hit' THEN 1.0
                        WHEN o.verdict = 'miss' THEN 0.0 END) AS rate
          FROM qd_agent_evaluation_outcomes o
         WHERE o.verdict IN ('hit', 'miss') AND o.decision_id IS NOT NULL
           AND """ + where + """
         GROUP BY o.decision_id
    """, params)
    rows = cur.fetchall()
    cur.close()
    out = []
    for r in rows:
        v = (r.get("rate") if isinstance(r, dict) else r[1])
        if v is not None:
            out.append(float(v))
    return out


def _mean_var(xs: List[float]) -> (float, float):
    n = len(xs)
    if not n:
        return None, None
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / max(1, n - 1)
    return m, var


def _ci_diff(inj: List[float], ctl: List[float]) -> Optional[Dict[str, Any]]:
    n_a, n_b = len(inj), len(ctl)
    if n_a < 2 or n_b < 2:
        return None
    m_a, v_a = _mean_var(inj)
    m_b, v_b = _mean_var(ctl)
    se = math.sqrt((v_a or 0.0) / n_a + (v_b or 0.0) / n_b)
    diff = m_a - m_b
    return {"diff": round(diff, 6), "ci_low": round(diff - _Z95 * se, 6),
            "ci_high": round(diff + _Z95 * se, 6),
            "n_clusters": min(n_a, n_b), "n_injected": n_a, "n_control": n_b}


def _control_window(conn) -> Optional[Dict[str, Any]]:
    cur = conn.cursor()
    cur.execute("""
        SELECT min(d.created_at) AS lo, max(d.created_at) AS hi
          FROM qd_agent_experience_injection_events e
          JOIN qd_agent_decisions d ON d.id = e.decision_id
         WHERE e.decision_id IS NOT NULL
    """)
    r = cur.fetchone()
    cur.close()
    d = dict(r) if isinstance(r, dict) else {"lo": r[0], "hi": r[1]}
    if not d.get("lo") or not d.get("hi"):
        return None
    return d


def evidence_for_experience(eid: str) -> Optional[Dict[str, Any]]:
    """单条经验的注入臂 vs 对照臂证据；任一步异常返回 None（fail-closed）。"""
    try:
        from .evaluation_store import _db
        with _db() as conn:
            win = _control_window(conn)
            if not win:
                return None
            inj = _rates_by_decision(conn, """
                o.decision_id IN (
                    SELECT o2.decision_id FROM (
                        SELECT outcome_id FROM qd_agent_experience_outcome_links
                         WHERE experience_id = %s) lk
                    JOIN qd_agent_evaluation_outcomes o2
                      ON o2.outcome_id = lk.outcome_id
                    WHERE o2.verdict IN ('hit', 'miss')
                )""", (eid,))
            ctl = _rates_by_decision(conn, """
                o.decision_id IN (
                    SELECT id FROM qd_agent_decisions
                     WHERE created_at BETWEEN %s AND %s)
                AND o.decision_id NOT IN (
                    SELECT e.decision_id
                      FROM qd_agent_experience_injection_events e,
                           jsonb_array_elements(
                               COALESCE(e.exposure_items, '[]'::jsonb)) AS it
                     WHERE e.decision_id IS NOT NULL
                       AND it->>'kind' = 'experience_shadow'
                       AND it->>'experience_id' = %s)
            """, (win["lo"], win["hi"], eid))
        return _ci_diff(inj, ctl)
    except Exception as e:
        logger.debug("[Promotion] 证据计算失败（fail-closed）: %s: %s",
                     type(e).__name__, e)
        return None


def _looks_for(cfg: Dict[str, Any], experiment: str,
               n_clusters: int) -> int:
    milestones = (cfg.get("experiments", {}).get(experiment, {})
                  .get("look_milestones") or [])
    return sum(1 for m in milestones
               if isinstance(m, (int, float)) and n_clusters >= m)


def run_promotion_check(experiment: str = _EXPERIMENT,
                        actor: str = "eval_worker") -> Dict[str, Any]:
    """盘后入口：对簇数达 n_min 的经验逐一判定档位；返回摘要（fail-soft）。"""
    out: Dict[str, Any] = {"experiment": experiment, "checked": 0,
                           "below_n_min": 0, "skipped": 0, "results": []}
    try:
        from . import consumption_policy as _cp
        from . import experiment_config as _cfg
        from .evaluation_store import _db
        if not _cp.enabled():
            out["reason"] = "consumption_policy_disabled"
            return out
        cfg = _cfg.load_config()
        thr = (cfg.get("experiments", {}).get(experiment, {})
               .get("thresholds") or {})
        n_min = thr.get("n_min")
        endpoint = (cfg.get("experiments", {}).get(experiment, {})
                    .get("primary_endpoint"))
        if not isinstance(n_min, int) or endpoint not in _cfg.ENDPOINTS:
            out["reason"] = "not_preregistered"
            return out
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT DISTINCT experience_id
                  FROM qd_agent_experience_outcome_links
                 WHERE experience_id <> ''
            """)
            eids = [r.get("experience_id") if isinstance(r, dict) else r[0]
                    for r in cur.fetchall()]
            cur.close()
        for eid in eids:
            ev = evidence_for_experience(eid)
            if not ev:
                out["skipped"] += 1
                continue
            out["checked"] += 1
            if ev["n_clusters"] < n_min:
                out["below_n_min"] += 1
                continue
            looks = _looks_for(cfg, experiment, ev["n_clusters"])
            r = _cp.apply_decision(
                experiment, experience_ids=[eid], endpoint=endpoint,
                ci_low=ev["ci_low"], ci_high=ev["ci_high"],
                n_clusters=ev["n_clusters"], looks=looks, actor=actor)
            out["results"].append({"experience_id": eid, **ev,
                                   "verdict": r.get("verdict"),
                                   "reason": r.get("reason"),
                                   "to_mode": r.get("to_mode"),
                                   "applied": r.get("applied")})
        return out
    except Exception as e:
        out["reason"] = "runner_error:%s" % type(e).__name__
        logger.warning("[Promotion] 升档检查跳过（fail-soft）: %s: %s",
                       type(e).__name__, e)
        return out
