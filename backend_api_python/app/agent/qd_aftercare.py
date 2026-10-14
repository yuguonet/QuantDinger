# -*- coding: utf-8 -*-
"""run 收尾旁路 —— 轨迹落库 / 计划清账 / 追责入库 / C3P 证据 / 案例记忆。

从 `qd_agent.py` 拆出（2026-10-13 P3 拆分）。这一整块的共同性质：
**全是旁路，全部 fail-open** —— 任何一环挂了都不能让用户收不到答复。

【四条链的产物与落点】
  ① 轨迹链  ：TraceCollector → qd_agent_traces（复盘用）
  ② 追责链  ：chain/intake → decisions / claims（T+N 回填真值）
  ③ C3P 链  ：chain/c3p → evidence（EV-1 证据向量，写侧）
  ④ 案例链  ：utils/case_memory → qd_agent_cases（下次同类任务注入参考）

【★ 顺序是契约，不能换】`_finish_collector` 里的调用次序：
  `_intake_accountability` → `_c3p_pipeline` → `_record_case`
  · C3P 必须排在 intake **之后**：它要拿 intake 产出的 decision_id / claim_ids /
    claims —— claim 的 `subject` 是算 `p_base` 的原料，且 Δp 必须按 claim 配对；
  · C3P 必须排在 case **之前**：产出的 evidence 要回传给案例记忆落进
    `outcome.evidence`（那是 P1-2 缺的写侧，没它库里一条真 EV-1 都没有）。

【★ 接线后 Δp 恒为 0 是预期行为】P3 校准未上线 ⇒ `coefs=None` ⇒ `p_final=p_base`。
本阶段在**攒数据**，不是在看效果。别为了"看到非零 Δp"去凭空编系数。
"""
from __future__ import annotations

import logging
from typing import List, Optional

from .qd_hooks import _HOOKS_LOCAL

logger = logging.getLogger(__name__)

# 「异常收尾」状态集：只有这些状态下，计划残留在才是"跑偏/被截断"的真信号。
# 正常 Idle 收尾时模型常把 plan 当 TODO 用、不回写 completed ⇒ 残留是噪声。
_ABNORMAL_RUN_STATUS = ("truncated", "LimitsExceeded", "grounding_failed",
                        "empty", "budget_wrapped")


class AftercareMixin:
    """run 收尾：以 mixin 形式并进 QDAgent（必须排在 MimocodeAgent 之前）。"""

    # ── qd_traces DB 链（闭环②：TraceCollector → chain.store）────
    def _make_collector(self, task):
        try:
            from .trace_collector import TraceCollector
            sid = getattr(_HOOKS_LOCAL, "session_id", None) or "default"
            query = task if isinstance(task, str) else str(task)
            # 2026-10-01：task 是 **prefetch 加工后**的文本（记忆/技能/RAG 块 + 用户问题），
            # 直接落库会让 qd_agent_traces.user_query 变成一坨注入块，复盘时看不到
            # 用户到底问了什么（实测首条修复记录就是 "[相关技能：auto-stock-analyze]…"）。
            # 这里剥掉注入前缀，只留 [用户问题] 之后的原文。
            marker = "\n[用户问题]\n"
            if marker in query:
                query = query.split(marker, 1)[1]
            return TraceCollector(session_id=sid, user_query=query[:2000])
        except Exception:
            return None

    def _plan_lint(self, status: str = "") -> None:
        """计划收尾轻校验（差距 A，2026-10-02）：只告警不拦截。

        update_plan 的清单若留着 in_progress/pending 收不了口，**只有在 run 非正常
        收尾时**才是"任务跑偏/被截断"的信号（预算熔断 / 步数耗尽 / 溯源失败 / 空回复）。
        正常 Idle 收尾时模型常把 plan 当 TODO 用、不回写 completed —— 此时残留是纯噪声
        （实测「分析600519」正常收尾也报 5 项残留）。故：异常收尾才 WARNING，
        正常收尾降为 debug；两种情况下都照旧落 trace 事件供事后对账。
        """
        plan = getattr(self, "current_plan", None)
        if not plan:
            return
        try:
            residue = [p.get("step", "") for p in plan
                       if isinstance(p, dict) and p.get("status") in ("in_progress", "pending")]
            if residue:
                if str(status or "") in _ABNORMAL_RUN_STATUS:
                    logger.warning(
                        "[plan-lint] run 异常收尾(status=%s)且计划未清账（%d 项）: %s",
                        status, len(residue), residue[:5])
                else:
                    logger.debug(
                        "[plan-lint] 正常收尾仍有计划残留（%d 项，非异常·不告警）", len(residue))
                if self.trace is not None:
                    self.trace.emit("plan_lint", residue=residue[:10],
                                    status=str(status or ""),
                                    plan_explanation=getattr(self, "plan_explanation", ""))
        except Exception:
            pass

    def _finish_collector(self, message: str, status: str) -> None:
        """四条旁路的总入口；调用次序是契约（见本模块文件头）。"""
        collector = getattr(self, "_collector", None)
        self._collector = None
        if collector is None:
            return
        # ── 差距 K：攒「任务→实际调用工具」对（JSONL，周级喂共现权重）────
        # 只写文件不改行为；空问题/空工具链不记；任何异常静默（旁路 fail-open）。
        try:
            from .preselect import record_task_calls
            record_task_calls(str(getattr(collector, "user_query", "") or ""),
                              list(getattr(collector, "_all_tools_called", None) or []))
        except Exception:
            pass
        root_id = None
        stats = None
        try:
            stats = getattr(self.model, "token_stats", None)
            collector.on_agent_finish(
                final_answer=message or "",
                total_steps=self._steps_taken,
                total_tokens=int(getattr(stats, "total_tokens", 0) or 0),
                model=str(getattr(getattr(self.model, "config", None), "model_name", "") or ""),
            )
            root_id = collector.flush()
        except Exception:
            pass
        self._intake_accountability(collector, message or "", root_id, stats)
        self._c3p_pipeline(collector, message or "")
        self._record_case(collector, message or "", status, root_id, stats)

    def _c3p_pipeline(self, collector, answer: str) -> None:
        """C3P 接线（v5 P2，2026-10-13）：一次推理 → 证据向量（EV-1）+ 配对双跑。

        【为什么必须 fail-open】这是增益层，且会打一次 LLM —— 挂了绝不能让用户
        收不到答复。整段包 try/except，`QD_C3P_ENABLED=0` 可整体关闭。
        """
        self.last_c3p: dict = {}
        acc = getattr(self, "last_accountability", None) or {}
        try:
            from .chain import c3p
            self.last_c3p = c3p.run_for_decision(
                user_query=str(getattr(collector, "user_query", "") or ""),
                answer=answer,
                tracked=bool(acc.get("tracked")),
                decision_id=acc.get("decision_id"),
                claim_ids=list(acc.get("claim_ids") or []),
                claims=list(acc.get("claims") or []),
            )
        except Exception as e:
            logger.debug("[c3p] 流水线跳过(fail-open): %s: %s", type(e).__name__, e)

    def _record_case(self, collector, message: str, status: str,
                     root_id: Optional[int], stats) -> None:
        """案例记忆写入侧（2026-10-10 接线）：run 收尾落一条案例（label=pending）。

        【为什么挂 _finish_collector 后置】这是唯一同时拿得到四样原料的位置：
        用户原话（collector.user_query，已剥掉 prefetch 注入块）、工具链
        （collector.plan_digest()）、最终答复/置信度、以及成本（steps/tokens/耗时）。
        【为什么 label 恒为 pending】防错误自我复制：correct/incorrect 由 T+N 真值
        经 chain/store 的 backfill_by_root 延迟回填，入库时谁也不知道对错。
        【fail-open】案例记忆是增益层，任何异常只 debug 日志，不影响回复主链。
        """
        try:
            from .utils import case_memory
            if not case_memory.case_memory_enabled() or collector is None:
                return
            query = str(getattr(collector, "user_query", "") or "").strip()
            if not query:
                return
            root = getattr(collector, "_root", None)
            # 失败坑（只记客观事实，不写“怎么修”——注入侧也只给坑不给修复路径）
            failures: List[str] = []
            if status == "grounding_failed":
                failures.append("数字溯源未通过（结论含不可溯源数字）")
            if status in ("truncated", "LimitsExceeded", "empty", "budget_wrapped"):
                failures.append("run 未正常收尾（status=%s）" % status)
            try:
                residue = [p.get("step", "") for p in (getattr(self, "current_plan", None) or [])
                           if isinstance(p, dict)
                           and p.get("status") in ("in_progress", "pending")]
                if residue:
                    failures.append("计划未清账：" + "、".join(str(s) for s in residue[:3]))
            except Exception:
                pass
            errs = [str(e)[:80] for e in (getattr(self, "tool_call_errors", None) or [])
                    if e][:3]
            if errs:
                failures.append("工具调用错误：" + "；".join(errs))
            cost = {
                "steps": int(getattr(self, "_steps_taken", 0) or 0),
                "tokens": int(getattr(stats, "total_tokens", 0) or 0),
                "latency_ms": int(getattr(root, "elapsed_ms", 0) or 0),
                "model": str(getattr(getattr(self.model, "config", None),
                                     "model_name", "") or ""),
                "status": str(status or ""),
            }
            # ★ evidence 来自紧邻的 `_c3p_pipeline`（P1-2 缺的写侧在此补上）。
            #   没有它，EV-1 契约只有读侧，库里一条真证据都不会有。
            _ev = (getattr(self, "last_c3p", None) or {}).get("evidence")
            case_memory.record_case(
                task_summary=query,
                root_id=root_id,
                level="root",
                tags=case_memory.classify_case_tags(query, message),
                plan_digest=collector.plan_digest() if hasattr(collector, "plan_digest") else [],
                failure_modes=failures,
                cost=cost,
                confidence=getattr(root, "confidence", None),
                evidence=_ev if isinstance(_ev, dict) else None,
            )
        except Exception as e:
            logger.debug("[case-memory] 案例落库跳过(fail-open): %s: %s",
                         type(e).__name__, e)

    def _intake_accountability(self, collector, answer: str,
                               root_id: Optional[int], stats) -> None:
        """追责入库（旁路·v1.1）：闸门 → 抽 Claim → 落 decisions/claims。

        【为什么挂在这里】这是**唯一**同时拿得到三样东西的位置：用户原话
        （collector.user_query，已被剥掉 prefetch 注入块）、最终答复、以及
        `trace_root_id`（flush 返回的 qd_agent_traces.id）。

        【为什么必须 fail-open】追责是慢调旁路，绝不能因为它挂了让用户收不到回复：
        整段包 try/except，且 `QD_ACCOUNTABILITY=0` 可整体关闭。
        """
        if not getattr(self.config, "accountability", True):
            return
        self.last_accountability: dict = {}
        try:
            from .chain.intake import record_decision
            self.last_accountability = record_decision(
                user_query=str(getattr(collector, "user_query", "") or ""),
                answer=answer,
                session_id=str(getattr(collector, "session_id", "") or ""),
                trace_root_id=root_id,
                run_id=str(getattr(getattr(self, "trace", None), "run_id", "") or ""),
                model=str(getattr(getattr(self.model, "config", None), "model_name", "") or ""),
                total_tokens=int(getattr(stats, "total_tokens", 0) or 0),
                latency_ms=int(getattr(getattr(collector, "_root", None),
                                       "elapsed_ms", 0) or 0),
            )
        except Exception as e:
            logger.debug("[accountability] 入库跳过(fail-open): %s: %s",
                         type(e).__name__, e)

    # ── 轨迹采集（工具级，闭环②的写侧）──────────────────────────
    def _trace_tool(self, action: dict, result, elapsed_ms: float, error: str = None) -> None:
        """工具调用落 qd_traces（闭环② DB 链，TraceCollector）。"""
        collector = getattr(self, "_collector", None)
        if collector is None:
            return
        try:
            out = result.get("output") if isinstance(result, dict) else result
            err = error or (None if not isinstance(result, dict) or result.get("success", True)
                            else str(out)[:200])
            collector.on_tool_call(action.get("tool", ""), action.get("params") or {},
                                   out, elapsed_ms, error=err)
        except Exception:
            pass
