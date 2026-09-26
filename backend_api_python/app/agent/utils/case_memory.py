# -*- coding: utf-8 -*-
"""案例记忆（提智方案 三波 T1，CBR，2026-09-24，署名：OpenClaw agent）。

来源：`agent_tizhi_final_plan_20260923.md` §三 三波 T1 + 用户方案 §三 一（案例记忆）。

职责（run 级"一次性 few-shot"，填技能酿造的粒度空档）：
  · `record_case()`：finalize 后置钩子落一条案例（任务描述/规划摘要/工具链/结果要点/
    延迟标签/失败坑/成本）；
  · `retrieve_cases()`：plan 期向量检索 top-3 相似案例（相似度低于阈值一条不注——
    宁缺毋滥）；
  · `backfill_by_root()`：**延迟标签回填**（灵魂设计）——T+N 回测定论后把 outcome
    从 pending 改写为 correct/incorrect（只读 correct，绝不写——P1 写保护）；
  · `query_brew_case_signals()`：plan_digest 聚类 ≥N 次且 correct 率高 → 酿造候选
    **信号④**（前置依赖 0.3 状态机四修，已完成）。

存储裁决（§四 #6）：pgvector 表 `qd_agent_cases`（字段用用户 schema）+ 可选 JSONL 快照。
实现沿用 `rag/pg_vector_store.py` 同款形态（JSONB 存向量 + Python 余弦；本仓 pgvector
扩展不可依赖），embedding 走 `rag/embeddings.py` 工厂（env EMBEDDING_*）；无 embedding
配置时退化为 2-gram 词面相似（零外呼），检索质量降级但通道不断。

检索加权（用户方案原文语义）：
  · label=incorrect → **硬排除**（错误结论不许以 few-shot 自我复制——比没记忆更危险）；
  · label=pending → 降权（默认 0.5×）；label=correct → 全权重；
  · 近重复（彼此相似度 >0.95）只留最新且已验证的一条。

易错点：
  · 一切 DB/向量操作 **fail-open**：案例记忆是增益层，绝不阻断规划/收尾主链；
    但失败要 warning 可见（本模块高发"声明了没接线"，静默=断链复发）；
  · `backfill_by_root` 只在 label 仍为 pending 时回填——人工反馈（human_reviewed）
    与二次校验不得被覆盖（③反馈保护同款语义）；
  · 注入语义严格两类：成功案例给"骨架候选 + 坑"，失败案例**只给坑、绝不附修复路径**
    （防模型抄修复路径而不理解任务差异）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "record_case", "retrieve_cases", "backfill_by_root",
    "query_brew_case_signals", "format_case_injection", "case_memory_enabled",
]

# ── 阈值（env=运维可调；C8 家族：上线后用评测/命中率校准）──────────────────
# 相似度下限：低于它一条不注（宁缺毋滥）。词面/向量共用一个阈值（都在 [0,1]）。
CASE_SIM_MIN = float(os.getenv("CASE_SIM_MIN", "0.35") or "0.35")
# 近重复判定：彼此相似度 > 该值只留最新已验证
CASE_DUP_SIM = float(os.getenv("CASE_DUP_SIM", "0.95") or "0.95")
# 检索扫描上限（与 PgVectorStore 同款"限扫"策略；小规模全量余弦足够）
CASE_SCAN_LIMIT = 2000
# pending 降权系数
_PENDING_WEIGHT = 0.5
# 信号④：聚类最小出现次数 / 最小已验证样本 / correct 率门槛
CASE_CLUSTER_MIN = 3
CASE_CLUSTER_MIN_LABELED = 2
CASE_CLUSTER_WIN_RATE = 0.7

_warned: set = set()


def case_memory_enabled() -> bool:
    """案例记忆开关（默认开；CASE_MEMORY_ENABLED=0 关闭存/取/回填全链路）。"""
    return (os.getenv("CASE_MEMORY_ENABLED", "1") or "1").strip().lower() \
        not in ("0", "false", "no", "off")


def _warn_once(key: str, msg: str) -> None:
    if key in _warned:
        return
    _warned.add(key)
    logger.warning("[CaseMemory] %s", msg)


# ═══════════════════════════════════════════════════════════════
#  存储（qd_agent_cases；additive schema，自动建表）
# ═══════════════════════════════════════════════════════════════

_DDL = """
CREATE TABLE IF NOT EXISTS qd_agent_cases (
    case_id      VARCHAR(64) PRIMARY KEY,
    root_id      BIGINT,
    task_summary TEXT NOT NULL,
    level        VARCHAR(4),
    tags         JSONB DEFAULT '[]',
    plan_digest  JSONB DEFAULT '[]',
    outcome      JSONB DEFAULT '{"label": "pending", "t_plus_n": null, "confidence": null}',
    failure_modes JSONB DEFAULT '[]',
    cost         JSONB DEFAULT '{}',
    embedding    JSONB,
    created_at   TIMESTAMP DEFAULT NOW(),
    updated_at   TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_qd_agent_cases_root   ON qd_agent_cases (root_id);
CREATE INDEX IF NOT EXISTS idx_qd_agent_cases_created ON qd_agent_cases (created_at DESC);
"""

_schema_ok = False


@contextmanager
def _get_conn():
    """取 DB 连接（与 chain/store 同源 app.utils.db）；不可用 yield None（fail-open）。

    contextmanager：配合 `with _get_conn() as conn:` 使用，`conn` 才是真实连接
    （PostgresConnection，支持 .cursor()）。不可误把返回值直接当连接用——
    `get_db_connection()` 是 @contextmanager，直接调用拿到的是 Provider。
    """
    try:
        from app.utils.db import get_db_connection
    except Exception as e:
        _warn_once("db", "DB 不可用，案例记忆离线（%s: %s）" % (type(e).__name__, e))
        yield None
        return
    with get_db_connection() as conn:
        yield conn


def _ensure_schema(cur, conn) -> None:
    global _schema_ok
    if _schema_ok:
        return
    cur.execute(_DDL)
    conn.commit()  # DDL 必须提交：连接是共享连接池的，不提交其他连接看不到这表
    _schema_ok = True


# ═══════════════════════════════════════════════════════════════
#  相似度（向量优先，词面兜底；都在 [0,1]）
# ═══════════════════════════════════════════════════════════════

_embedder = None
_embedder_tried = False


def _get_embedder():
    """embedding 工厂（env EMBEDDING_*，与 agent._build_retriever 同配置源）。

    惰性单例；未配置/初始化失败 → None（调用方退词面相似）。**只试一次**——
    每次检索重试工厂会把配置错误刷成 warning 风暴。
    """
    global _embedder, _embedder_tried
    if _embedder_tried:
        return _embedder
    _embedder_tried = True
    try:
        provider = (os.getenv("EMBEDDING_PROVIDER", "") or "").strip()
        if not provider:
            return None
        from rag.embeddings import EmbeddingModel
        _embedder = EmbeddingModel(
            provider=provider,
            model=os.getenv("EMBEDDING_MODEL", ""),
            api_key=os.getenv("EMBEDDING_API_KEY", ""),
            base_url=os.getenv("EMBEDDING_BASE_URL", ""),
        )
    except Exception as e:
        _warn_once("embed", "embedding 初始化失败，案例检索退化为词面相似（%s）" % e)
        _embedder = None
    return _embedder


_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")


def _lex_tokens(text: str) -> set:
    """词面 token：ASCII 词 + 中文 2-gram（与 plan_linter._tokens 同口径近似）。"""
    s = str(text or "").lower()
    toks = set(w for w in _TOKEN_RE.findall(s) if len(w) > 2)
    for seg in re.findall(r"[\u4e00-\u9fff]+", s):
        toks |= {seg[i:i + 2] for i in range(len(seg) - 1)}
    return toks


def _lex_sim(a: str, b: str) -> float:
    ta, tb = _lex_tokens(a), _lex_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / float(len(ta | tb))


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return (dot / (na * nb)) if na and nb else 0.0


# ═══════════════════════════════════════════════════════════════
#  写入（record / 回填）
# ═══════════════════════════════════════════════════════════════

def record_case(task_summary: str, *, root_id: Optional[int] = None, level: str = "",
                tags: Optional[List[str]] = None,
                plan_digest: Optional[List[dict]] = None,
                failure_modes: Optional[List[str]] = None,
                cost: Optional[dict] = None,
                confidence: Optional[float] = None) -> Optional[str]:
    """落一条案例（finalize 后置钩子调用；fail-open）。

    outcome.label 恒以 `pending` 开局——correct/incorrect 由 T+N 回测经
    `backfill_by_root` 回填（延迟标签，防错误自我复制）。

    Returns:
        case_id；失败/关闭返回 None。
    """
    if not case_memory_enabled() or not (task_summary or "").strip():
        return None
    case_id = "CASE-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    summary = str(task_summary).strip()[:500]
    emb = None
    try:
        embedder = _get_embedder()
        if embedder is not None:
            vec = embedder.embed_query(summary)
            emb = list(vec) if vec else None
    except Exception as e:
        logger.debug("[CaseMemory] embedding 跳过: %s", e)

    outcome = {"label": "pending", "t_plus_n": None, "confidence": confidence}
    try:
        with _get_conn() as conn:
            if conn is None:
                return None
            cur = conn.cursor()
            _ensure_schema(cur, conn)
            cur.execute(
                "INSERT INTO qd_agent_cases (case_id, root_id, task_summary, level, tags,"
                " plan_digest, outcome, failure_modes, cost, embedding)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING case_id",
                (case_id, root_id, summary, str(level or "")[:4],
                 json.dumps(list(tags or []), ensure_ascii=False),
                 json.dumps(list(plan_digest or []), ensure_ascii=False, default=str),
                 json.dumps(outcome, ensure_ascii=False),
                 json.dumps(list(failure_modes or []), ensure_ascii=False),
                 json.dumps(dict(cost or {}), ensure_ascii=False, default=str),
                 json.dumps(emb) if emb else None))
            cur.close()
            conn.commit()
        logger.info("[CaseMemory] 案例入库 %s (root=%s level=%s)",
                    case_id, root_id, level or "?")
        return case_id
    except Exception as e:
        _warn_once("record", "案例入库失败（主链不受影响）：%s" % e)
        return None


def backfill_by_root(root_id: int, correct: Optional[bool],
                     t_plus_n: Optional[float] = None) -> int:
    """延迟标签回填：T+N 定论后把 pending 改写为 correct/incorrect（chain/store 调用）。

    只回填 **label=pending** 的行（人工反馈/已定论不覆盖——③反馈保护同款语义）。
    correct=None（未定论）不动作。Returns: 回填行数。
    """
    if not case_memory_enabled() or root_id is None or correct is None:
        return 0
    label = "correct" if correct else "incorrect"
    try:
        with _get_conn() as conn:
            if conn is None:
                return 0
            cur = conn.cursor()
            cur.execute(
                "UPDATE qd_agent_cases SET outcome = jsonb_set(jsonb_set(outcome,"
                " '{label}', to_jsonb(%s::text)), '{t_plus_n}', to_jsonb(%s::float8)),"
                " updated_at = NOW()"
                " WHERE root_id = %s AND outcome->>'label' = 'pending'",
                (label, t_plus_n, root_id))
            n = cur.rowcount
            cur.close()
            conn.commit()
        if n:
            logger.info("[CaseMemory] 延迟标签回填 root=%s → %s (%d 行)", root_id, label, n)
        return n
    except Exception as e:
        _warn_once("backfill", "案例标签回填失败：%s" % e)
        return 0


# ═══════════════════════════════════════════════════════════════
#  检索 + 注入
# ═══════════════════════════════════════════════════════════════

def retrieve_cases(query: str, top_k: int = 3) -> List[dict]:
    """检索相似案例（加权 + 近重复去重 + 阈值过滤）。无命中返回 []（宁缺毋滥）。

    返回行：{case_id, task_summary, plan_digest, outcome, failure_modes, score, sim}。
    """
    if not case_memory_enabled() or not (query or "").strip():
        return []
    try:
        with _get_conn() as conn:
            if conn is None:
                return []
            cur = conn.cursor()
            _ensure_schema(cur, conn)
            cur.execute(
                "SELECT case_id, task_summary, plan_digest, outcome, failure_modes,"
                " embedding, created_at FROM qd_agent_cases"
                " ORDER BY created_at DESC LIMIT %s", (CASE_SCAN_LIMIT,))
            rows = cur.fetchall()
            cur.close()
    except Exception as e:
        _warn_once("retrieve", "案例检索失败（主链不受影响）：%s" % e)
        return []
    if not rows:
        return []

    q_vec = None
    try:
        embedder = _get_embedder()
        if embedder is not None:
            q_vec = embedder.embed_query(str(query)[:500])
    except Exception as e:
        logger.debug("[CaseMemory] query embedding 跳过: %s", e)

    scored: List[dict] = []
    for row in rows:
        (case_id, summary, digest, outcome, fmode, emb_json, created_at) = row
        outcome = outcome if isinstance(outcome, dict) else json.loads(outcome or "{}")
        label = str((outcome or {}).get("label") or "pending")
        if label == "incorrect":
            continue                                   # 硬排除：错误结论不许自我复制
        sim = 0.0
        if q_vec and emb_json:
            try:
                emb = json.loads(emb_json) if isinstance(emb_json, str) else emb_json
                sim = _cosine(q_vec, emb)
            except Exception:
                sim = 0.0
        if sim <= 0:
            sim = _lex_sim(query, summary)
        if sim < CASE_SIM_MIN:
            continue                                   # 宁缺毋滥
        weight = 1.0 if label == "correct" else _PENDING_WEIGHT
        digest = digest if isinstance(digest, list) else json.loads(digest or "[]")
        fmode = fmode if isinstance(fmode, list) else json.loads(fmode or "[]")
        scored.append({
            "case_id": case_id, "task_summary": summary, "plan_digest": digest,
            "outcome": outcome, "failure_modes": fmode, "sim": round(sim, 4),
            "score": round(sim * weight, 4), "label": label,
            "_created": str(created_at or ""),
        })

    scored.sort(key=lambda x: -x["score"])

    # 近重复（彼此 sim > CASE_DUP_SIM）只留最新且已验证的一条：
    # 按 score 序扫描，与已保留项近重复时仅在"后来者已验证、保留者未验证"时原位替换
    # （不迭代中改列表；原位替换保持 score 序）。
    kept: List[dict] = []
    for c in scored:
        dup_idx = None
        for _i, k in enumerate(kept):
            if _lex_sim(c["task_summary"], k["task_summary"]) > CASE_DUP_SIM:
                dup_idx = _i
                break
        if dup_idx is not None:
            if c["label"] == "correct" and kept[dup_idx]["label"] != "correct":
                kept[dup_idx] = c
        else:
            kept.append(c)
        if len(kept) >= top_k:
            break
    return kept[:top_k]


def format_case_injection(cases: Sequence[dict]) -> str:
    """案例 → planner 注入文本（严格两类语义，见头部"易错点"）。空/无案例返回 ""。"""
    if not cases:
        return ""
    lines = ["【历史相似案例——仅作参考，不要照搬结构】"]
    for c in cases:
        digest = c.get("plan_digest") or []
        tools = []
        for step in digest:
            for t in (step.get("tools") or []):
                if t not in tools:
                    tools.append(t)
        label = str((c.get("outcome") or {}).get("label") or "pending")
        summary = str(c.get("task_summary") or "")[:60]
        fmode = [str(f)[:60] for f in (c.get("failure_modes") or [])][:2]
        if label == "incorrect":
            # 失败案例：只给坑，绝不附修复路径
            line = '- 任务"%s"：校验错误，此路不通。' % summary
            if fmode:
                line += "失败坑：" + "；".join(fmode)
            lines.append(line)
            continue
        tag = "T+N 校验正确" if label == "correct" else "校验中（未定论）"
        line = '- 任务"%s"：%s。' % (summary, tag)
        if tools:
            line += "工具链骨架（候选，非处方）：" + " → ".join(tools[:6]) + "。"
        if fmode:
            line += "坑：" + "；".join(fmode) + "。"
        lines.append(line)
    lines.append("（以上为历史个案；市况/口径不同必须自行重新论证，不要照抄结论。）")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
#  酿造候选信号④（plan_digest 聚类 ≥N 次且 correct 率高）
# ═══════════════════════════════════════════════════════════════

def query_brew_case_signals(min_cluster: int = CASE_CLUSTER_MIN,
                            min_labeled: int = CASE_CLUSTER_MIN_LABELED,
                            min_win_rate: float = CASE_CLUSTER_WIN_RATE,
                            limit: int = 3) -> List[dict]:
    """案例聚类 → 酿造候选信号④（skill_brewer.brew_skills 的通道 3 消费）。

    聚类键 = plan_digest 的工具链签名（各阶段 tools 有序拼接）；门槛：
    出现 ≥ min_cluster 次、已定论 ≥ min_labeled、correct 率 ≥ min_win_rate。
    返回 [{chain_name, runs, win_rate, sample_root_id, _channel:"case"}]——
    chain_name/sample_root_id 从 qd_agent_traces 按 root_id 反查（酿造需要 run 树原料）。
    """
    if not case_memory_enabled():
        return []
    try:
        with _get_conn() as conn:
            if conn is None:
                return []
            cur = conn.cursor()
            _ensure_schema(cur, conn)
            cur.execute(
                "SELECT root_id, plan_digest, outcome FROM qd_agent_cases"
                " WHERE root_id IS NOT NULL ORDER BY created_at DESC LIMIT %s",
                (CASE_SCAN_LIMIT,))
            rows = cur.fetchall()
            if not rows:
                return []

            clusters: Dict[str, dict] = {}
            for root_id, digest, outcome in rows:
                digest = digest if isinstance(digest, list) else json.loads(digest or "[]")
                outcome = outcome if isinstance(outcome, dict) else json.loads(outcome or "{}")
                sig = "→".join(
                    "/".join(sorted(str(t) for t in (step.get("tools") or [])))
                    for step in digest if step.get("tools"))
                if not sig:
                    continue
                c = clusters.setdefault(sig, {"runs": 0, "labeled": 0, "correct": 0,
                                              "roots": []})
                c["runs"] += 1
                c["roots"].append(root_id)
                label = str((outcome or {}).get("label") or "pending")
                if label in ("correct", "incorrect"):
                    c["labeled"] += 1
                    c["correct"] += 1 if label == "correct" else 0

            out = []
            # chain_name / sample_root_id 需从 qd_agent_traces 反查（酿造要 run 树原料）；
            # 反查不到（root 已删）的簇不产候选——无原料的酿造候选是假信号。
            root_ids = sorted({r for c in clusters.values() for r in c["roots"]})
            name_by_root: Dict[int, str] = {}
            try:
                cur2 = conn.cursor()
                cur2.execute("SELECT id, name FROM qd_agent_traces WHERE id = ANY(%s)", (root_ids,))
                name_by_root = {int(r[0]): str(r[1] or "") for r in cur2.fetchall()}
                cur2.close()
            except Exception as e:
                _warn_once("signal4_lookup", "案例聚类 chain_name 反查失败：%s" % e)
                return []
            for sig, c in clusters.items():
                if c["runs"] < min_cluster or c["labeled"] < min_labeled:
                    continue
                wr = c["correct"] / float(c["labeled"])
                if wr < min_win_rate:
                    continue
                roots = [r for r in c["roots"] if name_by_root.get(int(r))
                         and "unknown" not in name_by_root[int(r)]]
                if not roots:
                    continue
                out.append({"signature": sig, "runs": c["runs"], "win_rate": round(wr, 4),
                            "chain_name": name_by_root[int(min(roots))],
                            "sample_root_id": min(roots), "_channel": "case"})
            out.sort(key=lambda x: (-x["runs"], -x["win_rate"]))
            return out[:limit]
    except Exception as e:
        _warn_once("signal4", "案例聚类信号查询失败：%s" % e)
        return []
