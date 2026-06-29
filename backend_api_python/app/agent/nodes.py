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

# ── 外部存储（不可序列化对象）────────────────────────────────
# TraceCollector / smolagents Agent 实例不可被 msgpack 序列化，
# 不能放入 LangGraph state。通过 session_id 在模块级 dict 中存取。
_collectors: Dict[str, Any] = {}
_collectors_ts: Dict[str, float] = {}   # session_id → 创建时间戳（TTL 用）
_agents: Dict[str, Any] = {}      # session_id → smolagents CodeAgent（中断用）
_COLLECTOR_TTL = 3600  # 1 小时

def _store_collector(session_id: str, collector: Any) -> None:
    if session_id:
        import time
        _collectors[session_id] = collector
        _collectors_ts[session_id] = time.time()

def _get_collector(session_id: str) -> Optional[Any]:
    return _collectors.get(session_id)

def _pop_collector(session_id: str) -> Optional[Any]:
    _collectors_ts.pop(session_id, None)
    return _collectors.pop(session_id, None)

def _cleanup_stale_collectors() -> None:
    """清理超时未消费的 TraceCollector，防止内存泄漏。"""
    import time
    now = time.time()
    stale = [sid for sid, ts in _collectors_ts.items() if now - ts > _COLLECTOR_TTL]
    for sid in stale:
        _collectors.pop(sid, None)
        _collectors_ts.pop(sid, None)
        logger.debug("[Trace] 清理过期 collector: %s", sid)

def _store_agent(session_id: str, agent: Any) -> None:
    """存储 agent 引用，供中断端点使用。"""
    if session_id:
        _agents[session_id] = agent

def _clear_agent(session_id: str) -> None:
    """agent 执行完毕后清理引用。"""
    _agents.pop(session_id, None)

def get_active_agent(session_id: str) -> Optional[Any]:
    """公开接口：获取当前活跃的 agent 实例（供中断使用）。"""
    return _agents.get(session_id)


# ═══════════════════════════════════════════════════════════════
#  公共工具
# ═══════════════════════════════════════════════════════════════

def _build_llm_call():
    """构建 LLM 调用函数（直连 OpenAI API，不走 smolagents）。"""
    from app.services.llm import LLMService
    import requests as _requests

    svc = LLMService()
    api_key = svc.get_api_key()
    base_url = svc.get_base_url()
    import os
    model_id = os.getenv("AGENT_LLM_MODEL", "").strip() or svc.get_default_model()

    def llm_call(messages) -> str:
        """支持 str 或 list[dict] 两种入参。"""
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        resp = _requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model_id, "messages": messages, "temperature": 0.05, "max_tokens": 1024},
            timeout=60.0,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
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
    from app.agent.intent_analyzer import analyze_intent
    from app.agent.tool_context import set_tool_context

    query = state["query"]

    _check_negative_feedback(state)

    # 意图分析
    domain = ""
    intent_data = {}
    intent_verb = ""
    intent_noun = ""
    domain_instructions = ""
    strategy = "direct"

    history = _get_history_from_state(state)
    intent = analyze_intent(query, history=history)
    if intent:
        domain = intent.domain
        strategy = intent.strategy
        domain_instructions = intent.domain_instructions
        intent_verb = getattr(intent, "verb", "") or ""
        intent_noun = getattr(intent, "noun", "") or ""
        intent_data = {
            "intent": intent.intent, "confidence": intent.confidence,
            "source": intent.source, "verb": intent_verb, "noun": intent_noun,
        }
        set_tool_context({"domain": domain, "strategy": strategy})

    # stock_code — 始终走 3 级提取（context → 正则 → 中文名解析）
    # 金融领域必须同时拿到 code + name，任一缺失则两者皆空
    stock_code = ""
    stock_name = ""
    if domain in ("finance", "trading"):
        import re
        # ── 级别 1: context（上轮 state 残留）────────────────────
        _ctx_code = state.get("stock_code", "")
        _ctx_name = state.get("stock_name", "")
        if _ctx_code and _ctx_name and _ctx_name in query:
            stock_code, stock_name = _ctx_code, _ctx_name
        # ── 级别 2: 正则提取 6 位数字代码 ─────────────────────
        if not stock_code:
            m = re.search(r'(?<!\d)(\d{6})(?!\d)', query)
            if m:
                _candidate_code = m.group(1)
                try:
                    from app.utils.basicinfo_db import get_stock_basic_db
                    _stock = get_stock_basic_db().get_stock(_candidate_code)
                    if _stock:
                        stock_code = _candidate_code
                        stock_name = _stock.get("name", "")
                except Exception:
                    pass
        # ── 级别 3: 中文名解析（正则未命中时）──────────────────
        if not stock_code:
            from app.agent.text_utils import extract_stock_from_message
            _code, _name = extract_stock_from_message(query)
            if _code and _name:
                stock_code, stock_name = _code, _name
        # ── 校验：stock_name 必须出现在用户消息中 ────────────────
        if stock_code and stock_name and stock_name not in query:
            logger.warning("[Prepare] stock_name '%s' 不在消息中，丢弃匹配", stock_name)
            stock_code, stock_name = "", ""
        # ── 校验：必须同时有 code 和 name ─────────────────────
        if bool(stock_code) != bool(stock_name):
            logger.warning("[Prepare] code/name 不完整 (code=%s, name=%s)，清空", stock_code, stock_name)
            stock_code, stock_name = "", ""

    # ── 编排路径缓存：qd_traces 命中 → 跳过 LLM#2 ──
    # 质量门：意图置信度 → 聚合 win_rate → 步数 → 子节点 → 工具权重
    intent_confidence = intent_data.get("confidence", 0) if intent_data else 0
    cached_tools = None
    if intent_verb and intent_noun:
        if intent_confidence < 0.7:
            logger.info("[Prepare] 意图置信度 %.2f < 0.7，跳过缓存", intent_confidence)
        else:
            from app.agent.chain.store import query_cached_tools
            cached_tools = query_cached_tools(domain, intent_verb, intent_noun, stock_code)
            if cached_tools:
                # 校验：缓存工具里不能有低权重工具
                try:
                    from app.agent.chain.store import query_low_weight_tools
                    low_weight = query_low_weight_tools()
                    if low_weight and set(cached_tools) & low_weight:
                        blocked = set(cached_tools) & low_weight
                        logger.info("[Prepare] 缓存拒绝: 含低权重工具 %s", blocked)
                        cached_tools = None
                except Exception:
                    pass
            if cached_tools:
                logger.info("[Prepare] 缓存命中: %s+%s+%s → %s", domain, intent_verb, intent_noun, cached_tools)
    # 未命中时显式置 None，防止 checkpointer 残留上轮旧值

    # TraceCollector（即使缓存命中也要创建，agent 仍需执行工具并追踪）
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
        "domain_instructions": domain_instructions,
        "strategy": strategy, "stock_code": stock_code, "stock_name": stock_name,
        "should_continue": True,
        "cached_tools": cached_tools,
    }


# ═══════════════════════════════════════════════════════════════
#  planner_node — 选工具
# ═══════════════════════════════════════════════════════════════

def planner_node(state: AgentState) -> Dict[str, Any]:
    from app.agent.planner import Planner

    step_records = state.get("step_records", [])

    # ── 编排路径缓存：prepare_node 已查 qd_traces，命中则跳过 LLM#2 ──
    cached_tools = state.get("cached_tools")
    if cached_tools and not step_records:
        logger.info("[Planner] 缓存命中，跳过 LLM#2: %s", cached_tools)
        return {
            "current_tools": cached_tools,
            "current_skill": None,
            "current_tool_strategy": "",
            "loop_step": state.get("loop_step", 0) + 1,
            "should_continue": True,
            "cached_tools": None,  # 用完即清
        }

    # ── 已有结论 → 直接结束，不再调 LLM#2 ──
    if step_records:
        last_content = step_records[-1].get("step_content", "")
        if _has_conclusion(last_content):
            logger.info("[Planner] 已有结论，结束规划")
            return {
                "should_continue": False,
                "all_phases_completed": True,
            }

    # ── 缓存未命中，走 LLM#2 规划 ──
    planner = Planner(call_llm=_build_llm_call())

    step_result = planner.plan_next_step(
        query=state["query"], intent=_build_intent_obj(state),
        stock_code=state.get("stock_code", ""), stock_name=state.get("stock_name", ""),
        step_records=step_records,
    )

    if not step_result.success or (not step_result.tools and not step_result.skill):
        # 无工具无 skill（闲聊等）→ LLM 直接回复，跳过 agent
        try:
            llm_call = _build_llm_call()
            reply = llm_call([
                {"role": "system", "content": "你是 QuantDinger 量化分析助手。简洁友好地回复用户。"},
                {"role": "user", "content": state["query"]},
            ])
            content = reply if isinstance(reply, str) else str(reply)
        except Exception as e:
            logger.warning("planner 闲聊 LLM 调用失败: %s", e)
            content = ""
        return {
            "should_continue": False,
            "all_phases_completed": True,
            "step_records": [{"step": 0, "step_content": content, "step_success": True}],
        }

    return {
        "current_tools": step_result.tools,
        "current_skill": step_result.skill,
        "current_tool_strategy": step_result.tool_strategy,
        "loop_step": state.get("loop_step", 0) + 1,
        "should_continue": True,  # 有工具，标记需要继续（finalize 判断是否循环）
    }


def route_after_planner(state: AgentState) -> str:
    if not state.get("should_continue", True):
        return "skip"
    return "run"


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

    # 用户消息中注入股票代码（"分析宇通客车" → "分析宇通客车(600066)"）
    query = state["query"]
    _query = query
    if stock_code and stock_name and stock_name in query:
        _query = query.replace(stock_name, f"{stock_name}({stock_code})", 1)

    # 步骤上下文
    parts = []
    if state.get("step_records"):
        parts.append("前序步骤结论:")
        for r in state["step_records"][-3:]:
            parts.append(f"  步骤{r['step']}: {r.get('description', '')} → {r.get('step_content', '')[:100]}")
    if state.get("current_tool_strategy"):
        parts.append(f"工具策略: {state['current_tool_strategy']}")
    if skill:
        parts.append(f"请用 read_skill 加载 {skill} 的指令并执行。")
    step_context = "\n".join(parts) if parts else _query

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
        user_message=_query, domain=state.get("domain", ""),
        domain_instructions=state.get("domain_instructions", ""),
        stock_code=stock_code, stock_name=stock_name,
        tool_categories=phase_tools or None,
        collector=_get_collector(state.get("session_id", "")),
        strategy=state.get("strategy", "direct"),
    )

    # 持久化 agent 引用，供 /interrupt 端点使用
    _session_id = state.get("session_id", "")
    _store_agent(_session_id, agent)

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
                if isinstance(sd, ActionStep):
                    if sd.tool_calls:
                        for tc in sd.tool_calls:
                            tool_calls_log.append({
                                "tool": getattr(tc, "name", ""),
                                "arguments": getattr(tc, "arguments", {}) or {},
                                "success": sd.error is None,
                                "duration": sd.timing.duration if hasattr(sd, "timing") and sd.timing else 0,
                            })
                    elif sd.code_action:
                        # CodeAgent: 从生成的 Python 代码中提取工具名
                        import re as _re
                        _tool_names = set()
                        for _t in tools:
                            _tname = getattr(_t, 'name', '')
                            if _tname and _tname in sd.code_action:
                                _tool_names.add(_tname)
                        for _tname in sorted(_tool_names):
                            tool_calls_log.append({
                                "tool": _tname,
                                "arguments": {},
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
    finally:
        # 执行完毕（成功或失败），清理 agent 引用
        _clear_agent(_session_id)

    record: StepRecord = {
        "step": state.get("loop_step", 0),
        "description": f"工具: {', '.join(tools)}" if tools else f"Skill: {skill}",
        "tools": tools, "skill": skill, "tool_strategy": state.get("current_tool_strategy", ""),
        "planner_reasoning": "", "step_content": content or "", "step_success": success,
        "steps_used": total_steps, "step_tokens": total_tokens,
        "tool_calls": tool_calls_log, "charts": charts,
    }

    # 累积已有记录（普通 List reducer，返回值会覆盖而非追加）
    prev_records = state.get("step_records", [])
    all_tool_calls = list(state.get("tool_calls_log", [])) + tool_calls_log
    all_charts = list(state.get("charts", [])) + charts

    return {
        "step_records": prev_records + [record],
        "tool_calls_log": all_tool_calls, "charts": all_charts,
        "total_steps": state.get("total_steps", 0) + total_steps,
        "total_tokens": state.get("total_tokens", 0) + total_tokens,
    }


def route_after_agent(state: AgentState) -> str:
    """agent 执行完毕后，决定继续还是结束。"""
    loop_step = state.get("loop_step", 0)
    max_loop = state.get("max_loop_steps", 10)

    if loop_step >= max_loop:
        return "finish"

    # ── 已有结论检测：agent 输出了 action → 不再循环 ──
    step_records = state.get("step_records", [])
    if step_records:
        last_content = step_records[-1].get("step_content", "")
        if _has_conclusion(last_content):
            logger.info("[Route] 已有结论，结束循环 (step %d)", loop_step)
            return "finish"

    return "continue"


def _has_conclusion(content: str) -> bool:
    """检测 agent 输出是否已包含结构化结论。"""
    if not content:
        return False
    # JSON 里有 action 字段
    if '"action"' in content and '"score"' in content:
        return True
    # 关键结论词
    _keywords = ['建议买入', '建议卖出', '建议持有', '建议观望', '建议回避',
                 '维持持有', '维持买入', '维持卖出']
    return any(kw in content for kw in _keywords)


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

    # TTL 清理过期 collector
    _cleanup_stale_collectors()

    return {
        "messages": [{"role": "assistant", "content": content}] if content else [],
        "final_output": final_output, "all_phases_completed": True,
        # 不设 should_continue — finalize 不参与循环决策
        "last_verb": state.get("intent_verb", ""), "last_noun": state.get("intent_noun", ""),
    }


# ═══════════════════════════════════════════════════════════════
#  辅助函数
# ═══════════════════════════════════════════════════════════════

def _check_negative_feedback(state: AgentState) -> None:
    """用户负面反馈 → 惩罚 qd_traces（correct=false）。

    工具链惩罚（tool_chains.json）已移除，T+N 回测的 correct 字段
    天然过滤掉被标记的链路。
    """
    from app.agent.chain import store as chain_store

    severity = _detect_feedback_severity(state.get("query", ""))
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

    logger.info("[Feedback] %s: verb=%s noun=%s", severity, last_verb, last_noun)


def _detect_feedback_severity(message: str) -> Optional[str]:
    """内置负面反馈检测（替代 tool_chains.detect_feedback_severity）。"""
    if not message:
        return None
    msg = message.strip()
    _SEVERE = ["完全不对", "大错特错", "错得离谱", "离谱", "反了", "完全错",
               "一塌糊涂", "乱七八糟", "瞎扯", "胡说", "垃圾", "废了", "没用", "一点用没有"]
    _MILD = ["不对", "不正确", "不好", "不行", "不准", "不太对",
             "有问题", "有误", "错了", "不太行", "不靠谱"]
    for pat in _SEVERE:
        if pat in msg:
            return "severe"
    for pat in _MILD:
        if pat in msg:
            return "mild"
    return None


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
        # 兜底写入（仅金融/交易域，且有 verb+noun）
        _verb = state.get('intent_verb', '')
        _noun = state.get('intent_noun', '')
        if not _verb and not _noun:
            return  # 闲聊等无意图场景，不写 qd_traces
        try:
            from app.agent.chain.schema import EvalNode, Layer, Status
            from app.agent.json_extractor import extract_decision
            from datetime import date

            sc = state.get("stock_code", "")
            dec = extract_decision(content) if content else None
            _domain = state.get('domain', '')
            root = EvalNode(
                layer=Layer.CHAIN.value,
                name=f"{_domain}+{_verb}+{_noun}",
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
