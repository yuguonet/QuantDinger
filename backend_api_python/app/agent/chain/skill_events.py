# -*- coding: utf-8 -*-
"""chain/skill_events.py — 技能选择事件链 + 版本级效果反馈（2026-10-19，阶段 4）。

对应 `tmp/skill_system_rework_implementation_plan_20261018.md` §6/§8/§14.2：

  事件链（§6.2）：每请求一条 `qd_skill_selection_events`——候选、选中、注入、
    执行、标签**分状态**，不把"只被召回"记作技能使用；版本在选择时冻结
    （后续文件变更不改写历史事件）；关联 trace root；写入失败只记诊断，
    不阻塞用户请求（fail-soft）。
  归因纪律（§6.3）：只有"唯一、有效的选择事件 + 实际注入 + 可关联的成熟标签"
    才计样本；多技能协作/无明确技能节点 ⇒ `unattributable`（排除，不分摊）；
    `pending`/`undecidable`/`data_missing`/用户中断/基础设施失败均**不计失败**。
  统计（§6.4）：按 (skill_id, version) 幂等聚合（event_id 去重，worker 重跑不
    重复计数）；低于样本门槛权重恒中性 1.0；有样本后带收缩、限幅 [0.9, 1.1]。
  读取（§6.5）：`weight_of()` 是技能效果反馈**唯一读取口**（TTL 缓存 +
    中性兜底 + 读侧限幅；诊断整行走 `feedback_detail()`，同表同缓存）——
    阶段 5 起由 Top-K 技能池排序消费（final = base × weight，只调序不过滤）。
  分离（§6.1）：与案例库（qd_agent_cases）、domain 权重、旧
    `qd_agent_weights layer='skill'` 三分离，不混算、不互相读取。

红线沿用：
  - 阈值/口径走 env（QD_SKILL_FEEDBACK_MIN_SAMPLES / QD_SKILL_FEEDBACK_CACHE_TTL /
    QD_SKILL_EVENTS），不硬编码；脏值回落默认。
  - DB 访问函数内惰性 import + fail-soft + `_db()` 可 monkeypatch（测试注入）。
  - 标签先到先得（first-wins）：重跑/重复成熟不改写已成熟标签（幂等）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Sequence

#: 排序器版本（写入事件；复现实验按版本还原口径）
RANKER_VERSION = "lexical-topk-v1"
#: 候选索引版本（阶段 2 类型化索引首版）
INDEX_VERSION = "candidate-v1"
#: 统计策略版本（随统计结果存档；改权重公式必须升版本）
POLICY_VERSION = "feedback-v1"

# ── 标签口径（§6.3）────────────────────────────────────────────
#: 计样本的成熟标签（correct/partial/incorrect）
SAMPLE_LABELS = ("correct", "partial", "incorrect")
#: 排除态：落 excluded_counts，**不计失败**（§6.3.3/§14.2）
EXCLUDED_LABELS = ("pending", "undecidable", "data_missing",
                   "interrupted", "infra_failure", "unattributable")
ALL_LABELS = SAMPLE_LABELS + EXCLUDED_LABELS

#: 漏斗六阶段（§14.2：分开统计；零曝光/曝光未选中不是负向样本）
FUNNEL_STAGES = ("eligible_but_not_exposed", "exposed_not_selected",
                 "selected_not_injected", "injected_not_executed",
                 "executed_pending", "executed_labeled")

_MIN_SAMPLES_DEFAULT = 15      # §15.4：低于门槛恒中性 1.0
_CACHE_TTL_DEFAULT = 60        # 秒：weight_of TTL 缓存
_WEIGHT_LO, _WEIGHT_HI = 0.9, 1.1   # §15.4：反馈限幅


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def enabled() -> bool:
    """事件链总开关（QD_SKILL_EVENTS，默认开；"0"/"false" 关）。

    关闭 ⇒ 所有写侧 no-op（读侧仍可用——影子统计停机不影响排序基线）。
    """
    return (os.getenv("QD_SKILL_EVENTS") or "1").strip().lower() \
        not in ("0", "false", "no", "off")


def min_samples() -> int:
    """有效样本门槛（QD_SKILL_FEEDBACK_MIN_SAMPLES，默认 15；负值夹 0）。"""
    return max(0, _env_int("QD_SKILL_FEEDBACK_MIN_SAMPLES", _MIN_SAMPLES_DEFAULT))


def _cache_ttl() -> float:
    return float(max(0, _env_int("QD_SKILL_FEEDBACK_CACHE_TTL", _CACHE_TTL_DEFAULT)))


def query_fingerprint(query: str) -> str:
    """查询指纹：小写 + 剥除非词字符（标点/空白全去）后 SHA-256 前 16 hex。

    口径：指纹用于**分组统计**（同题复跑/漏斗归因），不是精确匹配 ⇒ 标点与
    空白差异必须归一掉（「分析 600519！」=「分析600519」）；下划线保留
    （工具名 get_kline 不并字）。确定性：同输入同输出。
    """
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "", str(query or "").lower())
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


# ── DB 薄层（fail-soft；函数内惰性 import 便于测试注入）──────────

def _db():
    from app.utils.db import get_db_connection
    return get_db_connection()


def _log_debug(msg: str, *args) -> None:
    try:
        from ..log import logger
        logger.debug(msg, *args)
    except Exception:
        pass


# ── 事件写入（§6.2）────────────────────────────────────────────

def record_selection(*, request_id: str, session_id: str, query: str,
                     candidates: Sequence[dict], selected_skill_id: str = "",
                     selected_version: str = "",
                     index_version: str = INDEX_VERSION,
                     ranker_version: str = RANKER_VERSION) -> Optional[str]:
    """落一条选择事件（候选快照 + 选中 + 冻结版本）。返回 event_id；失败 ⇒ None。

    Args:
        candidates: [{skill_id, version, base_score, exposed, reasons}]——预选
            展示集与落选 eligible 的**完整快照**（漏斗 eligible_but_not_exposed
            的判据来源；未过门槛者不进 candidates）。
        selected_skill_id/version: planner 点名（预选 skill 字段）；空 = 未选中。
            版本必须在**选择时冻结**（调用方传 registry active_version）。
    """
    if not enabled():
        return None
    event_id = uuid.uuid4().hex
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO qd_skill_selection_events
                    (event_id, request_id, session_id, query_fingerprint,
                     index_version, ranker_version, candidates,
                     selected_skill_id, selected_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (event_id) DO NOTHING
            """, (event_id, str(request_id or "")[:80], str(session_id or "")[:80],
                  query_fingerprint(query), str(index_version or "")[:40],
                  str(ranker_version or "")[:40],
                  json.dumps(list(candidates or []), ensure_ascii=False),
                  str(selected_skill_id or "")[:100],
                  str(selected_version or "")[:80]))
            conn.commit()
            cur.close()
        return event_id
    except Exception as e:
        _log_debug("[SkillEvents] 选择事件写入失败（fail-soft，不阻塞请求）: %s", e)
        return None


def mark_injected(event_id: str, skill_id: str, version: str) -> bool:
    """标记实际注入的技能（可能来自 prefetch 关键词回落，非 planner 点名）。

    幂等：已注入 ⇒ no-op 成功（重复 worker 不双计）。
    """
    if not enabled() or not event_id:
        return False
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE qd_skill_selection_events
                SET injected = TRUE, injected_skill_id = %s,
                    injected_version = %s, updated_at = NOW()
                WHERE event_id = %s AND injected = FALSE
            """, (str(skill_id or "")[:100], str(version or "")[:80], event_id))
            n = cur.rowcount
            conn.commit()
            cur.close()
        # n=0 ⇒ 已注入（WHERE injected=FALSE 不命中）——幂等，仍算成功
        return True
    except Exception as e:
        _log_debug("[SkillEvents] 注入标记失败（fail-soft）: %s", e)
        return False


def mark_run_outcome(event_id: str, *, trace_root_id: Optional[int] = None,
                     skill_node_names: Sequence[str] = ()) -> bool:
    """回填 trace root + 执行状态（影子；§6.2 注入 ≠ 执行）。

    executed 判定由本模块持有：事件的 `injected_skill_id` 出现在本次 trace 的
    skill 节点名集合里（planner 真跑过该技能）才算执行；注入但没跑 ⇒
    executed=False（漏斗 injected_not_executed）。DB 不可用 ⇒ False（不抛）。
    """
    if not enabled() or not event_id:
        return False
    names = {str(n or "") for n in (skill_node_names or ())}
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("""SELECT injected_skill_id FROM qd_skill_selection_events
                           WHERE event_id = %s""", (event_id,))
            row = cur.fetchone()
            cur.close()
            if not row:
                return False
            executed = bool(str(row["injected_skill_id"] or "")) \
                and str(row["injected_skill_id"] or "") in names
            cur2 = conn.cursor()
            cur2.execute("""
                UPDATE qd_skill_selection_events
                SET trace_root_id = %s, executed = %s, updated_at = NOW()
                WHERE event_id = %s
            """, (trace_root_id, executed, event_id))
            conn.commit()
            cur2.close()
        return True
    except Exception as e:
        _log_debug("[SkillEvents] 执行状态回填失败（fail-soft）: %s", e)
        return False


# ── 标签成熟与归因（§6.3）──────────────────────────────────────

def finalize_label(event_id: str, label: str, *,
                   exclusion_reason: str = "") -> bool:
    """成熟标签落库（first-wins 幂等）。

    - label 必须 ∈ ALL_LABELS；非法 ⇒ False（不猜）。
    - 已成熟（label 非空）⇒ 同值 no-op 成功（Postgres 仍计 1 行）、异值不改写
      返回 False（T+N 重跑/重复成熟不得翻转已成熟标签——幂等第一入口）。
    - pending 等排除态同样落库（漏斗 executed_pending 的判据），但不计样本。
    """
    if not enabled() or not event_id:
        return False
    label = str(label or "")
    if label not in ALL_LABELS:
        return False
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE qd_skill_selection_events
                SET label = %s, exclusion_reason = %s, updated_at = NOW()
                WHERE event_id = %s
                  AND (label = '' OR label = %s)
            """, (label, str(exclusion_reason or "")[:80], event_id, label))
            n = cur.rowcount
            conn.commit()
            cur.close()
        return bool(n)
    except Exception as e:
        _log_debug("[SkillEvents] 标签落库失败（fail-soft）: %s", e)
        return False


def ingest_trace_labels(trace_root_id: Optional[int], skill_name: str,
                        correct: Optional[bool], *,
                        skill_node_count: int = 1) -> bool:
    """追责成熟结果 → 事件标签（影子；阶段 4 不换生产排序）。

    归因纪律（§6.3）：
      ① 事件必须存在且**实际注入并执行**了该技能（injected ∧ executed）；
      ② 该 root 的技能节点必须**唯一**（skill_node_count>1 = 多技能协作 ⇒
         `unattributable` 排除，不分摊、不猜测）；
      ③ correct 非空才成熟（None ⇒ pending，不计样本也不计失败）。
    """
    if not enabled() or not trace_root_id or not skill_name:
        return False
    if int(skill_node_count or 0) > 1:
        # 多技能协作：找得到事件也标不可归因（排除而非失败）
        try:
            with _db() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT event_id FROM qd_skill_selection_events
                    WHERE trace_root_id = %s AND injected_skill_id = %s
                      AND executed = TRUE AND label = ''
                """, (trace_root_id, str(skill_name)[:100]))
                rows = cur.fetchall()
                cur.close()
            for r in rows:
                finalize_label(r["event_id"], "unattributable",
                               exclusion_reason="multi_skill_root")
            return True
        except Exception as e:
            _log_debug("[SkillEvents] 多技能归因排除失败（fail-soft）: %s", e)
            return False
    if correct is None:
        label, reason = "pending", "awaiting_mature"
    elif correct:
        label, reason = "correct", ""
    else:
        label, reason = "incorrect", ""
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT event_id FROM qd_skill_selection_events
                WHERE trace_root_id = %s AND injected_skill_id = %s
                  AND injected = TRUE AND executed = TRUE AND label = ''
            """, (trace_root_id, str(skill_name)[:100]))
            rows = cur.fetchall()
            cur.close()
        if not rows:
            return False
        ok = False
        for r in rows:
            ok = finalize_label(r["event_id"], label, exclusion_reason=reason) or ok
        return ok
    except Exception as e:
        _log_debug("[SkillEvents] 追责标签入库失败（fail-soft）: %s", e)
        return False


# ── 版本级聚合（§6.4：幂等 + 分离）─────────────────────────────

def _shrink_weight(correct: int, partial: int, incorrect: int,
                   threshold: int) -> tuple:
    """带收缩的反馈权重（确定性、可复现；§15.4 限幅 [0.9, 1.1]）。

    口径：score = (correct + 0.5×partial) / n（0..1）；confidence = n/(n+K)
    （K = 门槛，样本越多越敢离中性）；weight = 1 + (score−0.5)×2×confidence，
    再夹到 [0.9, 1.1]。低于门槛 ⇒ 恒 1.0（中性，排序与无反馈基线一致）。
    partial 在 v1 标签源（trace correct 布尔）下恒 0，公式预留。
    """
    n = correct + partial + incorrect
    if n <= 0 or n < threshold:
        return 1.0, {"score": 0.0, "confidence": 0.0, "n": n,
                     "min_samples": threshold}
    score = (correct + 0.5 * partial) / n
    k = max(1, threshold)
    confidence = n / (n + k)
    w = 1.0 + (score - 0.5) * 2.0 * confidence
    w = max(_WEIGHT_LO, min(_WEIGHT_HI, w))
    return round(w, 6), {"score": round(score, 6), "confidence": round(confidence, 6),
                         "n": n, "min_samples": threshold}


def aggregate_feedback(*, skill_id: Optional[str] = None) -> Dict[str, dict]:
    """从事件幂等重算 qd_skill_feedback（按 event_id 天然去重，可反复跑）。

    只统计 injected ∧ executed 的事件（未执行不进样本）；label ∈ SAMPLE_LABELS
    进 correct/partial/incorrect；EXCLUDED_LABELS 进 excluded_counts；
    未成熟（label=''）既不计也不排（等成熟）。返回 {skill_id: 统计摘要}。
    """
    out: Dict[str, dict] = {}
    if not enabled():
        return out
    try:
        with _db() as conn:
            cur = conn.cursor()
            sql = """
                SELECT injected_skill_id AS skill_id, injected_version AS version,
                       label, COUNT(*) AS n
                FROM qd_skill_selection_events
                WHERE injected = TRUE AND executed = TRUE
                  AND injected_skill_id <> ''
            """
            params: list = []
            if skill_id:
                sql += " AND injected_skill_id = %s"
                params.append(str(skill_id)[:100])
            sql += " GROUP BY injected_skill_id, injected_version, label"
            cur.execute(sql, params)
            rows = cur.fetchall()
            cur.close()
    except Exception as e:
        _log_debug("[SkillEvents] 聚合扫描失败（fail-soft）: %s", e)
        return out

    agg: Dict[tuple, Dict[str, Any]] = {}
    for r in rows:
        key = (str(r["skill_id"]), str(r["version"] or ""))
        slot = agg.setdefault(key, {"correct": 0, "partial": 0, "incorrect": 0,
                                    "excluded": {}})
        label, n = str(r["label"] or ""), int(r["n"] or 0)
        if label == "correct":
            slot["correct"] += n
        elif label == "partial":
            slot["partial"] += n
        elif label == "incorrect":
            slot["incorrect"] += n
        elif label in EXCLUDED_LABELS:
            slot["excluded"][label] = slot["excluded"].get(label, 0) + n
        # label=''（未成熟）不进任何桶

    thr = min_samples()
    try:
        with _db() as conn:
            cur = conn.cursor()
            for (sid, ver), slot in sorted(agg.items()):
                w, unc = _shrink_weight(slot["correct"], slot["partial"],
                                        slot["incorrect"], thr)
                cur.execute("""
                    INSERT INTO qd_skill_feedback
                        (skill_id, version, eligible_count, correct_count,
                         partial_count, incorrect_count, excluded_counts,
                         weight, uncertainty, policy_version, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (skill_id, version) DO UPDATE SET
                        eligible_count = EXCLUDED.eligible_count,
                        correct_count = EXCLUDED.correct_count,
                        partial_count = EXCLUDED.partial_count,
                        incorrect_count = EXCLUDED.incorrect_count,
                        excluded_counts = EXCLUDED.excluded_counts,
                        weight = EXCLUDED.weight,
                        uncertainty = EXCLUDED.uncertainty,
                        policy_version = EXCLUDED.policy_version,
                        updated_at = NOW()
                """, (sid, ver,
                      slot["correct"] + slot["partial"] + slot["incorrect"],
                      slot["correct"], slot["partial"], slot["incorrect"],
                      json.dumps(slot["excluded"], ensure_ascii=False),
                      w, json.dumps(unc, ensure_ascii=False), POLICY_VERSION))
                out[sid] = {"version": ver, "weight": w,
                            "eligible": slot["correct"] + slot["partial"] + slot["incorrect"],
                            "excluded": dict(slot["excluded"])}
            conn.commit()
            cur.close()
    except Exception as e:
        _log_debug("[SkillEvents] 聚合写回失败（fail-soft）: %s", e)
    return out


# ── 唯一读取口（§6.5；阶段 5 才接入排序）────────────────────────

_CACHE: Dict[tuple, tuple] = {}
_CACHE_LOCK = threading.Lock()


def feedback_detail(skill_id: str, version: str = "") -> Optional[dict]:
    """诊断读口：权重 + 样本数 + 计数 + policy 版本（观测/排序 trace 用）。

    与 `weight_of` 同表同缓存（`weight_of` 是本数据的唯一排序消费出口）：
    排序乘子只要一个 float，本函数回整行——供阶段 5 排序 trace 记录
    「为什么是这个权重」（§14.3：新排序链记录 skill_id、版本、policy/
    ranker 版本及样本数）。语义与 weight_of 逐字对齐：
      · TTL 缓存**只落成功行**（DB 抖动不污染 TTL 窗）；
      · 无行 / DB 不可用 ⇒ None（调用方按中性 1.0 + 0 样本处理）。
    """
    key = (str(skill_id or ""), str(version or ""))
    if not key[0]:
        return None
    ttl = _cache_ttl()
    now = time.time()
    if ttl > 0:
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
        if hit and (now - hit[1]) < ttl:
            return hit[0]
    try:
        with _db() as conn:
            cur = conn.cursor()
            if key[1]:
                cur.execute("""SELECT skill_id, version, weight, eligible_count,
                                       correct_count, partial_count,
                                       incorrect_count, policy_version
                                FROM qd_skill_feedback
                                WHERE skill_id = %s AND version = %s""",
                            (key[0], key[1]))
            else:
                cur.execute("""SELECT skill_id, version, weight, eligible_count,
                                       correct_count, partial_count,
                                       incorrect_count, policy_version
                                FROM qd_skill_feedback
                                WHERE skill_id = %s
                                ORDER BY updated_at DESC LIMIT 1""", (key[0],))
            row = cur.fetchone()
            cur.close()
    except Exception as e:
        _log_debug("[SkillEvents] feedback_detail 读取失败: %s", e)
        return None
    if not row:
        return None
    out = dict(row)
    if ttl > 0:
        with _CACHE_LOCK:
            _CACHE[key] = (out, now)
    return out


def weight_of(skill_id: str, version: str = "") -> float:
    """技能效果反馈**唯一读取口**（TTL 缓存 + 中性兜底 + 读侧限幅）。

    - 无记录 / DB 不可用 / 低于样本门槛 ⇒ 1.0（中性：排序与无反馈基线一致）；
    - 版本隔离：新版本没有样本回到中性，不盲目继承旧版本（§6.4）；
    - **读侧限幅**（阶段 5 纵深防御）：写侧 `_shrink_weight` 已夹
      `[_WEIGHT_LO, _WEIGHT_HI]`，读口再夹一次——DB 行被旧策略/手工写飞时，
      任何消费方拿到的乘子仍有界（§15.4：限幅是新链安全阀，装在点用点上）；
    - 缓存负结果不落盘（DB 抖动不污染 TTL 窗）。
    """
    if not str(skill_id or ""):
        return 1.0
    detail = feedback_detail(skill_id, version)
    if not detail or detail.get("weight") is None:
        return 1.0
    try:
        w = float(detail["weight"])
    except (TypeError, ValueError):
        return 1.0
    return max(_WEIGHT_LO, min(_WEIGHT_HI, w))


def feedback_snapshot(*, limit: int = 50) -> List[dict]:
    """可观测快照（阶段 5 影子对照/复现实验用；只读）。"""
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT skill_id, version, eligible_count, correct_count,
                       partial_count, incorrect_count, excluded_counts,
                       weight, policy_version, updated_at
                FROM qd_skill_feedback
                ORDER BY updated_at DESC LIMIT %s
            """, (max(1, int(limit)),))
            rows = cur.fetchall()
            cur.close()
            return [dict(r) for r in rows]
    except Exception as e:
        _log_debug("[SkillEvents] 快照读取失败（fail-soft）: %s", e)
        return []


# ── 漏斗（§14.2：六阶段分开统计）────────────────────────────────

def funnel_report(*, since_days: int = 7, limit: int = 5000) -> Dict[str, Any]:
    """候选曝光漏斗（影子）：六阶段 + 排除原因，**零曝光/未选中不计负向**。

    判据（每事件）：
      eligible_but_not_exposed ← candidates 中 exposed=False 的条数；
      exposed_not_selected     ← exposed=True 且未成为 selected/injected；
      selected_not_injected    ← selected 非空且 injected=False；
      injected_not_executed    ← injected=True 且 executed=False；
      executed_pending         ← executed=True 且 label 为空/pending；
      executed_labeled         ← executed=True 且 label ∈ SAMPLE_LABELS。
    """
    out: Dict[str, Any] = {s: 0 for s in FUNNEL_STAGES}
    out["excluded"] = {}
    out["events"] = 0
    if not enabled():
        return out
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT candidates, selected_skill_id, injected, injected_skill_id,
                       executed, label
                FROM qd_skill_selection_events
                WHERE created_at >= NOW() - make_interval(days => %s)
                ORDER BY created_at DESC LIMIT %s
            """, (max(0, int(since_days)), max(1, int(limit))))
            rows = cur.fetchall()
            cur.close()
    except Exception as e:
        _log_debug("[SkillEvents] 漏斗扫描失败（fail-soft）: %s", e)
        return out

    for r in rows:
        out["events"] += 1
        try:
            cands = json.loads(r["candidates"]) if isinstance(r["candidates"], str) \
                else (r["candidates"] or [])
        except (ValueError, TypeError):
            cands = []
        chosen = str(r["selected_skill_id"] or "") or str(r["injected_skill_id"] or "")
        for c in cands:
            if not isinstance(c, dict):
                continue
            if c.get("exposed"):
                if str(c.get("skill_id") or "") != chosen:
                    out["exposed_not_selected"] += 1
            else:
                out["eligible_but_not_exposed"] += 1
        injected, executed = bool(r["injected"]), bool(r["executed"])
        label = str(r["label"] or "")
        if r["selected_skill_id"] and not injected:
            out["selected_not_injected"] += 1
        if injected and not executed:
            out["injected_not_executed"] += 1
        if executed:
            if label in SAMPLE_LABELS:
                out["executed_labeled"] += 1
            else:
                out["executed_pending"] += 1
        if label in EXCLUDED_LABELS:
            out["excluded"][label] = out["excluded"].get(label, 0) + 1
    return out


def _cli(argv: Sequence[str]) -> int:
    """手工入口：`python -m app.agent.chain.skill_events [--aggregate] [--funnel]`。"""
    args = list(argv or [])
    if "--aggregate" in args:
        res = aggregate_feedback()
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    if "--funnel" in args:
        print(json.dumps(funnel_report(), ensure_ascii=False, indent=2))
        return 0
    if "--snapshot" in args:
        print(json.dumps(feedback_snapshot(), ensure_ascii=False, indent=2, default=str))
        return 0
    print("usage: python -m app.agent.chain.skill_events "
          "[--aggregate] [--funnel] [--snapshot]")
    return 1


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
