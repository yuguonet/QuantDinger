"""chain/evaluation_store.py — T+N 回评升级的 P0 观测存储（§10 四表）。

当前设计（只做口径与观测，不做内容提取、不改 prompt、不启用反馈）：

  qd_agent_experiences           经验目录（P0 建表但保持空，不读 skill 内容）
  qd_agent_experience_injection_events 请求级曝光 + 实验分配 + 决策快照
  qd_agent_evaluation_outcomes         唯一 prediction_id 上的不可变 outcome 版本
  qd_agent_experience_outcome_links    曝光 ↔ outcome 多对多归因连接
  qd_agent_decision_snapshots          决策生成时冻结的快照 + hash（决策/回评两种 scope）
  qd_agent_experience_stats            经验统计视图（落库列与 §5.4.2 一一对应）

  共享核心信封是各表实体列；金融专属字段（entry_fill / exit_fill / net_return /
  benchmark_return / excess_return / max_drawdown）只进 outcomes.adapter_data，
  不进共享列，也不给其他领域强加这些语义（§10.3）。

红线：
  - DDL 只能 additive 幂等 CREATE TABLE/INDEX IF NOT EXISTS，不改既有表、不碰
    migrations/；建表失败必须告警并返回失败，不返回"写入成功"的假结果。
  - get_db_connection 函数内惰性 import + fail-soft + _db() 可 monkeypatch。
  - 总开关 QD_EVAL_OBSERVATION_ENABLED（"0"/"false" 关闭 ⇒ 写侧全部 no-op）。
  - 任何异常都不影响主对话：所有写函数返回 None/False，不向上抛。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence

from ..log import logger
from .prediction_contract import (
    CONTRACT_VERSION as _CONTRACT_VERSION,
    SNAPSHOT_VERSION,
    build_snapshot as _build_snapshot,
    verify_snapshot as _verify_snapshot,
    validate_contract as _validate_contract,
    verdict_eligibility as _verdict_eligibility,
)

SCHEMA_VERSION = "tn-eval-p0-v2"
OUTCOME_POLICY_VERSION = "canonical-resolution-v1"


def outcome_policy_version_of(resolver_ver: Any = "") -> str:
    """outcome 策略版本 = 基础口径 + resolver/数据口径版本，避免同源不同版本混算。"""
    suffix = str(resolver_ver or "").strip()
    return OUTCOME_POLICY_VERSION if not suffix else "%s+%s" % (
        OUTCOME_POLICY_VERSION, suffix[:16])

_FINANCE_ONLY_KEYS = ("entry_fill", "exit_fill", "net_return",
                      "benchmark_return", "excess_return", "max_drawdown")

_DDL = """
CREATE TABLE IF NOT EXISTS qd_agent_experiences (
    experience_id        VARCHAR(64) NOT NULL,
    experience_version   VARCHAR(32) NOT NULL DEFAULT 'v1',
    experience_type      VARCHAR(32) NOT NULL DEFAULT 'general',
    normalized_content   JSONB NOT NULL DEFAULT '{}',
    content_sha          VARCHAR(64),
    source_kind          VARCHAR(32),
    source_ref           VARCHAR(128),
    source_version       VARCHAR(64),
    source_span_or_event TEXT,
    condition_key        VARCHAR(64),
    source_available_at  TIMESTAMPTZ,
    registered_at        TIMESTAMPTZ DEFAULT NOW(),
    evidence_status      VARCHAR(16) NOT NULL DEFAULT 'candidate',
    consumption_mode     VARCHAR(16) NOT NULL DEFAULT 'observe',
    parent_experience_id VARCHAR(64),
    split_rule           TEXT,
    domain               VARCHAR(32),
    domain_schema_version VARCHAR(32),
    created_at           TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (experience_id, experience_version)
);
CREATE INDEX IF NOT EXISTS idx_qd_exp_status  ON qd_agent_experiences (evidence_status);
CREATE INDEX IF NOT EXISTS idx_qd_exp_source  ON qd_agent_experiences (source_kind, source_ref);
CREATE INDEX IF NOT EXISTS idx_qd_exp_cond    ON qd_agent_experiences (condition_key);

CREATE TABLE IF NOT EXISTS qd_agent_experience_injection_events (
    event_id           VARCHAR(64) PRIMARY KEY,
    request_id         VARCHAR(80),
    session_id         VARCHAR(80),
    decision_id        BIGINT,
    trace_root_id      BIGINT,
    subject            VARCHAR(128),
    subject_kind       VARCHAR(16),
    horizon            VARCHAR(8),
    prediction_target  VARCHAR(64),
    experiment_arm     VARCHAR(32),
    assignment_prob    REAL,
    exposure_stage     VARCHAR(24),
    experience_ids     JSONB NOT NULL DEFAULT '[]',
    exposure_items     JSONB NOT NULL DEFAULT '[]',
    bundle_id          VARCHAR(64),
    bundle_sha         VARCHAR(64),
    prompt_sha         VARCHAR(64),
    tool_chain_sha     VARCHAR(64),
    model_name         VARCHAR(100),
    skill_id           VARCHAR(100),
    skill_version      VARCHAR(80),
    token_budget       INTEGER,
    position           INTEGER,
    domain             VARCHAR(32),
    snapshot_version   VARCHAR(32),
    created_at         TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_qd_inj_decision ON qd_agent_experience_injection_events (decision_id);
CREATE INDEX IF NOT EXISTS idx_qd_inj_created  ON qd_agent_experience_injection_events (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_qd_inj_bundle   ON qd_agent_experience_injection_events (bundle_sha);

CREATE TABLE IF NOT EXISTS qd_agent_evaluation_outcomes (
    outcome_id              VARCHAR(80) PRIMARY KEY,
    prediction_id           VARCHAR(256) NOT NULL,
    decision_id             BIGINT,
    claim_id                BIGINT,
    resolution_id           BIGINT,
    outcome_policy_version  VARCHAR(32) NOT NULL,
    label_rule_version      VARCHAR(32),
    snapshot_version        VARCHAR(32),
    experiment_arm          VARCHAR(32),
    domain                  VARCHAR(32),
    domain_schema_version   VARCHAR(32),
    subject                 VARCHAR(128),
    subject_kind            VARCHAR(16),
    horizon                 VARCHAR(8),
    as_of                   DATE,
    data_as_of              DATE,
    outcome_available_at    TIMESTAMPTZ,
    resolved_at             TIMESTAMPTZ,
    prediction_target       VARCHAR(64),
    target_event            TEXT,
    forecast_capture_status VARCHAR(24),
    probability_source      VARCHAR(32),
    probability_target      VARCHAR(64),
    probability_horizon     VARCHAR(8),
    coverage_gap            JSONB NOT NULL DEFAULT '[]',
    condition_key           VARCHAR(64),
    sample_layer            VARCHAR(24),
    model_name              VARCHAR(100),
    prompt_sha              VARCHAR(64),
    skill_id                VARCHAR(100),
    skill_version           VARCHAR(80),
    tool_chain_sha          VARCHAR(64),
    experience_bundle_id    VARCHAR(64),
    experience_bundle_sha   VARCHAR(64),
    experience_injection_ids JSONB NOT NULL DEFAULT '[]',
    experience_assignment   JSONB NOT NULL DEFAULT '{}',
    predicted_probability   REAL,
    predicted_direction     VARCHAR(16),
    verdict                 VARCHAR(24),
    verdict_score           REAL,
    brier_score             REAL,
    execution_status        VARCHAR(24),
    data_quality            VARCHAR(24),
    actual_windows          JSONB NOT NULL DEFAULT '{}',
    deviation_windows       JSONB NOT NULL DEFAULT '{}',
    adapter_data            JSONB NOT NULL DEFAULT '{}',
    canonical               BOOLEAN NOT NULL DEFAULT TRUE,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (prediction_id, outcome_policy_version)
);
CREATE INDEX IF NOT EXISTS idx_qd_out_decision ON qd_agent_evaluation_outcomes (decision_id);
CREATE INDEX IF NOT EXISTS idx_qd_out_claim    ON qd_agent_evaluation_outcomes (claim_id);
CREATE INDEX IF NOT EXISTS idx_qd_out_verdict  ON qd_agent_evaluation_outcomes (verdict, resolved_at DESC);
CREATE INDEX IF NOT EXISTS idx_qd_out_canonical ON qd_agent_evaluation_outcomes (prediction_id, canonical);

CREATE TABLE IF NOT EXISTS qd_agent_experience_outcome_links (
    link_id                BIGSERIAL PRIMARY KEY,
    event_id               VARCHAR(64) NOT NULL,
    outcome_id             VARCHAR(80) NOT NULL,
    experience_id          VARCHAR(64),
    eligible_for_attribution BOOLEAN NOT NULL DEFAULT TRUE,
    exclusion_reason       VARCHAR(64),
    experiment_arm         VARCHAR(32),
    assignment_prob        REAL,
    created_at             TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (event_id, outcome_id)
);
CREATE INDEX IF NOT EXISTS idx_qd_link_outcome ON qd_agent_experience_outcome_links (outcome_id);
CREATE INDEX IF NOT EXISTS idx_qd_link_exp     ON qd_agent_experience_outcome_links (experience_id);

CREATE TABLE IF NOT EXISTS qd_agent_decision_snapshots (
    snapshot_id       VARCHAR(96) PRIMARY KEY,
    scope             VARCHAR(16) NOT NULL,
    ref_id            VARCHAR(80) NOT NULL,
    decision_id       BIGINT,
    claim_id          BIGINT,
    snapshot_sha      VARCHAR(64) NOT NULL,
    snapshot_version  VARCHAR(32),
    contract_version  VARCHAR(64),
    domain            VARCHAR(32),
    snapshot          JSONB NOT NULL DEFAULT '{}',
    created_at        TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_qd_snap_decision ON qd_agent_decision_snapshots (decision_id, scope);
CREATE INDEX IF NOT EXISTS idx_qd_snap_ref      ON qd_agent_decision_snapshots (scope, ref_id);

CREATE TABLE IF NOT EXISTS qd_agent_experience_stats (
    experience_id               VARCHAR(64) NOT NULL,
    experience_version          VARCHAR(32) NOT NULL DEFAULT 'v1',
    eligible_exposures          INTEGER NOT NULL DEFAULT 0,
    actual_injections           INTEGER NOT NULL DEFAULT 0,
    matured_outcomes            INTEGER NOT NULL DEFAULT 0,
    prediction_eligible_outcomes INTEGER NOT NULL DEFAULT 0,
    execution_eligible_outcomes INTEGER NOT NULL DEFAULT 0,
    excluded_outcomes           INTEGER NOT NULL DEFAULT 0,
    exclusion_reasons           JSONB NOT NULL DEFAULT '[]',
    success_count               INTEGER NOT NULL DEFAULT 0,
    failure_count               INTEGER NOT NULL DEFAULT 0,
    partial_count               INTEGER NOT NULL DEFAULT 0,
    prediction_success_rate     REAL,
    brier_score                 REAL,
    calibration_error           REAL,
    applicability_conditions    JSONB NOT NULL DEFAULT '[]',
    confidence_interval         JSONB,
    effective_weight            REAL,
    weight_policy_version       VARCHAR(64),
    last_matured_at             TIMESTAMPTZ,
    updated_at                  TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (experience_id, experience_version)
);
"""

_schema_ok = False
_schema_lock = threading.Lock()
_schema_error = ""


def enabled() -> bool:
    return (os.getenv("QD_EVAL_OBSERVATION_ENABLED") or "1").strip().lower() \
        not in ("0", "false", "no", "off")


def _db():
    from app.utils.db import get_db_connection
    return get_db_connection()


def _warn(msg: str, *args) -> None:
    try:
        logger.warning("[EvalStore] " + msg, *args)
    except Exception:
        pass


def _debug(msg: str, *args) -> None:
    try:
        logger.debug("[EvalStore] " + msg, *args)
    except Exception:
        pass


def schema_status() -> Dict[str, Any]:
    return {"ok": bool(_schema_ok), "version": SCHEMA_VERSION,
            "error": _schema_error, "enabled": enabled()}


def ensure_schema(force: bool = False) -> bool:
    global _schema_ok, _schema_error
    if _schema_ok and not force:
        return True
    with _schema_lock:
        if _schema_ok and not force:
            return True
        try:
            with _db() as conn:
                cur = conn.cursor()
                cur.execute(_DDL)
                conn.commit()
                cur.close()
            _schema_ok = True
            _schema_error = ""
            return True
        except Exception as e:
            _schema_ok = False
            _schema_error = "%s: %s" % (type(e).__name__, e)
            _warn("建表失败（四表未就绪，写侧跳过；不返回假成功）: %s", _schema_error)
            return False


def _need_db() -> bool:
    return enabled() and ensure_schema()


def _s(value: Any, limit: int) -> Optional[str]:
    if value is None:
        return None
    return str(value)[:limit]


def _j(value: Any) -> str:
    return json.dumps(value if value is not None else [], ensure_ascii=False, default=str)


def fingerprint(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:32]


def prediction_id_of(*, decision_id: Any, claim_id: Any, subject: Any,
                     prediction_target: Any, horizon: Any) -> str:
    parts = [str(x or "") for x in (decision_id, claim_id, subject,
                                    prediction_target, horizon)]
    return ":".join(parts)[:256]


def record_exposure(*, request_id: str = "", session_id: str = "",
                    decision_id: Optional[int] = None,
                    trace_root_id: Optional[int] = None,
                    subject: str = "", subject_kind: str = "",
                    horizon: str = "", prediction_target: str = "",
                    experiment_arm: str = "", assignment_prob: Optional[float] = None,
                    exposure_stage: str = "", experience_ids: Sequence[str] = (),
                    exposure_items: Sequence[dict] = (),
                    bundle_id: str = "", bundle_sha: str = "",
                    prompt_sha: str = "", tool_chain_sha: str = "",
                    model_name: str = "", skill_id: str = "",
                    skill_version: str = "", token_budget: Optional[int] = None,
                    position: Optional[int] = None, domain: str = "") -> Optional[str]:
    if not _need_db():
        return None
    event_id = uuid.uuid4().hex
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO qd_agent_experience_injection_events
                    (event_id, request_id, session_id, decision_id, trace_root_id,
                     subject, subject_kind, horizon, prediction_target,
                     experiment_arm, assignment_prob, exposure_stage,
                     experience_ids, exposure_items, bundle_id, bundle_sha,
                     prompt_sha, tool_chain_sha, model_name, skill_id, skill_version,
                     token_budget, position, domain, snapshot_version)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (event_id) DO NOTHING
            """, (event_id, _s(request_id, 80), _s(session_id, 80), decision_id,
                  trace_root_id, _s(subject, 128), _s(subject_kind, 16),
                  _s(horizon, 8), _s(prediction_target, 64),
                  _s(experiment_arm, 32), assignment_prob,
                  _s(exposure_stage, 24), _j(list(experience_ids or [])),
                  _j(list(exposure_items or [])), _s(bundle_id, 64),
                  _s(bundle_sha, 64), _s(prompt_sha, 64),
                  _s(tool_chain_sha, 64), _s(model_name, 100),
                  _s(skill_id, 100), _s(skill_version, 80), token_budget,
                  position, _s(domain, 32), SNAPSHOT_VERSION))
            conn.commit()
            cur.close()
        return event_id
    except Exception as e:
        _debug("曝光事件写入跳过（fail-soft）: %s: %s", type(e).__name__, e)
        return None


def register_experience(*, experience_id: str, experience_type: str = "general",
                        normalized_content: Optional[dict] = None,
                        content_sha: str = "", source_kind: str = "",
                        source_ref: str = "", source_version: str = "",
                        source_span_or_event: str = "", condition_key: str = "",
                        domain: str = "", extractor_ver: str = "",
                        evidence_status: str = "candidate",
                        consumption_mode: str = "observe",
                        experience_version: str = "v1") -> bool:
    """登记一条 candidate 经验（1118 P0-4 前端捕获写侧）。

    幂等键 = (experience_id, experience_version)，重复捕获同一来源内容不新增行。
    一律 candidate / observe —— 前端捕获只证明"内容进了上下文"，不证明有效。
    fail-soft：任何异常返回 False，不影响主对话。
    """
    if not _need_db() or not experience_id:
        return False
    if evidence_status not in ("candidate", "validated", "retired"):
        evidence_status = "candidate"
    if consumption_mode not in ("observe", "shadow", "active"):
        consumption_mode = "observe"
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO qd_agent_experiences
                    (experience_id, experience_version, experience_type,
                     normalized_content, content_sha, source_kind, source_ref,
                     source_version, source_span_or_event, condition_key,
                     evidence_status, consumption_mode, domain, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                ON CONFLICT (experience_id, experience_version) DO NOTHING
            """, (_s(experience_id, 64), _s(experience_version, 32),
                  _s(experience_type, 32), _j(normalized_content or {}),
                  _s(content_sha, 64), _s(source_kind, 32), _s(source_ref, 128),
                  _s(source_version, 64), _s(source_span_or_event, 128),
                  _s(condition_key, 64), evidence_status, consumption_mode,
                  _s(domain, 32)))
            conn.commit()
            cur.close()
        return True
    except Exception as e:
        _debug("经验登记跳过（fail-soft）: %s: %s", type(e).__name__, e)
        return False


def save_decision_snapshot(*, decision_id: int, snapshot: Dict[str, Any],
                           domain: str = "", scope: str = "decision",
                           ref_id: Any = None, claim_id: Any = None,
                           contract_version: str = "") -> Optional[str]:
    """冻结快照落库（决策时/回评时两种 scope）；幂等、fail-soft、不覆盖旧行。"""
    if not _need_db() or not decision_id or not isinstance(snapshot, dict):
        return None
    sha = str(snapshot.get("snapshot_sha") or "")
    if not sha:
        return None
    sid = "%s:%s" % (scope, ref_id if ref_id is not None else decision_id)
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO qd_agent_decision_snapshots
                    (snapshot_id, scope, ref_id, decision_id, claim_id,
                     snapshot_sha, snapshot_version, contract_version,
                     domain, snapshot)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (snapshot_id) DO NOTHING RETURNING snapshot_id
            """, (sid[:96], _s(scope, 16), _s(str(ref_id if ref_id is not None
                                                else decision_id), 80),
                  int(decision_id), claim_id, sha[:64],
                  _s(snapshot.get("snapshot_version"), 32),
                  _s(contract_version, 64), _s(domain, 32), _j(snapshot)))
            row = cur.fetchone()
            conn.commit()
            cur.close()
        return str(row["snapshot_id"]) if row else None
    except Exception as e:
        _debug("决策快照落库跳过（fail-soft）: %s: %s", type(e).__name__, e)
        return None


def load_decision_snapshot(decision_id: int) -> Optional[Dict[str, Any]]:
    """读决策时冻结的快照；不存在/损坏返回 None，绝不重建后冒充原快照。"""
    if not _need_db() or not decision_id:
        return None
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT snapshot FROM qd_agent_decision_snapshots
                 WHERE scope = 'decision' AND decision_id = %s
                 ORDER BY created_at DESC LIMIT 1
            """, (int(decision_id),))
            row = cur.fetchone()
            cur.close()
        if not row:
            return None
        snap = row["snapshot"] if isinstance(row, dict) else row[0]
        if isinstance(snap, str):
            snap = json.loads(snap)
        return snap if isinstance(snap, dict) else None
    except Exception as e:
        _debug("决策快照读取跳过（fail-soft）: %s: %s", type(e).__name__, e)
        return None


def recompute_experience_stats(experience_id: str,
                               experience_version: str = "v1", *,
                               applicability: Optional[str] = None) -> bool:
    """按曝光/link/outcome 重算 §5.4.2 统计列并幂等落库；任一异常 fail-soft。"""
    if not _need_db() or not experience_id:
        return False
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT count(*) AS eligible,
                       count(*) FILTER (WHERE exposure_stage = 'injected') AS injected
                  FROM qd_agent_experience_injection_events
                 WHERE experience_ids @> %s::jsonb
            """, (_j([experience_id]),))
            exp_row = cur.fetchone()
            cur.execute("""
                SELECT count(*) FILTER (WHERE o.verdict <> '') AS matured,
                       count(*) FILTER (WHERE o.sample_layer = 'learning') AS pred_ok,
                       count(*) FILTER (WHERE o.forecast_capture_status = 'resolved') AS exec_ok,
                       count(*) FILTER (WHERE o.forecast_capture_status = 'excluded') AS excluded,
                       count(*) FILTER (WHERE o.verdict = 'hit') AS hits,
                       count(*) FILTER (WHERE o.verdict = 'miss') AS misses,
                       count(*) FILTER (WHERE o.verdict = 'partial') AS partials,
                       avg(o.brier_score) FILTER (WHERE o.brier_score IS NOT NULL) AS brier,
                       avg(abs(o.predicted_probability - o.verdict_score))
                           FILTER (WHERE o.predicted_probability IS NOT NULL
                                     AND o.verdict_score IS NOT NULL) AS cal_err,
                       max(o.resolved_at) AS last_matured,
                       coalesce(jsonb_agg(DISTINCT l.exclusion_reason)
                                FILTER (WHERE l.exclusion_reason IS NOT NULL),
                                '[]'::jsonb) AS reasons
                  FROM qd_agent_experience_outcome_links l
                  JOIN qd_agent_evaluation_outcomes o ON o.outcome_id = l.outcome_id
                 WHERE l.experience_id = %s
            """, (experience_id,))
            out_row = cur.fetchone()
            cur.execute("""
                SELECT source_kind, condition_key FROM qd_agent_experiences
                 WHERE experience_id = %s AND experience_version = %s
            """, (experience_id, experience_version))
            meta = cur.fetchone()
            cur.close()
        e = dict(exp_row) if exp_row else {}
        o = dict(out_row) if out_row else {}
        m = dict(meta) if meta else {}
        hits, misses, partials = (int(o.get("hits") or 0),
                                  int(o.get("misses") or 0),
                                  int(o.get("partials") or 0))
        denom = hits + misses
        rate = (hits / denom) if denom else None
        weight, wver = None, None
        if applicability is not None:
            try:
                from . import effective_weight as _ew
                r = _ew.compute(
                    {"success_count": hits, "failure_count": misses,
                     "partial_count": partials,
                     "matured_attributable": int(o.get("matured") or 0),
                     "n_clusters": int(o.get("matured") or 0),
                     "prediction_eligible": int(o.get("pred_ok") or 0)},
                    source_kind=str(m.get("source_kind") or ""),
                    applicability=str(applicability))
                weight, wver = r.get("weight"), r.get("weight_policy_version")
            except Exception as e2:
                _debug("经验权重计算跳过（fail-soft）: %s", e2)
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO qd_agent_experience_stats
                    (experience_id, experience_version, eligible_exposures,
                     actual_injections, matured_outcomes,
                     prediction_eligible_outcomes, execution_eligible_outcomes,
                     excluded_outcomes, exclusion_reasons, success_count,
                     failure_count, partial_count, prediction_success_rate,
                     brier_score, calibration_error, applicability_conditions,
                     confidence_interval, effective_weight, weight_policy_version,
                     last_matured_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (experience_id, experience_version) DO UPDATE SET
                    eligible_exposures = EXCLUDED.eligible_exposures,
                    actual_injections = EXCLUDED.actual_injections,
                    matured_outcomes = EXCLUDED.matured_outcomes,
                    prediction_eligible_outcomes = EXCLUDED.prediction_eligible_outcomes,
                    execution_eligible_outcomes = EXCLUDED.execution_eligible_outcomes,
                    excluded_outcomes = EXCLUDED.excluded_outcomes,
                    exclusion_reasons = EXCLUDED.exclusion_reasons,
                    success_count = EXCLUDED.success_count,
                    failure_count = EXCLUDED.failure_count,
                    partial_count = EXCLUDED.partial_count,
                    prediction_success_rate = EXCLUDED.prediction_success_rate,
                    brier_score = EXCLUDED.brier_score,
                    calibration_error = EXCLUDED.calibration_error,
                    confidence_interval = EXCLUDED.confidence_interval,
                    effective_weight = EXCLUDED.effective_weight,
                    weight_policy_version = EXCLUDED.weight_policy_version,
                    last_matured_at = EXCLUDED.last_matured_at,
                    updated_at = NOW()
                RETURNING experience_id
            """, (experience_id, _s(experience_version, 32),
                  int(e.get("eligible") or 0), int(e.get("injected") or 0),
                  int(o.get("matured") or 0), int(o.get("pred_ok") or 0),
                  int(o.get("exec_ok") or 0), int(o.get("excluded") or 0),
                  _j(o.get("reasons") or []), hits, misses, partials, rate,
                  o.get("brier"), o.get("cal_err"),
                  _j([m.get("condition_key")] if m.get("condition_key") else []),
                  None, weight, _s(wver, 64), o.get("last_matured")))
            conn.commit()
            cur.close()
        return True
    except Exception as e:
        _debug("经验统计落库跳过（fail-soft）: %s: %s", type(e).__name__, e)
        return False


def latest_exposure_for_session(session_id: str) -> Optional[Dict[str, Any]]:
    """读本会话最近一次曝光事件，供决策时冻结 prompt/工具链/经验包版本。"""
    if not _need_db() or not session_id:
        return None
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT prompt_sha, tool_chain_sha, model_name, skill_id,
                       skill_version, bundle_id, bundle_sha, experience_ids,
                       experiment_arm, assignment_prob, exposure_stage, domain
                  FROM qd_agent_experience_injection_events
                 WHERE session_id = %s
                 ORDER BY created_at DESC LIMIT 1
            """, (_s(session_id, 80),))
            row = cur.fetchone()
            cur.close()
        if not row:
            return None
        d = dict(row) if isinstance(row, dict) else {}
        return {
            "prompt_sha": d.get("prompt_sha") or "",
            "tool_chain_sha": d.get("tool_chain_sha") or "",
            "model_name": d.get("model_name") or "",
            "skill_id": d.get("skill_id") or "",
            "skill_version": d.get("skill_version") or "",
            "experience_bundle_id": d.get("bundle_id") or "",
            "experience_bundle_sha": d.get("bundle_sha") or "",
            "experience_injection_ids": d.get("experience_ids") or [],
            "experiment_arm": d.get("experiment_arm") or "",
            "assignment_prob": d.get("assignment_prob"),
            "exposure_stage": d.get("exposure_stage") or "",
            "domain": d.get("domain") or "",
        }
    except Exception as e:
        _debug("曝光上下文读取跳过（fail-soft）: %s: %s", type(e).__name__, e)
        return None


def linked_experience_ids(outcome_id: str) -> List[str]:
    if not _need_db() or not outcome_id:
        return []
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT DISTINCT experience_id FROM qd_agent_experience_outcome_links
                 WHERE outcome_id = %s AND experience_id IS NOT NULL
            """, (outcome_id,))
            rows = cur.fetchall() or []
            cur.close()
        return [str(r["experience_id"]) if isinstance(r, dict) else str(r[0])
                for r in rows]
    except Exception as e:
        _debug("经验 link 反查跳过（fail-soft）: %s: %s", type(e).__name__, e)
        return []


def backfill_exposure_decision(event_id: str, decision_id: int) -> bool:
    if not _need_db() or not event_id or not decision_id:
        return False
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE qd_agent_experience_injection_events
                SET decision_id = %s WHERE event_id = %s AND decision_id IS NULL
            """, (int(decision_id), event_id))
            conn.commit()
            cur.close()
        return True
    except Exception as e:
        _debug("曝光事件回填 decision_id 跳过（fail-soft）: %s", e)
        return False


def upsert_outcome(*, decision_id: Any = None, claim_id: Any = None,
                   resolution_id: Any = None, subject: str = "",
                   subject_kind: str = "", horizon: str = "",
                   prediction_target: str = "", target_event: str = "",
                   label_rule_version: str = "", experiment_arm: str = "",
                   domain: str = "finance", domain_schema_version: str = "",
                   as_of: Optional[date] = None, data_as_of: Optional[date] = None,
                   outcome_available_at: Optional[datetime] = None,
                   resolved_at: Optional[datetime] = None,
                   forecast_capture_status: str = "", probability_source: str = "",
                   probability_target: str = "", probability_horizon: str = "",
                   coverage_gap: Sequence[str] = (), condition_key: str = "",
                   sample_layer: str = "", model_name: str = "",
                   prompt_sha: str = "", skill_id: str = "",
                   skill_version: str = "", tool_chain_sha: str = "",
                   experience_bundle_id: str = "", experience_bundle_sha: str = "",
                   experience_injection_ids: Sequence[str] = (),
                   experience_assignment: Optional[dict] = None,
                   predicted_probability: Optional[float] = None,
                   predicted_direction: str = "", verdict: str = "",
                   verdict_score: Optional[float] = None,
                   brier_score: Optional[float] = None,
                   execution_status: str = "", data_quality: str = "",
                   actual_windows: Optional[dict] = None,
                   deviation_windows: Optional[dict] = None,
                   adapter_data: Optional[dict] = None,
                   canonical: bool = True,
                   outcome_policy_version: str = OUTCOME_POLICY_VERSION) -> Optional[str]:
    if not _need_db():
        return None
    pid = prediction_id_of(decision_id=decision_id, claim_id=claim_id,
                           subject=subject, prediction_target=prediction_target,
                           horizon=horizon)
    oid = fingerprint(pid + "|" + str(outcome_policy_version or ""))
    ext = dict(adapter_data or {})
    ext = {k: v for k, v in ext.items() if k in _FINANCE_ONLY_KEYS}
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO qd_agent_evaluation_outcomes
                    (outcome_id, prediction_id, decision_id, claim_id, resolution_id,
                     outcome_policy_version, label_rule_version, snapshot_version,
                     experiment_arm, domain, domain_schema_version,
                     subject, subject_kind, horizon, as_of, data_as_of,
                     outcome_available_at, resolved_at, prediction_target, target_event,
                     forecast_capture_status, probability_source, probability_target,
                     probability_horizon, coverage_gap, condition_key, sample_layer,
                     model_name, prompt_sha, skill_id, skill_version, tool_chain_sha,
                     experience_bundle_id, experience_bundle_sha,
                     experience_injection_ids, experience_assignment,
                     predicted_probability, predicted_direction, verdict, verdict_score,
                     brier_score, execution_status, data_quality,
                     actual_windows, deviation_windows, adapter_data, canonical)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (prediction_id, outcome_policy_version) DO UPDATE SET
                    canonical = EXCLUDED.canonical,
                    resolved_at = COALESCE(EXCLUDED.resolved_at, qd_agent_evaluation_outcomes.resolved_at),
                    verdict = EXCLUDED.verdict,
                    verdict_score = EXCLUDED.verdict_score,
                    resolution_id = EXCLUDED.resolution_id,
                    forecast_capture_status = EXCLUDED.forecast_capture_status,
                    execution_status = EXCLUDED.execution_status,
                    sample_layer = EXCLUDED.sample_layer,
                    brier_score = EXCLUDED.brier_score,
                    data_as_of = EXCLUDED.data_as_of,
                    data_quality = EXCLUDED.data_quality,
                    coverage_gap = EXCLUDED.coverage_gap,
                    actual_windows = EXCLUDED.actual_windows,
                    adapter_data = EXCLUDED.adapter_data,
                    outcome_available_at = COALESCE(EXCLUDED.outcome_available_at, qd_agent_evaluation_outcomes.outcome_available_at)
                WHERE COALESCE(qd_agent_evaluation_outcomes.verdict, '') = ''
                  AND EXCLUDED.verdict IN ('hit', 'miss', 'partial')
                RETURNING outcome_id
            """, (oid, pid, decision_id, claim_id, resolution_id,
                  _s(outcome_policy_version, 32), _s(label_rule_version, 32),
                  SNAPSHOT_VERSION, _s(experiment_arm, 32), _s(domain, 32),
                  _s(domain_schema_version, 32), _s(subject, 128),
                  _s(subject_kind, 16), _s(horizon, 8), as_of, data_as_of,
                  outcome_available_at, resolved_at, _s(prediction_target, 64),
                  _s(target_event, 512), _s(forecast_capture_status, 24),
                  _s(probability_source, 32), _s(probability_target, 64),
                  _s(probability_horizon, 8), _j(list(coverage_gap or [])),
                  _s(condition_key, 64), _s(sample_layer, 24),
                  _s(model_name, 100), _s(prompt_sha, 64), _s(skill_id, 100),
                  _s(skill_version, 80), _s(tool_chain_sha, 64),
                  _s(experience_bundle_id, 64), _s(experience_bundle_sha, 64),
                  _j(list(experience_injection_ids or [])),
                  _j(experience_assignment or {}), predicted_probability,
                  _s(predicted_direction, 16), _s(verdict, 24), verdict_score,
                  brier_score, _s(execution_status, 24), _s(data_quality, 24),
                  _j(actual_windows or {}), _j(deviation_windows or {}),
                  _j(ext), bool(canonical)))
            conn.commit()
            cur.close()
        return oid
    except Exception as e:
        _debug("outcome 写入跳过（fail-soft）: %s: %s", type(e).__name__, e)
        return None


def link_outcomes(*, event_id: str, outcome_ids: Sequence[str],
                  experience_id: str = "",
                  eligible_for_attribution: bool = True,
                  exclusion_reason: str = "", experiment_arm: str = "",
                  assignment_prob: Optional[float] = None) -> int:
    if not _need_db() or not event_id or not outcome_ids:
        return 0
    written = 0
    try:
        with _db() as conn:
            cur = conn.cursor()
            for oid in dict.fromkeys(str(x) for x in outcome_ids if x):
                cur.execute("""
                    INSERT INTO qd_agent_experience_outcome_links
                        (event_id, outcome_id, experience_id,
                         eligible_for_attribution, exclusion_reason,
                         experiment_arm, assignment_prob)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (event_id, outcome_id) DO UPDATE SET
                        experience_id = COALESCE(
                            qd_agent_experience_outcome_links.experience_id,
                            EXCLUDED.experience_id),
                        eligible_for_attribution = EXCLUDED.eligible_for_attribution,
                        exclusion_reason = EXCLUDED.exclusion_reason
                RETURNING link_id
                """, (event_id, oid, _s(experience_id, 64),
                      bool(eligible_for_attribution),
                      _s(exclusion_reason, 64), _s(experiment_arm, 32),
                      assignment_prob))
                written += int(cur.rowcount > 0)
            conn.commit()
            cur.close()
        return written
    except Exception as e:
        _debug("outcome link 写入跳过（fail-soft）: %s: %s", type(e).__name__, e)
        return written


def backfill_decision_by_session(session_id: str, decision_id: int,
                                 prompt_sha: str = "") -> int:
    if not _need_db() or not session_id or not decision_id:
        return 0
    try:
        with _db() as conn:
            cur = conn.cursor()
            if prompt_sha:
                cur.execute("""
                    UPDATE qd_agent_experience_injection_events
                    SET decision_id = %s
                     WHERE session_id = %s AND prompt_sha = %s AND decision_id IS NULL
                """, (int(decision_id), _s(session_id, 80), _s(prompt_sha, 64)))
            else:
                cur.execute("""
                    UPDATE qd_agent_experience_injection_events
                    SET decision_id = %s
                     WHERE event_id = (
                        SELECT event_id FROM qd_agent_experience_injection_events
                         WHERE session_id = %s AND decision_id IS NULL
                         ORDER BY created_at DESC LIMIT 1)
                """, (int(decision_id), _s(session_id, 80)))
            n = cur.rowcount
            conn.commit()
            cur.close()
        return int(n or 0)
    except Exception as e:
        _debug("曝光事件按 session 回填跳过（fail-soft）: %s", e)
        return 0


def shadow_experience_ids(items: Any) -> set:
    """从曝光条目列表提取 shadow 注入的经验 ID 集合（纯函数）。"""
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except ValueError:
            return set()
    out = set()
    for it in items or []:
        if isinstance(it, dict) \
                and str(it.get("kind") or "") == "experience_shadow":
            eid = str(it.get("experience_id") or "")
            if eid:
                out.add(eid)
    return out


def sync_outcome_from_resolution(*, claim_id: int, resolution_id: int,
                                 verdict: str = "", verdict_score: Optional[float] = None,
                                 actual: Optional[dict] = None,
                                 actual_as_of: Optional[date] = None,
                                 resolved_at: Optional[datetime] = None,
                                 resolver_ver: str = "",
                                 data_source: str = "",
                                 as_of_override: Optional[date] = None) -> Optional[str]:
    if not _need_db() or not claim_id or not resolution_id:
        return None
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT c.id AS claim_id, c.decision_id, c.subject, c.subject_kind,
                       c.horizon, c.predicted, c.confidence, c.source_quote,
                       d.domain, d.model, d.trace_root_id, d.created_at AS dec_created
                  FROM qd_agent_claims c
                  JOIN qd_agent_decisions d ON d.id = c.decision_id
                 WHERE c.id = %s
            """, (int(claim_id),))
            row = cur.fetchone()
            cur.close()
        if not row:
            return None
        r = dict(row) if isinstance(row, dict) else {
            "claim_id": row[0], "decision_id": row[1], "subject": row[2],
            "subject_kind": row[3], "horizon": row[4], "predicted": row[5],
            "confidence": row[6], "source_quote": row[7], "domain": row[8],
            "model": row[9], "trace_root_id": row[10], "dec_created": row[11]}
        predicted = r.get("predicted") or {}
        if not isinstance(predicted, dict):
            predicted = {}
        fw = dict(actual or {}) if isinstance(actual, dict) else {}
        finance = {k: fw[k] for k in _FINANCE_ONLY_KEYS if k in fw}
        coverage = []
        if str(verdict or "") in ("undecidable", "data_missing"):
            coverage.append(str(verdict))
        truncated = bool(fw.get("truncated") or (actual or {}).get("truncated")) \
            if isinstance(actual, dict) else False
        if truncated and "truncated" not in coverage:
            coverage.append("truncated")
        # 概率契约（1118 §5.3）：版本化校验器是唯一口径真源。
        # confidence 是把握度映射，不得冒充概率（§6.1）。
        # 旧 tag 名并写 —— coverage 漏斗与既有报表仍按旧名取数。
        dec_date = (r.get("dec_created").date()
                    if hasattr(r.get("dec_created"), "date") else None)
        if as_of_override is not None:
            dec_date = as_of_override
        snap = _build_snapshot(
            decision_id=r.get("decision_id"), claim_id=claim_id,
            subject=r.get("subject"), subject_kind=r.get("subject_kind"),
            horizon=r.get("horizon"), prediction_target="direction",
            as_of=dec_date, label_rule_version=OUTCOME_POLICY_VERSION,
            model_name=str(r.get("model") or ""),
            probability_source=predicted.get("p_source"),
            probability_target=predicted.get("p_target"),
            probability_horizon=predicted.get("p_horizon"),
            predicted_probability=predicted.get("p"),
        )
        frozen = load_decision_snapshot(int(r.get("decision_id") or 0))
        if frozen and _verify_snapshot(frozen):
            for candidate in (frozen.get("claim_snapshots") or []):
                if (isinstance(candidate, dict)
                        and int(candidate.get("claim_id") or 0) == int(claim_id)
                        and _verify_snapshot(candidate)):
                    snap = candidate
                    break
        from . import domain_adapter as _da
        try:
            adapt = _da.validate(str(r.get("domain") or "finance"),
                                 resolver_ver, _da.load_config(), snapshot=snap)
        except Exception as e:
            adapt = {"ok": False, "reason": "adapter_unavailable:%s" % type(e).__name__,
                     "coverage_gap": ["adapter_unavailable"]}
        for gap in adapt.get("coverage_gap") or []:
            if gap not in coverage:
                coverage.append(gap)
        contract = _validate_contract(snap)
        for g in contract.get("coverage_gap") or []:
            if g == "p_invalid" and "p_out_of_range" not in coverage:
                coverage.append("p_out_of_range")
            if g.startswith("probability_target_mismatch") \
                    and "horizon:" in g and "horizon_mismatch" not in coverage:
                coverage.append("horizon_mismatch")
            if g not in coverage:
                coverage.append(g)
        p_val = contract.get("probability_value")
        prob_source = contract.get("probability_source_clean") or ""
        p_target = str(predicted.get("p_target") or "")
        p_horizon = str(predicted.get("p_horizon") or "")
        eligibility = _verdict_eligibility(contract, str(verdict or ""))
        adapter_ok = bool(adapt.get("ok"))
        data_ok = adapter_ok and not truncated
        brier = None
        if data_ok and eligibility.get("brier_eligible") and verdict_score is not None:
            try:
                brier = (p_val - float(verdict_score)) ** 2
            except (TypeError, ValueError):
                coverage.append("brier_calc_failed")
        if p_val is None and str(verdict or "") in ("hit", "miss", "partial"):
            coverage.append("no_probability")
        oid = upsert_outcome(
            decision_id=r.get("decision_id"), claim_id=claim_id,
            resolution_id=resolution_id, subject=str(r.get("subject") or ""),
            subject_kind=str(r.get("subject_kind") or ""),
            horizon=str(r.get("horizon") or ""),
            prediction_target="direction", domain=str(r.get("domain") or "finance"),
            domain_schema_version=_s(resolver_ver, 32),
            as_of=dec_date,
            data_as_of=actual_as_of, resolved_at=resolved_at,
            outcome_available_at=resolved_at,
            forecast_capture_status=(
                "resolved" if data_ok and eligibility.get(
                    "prediction_verdict") in ("hit", "miss", "partial")
                else "excluded"),
            sample_layer=("learning" if data_ok and eligibility.get(
                "learning_eligibility") else "prediction_only"),
            probability_source=prob_source,
            probability_target=p_target if p_val is not None else "",
            probability_horizon=p_horizon if p_val is not None else "",
            coverage_gap=coverage, model_name=str(r.get("model") or ""),
            predicted_probability=p_val,
            predicted_direction=str(predicted.get("dir") or ""),
            verdict=str(verdict or "") if data_ok else "",
            verdict_score=verdict_score if data_ok else None,
            brier_score=brier,
            execution_status="evaluated" if data_ok else "excluded",
            data_quality=str(data_source or ""),
            actual_windows={"values": fw}, adapter_data=finance, canonical=True,
            outcome_policy_version=outcome_policy_version_of(resolver_ver),
            label_rule_version=OUTCOME_POLICY_VERSION)
        if oid:
            inst = None
            try:
                with _db() as conn:
                    cur = conn.cursor()
                    cur.execute("""
                        SELECT event_id, experience_ids, exposure_items
                          FROM qd_agent_experience_injection_events
                         WHERE decision_id = %s ORDER BY created_at DESC LIMIT 1
                    """, (int(r.get("decision_id") or 0),))
                    ir = cur.fetchone()
                    cur.close()
                if isinstance(ir, dict):
                    inst = ir.get("event_id")
                    raw_ids = ir.get("experience_ids")
                    raw_items = ir.get("exposure_items")
                else:
                    inst = ir[0] if ir else None
                    raw_ids = ir[1] if ir else None
                    raw_items = ir[2] if ir else None
                if isinstance(raw_ids, str):
                    try:
                        raw_ids = json.loads(raw_ids)
                    except ValueError:
                        raw_ids = []
                if isinstance(raw_items, str):
                    try:
                        raw_items = json.loads(raw_items)
                    except ValueError:
                        raw_items = []
                exp_ids = [str(x) for x in (raw_ids or []) if x]
            except Exception as e:
                _debug("曝光事件反查跳过（fail-soft）: %s", e)
                exp_ids = []
                raw_items = []
            _shadow_ids = shadow_experience_ids(raw_items)
            _attributed = next((x for x in exp_ids if x in _shadow_ids), "")
            if not _attributed and exp_ids:
                _attributed = exp_ids[0]
            _arm = "shadow" if _attributed in _shadow_ids else ""
            if inst:
                link_outcomes(event_id=inst, outcome_ids=[oid],
                              experience_id=_attributed,
                              eligible_for_attribution=data_ok,
                              exclusion_reason=",".join(coverage),
                              experiment_arm=_arm)
        if oid:
            save_decision_snapshot(
                decision_id=int(r.get("decision_id") or 0), snapshot=snap,
                domain=str(r.get("domain") or "finance"), scope="outcome",
                ref_id=oid, claim_id=claim_id,
                contract_version=_CONTRACT_VERSION)
            for eid in linked_experience_ids(oid):
                recompute_experience_stats(eid)
        return oid
    except Exception as e:
        _debug("resolution→outcome 同步跳过（fail-soft）: %s: %s",
               type(e).__name__, e)
        return None


def coverage_funnel() -> Dict[str, Any]:
    if not _need_db():
        return {"ok": False, "reason": "disabled_or_schema_unavailable"}
    try:
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT (SELECT count(*) FROM qd_agent_experience_injection_events) AS events,
                       (SELECT count(*) FROM qd_agent_evaluation_outcomes) AS outcomes,
                       (SELECT count(*) FROM qd_agent_experience_outcome_links) AS links,
                       (SELECT count(*) FROM qd_agent_experiences) AS experiences,
                       (SELECT count(*) FROM qd_agent_claims
                         WHERE claim_type = 'direction') AS direction_claims,
                       (SELECT count(*) FROM qd_agent_claims
                         WHERE claim_type = 'direction'
                           AND predicted->>'p' IS NOT NULL) AS claims_with_p,
                       (SELECT count(*) FROM qd_agent_claims
                         WHERE claim_type = 'direction'
                           AND predicted->>'p_source' = 'calibrated') AS claims_calibrated,
                       (SELECT count(*) FROM qd_agent_claims
                         WHERE claim_type = 'direction'
                           AND predicted->>'p_source' = 'heuristic_confidence')
                         AS claims_heuristic,
                       (SELECT count(*) FROM qd_agent_evaluation_outcomes
                         WHERE brier_score IS NOT NULL) AS outcomes_with_brier,
                       (SELECT count(*) FROM qd_agent_evaluation_outcomes
                         WHERE coverage_gap @> '["no_probability"]'::jsonb)
                         AS outcomes_no_probability
            """)
            row = cur.fetchone()
            cur.close()
        if not row:
            return {"ok": False, "reason": "no_row"}
        d = dict(row) if isinstance(row, dict) else {
            "events": row[0], "outcomes": row[1], "links": row[2],
            "experiences": row[3], "direction_claims": row[4],
            "claims_with_p": row[5], "claims_calibrated": row[6],
            "claims_heuristic": row[7], "outcomes_with_brier": row[8],
            "outcomes_no_probability": row[9]}
        total = int(d.get("direction_claims") or 0)
        d["probability_coverage"] = (
            round(int(d.get("claims_with_p") or 0) / total, 4) if total else None)
        d["ok"] = True
        d["schema"] = schema_status()
        return d
    except Exception as e:
        _debug("coverage 漏斗查询跳过（fail-soft）: %s: %s", type(e).__name__, e)
        return {"ok": False, "reason": "%s: %s" % (type(e).__name__, e)}
