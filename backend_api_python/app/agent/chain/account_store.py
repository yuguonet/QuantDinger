# -*- coding: utf-8 -*-
"""AccountStore — 追责系统 v1.1 的持久化层（qd_agent_decisions / claims / resolutions）。

与 `chain/store.py`（旧 qd_agent_traces 树）**分开成文件**的原因：
  ① 两张体系是**不同代际**——旧表按"个股交易决策"设计，新表按"领域 + Claim"设计，
     混在一个文件里会让"哪套在服役"变得模糊；
  ② 旧表在 S1~S3 期间一直在写，新表是纯增量，分开才能做到"删新表即可回滚"。

【铁律】
  · 游标行是 **dict**（`app.utils.db` 固定 RealDictCursor）⇒ 必须 `row['k']`，
    `row[0]` 直接 KeyError。
  · 简单 INSERT 会被底层自动追加 `RETURNING id` ⇒ 一律自带 `RETURNING <实存列>`。
  · `ON CONFLICT DO UPDATE` 里引用已存在行必须用**真表名**。
  · 全链路 fail-open：任何异常都**不许**打断主对话（追责是旁路，不是主链）。
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any, Dict, List, Optional

from log import logger


def _conn():
    from app.utils.db import get_db_connection
    return get_db_connection()


# ═══════════════════════════════════════════════════════════════
#  域策略：库里配置优先，库不可达用内置默认（fail-open 行为一致）
# ═══════════════════════════════════════════════════════════════
def load_domain_policy() -> Dict[str, Dict[str, Any]]:
    """读 `qd_domain_resolvers` ⇒ {domain: {enabled, horizon_default, judge_enabled}}。

    读不到（表未建 / DB 不通）返回 `claims.DOMAIN_POLICY` 内置默认，
    保证"库没就绪时行为与配置一致"，不会退化成"全都入库"。
    """
    from chain.claims import DOMAIN_POLICY
    try:
        with _conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT domain, horizon_default, judge_enabled, enabled, judge_model "
                "FROM qd_domain_resolvers")
            rows = cur.fetchall() or []
        if not rows:
            return dict(DOMAIN_POLICY)
        out: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            out[r["domain"]] = {
                "enabled": bool(r.get("enabled")),
                "horizon_default": r.get("horizon_default") or "T+3",
                "judge_enabled": bool(r.get("judge_enabled")),
                "judge_model": r.get("judge_model") or "",
            }
        # 库里没有的域沿用内置默认（新增域只改库，不改代码）
        for dom, cfg in DOMAIN_POLICY.items():
            out.setdefault(dom, dict(cfg))
        return out
    except Exception as e:
        logger.debug("[AccountStore] 读域策略失败，用内置默认: %s", e)
        return dict(DOMAIN_POLICY)


# ═══════════════════════════════════════════════════════════════
#  写入
# ═══════════════════════════════════════════════════════════════
def save_decision(*, session_id: str = "", trace_root_id: Optional[int] = None,
                  run_id: str = "", domain: str = "", intent: str = "",
                  user_query: str = "", answer: str = "", model: str = "",
                  total_tokens: int = 0, latency_ms: int = 0,
                  gate_ver: str = "", gate_reason: str = "") -> Optional[int]:
    """写决策头，返回 decision_id（失败返回 None）。"""
    try:
        with _conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO qd_agent_decisions"
                " (session_id, trace_root_id, run_id, domain, intent, user_query,"
                "  answer, model, total_tokens, latency_ms, gate_ver, gate_reason)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (session_id or "", trace_root_id, run_id or "", domain or "",
                 intent or "", (user_query or "")[:4000], (answer or "")[:8000],
                 model or "", int(total_tokens or 0), int(latency_ms or 0),
                 gate_ver or "", gate_reason or ""))
            row = cur.fetchone()
            conn.commit()
            return int(row["id"]) if row else None
    except Exception as e:
        logger.warning("[AccountStore] 写 decision 失败(忽略): %s: %s",
                       type(e).__name__, e)
        return None


def save_claims(decision_id: int, claims: List[Dict[str, Any]],
                extractor_ver: str = "") -> List[int]:
    """批量写 claim，返回写入的 id 列表。"""
    if not decision_id or not claims:
        return []
    ids: List[int] = []
    try:
        with _conn() as conn:
            cur = conn.cursor()
            for c in claims:
                cur.execute(
                    "INSERT INTO qd_agent_claims"
                    " (decision_id, seq, claim_type, subject, subject_kind, horizon,"
                    "  due_date, predicted, confidence, evidence, source_quote,"
                    "  extractor_ver)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (int(decision_id), int(c.get("seq") or 0),
                     str(c.get("claim_type") or ""), c.get("subject"),
                     c.get("subject_kind"), str(c.get("horizon") or "T+3"),
                     c.get("due_date"),
                     json.dumps(c.get("predicted") or {}, ensure_ascii=False),
                     c.get("confidence"), (c.get("evidence") or "")[:1000],
                     (c.get("source_quote") or "")[:500], extractor_ver or ""))
                row = cur.fetchone()
                if row:
                    ids.append(int(row["id"]))
            conn.commit()
    except Exception as e:
        logger.warning("[AccountStore] 写 claims 失败(忽略): %s: %s",
                       type(e).__name__, e)
    return ids


def refresh_decision_summary(decision_id: int) -> None:
    """按 claims 反算 decision 的 claim_count / due_date / resolve_status。"""
    if not decision_id:
        return
    try:
        with _conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE qd_agent_decisions d SET"
                "  claim_count = s.n,"
                "  due_date = s.due,"
                "  resolve_status = CASE"
                "    WHEN s.n = 0 THEN 'none'"
                "    WHEN s.pending = 0 THEN 'resolved'"
                "    WHEN s.pending < s.n THEN 'partial'"
                "    ELSE 'pending' END"
                " FROM (SELECT count(*) AS n,"
                "              max(due_date) AS due,"
                "              count(*) FILTER (WHERE status = 'pending') AS pending"
                "       FROM qd_agent_claims WHERE decision_id = %s) s"
                " WHERE d.id = %s",
                (int(decision_id), int(decision_id)))
            conn.commit()
    except Exception as e:
        logger.debug("[AccountStore] 反算 decision 汇总失败: %s", e)


def query_due_claims(limit: int = 50, as_of: Optional[date] = None) -> List[Dict[str, Any]]:
    """取到期未判的 claim（含决策上下文，供 judge 看原话）。"""
    day = as_of or date.today()
    try:
        with _conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT c.id, c.decision_id, c.claim_type, c.subject, c.subject_kind,"
                "       c.horizon, c.due_date, c.predicted, c.confidence,"
                "       c.source_quote, d.domain, d.intent, d.user_query, d.answer,"
                "       d.created_at::date AS exec_date"
                " FROM qd_agent_claims c"
                " JOIN qd_agent_decisions d ON d.id = c.decision_id"
                " WHERE c.status = 'pending'"
                "   AND (c.due_date IS NULL OR c.due_date <= %s)"
                " ORDER BY c.due_date NULLS LAST, c.id"
                " LIMIT %s",
                (day, int(limit)))
            return [dict(r) for r in (cur.fetchall() or [])]
    except Exception as e:
        logger.warning("[AccountStore] 查询到期 claim 失败: %s", e)
        return []


def save_resolution(*, claim_id: int, actual: Dict[str, Any],
                    actual_as_of: Optional[date], data_source: str,
                    verdict: str, verdict_score: Optional[float],
                    deviation: Optional[Dict[str, Any]],
                    attribution: str = "", attribution_note: str = "",
                    resolver_kind: str = "market_data", resolver_model: str = "",
                    resolver_ver: str = "", judge_raw: str = "",
                    cost_tokens: int = 0, sampled: bool = False) -> Optional[int]:
    """写判定结果 + 偏差细则。"""
    try:
        with _conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO qd_agent_resolutions"
                " (claim_id, actual, actual_as_of, data_source, verdict, verdict_score,"
                "  deviation, attribution, attribution_note, resolver_kind,"
                "  resolver_model, resolver_ver, judge_raw, cost_tokens, sampled)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (int(claim_id), json.dumps(actual or {}, ensure_ascii=False),
                 actual_as_of, data_source or "", verdict,
                 verdict_score, json.dumps(deviation or {}, ensure_ascii=False),
                 attribution or "", attribution_note or "", resolver_kind,
                 resolver_model or "", resolver_ver or "", judge_raw or "",
                 int(cost_tokens or 0), bool(sampled)))
            row = cur.fetchone()
            cur.execute("UPDATE qd_agent_claims SET status = %s WHERE id = %s",
                        ("unresolvable" if verdict == "undecidable" and
                         attribution == "data_missing" else "resolved",
                         int(claim_id)))
            conn.commit()
            return int(row["id"]) if row else None
    except Exception as e:
        logger.warning("[AccountStore] 写 resolution 失败(忽略): %s: %s",
                       type(e).__name__, e)
        return None


def stats(limit_days: int = 30) -> Dict[str, Any]:
    """追责健康度（验收 §7 的机检口径）：入库量 / claim 率 / 判定分布。"""
    out: Dict[str, Any] = {"decisions": 0, "claims": 0, "resolved": 0,
                           "by_verdict": {}, "by_domain": {}}
    try:
        with _conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT count(*) AS n, count(*) FILTER (WHERE claim_count > 0) AS with_claim"
                " FROM qd_agent_decisions"
                " WHERE created_at > NOW() - (%s || ' days')::interval", (int(limit_days),))
            r = cur.fetchone() or {}
            out["decisions"] = int(r.get("n") or 0)
            out["claim_rate"] = (int(r.get("with_claim") or 0) /
                                 max(1, int(r.get("n") or 0)))
            cur.execute(
                "SELECT c.status, count(*) AS n FROM qd_agent_claims c"
                " JOIN qd_agent_decisions d ON d.id = c.decision_id"
                " WHERE d.created_at > NOW() - (%s || ' days')::interval"
                " GROUP BY c.status", (int(limit_days),))
            # ★ claims 必须与 decisions **同窗口**：旧实现 claims 全表聚合、
            #   decisions 带 30 天窗口 ⇒ "claims=0 但 by_claim_status 有数" 的自相矛盾，
            #   且 out["claims"] 从未被赋值，恒为 0（健康度指标失真，看不出入库量）。
            rows = cur.fetchall() or []
            out["by_claim_status"] = {x["status"]: int(x["n"]) for x in rows}
            out["claims"] = sum(int(x["n"]) for x in rows)
            cur.execute("SELECT verdict, count(*) AS n FROM qd_agent_resolutions"
                        " GROUP BY verdict")
            out["by_verdict"] = {x["verdict"]: int(x["n"]) for x in (cur.fetchall() or [])}
            cur.execute("SELECT domain, count(*) AS n FROM qd_agent_decisions"
                        " GROUP BY domain")
            out["by_domain"] = {x["domain"]: int(x["n"]) for x in (cur.fetchall() or [])}
    except Exception as e:
        logger.debug("[AccountStore] 统计失败: %s", e)
    return out
