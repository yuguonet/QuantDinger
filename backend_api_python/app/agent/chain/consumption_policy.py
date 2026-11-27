"""chain/consumption_policy.py — 档位自动生效执行链（方案 §8.2/§9.3/§9.4）。

把 experiment_config 的纯判定接成可执行链：读当前档位 → 检查人工否决 →
调 decide_mode → 达标写回 qd_agent_experiences.consumption_mode → 全量留痕。
自带独立留痕表 qd_agent_consumption_decisions（additive、幂等），不改生产表
DDL、不接生产调用方；人可 veto / revoke_veto / demote，全部留痕可观测。

硬纪律：阈值只来自版本化配置（代码不填数）；配置未预注册 / 版本不符 / 证据
不全一律 fail-closed 维持原档；DB 异常 fail-soft 返回 undecidable 不阻断主
流程；一次只升一档；回滚即删本文件（留痕表留空无害）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from typing import Any, Dict, Iterable, Optional, Sequence

from . import experiment_config as _cfg

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = "consumption-policy-v1"
_MODES = ("observe", "shadow", "active")

_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS qd_agent_consumption_decisions (
    decision_sha       VARCHAR(64) PRIMARY KEY,
    experiment         VARCHAR(64) NOT NULL,
    experience_id      VARCHAR(64),
    experience_version VARCHAR(32) NOT NULL DEFAULT 'v1',
    from_mode          VARCHAR(16) NOT NULL,
    to_mode            VARCHAR(16) NOT NULL,
    verdict            VARCHAR(24) NOT NULL,
    reason             VARCHAR(64) NOT NULL,
    evidence           JSONB NOT NULL DEFAULT '{}',
    config_sha         VARCHAR(64),
    actor              VARCHAR(32) NOT NULL DEFAULT 'auto',
    created_at         TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_qd_consump_exp
    ON qd_agent_consumption_decisions (experiment, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_qd_consump_expid
    ON qd_agent_consumption_decisions (experience_id, created_at DESC);
"""

_ledger_ok = False
_ledger_error = ""
_ledger_lock = threading.Lock()


def _db():
    from app.utils.db import get_db_connection
    return get_db_connection()


def enabled() -> bool:
    base = (os.getenv("QD_EVAL_OBSERVATION_ENABLED") or "1").strip().lower() \
        not in ("0", "false", "no", "off")
    own = (os.getenv("QD_CONSUMPTION_POLICY_ENABLED") or "1").strip().lower() \
        not in ("0", "false", "no", "off")
    return base and own


def schema_status() -> Dict[str, Any]:
    return {"ok": bool(_ledger_ok), "version": _SCHEMA_VERSION,
            "error": _ledger_error, "enabled": enabled()}


def ensure_ledger(force: bool = False) -> bool:
    global _ledger_ok, _ledger_error
    if _ledger_ok and not force:
        return True
    with _ledger_lock:
        if _ledger_ok and not force:
            return True
        try:
            with _db() as conn:
                cur = conn.cursor()
                for stmt in filter(None, _LEDGER_DDL.split(";")):
                    if stmt.strip():
                        cur.execute(stmt)
                conn.commit()
                cur.close()
            _ledger_ok, _ledger_error = True, ""
            return True
        except Exception as e:
            _ledger_ok = False
            _ledger_error = "%s: %s" % (type(e).__name__, e)
            logger.warning("[ConsPolicy] 留痕表初始化失败: %s", _ledger_error)
            return False


def _config_sha(path: Optional[str]) -> str:
    try:
        p = path or str(_cfg.config_path())
        with open(p, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()[:16]
    except Exception:
        return ""


def _decision_sha(experiment: str, experience_id: Optional[str],
                  from_mode: str, decision: Dict[str, Any],
                  evidence: Dict[str, Any], config_sha: str,
                  actor: str) -> str:
    raw = json.dumps({
        "experiment": experiment, "experience_id": experience_id,
        "from_mode": from_mode, "to_mode": decision.get("to_mode"),
        "verdict": decision.get("verdict"), "reason": decision.get("reason"),
        "evidence": evidence, "config_sha": config_sha, "actor": actor,
    }, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _evidence(endpoint, ci_low, ci_high, n_clusters, looks,
              drawdown_ci_high) -> Dict[str, Any]:
    return {k: v for k, v in (
        ("endpoint", endpoint), ("ci_low", ci_low), ("ci_high", ci_high),
        ("n_clusters", n_clusters), ("looks", looks),
        ("drawdown_ci_high", drawdown_ci_high)) if v is not None}


def _load_modes(conn, experience_ids: Sequence[str]) -> Dict[str, str]:
    cur = conn.cursor()
    cur.execute("""
        SELECT experience_id, consumption_mode
        FROM qd_agent_experiences WHERE experience_id = ANY(%s)
    """, (list(experience_ids),))
    rows = cur.fetchall()
    cur.close()
    if rows and isinstance(rows[0], dict):
        return {str(r["experience_id"]): str(r["consumption_mode"]) for r in rows}
    return {str(r[0]): str(r[1]) for r in rows}


def _active_vetoes(conn, experiment: str,
                   experience_ids: Sequence[str]) -> set:
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT ON (experience_id) experience_id, verdict
        FROM qd_agent_consumption_decisions
        WHERE experiment = %s AND experience_id = ANY(%s)
          AND verdict IN ('veto', 'revoke_veto')
        ORDER BY experience_id, created_at DESC
    """, (experiment, list(experience_ids)))
    rows = cur.fetchall()
    cur.close()
    if rows and isinstance(rows[0], dict):
        return {str(r["experience_id"]) for r in rows if r["verdict"] == "veto"}
    return {str(r[0]) for r in rows if r[1] == "veto"}


def _write_ledger(conn, *, experiment, experience_id, experience_version,
                  from_mode, decision, evidence, config_sha, actor) -> str:
    sha = _decision_sha(experiment, experience_id, from_mode,
                        decision, evidence, config_sha, actor)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO qd_agent_consumption_decisions
            (decision_sha, experiment, experience_id, experience_version,
             from_mode, to_mode, verdict, reason, evidence, config_sha,
             actor, created_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
        ON CONFLICT (decision_sha) DO NOTHING
    """, (sha, experiment, experience_id, experience_version, from_mode,
          str(decision.get("to_mode")), str(decision.get("verdict")),
          str(decision.get("reason")), json.dumps(evidence, ensure_ascii=False),
          config_sha, actor))
    cur.close()
    return sha


def apply_decision(experiment: str, *, experience_ids: Sequence[str] = (),
                   current_mode: str = "observe", endpoint: Optional[str] = None,
                   ci_low=None, ci_high=None, n_clusters=None, looks=None,
                   drawdown_ci_high=None, config_path: Optional[str] = None,
                   actor: str = "auto") -> Dict[str, Any]:
    """按预注册阈值判定并自动写回档位；被人工否决或证据不足一律维持原档。"""
    ev = _evidence(endpoint, ci_low, ci_high, n_clusters, looks,
                   drawdown_ci_high)
    result = {"ok": False, "experiment": experiment,
              "verdict": "undecidable", "reason": "", "from_mode": current_mode,
              "to_mode": current_mode, "applied": False,
              "vetoed": False, "ledger_sha": None}
    if not enabled():
        result["reason"] = "disabled"
        return result
    try:
        cfg = _cfg.load_config(config_path)
    except Exception as e:
        result["reason"] = "config_error:%s" % type(e).__name__
        return result
    config_sha = _config_sha(config_path)
    ledger_on = ensure_ledger()
    try:
        with _db() as conn:
            ids = [str(x) for x in experience_ids if str(x)]
            modes = _load_modes(conn, ids) if ids else {}
            if ids and not modes:
                result["reason"] = "experience_missing"
                return result
            if ids:
                distinct = set(modes.values())
                if len(distinct) != 1:
                    result["reason"] = "mixed_modes"
                    result["from_mode"] = ""
                    return result
                effective_mode = next(iter(distinct))
            else:
                effective_mode = current_mode
            result["from_mode"] = effective_mode

            if ids and _active_vetoes(conn, experiment, ids):
                result.update(ok=True, verdict="vetoed",
                              reason="human_veto_active",
                              to_mode=effective_mode, vetoed=True)
                if ledger_on:
                    for eid in ids:
                        result["ledger_sha"] = _write_ledger(
                            conn, experiment=experiment, experience_id=eid,
                            experience_version="v1", from_mode=effective_mode,
                            decision=result, evidence=ev, config_sha=config_sha,
                            actor=actor)
                    conn.commit()
                return result

            decision = _cfg.decide_mode(
                cfg, experiment, effective_mode, endpoint=endpoint,
                ci_low=ci_low, ci_high=ci_high, n_clusters=n_clusters,
                looks=looks, drawdown_ci_high=drawdown_ci_high)
            target = str(decision.get("consumption_mode"))
            result.update(verdict=decision.get("verdict"),
                          reason=decision.get("reason"), to_mode=target,
                          ok=True)

            if target != effective_mode and decision.get("verdict") == "promote":
                cur = conn.cursor()
                for eid in ids:
                    cur.execute("""
                        UPDATE qd_agent_experiences SET consumption_mode = %s
                        WHERE experience_id = %s AND consumption_mode = %s
                    """, (target, eid, effective_mode))
                cur.close()
                result["applied"] = bool(ids)

            if ledger_on:
                targets = ids or [None]
                for eid in targets:
                    result["ledger_sha"] = _write_ledger(
                        conn, experiment=experiment, experience_id=eid,
                        experience_version="v1", from_mode=effective_mode,
                        decision=result, evidence=ev, config_sha=config_sha,
                        actor=actor)
                conn.commit()
            return result
    except Exception as e:
        result.update(verdict="undecidable",
                      reason="db_error:%s" % type(e).__name__)
        logger.warning("[ConsPolicy] 判定执行失败（fail-soft）: %s", e)
        return result


def human_override(experiment: str, *, experience_ids: Sequence[str],
                   action: str, reason: str = "", actor: str = "human",
                   config_path: Optional[str] = None) -> Dict[str, Any]:
    """人工否决 / 撤销否决 / 降档；所有动作留痕，可观测可审计。"""
    result = {"ok": False, "experiment": experiment, "verdict": "undecidable",
              "reason": "", "applied": False}
    if not enabled():
        result["reason"] = "disabled"
        return result
    if action not in ("veto", "revoke_veto", "demote") or not experience_ids:
        result["reason"] = "invalid_action"
        return result
    if not ensure_ledger():
        result["reason"] = "ledger_unavailable"
        return result
    config_sha = _config_sha(config_path)
    ev = {"reason": reason}
    try:
        with _db() as conn:
            ids = [str(x) for x in experience_ids if str(x)]
            modes = _load_modes(conn, ids)
            if not modes:
                result["reason"] = "experience_missing"
                return result
            for eid in ids:
                cur_mode = modes.get(eid)
                if cur_mode is None:
                    continue
                if action == "demote":
                    idx = _MODES.index(cur_mode) if cur_mode in _MODES else 0
                    target = _MODES[max(0, idx - 1)]
                    decision = {"to_mode": target,
                                "verdict": "demote", "reason": reason}
                    if target != cur_mode:
                        cur = conn.cursor()
                        cur.execute("""
                            UPDATE qd_agent_experiences SET consumption_mode = %s
                            WHERE experience_id = %s AND consumption_mode = %s
                        """, (target, eid, cur_mode))
                        cur.close()
                        result["applied"] = True
                else:
                    decision = {"to_mode": cur_mode,
                                "verdict": action, "reason": reason}
                _write_ledger(conn, experiment=experiment, experience_id=eid,
                              experience_version="v1", from_mode=cur_mode,
                              decision=decision, evidence=ev,
                              config_sha=config_sha, actor=actor)
            conn.commit()
            result.update(ok=True, verdict=action, reason=reason or action)
            return result
    except Exception as e:
        result.update(verdict="undecidable",
                      reason="db_error:%s" % type(e).__name__)
        logger.warning("[ConsPolicy] 人工干预失败（fail-soft）: %s", e)
        return result


def decision_ledger(experiment: Optional[str] = None,
                    experience_id: Optional[str] = None,
                    limit: int = 50) -> Dict[str, Any]:
    """只读查询档位决策留痕；DB 不可用返回空列表 + 原因。"""
    if not ensure_ledger():
        return {"ok": False, "rows": [], "reason": "ledger_unavailable"}
    try:
        clauses, args = [], []
        if experiment:
            clauses.append("experiment = %s")
            args.append(experiment)
        if experience_id:
            clauses.append("experience_id = %s")
            args.append(experience_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with _db() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT decision_sha, experiment, experience_id, from_mode,
                       to_mode, verdict, reason, evidence, config_sha, actor,
                       created_at
                FROM qd_agent_consumption_decisions""" + where +
                        " ORDER BY created_at DESC LIMIT %s",
                        tuple(args) + (int(limit),))
            rows = cur.fetchall()
            cur.close()
        if rows and isinstance(rows[0], dict):
            return {"ok": True, "rows": [dict(r) for r in rows]}
        cols = ("decision_sha", "experiment", "experience_id", "from_mode",
                "to_mode", "verdict", "reason", "evidence", "config_sha",
                "actor", "created_at")
        return {"ok": True,
                "rows": [dict(zip(cols, r)) for r in rows]}
    except Exception as e:
        return {"ok": False, "rows": [],
                "reason": "%s: %s" % (type(e).__name__, e)}
