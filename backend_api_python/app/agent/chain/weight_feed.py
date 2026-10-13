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

【G24：一 claim 一票（2026-10-13 实测新增）】
`qd_agent_resolutions` 是**判定留痕**而非"一 claim 一结果"：
`account_store.save_resolution()` 是纯 INSERT（无 UPSERT），同一 claim 每次重新
判定都会**新增一行**。实测 16 条 resolution 只对应 10 个 claim（claim 89~94 各两条）。

原实现直接 `FROM qd_agent_resolutions r ... GROUP BY d.domain` ⇒ **同一 claim 的
每一条留痕都被当成一票**。当前侥幸没爆，只因为重复的 6 条恰好都是 undecidable
（不在白名单里）；一旦重算或重试后翻成 hit/miss，n 就会随重试次数虚涨。

裁决：**保留留痕语义**（不动 save_resolution），只把统计口径改成
「每 claim 只取最新一条」。去重与聚合都做成纯函数（_dedup_latest / _aggregate_domain），
SQL 只负责取明细 —— 既便于离线守口径，也为 P2/P3 按 condition_key 分组留好位置。
"""
from __future__ import annotations

import logging
import os
from typing import Dict, List

from .schema import COUNTED_VERDICTS as _COUNTED_VERDICTS   # 真源在 schema.py
from .schema import VERDICT_SCORE as _VERDICT_SCORE         # 真源在 schema.py

logger = logging.getLogger(__name__)

MIN_SAMPLES = int(os.getenv("QD_FEED_MIN_SAMPLES", "5"))
_EMA_DIVISOR = 50.0
_EMA_MIN, _EMA_MAX = 0.10, 0.50

# 进聚合的 verdict 白名单 —— **SQL 与测试共用同一份**，禁止在 SQL 里硬编码。
# 2026-10-13 教训：只改 _VERDICT_SCORE 而漏掉 SQL 的 IN 列表 ⇒ delisted 根本进不了
# 聚合，前面两处改动全部白做。抽成常量后由 tests/test_v5_p0_closure.py 守住。
#
# ★ 2026-10-14：真源已上移到 `chain/schema.py`（P3 条件校准是第二个消费方，
#   两处各写一份必然分叉）。本模块顶部保留**原私有别名**做引用，不是抄一份 ——
#   改口径请改 schema.py，本模块与外部测试读到的仍是同一个对象。


def _alpha(n: int) -> float:
    """样本越少，新证据吃得越少（慢调）。"""
    return max(_EMA_MIN, min(_EMA_MAX, n / _EMA_DIVISOR))


# verdict → 聚合桶名（与 _COUNTED_VERDICTS / _VERDICT_SCORE 同源，不另立一套）
_BUCKET = {"hit": "hits", "partial": "partial", "miss": "misses", "delisted": "delisted"}


def _dedup_latest(rows: List[dict]) -> List[dict]:
    """一 claim 只留**最新一条** resolution（G24）。

    ★ 判定依据是**时间顺序**（res_id 最大），不是"哪条看起来更有用"：
      同一 claim 的旧留痕可能仍是 undecidable，新留痕已翻成 hit，
      若按 verdict 过滤来"凑"结果，就会变成"只数好看的那条"，与 G18 同类错误。

    Args:
        rows: [{"claim_id","res_id","domain","verdict"}, ...]

    Returns:
        去重后的明细行（每 claim_id 至多一条）
    """
    best: Dict[object, dict] = {}
    for r in rows or []:
        cid = r.get("claim_id")
        try:
            rid = int(r.get("res_id") or 0)
        except (TypeError, ValueError):
            rid = 0
        prev = best.get(cid)
        if prev is None:
            best[cid] = r
            continue
        try:
            prev_rid = int(prev.get("res_id") or 0)
        except (TypeError, ValueError):
            prev_rid = 0
        if rid > prev_rid:
            best[cid] = r
    return list(best.values())


def _aggregate_domain(rows: List[dict]) -> List[dict]:
    """按 domain 聚合已判定 resolutions（纯函数，口径唯一真源）。

    白名单与计分都取自模块常量，SQL 侧不再各自 GROUP BY ——
    改一处漏一处是 2026-10-13 踩过的坑（只改 _VERDICT_SCORE 漏掉 SQL 白名单）。

    Returns:
        [{"domain","n","hits","partial","misses","delisted","win_rate"}, ...]
    """
    acc: Dict[str, dict] = {}
    for r in rows or []:
        dom = r.get("domain")
        if not dom:
            continue
        v = str(r.get("verdict") or "")
        bucket = _BUCKET.get(v)
        if bucket is None:                  # undecidable / data_missing：不计入
            continue
        a = acc.setdefault(str(dom), {"domain": str(dom), "hits": 0, "partial": 0,
                                      "misses": 0, "delisted": 0})
        a[bucket] += 1
    out: List[dict] = []
    for a in acc.values():
        n = a["hits"] + a["partial"] + a["misses"] + a["delisted"]
        if n <= 0:
            continue
        a["n"] = n
        # delisted 已在分母里（n 含它）且计 0 分 ⇒ 自然拉低胜率（v4 G18）
        a["win_rate"] = round((a["hits"] + 0.5 * a["partial"]) / n, 4)
        out.append(a)
    return out


def collect_domain_stats() -> List[dict]:
    """按 domain 聚合已判定 resolutions（每 claim 只算一票，见模块头 G24）。

    ★ SQL 只负责取**明细行**，去重/过滤/计分全部在纯函数里完成：
      口径只有一份（可离线测），将来按 condition_key 分组（P2/P3）也只改 Python 侧。
      量级评估：日频 150 只 × 3 claim ≈ 450 条/日，年 10 万级；盘后跑一次取全量
      明细可接受。若将来成为瓶颈，再下沉为 SQL 侧 `DISTINCT ON (claim_id)`，
      但**口径定义仍以 _dedup_latest / _aggregate_domain 为准**。

    Returns:
        [{"domain","n","hits","partial","misses","delisted","win_rate"}, ...]
    """
    try:
        from app.utils.db import get_db_connection
    except ImportError:
        return []
    # ★ 白名单来自模块常量，不在这里硬编码（改一处漏一处的教训，2026-10-13）。
    #   SQL 侧先用同一常量做一次粗筛（省传输），真正的口径仍在 Python 侧。
    _in_list = ", ".join("'" + str(v) + "'" for v in _COUNTED_VERDICTS)
    sql = f"""
        SELECT r.claim_id AS claim_id,
               r.id       AS res_id,
               d.domain   AS domain,
               r.verdict  AS verdict
        FROM qd_agent_resolutions r
        JOIN qd_agent_claims     c ON c.id = r.claim_id
        JOIN qd_agent_decisions  d ON d.id = c.decision_id
        WHERE r.verdict IN ({_in_list})
    """
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()          # ★ RealDictRow：必须 row['k']
            cur.execute(sql)
            raw = [dict(x) for x in (cur.fetchall() or [])]
            cur.close()
    except Exception as e:
        logger.warning("[weight_feed] 聚合失败: %s: %s", type(e).__name__, e)
        return []
    return _aggregate_domain(_dedup_latest(raw))


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
