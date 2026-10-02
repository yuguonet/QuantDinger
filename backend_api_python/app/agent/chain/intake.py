# -*- coding: utf-8 -*-
"""Intake — 追责入库的唯一入口（闸门 → 提取 → 落库），v1.1。

调用方**只**用 `record_decision()`；闸门、提取、落库的细节不对外暴露。

【修正① 落在哪（不追责的不入库）】
  闸门在**写库之前**。不通过的决策一行都不写（不是"写进去再标 claim_count=0"）。
  漏追责可接受（慢调），脏数据不行。闸门结果以 logger.debug 留痕 + 返回 reason，
  便于回溯"这条为什么没进"。

【全链路 fail-open】
  追责是旁路：任何异常都**不许**影响主对话。`record_decision` 永不抛异常。
"""
from __future__ import annotations

import time as _time
from datetime import date
from typing import Any, Dict, Optional

from log import logger

# 域策略缓存：避免每轮对话都查库（策略极少变，60s 足够）
_POLICY_CACHE: Dict[str, Any] = {"ts": 0.0, "policy": None}
_POLICY_TTL = 60.0


def _policy() -> Dict[str, Dict[str, Any]]:
    now = _time.time()
    if _POLICY_CACHE["policy"] and now - float(_POLICY_CACHE["ts"] or 0) < _POLICY_TTL:
        return _POLICY_CACHE["policy"]
    try:
        from chain.account_store import load_domain_policy
        pol = load_domain_policy()
    except Exception as e:
        logger.debug("[Intake] 读域策略失败，用内置默认: %s", e)
        from chain.claims import DOMAIN_POLICY
        pol = dict(DOMAIN_POLICY)
    _POLICY_CACHE["policy"] = pol
    _POLICY_CACHE["ts"] = now
    return pol


def record_decision(*, user_query: str, answer: str, session_id: str = "",
                    trace_root_id: Optional[int] = None, run_id: str = "",
                    model: str = "", total_tokens: int = 0, latency_ms: int = 0,
                    intent: str = "", exec_date: Optional[date] = None) -> Dict[str, Any]:
    """一条问答结束后调用：过闸门 → 抽 claim → 落库。

    Returns:
        {"tracked": bool, "reason": str, "domain": str,
         "decision_id": Optional[int], "claim_ids": list, "elapsed_ms": float}
    """
    t0 = _time.perf_counter()
    out: Dict[str, Any] = {"tracked": False, "reason": "", "domain": "",
                           "decision_id": None, "claim_ids": [], "elapsed_ms": 0.0}
    try:
        from chain import claims as _claims
        from chain import account_store as _as
        pol = _policy()
        domain = _claims.classify_domain(user_query, answer)
        out["domain"] = domain

        allow, reason = _claims.intake_gate(user_query, answer, domain, pol)
        out["reason"] = reason
        if not allow:
            logger.debug("[Intake] 闸门拦下(domain=%s, reason=%s) query=%s",
                         domain, reason, (user_query or "")[:60])
            return out

        extracted = _claims.extract_claims(user_query, answer, domain,
                                           exec_date=exec_date, policy=pol)
        decision_id = _as.save_decision(
            session_id=session_id, trace_root_id=trace_root_id, run_id=run_id,
            domain=domain, intent=intent, user_query=user_query, answer=answer,
            model=model, total_tokens=total_tokens, latency_ms=latency_ms,
            gate_ver=_claims.GATE_VER, gate_reason=reason)
        if not decision_id:
            out["reason"] = "save_decision_failed"
            return out
        ids = _as.save_claims(decision_id, extracted,
                              extractor_ver=_claims.EXTRACTOR_VER)
        try:
            _as.refresh_decision_summary(decision_id)
        except Exception:
            pass
        out.update({"tracked": True, "decision_id": decision_id, "claim_ids": ids})
        logger.info("[Intake] 入库 decision=%s domain=%s claims=%d",
                    decision_id, domain, len(ids))
    except Exception as e:                      # 旁路永不阻断主链
        out["reason"] = f"error:{type(e).__name__}"
        logger.warning("[Intake] 追责入库失败(忽略): %s: %s", type(e).__name__, e)
    finally:
        out["elapsed_ms"] = round((_time.perf_counter() - t0) * 1000, 2)
    return out
