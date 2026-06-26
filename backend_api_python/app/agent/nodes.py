# -*- coding: utf-8 -*-
"""
Nodes — LangGraph 节点函数。

图：prepare → planner → agent → finalize
上下文通过 LangGraph Checkpointer 自动持久化。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from app.agent.state import AgentState, AgentResult, StepRecord

logger = logging.getLogger(__name__)

# ── TraceCollector 外部存储 ────────────────────────────────────
# TraceCollector 实例不可被 msgpack 序列化，不能放入 graph state。
# 通过 session_id 在模块级 dict 中存取，避免 checkpointer 报错。
_collectors: Dict[str, Any] = {}

def _store_collector(session_id: str, collector: Any) -> None:
    if session_id:
        _collectors[session_id] = collector

def _get_collector(session_id: str) -> Optional[Any]:
    return _collectors.get(session_id)

def _pop_collector(session_id: str) -> Optional[Any]:
    return _collectors.pop(session_id, None)


# ═══════════════════════════════════════════════════════════════
#  公共工具
# ═══════════════════════════════════════════════════════════════

def _build_llm_call():
    from app.agent.model import build_model
    smol_model = build_model()
    def llm_call(prompt: str) -> str:
        from smolagents import ChatMessage
        response = smol_model([ChatMessage(role="user", content=prompt)])
        return response.content if hasattr(response, "content") else str(response)
    return llm_call


def _build_intent_obj(state: AgentState):
    from app.agent.intent_analyzer import IntentResult
    intent_data = state.get("intent", {})
    if not intent_data:
        return None
    return IntentResult(
        intent=intent_data.get("intent", ""),
        domain=state.get("domain", ""),
        verb=intent_data.get("verb", ""),
        noun=intent_data.get("noun", ""),
        confidence=intent_data.get("confidence", 0.5),
        source=intent_data.get("source", ""),
    )


def _get_history_from_state(state: AgentState) -> list:
    messages = state.get("messages", [])
    return [{"role": m.get("role", "user"), "content": m.get("content", "")} for m in messages[-12:]]


# ═══════════════════════════════════════════════════════════════
#  prepare_node — 意图分析（入口）
# ═══════════════════════════════════════════════════════════════

def prepare_node(state: AgentState) -> Dict[str, Any]:
    from app.agent.intent_analyzer import (
        _quick_intent_check, analyze_intent, format_intent_for_agent,
    )
    from app.agent.tool_context import set_tool_context

    query = state["query"]

    _check_negative_feedback(state)

    # 快速通道
    quick = _quick_intent_check(query)
    if quick:
        _quick_replies = {
            "greeting": "你好！我是 QuantDinger 量化分析助手，有什么需要帮忙的？",
            "farewell": "再见！有问题随时找我。",
            "thanks": "不客气！有需要随时找我。",
            "empty": "请告诉我你需要什么帮助。",
        }
        reply = _quick_replies.get(quick.intent, "你好！有什么需要帮忙的？")
        return {
            "messages": [{"role": "user", "content": query}, {"role": "assistant", "content": reply}],
            "final_output": {"analysis": reply, "signal": "quick_reply"},
            "should_continue": False,
            "all_phases_completed": True,
        }

    # 意图分析
    domain = ""
    intent_data = {}
    intent_verb = ""
    intent_noun = ""
    domain_instructions = ""
    intent_context = ""
    strategy = "direct"

    history = _get_history_from_state(state)
    intent = analyze_intent(query, history=history)
    if intent:
        domain = intent.domain
        strategy = intent.strategy
        domain_instructions = intent.domain_instructions
        intent_verb = getattr(intent, "verb", "") or ""
        intent_noun = getattr(intent, "noun", "") or ""
        intent_context = format_intent_for_agent(intent, query)
        intent_data = {
            "intent": intent.intent, "confidence": intent.confidence,
            "source": intent.source, "verb": intent_verb, "noun": intent_noun,
        }
        set_tool_context({"domain": domain, "strategy": strategy})

    # stock_code
    stock_code = state.get("stock_code", "")
    stock_name = state.get("stock_name", "")
    if domain in ("finance", "trading") and not stock_code:
        import re
        m = re.search(r'(?<!\d)(\d{6})(?!\d)', query)
        if m:
            stock_code = m.group(1)
            try:
                from app.utils.basicinfo_db import get_stock_basic_db
                _stock = get_stock_basic_db().get_stock(stock_code)
                if _stock:
                    stock_name = _stock.get("name", "")
            except Exception:
                pass
        if not stock_code:
            from app.agent.text_utils import extract_stock_from_message
            _code, _name = extract_stock_from_message(query)
            if _code:
                stock_code, stock_name = _code, _name or stock_name

    # 上下文拼接
    enriched = query
    messages = state.get("messages", [])
    if messages:
        parts = [f"[历史] 最近 {len(messages)} 条消息"]
        if stock_code:
            parts.append(f"股票代码: {stock_code}")
        if stock_name:
            parts.append(f"股票名称: {stock_name}")
        enriched = "\n".join(parts) + f"\n\n{query}"

    # TraceCollector
    collector = None
    if strategy == "traced":
        from app.agent.trace_collector import TraceCollector
        collector = TraceCollector(session_id=state.get("session_id", ""), user_query=query)
        collector.intent_verb = intent_verb
        collector.intent_noun = intent_noun
        collector.domain = domain
        if stock_code:
            collector.stock_code = stock_code
        if stock_name:
            collector.stock_name = stock_name

    # collector 存入外部存储（不可 msgpack 序列化，不能进 graph state）
    _store_collector(state.get("session_id", ""), collector)

    return {
        "messages": [{"role": "user", "content": query}],
        "domain": domain, "intent": intent_data,
        "intent_verb": intent_verb, "intent_noun": intent_noun,
        "domain_instructions": domain_instructions, "intent_context": intent_context,
        "strategy": strategy, "stock_code": stock_code, "stock_name": stock_name,
        "enriched": enriched, "should_continue": True,
    }


def route_after_prepare(state: AgentState) -> str:
    if not state.get("should_continue", True):
        return "skip"
    return "plan"


# ═══════════════════════════════════════════════════════════════
#  planner_node — 选工具
# ═══════════════════════════════════════════════════════════════

def planner_node(state: AgentState) -> Dict[str, Any]:
    from app.agent.planner import Planner

    planner = Planner(call_llm=_build_llm_call())

    step_result = planner.plan_next_step(
        query=state["query"], intent=_build_intent_obj(state),
        stock_code=state.get("stock_code", ""), stock_name=state.get("stock_name", ""),
        step_records=state.get("step_records", []),
    )

    if not step_result.success or (not step_result.tools and not step_result.skill):
        return {"should_continue": False, "all_phases_completed": True}

    return {
        "current_tools": step_result.tools,
        "current_skill": step_result.skill,
        "current_rules": step_result.rules,
        "loop_step": state.get("loop_step", 0) + 1,
    }


# ═══════════════════════════════════════════════════════════════
#  agent_node — 执行 ReAct
# ═══════════════════════════════════════════════════════════════

def agent_node(state: AgentState) -> Dict[str, Any]:
    from app.agent.agent import get_smolagent
    from smolagents import ActionStep

    tools = state.get("current_tools", [])
    skill = state.get("current_skill", "")
    stock_code = state.get("stock_code", "")
    stock_name = state.get("stock_name", "")

    # 步骤上下文
    parts = []
    if state.get("step_records"):
        parts.append("前序步骤结论:")
        for r in state["step_records"][-3:]:
            parts.append(f"  步骤{r['step']}: {r.get('description', '')} → {r.get('step_content', '')[:100]}")
    if stock_code:
        parts.append(f"标的: {stock_name or '未知'}（{stock_code}）")
    if state.get("current_rules"):
        parts.append(f"规则: {state['current_rules']}")
    if skill:
        parts.append(f"请用 read_skill 加载 {skill} 的指令并执行。")
    step_context = "\n".join(parts) if parts else state["query"]

    phase_tools = tools
    if skill:
        try:
            from app.agent.semantics import get_all_skill_metas
            meta_skill = get_all_skill_metas().get(skill)
            if meta_skill and meta_skill.tools:
                phase_tools = meta_skill.tools + ["get_skill_catalog", "read_skill"]
        except Exception:
            pass

    agent = get_smolagent(
        user_id=state.get("user_id", "1"), max_steps=10,
        user_message=state["query"], domain=state.get("domain", ""),
        domain_instructions=state.get("domain_instructions", ""),
        intent_context=state.get("intent_context", ""),
        stock_code=stock_code, tool_categories=phase_tools or None,
        collector=_get_collector(state.get("session_id", "")),
        strategy=state.get("strategy", "direct"),
    )

    tool_calls_log = []
    charts = []
    try:
        result = agent.run(step_context, max_steps=10)
        if hasattr(result, "output"):
            raw = result.output
            content = json.dumps(raw, ensure_ascii=False) if isinstance(raw, dict) else (str(raw) if raw else "")
        else:
            content = str(result) if result else ""

        if hasattr(result, "steps") and result.steps:
            for sd in result.steps:
                if isinstance(sd, ActionStep) and sd.tool_calls:
                    for tc in sd.tool_calls:
                        tool_calls_log.append({
                            "tool": getattr(tc, "name", ""),
                            "arguments": getattr(tc, "arguments", {}) or {},
                            "success": sd.error is None,
                            "duration": sd.timing.duration if hasattr(sd, "timing") and sd.timing else 0,
                        })
                obs = getattr(sd, "observations", None) or ""
                if obs and isinstance(obs, str):
                    import re
                    for m in re.finditer(r'__CHART_B64__([A-Za-z0-9+/=]+)__END_CHART__', obs):
                        charts.append(m.group(1))

        success = bool(content)
        total_steps = len(result.steps) if hasattr(result, "steps") and result.steps else 0
        tu = result.token_usage if hasattr(result, "token_usage") else None
        total_tokens = (tu.input_tokens + tu.output_tokens) if tu else 0
    except Exception as e:
        logger.error("[Agent] 执行失败: %s", e)
        content, success, total_steps, total_tokens = f"执行失败: {e}", False, 0, 0

    record: StepRecord = {
        "step": state.get("loop_step", 0),
        "description": f"工具: {', '.join(tools)}" if tools else f"Skill: {skill}",
        "tools": tools, "skill": skill, "rules": state.get("current_rules", ""),
        "planner_reasoning": "", "step_content": content or "", "step_success": success,
        "steps_used": total_steps, "step_tokens": total_tokens,
        "tool_calls": tool_calls_log, "charts": charts,
    }

    return {
        "step_records": [record], "step_content": content or "", "step_success": success,
        "tool_calls_log": tool_calls_log, "charts": charts,
        "total_steps": state.get("total_steps", 0) + total_steps,
        "total_tokens": state.get("total_tokens", 0) + total_tokens,
    }


# ═══════════════════════════════════════════════════════════════
#  finalize_node — 后处理（无 Judge）
# ═══════════════════════════════════════════════════════════════

def finalize_node(state: AgentState) -> Dict[str, Any]:
    step_records = state.get("step_records", [])

    # 快速通道（prepare_node 已设置 final_output）
    if state.get("final_output") and not step_records:
        _pop_collector(state.get("session_id", ""))  # 清理未使用的 collector
        return {
            "messages": [{"role": "assistant", "content": json.dumps(state["final_output"], ensure_ascii=False)}],
            "last_verb": state.get("intent_verb", ""), "last_noun": state.get("intent_noun", ""),
        }

    # Agent 的 final_answer 就是最终输出，不需要 Judge 汇总
    # 从 step_records 拼接内容
    contents = [r.get("step_content", "") for r in step_records if r.get("step_content")]
    content = "\n\n".join(contents) if contents else ""

    # 尝试从最后一步提取结构化 JSON
    final_output = {}
    if contents:
        from app.agent.json_extractor import extract_decision
        dec = extract_decision(contents[-1])
        if dec:
            final_output = dec
        else:
            final_output = {"analysis": content}

    # 闭环
    _learn_from_execution(state)
    _save_traces(state, content)

    return {
        "messages": [{"role": "assistant", "content": content}] if content else [],
        "final_output": final_output, "all_phases_completed": True,
        "last_verb": state.get("intent_verb", ""), "last_noun": state.get("intent_noun", ""),
    }


# ═══════════════════════════════════════════════════════════════
#  辅助函数
# ═══════════════════════════════════════════════════════════════

def _check_negative_feedback(state: AgentState) -> None:
    try:
        from app.agent.chain.tool_chains import detect_feedback_severity, penalize_chain
        from app.agent.chain import store as chain_store

        severity = detect_feedback_severity(state.get("query", ""))
        if not severity:
            return
        last_verb = state.get("last_verb", "")
        last_noun = state.get("last_noun", "")
        if not last_verb or not last_noun:
            return

        stock_code = state.get("stock_code", "")
        if stock_code:
            trace = chain_store.query_latest_root(stock_code)
            if trace:
                root_id = trace["id"]
                if chain_store.get_penalty_count(stock_code) >= 3:
                    chain_store.delete_tree(root_id)
                else:
                    chain_store.mark_root_wrong(root_id)

        penalize_chain(last_verb, last_noun, severity)
        logger.info("[Feedback] %s: verb=%s noun=%s", severity, last_verb, last_noun)
    except Exception as e:
        logger.warning("[Feedback] 异常: %s", e)


def _learn_from_execution(state: AgentState) -> None:
    try:
        verb = state.get("intent_verb", "")
        noun = state.get("intent_noun", "")
        if not verb and not noun:
            return

        from app.agent.evaluator import learn_from_execution

        all_tool_calls = []
        for r in state.get("step_records", []):
            all_tool_calls.extend(r.get("tool_calls", []))

        agent_result = AgentResult(
            success=state.get("all_phases_completed", False),
            content=json.dumps(state.get("final_output", {}), ensure_ascii=False),
            tool_calls_log=all_tool_calls,
            total_steps=state.get("total_steps", 0),
            total_tokens=state.get("total_tokens", 0),
        )
        learn_from_execution(agent_result, verb, noun,
                             all_phases_completed=state.get("all_phases_completed", False))
    except Exception as e:
        logger.warning("[Learn] 异常: %s", e)


def _save_traces(state: AgentState, content: str) -> None:
    collector = _pop_collector(state.get("session_id", ""))
    if collector:
        try:
            collector.on_agent_finish(
                final_answer=content,
                total_steps=state.get("total_steps", 0),
                total_tokens=state.get("total_tokens", 0),
                model="langgraph",
            )
        except Exception as e:
            logger.warning("[Trace] 存库失败: %s", e)
    else:
        try:
            from app.agent.chain.schema import EvalNode, Layer, Status
            from app.agent.json_extractor import extract_decision
            from datetime import date

            sc = state.get("stock_code", "")
            dec = extract_decision(content) if content else None
            root = EvalNode(
                layer=Layer.CHAIN.value,
                name=f"{state.get('intent_verb', '')}+{state.get('intent_noun', '')}" or "agent",
                exec_date=date.today(), stock_code=sc, stock_name=state.get("stock_name", ""),
                score=dec.get("score") if dec else None,
                direction=dec.get("direction", "") if dec else "",
                action=dec.get("action", "") if dec else "",
                signal=dec.get("signal", "") if dec else "",
                analysis=content[:2000] if content else "",
                input_params={"user_query": state.get("query", "")},
                status=Status.OK.value if content else Status.FAILED.value,
            )
            from app.agent.chain.store import save_tree
            save_tree(root)
        except Exception as e:
            logger.warning("[Trace] 兜底失败: %s", e)
