# -*- coding: utf-8 -*-
"""Intake — 追责入库的唯一入口（闸门 → 提取 → 落库），v1.1。

调用方**只**用 `record_decision()`；闸门、提取、落库的细节不对外暴露。

【修正① 落在哪（不追责的不入库）】
  闸门在**写库之前**。不通过的决策一行都不写（不是"写进去再标 claim_count=0"）。
  漏追责可接受（慢调），脏数据不行。闸门结果以 logger.debug 留痕 + 返回 reason，
  便于回溯"这条为什么没进"。

【全链路 fail-open】
  追责是旁路：任何异常都**不许**影响主对话。`record_decision` 永不抛异常。

【时间基准（exec_date）—— 缺省即不追责】
  exec_date 是**该条回答的生成日**，不是"现在"。到期日（due_date）由它按交易日历
  推出第 N 个交易日，基准错一天，整条 claim 的结算窗口就整体偏移。
    · 实时链路：传 `current_data_day()`（回答当场生成，等同当前数据日）
    · 历史回放：传该记录的**保存时间**（会话/消息的 created_at 所在数据日）
  拿不到可靠基准时**不猜**：`claims.due_date_of` 会退到 `date.today()`，若在回放时
  触发，就是把三天前的判断按今天重新计时——结果必错且无声。因此这里把缺失
  exec_date 当作追责前置条件不成立：整条不落库（reason=`exec_date_unknown`），
  宁可漏掉，不能错了。

【用户维度（2026-10-15 用户裁定，长期有效）】
  闭环/问责与**用户无关**：decisions/claims/resolutions 全链**没有 user_id 列**，
  也不建注入链路——是有意设计，不是漏字段、更不是待修 bug。
  session_id 由调用方透传，仅作弱关联/排查线索：Flask 层带 `user:<uid>:` 命名
  空间前缀；exp_grind/cron 传合成 sid；直接调用不传 = 空串。**空或合成的
  session_id 放行即可**，勿按"丢了用户身份"去补注入链（旧 M4 同族，已裁定搁置）。
"""
from __future__ import annotations

import os
import time as _time
from datetime import date
from typing import Any, Dict, Optional

from ..log import logger

# 域策略缓存：避免每轮对话都查库（策略极少变，60s 足够）
_POLICY_CACHE: Dict[str, Any] = {"ts": 0.0, "policy": None}
_POLICY_TTL = 60.0


def _llm_claims_enabled() -> bool:
    """claim 抽取走 LLM 通道（默认开）。`QD_CLAIM_LLM=0` 回退纯规则抽取。"""
    return str(os.getenv("QD_CLAIM_LLM", "1")).strip().lower() not in (
        "0", "false", "off", "no")


def _policy() -> Dict[str, Dict[str, Any]]:
    now = _time.time()
    if _POLICY_CACHE["policy"] and now - float(_POLICY_CACHE["ts"] or 0) < _POLICY_TTL:
        return _POLICY_CACHE["policy"]
    try:
        from .account_store import load_domain_policy
        pol = load_domain_policy()
    except Exception as e:
        logger.debug("[Intake] 读域策略失败，用内置默认: %s", e)
        from .claims import DOMAIN_POLICY
        pol = dict(DOMAIN_POLICY)
    _POLICY_CACHE["policy"] = pol
    _POLICY_CACHE["ts"] = now
    return pol


def _freeze_snapshot(*, decision_id: int, domain: str, model: str,
                     exec_date: date, context: Dict[str, Any],
                     claim_ids: List[Optional[int]],
                     claims: List[Dict[str, Any]]) -> Optional[str]:
    """决策生成后立刻冻结快照并落库；失败只影响冻结，不影响主链路。"""
    try:
        from . import prediction_contract as _pc
        from . import evaluation_store as _evs
        ctx = context or {}
        claim_snaps = []
        for cid, claim in zip(claim_ids, claims):
            if not cid or not isinstance(claim, dict):
                continue
            p = claim.get("predicted") or {}
            if not isinstance(p, dict):
                p = {}
            claim_snaps.append(_pc.build_snapshot(
                decision_id=decision_id, claim_id=cid,
                subject=claim.get("subject") or "",
                subject_kind=claim.get("subject_kind") or "",
                horizon=claim.get("horizon") or "",
                prediction_target="direction", as_of=exec_date,
                data_as_of=exec_date, model_name=model,
                prompt_sha=ctx.get("prompt_sha") or "",
                skill_id=ctx.get("skill_id") or "",
                skill_version=ctx.get("skill_version") or "",
                tool_chain=ctx.get("tool_chain"),
                tool_chain_sha=ctx.get("tool_chain_sha") or "",
                experience_bundle_id=ctx.get("experience_bundle_id") or "",
                experience_bundle_sha=ctx.get("experience_bundle_sha") or "",
                experience_injection_ids=ctx.get("experience_injection_ids") or (),
                label_rule_version=_evs.OUTCOME_POLICY_VERSION,
                probability_source=p.get("p_source") or "",
                probability_target=p.get("p_target") or "",
                probability_horizon=p.get("p_horizon") or "",
                predicted_probability=p.get("p"),
                predicted_direction=p.get("dir") or "",
                condition_key=claim.get("condition_key") or "",
                entry_rule=ctx.get("entry_rule") or "",
                entry_price=ctx.get("entry_price"),
                stop_rule=ctx.get("stop_rule") or "",
                stop_price=ctx.get("stop_price"),
                target_rule=ctx.get("target_rule") or "",
                target_price=ctx.get("target_price"),
                is_actionable=bool(ctx.get("is_actionable", False))))
        head = claim_snaps[0] if claim_snaps else {}
        snap = _pc.build_snapshot(
            decision_id=decision_id, subject=head.get("subject") or "",
            subject_kind=head.get("subject_kind") or "",
            horizon=head.get("horizon") or "",
            prediction_target="direction", as_of=exec_date,
            data_as_of=exec_date, model_name=model,
            prompt_sha=ctx.get("prompt_sha") or "",
            skill_id=ctx.get("skill_id") or "",
            skill_version=ctx.get("skill_version") or "",
            tool_chain=ctx.get("tool_chain"),
            tool_chain_sha=ctx.get("tool_chain_sha") or "",
            experience_bundle_id=ctx.get("experience_bundle_id") or "",
            experience_bundle_sha=ctx.get("experience_bundle_sha") or "",
            experience_injection_ids=ctx.get("experience_injection_ids") or (),
            experience_snapshot_at=ctx.get("experience_snapshot_at"),
            label_rule_version=_evs.OUTCOME_POLICY_VERSION,
            query_type=domain, entry_rule=ctx.get("entry_rule") or "",
            entry_price=ctx.get("entry_price"), stop_rule=ctx.get("stop_rule") or "",
            stop_price=ctx.get("stop_price"), target_rule=ctx.get("target_rule") or "",
            target_price=ctx.get("target_price"),
            is_actionable=bool(ctx.get("is_actionable", False)),
            claim_snapshots=claim_snaps)
        _evs.save_decision_snapshot(
            decision_id=int(decision_id), snapshot=snap, domain=domain,
            contract_version=_pc.CONTRACT_VERSION)
        return str(snap.get("snapshot_sha") or "")
    except Exception as e:
        logger.warning("[Intake] 决策快照冻结跳过: %s: %s", type(e).__name__, e)
        return None


def record_decision(*, user_query: str, answer: str, session_id: str = "",
                    trace_root_id: Optional[int] = None, run_id: str = "",
                    model: str = "", total_tokens: int = 0, latency_ms: int = 0,
                    intent: str = "", exec_date: Optional[date] = None,
                    snapshot_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """一条问答结束后调用：过闸门 → 抽 claim → 落库。

    `exec_date` 必为**回答生成日**（实时=`current_data_day()`，历史回放=记录保存
    时间的数据日）。缺失时不做抽取也不落库——无基准日即无法定义到期日，宁可漏掉。

    Returns:
        {"tracked": bool, "reason": str, "domain": str,
         "decision_id": Optional[int], "claim_ids": list, "claims": list,
         "elapsed_ms": float}

        ★ `claims` 是**抽取出的 claim 内容**（与 `claim_ids` 同序），v5 P2 接线加入：
          C3P（`chain/c3p.py`）需要 claim 里的 `subject` / `subject_kind` / `horizon`
    才能算 `p_base` 并按 claim 配对。只有 id 就只能回库再查一遍（多一次往返，
    且顺序没有保证）。**纯增量字段**，不影响既有消费方。

    `snapshot_context` 是调用方在决策当场可见的 prompt/工具链/经验包版本，
    用于 `§5.1` 决策快照冻结；拿不到的字段留空，绝不事后猜测。
    """
    t0 = _time.perf_counter()
    out: Dict[str, Any] = {"tracked": False, "reason": "", "domain": "",
                           "decision_id": None, "claim_ids": [], "claims": [],
                           "extract_channel": "", "snapshot_sha": "",
                           "elapsed_ms": 0.0}
    try:
        from . import claims as _claims
        from . import account_store as _as
        pol = _policy()
        domain = _claims.classify_domain(user_query, answer)
        out["domain"] = domain

        allow, reason = _claims.intake_gate(user_query, answer, domain, pol)
        out["reason"] = reason
        if not allow:
            logger.debug("[Intake] 闸门拦下(domain=%s, reason=%s) query=%s",
                         domain, reason, (user_query or "")[:60])
            return out

        if exec_date is None:
            out["reason"] = "exec_date_unknown"
            logger.warning("[Intake] 缺基准日(exec_date)，无法定到期日，按宁可漏掉不入库 "
                           "domain=%s query=%s", domain, (user_query or "")[:60])
            return out

        extracted = None
        if _llm_claims_enabled():
            from . import claim_extract_llm as _cll
            extracted = _cll.extract_claims(user_query, answer, domain,
                                            exec_date=exec_date, policy=pol)
        if extracted is None:
            extracted = _claims.extract_claims(user_query, answer, domain,
                                              exec_date=exec_date, policy=pol)
            out["extract_channel"] = "rule"
            extractor_ver = _claims.EXTRACTOR_VER
        else:
            from .claim_extract_llm import EXTRACTOR_VER as _LLM_VER
            out["extract_channel"] = "llm"
            extractor_ver = _LLM_VER
        decision_id = _as.save_decision(
            session_id=session_id, trace_root_id=trace_root_id, run_id=run_id,
            domain=domain, intent=intent, user_query=user_query, answer=answer,
            model=model, total_tokens=total_tokens, latency_ms=latency_ms,
            gate_ver=_claims.GATE_VER, gate_reason=reason)
        if not decision_id:
            out["reason"] = "save_decision_failed"
            return out
        ids = _as.save_claims(decision_id, extracted,
                              extractor_ver=extractor_ver)
        out["snapshot_sha"] = _freeze_snapshot(
            decision_id=int(decision_id), domain=domain, model=model,
            exec_date=exec_date, context=snapshot_context or {},
            claim_ids=ids, claims=list(extracted or [])) or ""
        try:
            _as.refresh_decision_summary(decision_id)
        except Exception:
            pass
        out.update({"tracked": True, "decision_id": decision_id,
                    "claim_ids": ids, "claims": list(extracted or [])})
        logger.info("[Intake] 入库 decision=%s domain=%s claims=%d",
                    decision_id, domain, len(ids))
        # claim_ids 与 claims 等长同序；None 位＝该条未落库，下游 c3p 会跳过归因。
        # 这里必须显式记一笔——否则"抽取成功但没入库"会完全静默。
        _miss = sum(1 for i in ids if i is None)
        if _miss:
            logger.warning("[Intake] decision=%s 有 %d/%d 条 claim 未落库(跳过归因)",
                           decision_id, _miss, len(ids))
    except Exception as e:                      # 旁路永不阻断主链
        out["reason"] = f"error:{type(e).__name__}"
        logger.warning("[Intake] 追责入库失败(忽略): %s: %s", type(e).__name__, e)
    finally:
        out["elapsed_ms"] = round((_time.perf_counter() - t0) * 1000, 2)
    return out
