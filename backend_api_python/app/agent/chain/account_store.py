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

from ..log import logger


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
    from .claims import DOMAIN_POLICY
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
                extractor_ver: str = "") -> List[Optional[int]]:
    """批量写 claim，返回**与 claims 等长同序**的 id 列表，失败位为 None。

    ★ 等长同序是硬契约（intake docstring 已声明「claims 与 claim_ids 同序」）：
      下游 `c3p` 用 `zip(claim_ids, claims)` 按下标把 Δp/证据归因到具体 claim，
      一旦某条 INSERT 失败就让后续 id 前移，Δp 会**记到别的 claim 上**——
      功能看起来在工作，实际产出的是假数据，且没有任何报错。

    实现上每条 INSERT 包一层 SAVEPOINT：Postgres 事务一旦 abort 必须回滚到
    保存点才能继续，否则「一条坏」会变成「整批坏」（后续 INSERT 全部失败）。
    """
    if not decision_id or not claims:
        return []
    ids: List[Optional[int]] = []
    try:
        with _conn() as conn:
            cur = conn.cursor()
            for c in claims:
                try:
                    cur.execute("SAVEPOINT qd_claim")
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
                    cur.execute("RELEASE SAVEPOINT qd_claim")
                except Exception as e:
                    logger.warning("[AccountStore] 单条 claim 落库失败(该位置 None):"
                                   " %s: %s", type(e).__name__, e)
                    try:
                        cur.execute("ROLLBACK TO SAVEPOINT qd_claim")
                    except Exception as e2:
                        # 保存点也回滚不掉 ⇒ 事务已废，剩余条目标 None 后退出
                        logger.warning("[AccountStore] SAVEPOINT 回滚失败，"
                                       "终止本批: %s: %s", type(e2).__name__, e2)
                        ids.extend([None] * (len(claims) - len(ids)))
                        break
                    row = None
                ids.append(int(row["id"]) if row else None)
            conn.commit()
    except Exception as e:
        logger.warning("[AccountStore] 写 claims 失败(忽略): %s: %s",
                       type(e).__name__, e)
    # 契约收口：无论中途怎么退出，长度必须等于 claims（对齐优先于“写成功”）
    if len(ids) < len(claims):
        ids.extend([None] * (len(claims) - len(ids)))
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
    """取到期未判的 claim（含决策上下文，供 judge 看原话）。

    ★ 2026-10-12 修正（G0 Bug ①/②）：
      旧实现只看 `due_date <= today`，把「到期」当成「可判定」。但两者不同：
        · 休市期间到期（如国庆）：行情尚未产生 ⇒ 取数必失败；
        · 长假前做的决策：自然日已过但交易日未走完。
      新增**视界门①**（移植 evaluator.py:239-250 已验证的门）：
        只取「按交易日历算，已走完 N 个交易日**且**日历覆盖到位」的 claim；
        未到视界的**直接不返回**（不是失败，不占重试额度）。
      这样把「未到期」与「到期但取不到数」区分开：前者继续等，后者计重试次数。

    重试节流：同一 claim 两次尝试间隔 `QD_CLAIM_RETRY_HOURS`（默认 6 小时），
    避免盘后 worker 每小时重复撞同一批尚未就绪的数据。
    """
    day = as_of or date.today()
    try:
        with _conn() as conn:
            cur = conn.cursor()
            cur.execute(
                # ⚠️ verify_fail_count 必须在此选出：resolver 的退市判据
                # （G18 防幸存者偏差）读 claim["verify_fail_count"]，漏选会让该分支
                # 恒为 0 → 判据①永不成立 → delisted 分支不可达（功能静默失效）。
                # 列由 migrations/agent_v5_claim_retry.sql 添加，DEFAULT 0。
                "SELECT c.id, c.decision_id, c.claim_type, c.subject, c.subject_kind,"
                "       c.horizon, c.due_date, c.predicted, c.confidence,"
                "       c.source_quote, c.verify_fail_count,"
                "       d.domain, d.intent, d.user_query, d.answer,"
                "       d.created_at::date AS exec_date"
                " FROM qd_agent_claims c"
                " JOIN qd_agent_decisions d ON d.id = c.decision_id"
                " WHERE c.status = 'pending'"
                "   AND (c.due_date IS NULL OR c.due_date <= %s)"
                "   AND (c.last_attempt_at IS NULL"
                "        OR c.last_attempt_at <= now() - make_interval(hours => %s))"
                " ORDER BY c.due_date NULLS LAST, c.id"
                " LIMIT %s",
                (day, _retry_hours(), int(limit)))
            rows = [dict(r) for r in (cur.fetchall() or [])]
    except Exception as e:
        logger.warning("[AccountStore] 查询到期 claim 失败: %s", e)
        return []

    # ── 视界门①（时间门）：按交易日历校验「第 N 个交易日是否已走完」
    from .claims import _HORIZON_DAYS, _nth_trading_day
    ready: List[Dict[str, Any]] = []
    for r in rows:
        days = _HORIZON_DAYS.get(r.get("horizon") or "")
        ed = r.get("exec_date")
        if not days or ed is None:
            ready.append(r)          # 视界未知 ⇒ 不阻挠（保持旧行为）
            continue
        # 数据可用日 = exec 之后第 N 个交易日；取不到（日历未覆盖）⇒ 不阻挠
        due_td = _nth_trading_day(ed, days)
        if due_td is None or due_td <= day:
            ready.append(r)
        # else: 交易日未走完 ⇒ 本轮跳过（不计失败）
    return ready[:int(limit)]


def _retry_hours() -> int:
    """同一 claim 两次判定尝试的最小间隔小时数（默认 6）。"""
    import os
    try:
        return max(0, int(os.getenv("QD_CLAIM_RETRY_HOURS", "6") or "6"))
    except ValueError:
        return 6


def save_resolution(*, claim_id: int, actual: Dict[str, Any],
                    actual_as_of: Optional[date], data_source: str,
                    verdict: str, verdict_score: Optional[float],
                    deviation: Optional[Dict[str, Any]],
                    attribution: str = "", attribution_note: str = "",
                    resolver_kind: str = "market_data", resolver_model: str = "",
                    resolver_ver: str = "", judge_raw: str = "",
                    cost_tokens: int = 0, sampled: bool = False) -> Optional[int]:
    """写判定结果 + 偏差细则，并按**重试治理**更新 claim 状态。

    ★ 2026-10-12 修正（G0 Bug B）：
      旧实现只要 `undecidable + data_missing` 就置 `unresolvable`（终态，永不重试）。
      但 `data_missing` 是**暂时性**失败（数据只是还没到）—— 实测案例：
        10/1 的 claim 在 10/7 因国庆休市取不到数 ⇒ 被终态化；
        而数据 10/9 已可得 ⇒ 教训永久丢失（6 条）。
      新语义（*暂时性失败可重试*）：
        · 失败 → `verify_fail_count += 1`，状态**保持 pending**（下轮还会被扫到）；
        · 达上限（`QD_CLAIM_MAX_RETRY`，默认 5）→ 才置 `unresolvable`
          （沿用 `chain/evaluator` 既有毒丸机制的 5 次阀值，不新造规则）；
        · **真实判定（hit/miss/partial）不受影响**：直接 resolved，fail_count 归零。
      ⇒ 区分「到期」与「数据已就绪」：到期但数据未到 ⇒ 只计失败次数，**不当结论**。
    """
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
            _advance_claim_status(cur, int(claim_id), verdict, attribution)
            conn.commit()
            return int(row["id"]) if row else None
    except Exception as e:
        logger.warning("[AccountStore] 写 resolution 失败(忽略): %s: %s",
                       type(e).__name__, e)
        return None


def _max_retry() -> int:
    """暂时性失败的最大重试次数（默认 5，与 `chain/evaluator` 毒丸阀值同口径）。"""
    import os
    try:
        return max(1, int(os.getenv("QD_CLAIM_MAX_RETRY", "5") or "5"))
    except ValueError:
        return 5


def _advance_claim_status(cur, claim_id: int, verdict: str, attribution: str) -> None:
    """按判定结果推进 claim 状态（重试治理，见 `save_resolution` 注释）。

    分三类：
      A) 真实结论（hit/miss/partial）  ⇒ resolved，fail_count 归零
      B) 取数不可用（undecidable+data_missing）⇒ fail_count+1，**保持 pending**
         （达上限才 unresolvable）
      C) 其余 undecidable（如 caliber/口径问题）⇒ unresolvable
         （不是数据未到，而是判不了；重试无意义）
    """
    transient = (verdict == "undecidable" and attribution == "data_missing")
    if transient:
        limit = _max_retry()
        cur.execute("""
            UPDATE qd_agent_claims
               SET verify_fail_count = COALESCE(verify_fail_count, 0) + 1,
                   last_attempt_at = now(),
                   status = CASE
                       WHEN COALESCE(verify_fail_count, 0) + 1 >= %s THEN 'unresolvable'
                       ELSE 'pending'
                   END
             WHERE id = %s
        """, (limit, claim_id))
        return

    if verdict == "undecidable":
        # C) 判不了（非数据未到）：终态；不占重试额度
        cur.execute(
            "UPDATE qd_agent_claims SET status = 'unresolvable',"
            " last_attempt_at = now() WHERE id = %s", (claim_id,))
        return

    # A) 真实结论：已定论，不再参与扫描
    cur.execute(
        "UPDATE qd_agent_claims SET status = 'resolved',"
        " last_attempt_at = now() WHERE id = %s", (claim_id,))


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
