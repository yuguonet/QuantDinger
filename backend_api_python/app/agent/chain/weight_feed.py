# -*- coding: utf-8 -*-
"""weight_feed — 补上追责链的**断头路**：resolutions → qd_agent_weights（2026-10-01）。

【断在哪】
v1 追责系统上线后，`qd_agent_resolutions` 每天产出 hit/miss/partial 判定，但
**没有任何代码把这些判定喂回权重表** —— 唯一的权重写入方仍是旧 `update_weights`
（读 qd_agent_traces）。结果：追责跑得再勤，工具/技能的选择行为一动不动，
追责只是"审计账本"。这是差距分析里的 P0-1。

【本模块做什么】
把 resolutions 按 **domain** 聚合出近期判定命中率，慢调写入
`qd_agent_weights(layer='domain')`，供 `weight_hints` 消费（预选提示 + 技能排序）。

【为什么 granularity 只到 domain】
resolutions 挂 claim → decision，decision 只带 domain/intent，**不带 tool/skill
归因**（一次 run 用了哪些工具在 traces 里，不在 decisions 里）。硬按 user_query
反推工具归属会产生伪精确。domain 是这张表能诚实支撑的最细粒度；tool/skill 粒度
仍由旧 update_weights（traces 侧）负责 —— 两条产线各写各的层，互不覆盖。

【为什么是慢调 EMA】
v1.1 裁定：追责是慢调、允许少量误判。单条判定不足以翻转权重，故：
  w_new = w_old * (1 - a) + win_rate * a，a = clamp(n / 50, 0.10, 0.50)
样本越少越保守（最低只吃 10% 的新证据）；最小样本 MIN_SAMPLES 以下只记账不调权。

【不计入权重的两类】undecidable / data_missing —— 常量污染，与"容忍误判"两回事。
"""
from __future__ import annotations

import logging
import os
from typing import Dict, List

logger = logging.getLogger(__name__)

MIN_SAMPLES = int(os.getenv("QD_FEED_MIN_SAMPLES", "5"))
_EMA_DIVISOR = 50.0
_EMA_MIN, _EMA_MAX = 0.10, 0.50

# verdict → 计分（partial 折半；undecidable/data_missing 不计入，见模块头）
_VERDICT_SCORE = {"hit": 1.0, "partial": 0.5, "miss": 0.0}


def _alpha(n: int) -> float:
    """样本越少，新证据吃得越少（慢调）。"""
    return max(_EMA_MIN, min(_EMA_MAX, n / _EMA_DIVISOR))


def collect_domain_stats() -> List[dict]:
    """按 domain 聚合已判定 resolutions。

    Returns:
        [{"domain","n","hits","partial","misses","win_rate"}, ...]
    """
    try:
        from app.utils.db import get_db_connection
    except ImportError:
        return []
    sql = """
        SELECT d.domain AS domain,
               count(*) FILTER (WHERE r.verdict = 'hit')     AS hits,
               count(*) FILTER (WHERE r.verdict = 'partial') AS partial,
               count(*) FILTER (WHERE r.verdict = 'miss')    AS misses
        FROM qd_agent_resolutions r
        JOIN qd_agent_claims     c ON c.id = r.claim_id
        JOIN qd_agent_decisions  d ON d.id = c.decision_id
        WHERE r.verdict IN ('hit', 'partial', 'miss')
        GROUP BY d.domain
    """
    rows = []
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()          # ★ RealDictRow：必须 row['k']
            cur.execute(sql)
            raw = cur.fetchall()
            cur.close()
    except Exception as e:
        logger.warning("[weight_feed] 聚合失败: %s: %s", type(e).__name__, e)
        return []
    for r in raw:
        dom = r.get("domain")
        if not dom:
            continue
        hits = int(r.get("hits") or 0)
        part = int(r.get("partial") or 0)
        miss = int(r.get("misses") or 0)
        n = hits + part + miss
        if n <= 0:
            continue
        rows.append({
            "domain": dom, "n": n, "hits": hits, "partial": part, "misses": miss,
            "win_rate": round((hits + 0.5 * part) / n, 4),
        })
    return rows


def feed(dry_run: bool = False) -> dict:
    """把 domain 命中率慢调写回 qd_agent_weights（layer='domain'）。

    Returns:
        {"scanned":N, "updated":{domain:{"old","new","n","win_rate"}}, "skipped":[...]}
    """
    out: Dict[str, object] = {"scanned": 0, "updated": {}, "skipped": []}
    stats = collect_domain_stats()
    out["scanned"] = len(stats)
    if not stats or dry_run:
        if dry_run:
            out["would_update"] = stats
        return out
    try:
        from app.utils.db import get_db_connection
    except ImportError:
        return out
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            for s in stats:
                dom, n, wr = s["domain"], s["n"], s["win_rate"]
                cur.execute(
                    "SELECT weight FROM qd_agent_weights "
                    "WHERE layer = 'domain' AND name = %s "
                    "  AND COALESCE(skill_name, '') = ''", (dom,))
                row = cur.fetchone()
                old = float(row["weight"]) if row else 1.0
                if n < MIN_SAMPLES:
                    # 样本不足：只同步样本数，不动权重（慢调的另一半纪律）
                    cur.execute(
                        "UPDATE qd_agent_weights SET sample_count = %s, "
                        "win_rate = %s, last_updated = NOW() "
                        "WHERE layer = 'domain' AND name = %s "
                        "  AND COALESCE(skill_name, '') = ''", (n, wr, dom))
                    out["skipped"].append({"domain": dom, "n": n,
                                           "reason": f"样本 < {MIN_SAMPLES}，只记账"})
                    continue
                new = round(old * (1 - _alpha(n)) + wr * _alpha(n), 4)
                # ★ 无 id 列 ⇒ 游标兼容层会擅自补 RETURNING id，必须自带 RETURNING
                cur.execute(
                    """
                    INSERT INTO qd_agent_weights
                        (layer, name, skill_name, weight, win_rate, sample_count,
                         last_updated)
                    VALUES ('domain', %s, NULL, %s, %s, %s, NOW())
                    ON CONFLICT (layer, name, COALESCE(skill_name, '')) DO UPDATE
                       SET weight = %s,
                           win_rate = %s,
                           sample_count = %s,
                           last_updated = NOW()
                    RETURNING name
                    """,
                    (dom, new, wr, n, new, wr, n))
                cur.fetchone()
                out["updated"][dom] = {"old": old, "new": new, "n": n,
                                       "win_rate": wr,
                                       "alpha": round(_alpha(n), 3)}
            conn.commit()
            cur.close()
    except Exception as e:
        logger.warning("[weight_feed] 写入失败: %s: %s", type(e).__name__, e)
    return out
