# -*- coding: utf-8 -*-
"""
Nodes — LangGraph 节点函数。

图：prepare → planner → agent → finalize
上下文通过 LangGraph Checkpointer 自动持久化。

P1: trim_messages 替代硬编码 messages[-12:]
P2: finalize_node 自动压缩旧消息 → context_summary
    prepare_node 注入 context_summary 到旁路 LLM 调用
"""
from __future__ import annotations

import json

from typing import Any, Dict, Optional

from app.agent.state import AgentState, AgentResult, StepRecord
from app.agent.utils import trim_messages, estimate_tokens

from app.agent.log import logger
from app.agent.cache import cache

# ── 上下文裁剪常量 ──────────────────────────────────────────
MAX_CONTEXT_TOKENS = 8000      # LLM 调用时的消息 token 预算
KEEP_RECENT_MESSAGES = 6       # 裁剪时至少保留的最近消息数


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

_llm_call_fn = None  # 缓存 LLM 调用函数，避免每次重建


def _build_llm_call():
    """构建 LLM 调用函数（直连 OpenAI API，不走 smolagents）。模块级缓存，只构建一次。"""
    global _llm_call_fn
    if _llm_call_fn is not None:
        return _llm_call_fn

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
        payload = {
            "model": model_id,
            "messages": messages,
            "temperature": 0.05,
            "max_tokens": 1024,
        }
        logger.debug("[LLM] 请求: %s %s, model=%s, msgs=%d",
                     f"{base_url}/chat/completions", "POST", model_id, len(messages))
        try:
            resp = _requests.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=60.0,
            )
            if resp.status_code != 200:
                logger.warning("[LLM] 服务返回 %d: %s", resp.status_code, resp.text[:300])
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logger.error("[LLM] 调用失败: %s", e)
            raise

    _llm_call_fn = llm_call
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


def _get_history_from_state(state: AgentState, max_tokens: int = MAX_CONTEXT_TOKENS) -> list:
    """从 state 获取裁剪后的消息历史（token 级裁剪，非硬编码条数）。

    P1: 用 trim_messages 替代 messages[-12:]
    """
    messages = state.get("messages", [])
    if not messages:
        return []
    # 统一转为 OpenAI API 格式: {"role":..., "content":...}
    _type_to_role = {"human": "user", "ai": "assistant", "system": "system", "tool": "tool"}
    dicts = []
    for m in messages:
        if isinstance(m, dict):
            role = m.get("role") or _type_to_role.get(m.get("type", "")) or "user"
            content = m.get("content", "")
            dicts.append({"role": role, "content": content if content else ""})
        elif hasattr(m, "model_dump"):
            d = m.model_dump()
            role = d.get("role") or _type_to_role.get(d.get("type", "")) or "user"
            content = d.get("content", "")
            dicts.append({"role": role, "content": content if content else ""})
        elif hasattr(m, "role") and hasattr(m, "content"):
            dicts.append({"role": m.role, "content": m.content})
        else:
            dicts.append({"role": "user", "content": str(m)})
    return trim_messages(dicts, max_tokens=max_tokens, keep_recent=KEEP_RECENT_MESSAGES)


# ═══════════════════════════════════════════════════════════════
#  prepare_node — 意图分析（入口）
# ═══════════════════════════════════════════════════════════════

def prepare_node(state: AgentState) -> Dict[str, Any]:
    from app.agent.intent_analyzer import analyze_intent
    from app.agent.tool_context import set_tool_context

    query = state["query"]

    _check_negative_feedback(state)

    # 意图分析（精简版：路由 + 股票提取 + 上下文摘要）
    domain = ""
    intent_data = {}
    intent_verb = ""
    intent_noun = ""
    domain_instructions = ""
    strategy = "direct"

    history = _get_history_from_state(state)
    prev_context_summary = state.get("context_summary", "")
    intent = analyze_intent(query, history=history, context_summary=prev_context_summary)
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
        set_tool_context({"domain": domain, "strategy": strategy, "session_id": state.get("session_id", "")})

    # ── chat / unknown 域：旁路 LLM 直接回复，不进 planner ──
    if domain in ("chat", "unknown"):
        # 历史查询：LLM 分类为 history 时从 Checkpointer 读取
        if intent_noun == "history":
            _history_answer = _try_answer_from_history(state)
            if _history_answer:
                return {
                    "messages": [{"role": "user", "content": query}],
                    "domain": domain, "intent": intent_data,
                    "intent_verb": intent_verb, "intent_noun": intent_noun,
                    "domain_instructions": domain_instructions,
                    "strategy": strategy,
                    "final_output": {"reply": _history_answer},
                    "should_continue": False,
                    "all_phases_completed": True,
                }

        try:
            llm_call = _build_llm_call()
            _history = _get_history_from_state(state, max_tokens=4000)
            # P2: 注入 context_summary 到 system prompt
            _system = "你是 QuantDinger 量化分析助手。简洁友好地回复用户。"
            if prev_context_summary:
                _system += f"\n\n[上文摘要] {prev_context_summary}"
            _msgs = [{"role": "system", "content": _system}, *_history[-6:], {"role": "user", "content": query}]
            content = llm_call(_msgs)
        except Exception as e:
            logger.warning("[Prepare] 旁路 LLM 调用失败: %s", e)
            content = ""
        return {
            "messages": [{"role": "user", "content": query}],
            "domain": domain, "intent": intent_data,
            "intent_verb": intent_verb, "intent_noun": intent_noun,
            "domain_instructions": domain_instructions,
            "strategy": strategy,
            "final_output": {"reply": content},
            "should_continue": False,
            "all_phases_completed": True,
        }

    # stock_code — 用 _resolve_stock 作为唯一来源
    stock_code = ""
    stock_name = ""
    if domain in ("finance", "trading"):
        import re
        # 从消息中提取关键词：6位代码 或 中文名
        _keyword = ""
        m = re.search(r'(?<!\d)(\d{6})(?!\d)', query)
        if m:
            _keyword = m.group(1)
        else:
            # 去掉常见动词前缀再提取中文名
            _cleaned = re.sub(r'^(分析|查看|看看|查查|帮我看看|帮我查|帮我分析)', '', query)
            _candidates = re.findall(r'[\u4e00-\u9fff]{2,8}', _cleaned)
            if _candidates:
                _keyword = _candidates[0]

        if _keyword:
            try:
                from app.agent.tools.data_tools import _resolve_stock
                _res = _resolve_stock(_keyword, limit=1)
                _items = _res.get("results", []) if isinstance(_res, dict) else []
                if _items:
                    stock_code = _items[0].get("code", "")
                    stock_name = _items[0].get("name", "")
                    logger.info("[Prepare] _resolve_stock('%s') → %s(%s)", _keyword, stock_name, stock_code)
            except Exception as e:
                logger.warning("[Prepare] _resolve_stock 失败: %s", e)

        # 未识别到股票 → 从上一轮继承
        if not stock_code:
            _ctx_code = state.get("stock_code", "")
            _ctx_name = state.get("stock_name", "")
            if _ctx_code and _ctx_name:
                stock_code, stock_name = _ctx_code, _ctx_name
                logger.info("[Prepare] 继承上轮股票: %s(%s)", stock_name, stock_code)

    # ── 编排路径缓存：qd_traces 命中 → 跳过 LLM#2 ──
    intent_confidence = intent_data.get("confidence", 0) if intent_data else 0
    cached_tools = None
    if intent_verb and intent_noun:
        if intent_confidence < 0.7:
            logger.info("[Prepare] 意图置信度 %.2f < 0.7，跳过缓存", intent_confidence)
        else:
            from app.agent.chain.store import query_cached_tools
            cached_tools = query_cached_tools(domain, intent_verb, intent_noun, stock_code)
            if cached_tools:
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

    # ── 全域注入上轮 context_summary 到 domain_instructions ──
    if prev_context_summary:
        domain_instructions = (domain_instructions or "") + f"\n\n## 上文摘要\n{prev_context_summary}"

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
    _store_collector(state.get("session_id", ""), collector)

    return {
        "messages": [{"role": "user", "content": query}],
        "domain": domain, "intent": intent_data,
        "intent_verb": intent_verb, "intent_noun": intent_noun,
        "domain_instructions": domain_instructions,
        "strategy": strategy, "stock_code": stock_code, "stock_name": stock_name,
        "should_continue": True,
        "cached_tools": cached_tools,
        "context_summary": intent.context_summary if intent else "",
    }


# ═══════════════════════════════════════════════════════════════
#  route_after_prepare — prepare 后路由
# ═══════════════════════════════════════════════════════════════

def route_after_prepare(state: AgentState) -> str:
    """prepare 已完成的场景（chat/unknown/未识别股票）→ 直接到 finalize。"""
    if not state.get("should_continue", True):
        return "direct"
    return "plan"


# ═══════════════════════════════════════════════════════════════
#  planner_node — 选工具
# ═══════════════════════════════════════════════════════════════

def planner_node(state: AgentState) -> Dict[str, Any]:
    from app.agent.planner import Planner

    step_records = state.get("step_records", [])

    cached_tools = state.get("cached_tools")
    if cached_tools and not step_records:
        logger.info("[Planner] 缓存命中，跳过 LLM#2: %s", cached_tools)
        return {
            "current_tools": cached_tools,
            "current_skill": None,
            "current_tool_strategy": "",
            "loop_step": state.get("loop_step", 0) + 1,
            "should_continue": True,
            "cached_tools": None,
        }

    if step_records:
        if _has_conclusion(step_records):
            logger.info("[Planner] 已有结论，结束规划")
            return {"should_continue": False, "all_phases_completed": True}

    planner = Planner(call_llm=_build_llm_call())

    # 上下文摘要（指代消解，轻量注入）
    context_summary = state.get("context_summary", "")

    step_result = planner.plan_next_step(
        query=state["query"], intent=_build_intent_obj(state),
        stock_code=state.get("stock_code", ""), stock_name=state.get("stock_name", ""),
        context_summary=context_summary,
        step_records=step_records,
    )

    if not step_result.success or (not step_result.tools and not step_result.skill):
        try:
            llm_call = _build_llm_call()
            reply = llm_call([
                {"role": "system", "content": "你是 QuantDinger 量化分析助手。简洁友好地回复用户。"},
                {"role": "user", "content": state["query"]},
            ])
            content = reply if isinstance(reply, str) else str(reply)
        except Exception as e:
            logger.warning("[Planner] 兜底 LLM 调用失败: %s", e)
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
        "current_description": step_result.description,
        "loop_step": state.get("loop_step", 0) + 1,
        "should_continue": True,
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
    from app.agent.tool_context import set_tool_context

    # 确保 tool_context 包含 session_id（smolagents 执行时 contextvars 可能丢失）
    set_tool_context({
        "domain": state.get("domain", ""),
        "strategy": state.get("strategy", ""),
        "session_id": state.get("session_id", ""),
        "user_id": state.get("user_id", "1"),
        "stock_code": state.get("stock_code", ""),
        "stock_name": state.get("stock_name", ""),
    })

    tools = state.get("current_tools", [])
    skill = state.get("current_skill", "")
    stock_code = state.get("stock_code", "")
    stock_name = state.get("stock_name", "")

    query = state["query"]
    _query = query
    if stock_code and stock_name:
        if stock_name not in _query and stock_code in _query:
            _query = _query.replace(stock_code, f"{stock_code}({stock_name})", 1)
        elif stock_code not in _query and stock_name in _query:
            _query = _query.replace(stock_name, f"{stock_name}({stock_code})", 1)

    # 步骤上下文
    parts = []
    if state.get("current_description"):
        parts.append(f"本步目标: {state['current_description']}")
    if state.get("current_tool_strategy"):
        parts.append(f"工具策略: {state['current_tool_strategy']}")
    if state.get("step_records"):
        parts.append("前序步骤结论:")
        for r in state["step_records"][-3:]:
            parts.append(f"  步骤{r['step']}: {r.get('description', '')} → {r.get('step_content', '')[:100]}")
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
        _clear_agent(_session_id)

    # ── 统一解壳 ──
    _shell_valid = False
    _shell_data = None
    _shell_errors = []
    _shell_conclusion = True
    if content:
        from app.agent.json_extractor import extract_json
        _parsed = extract_json(content)
        if _parsed and _parsed.get("reply"):
            _shell_data = _parsed.get("data", {}) or {}
            _shell_errors = _parsed.get("errors", []) or []
            _shell_conclusion = _parsed.get("conclusion", True)
            content = _parsed["reply"]
            _shell_valid = True

    # ── 保存工具结果到 session_store（跨轮复用）──
    if tool_calls_log and _session_id:
        try:
            from app.agent.session_store import get_session_store
            _store = get_session_store()
            _tool_summary = {}
            for tc in tool_calls_log:
                _tname = tc.get("tool", "")
                if _tname:
                    _tool_summary[_tname] = {
                        "success": tc.get("success", True),
                        "args": tc.get("arguments", {}),
                    }
            if _tool_summary:
                _store.save_tool_results(_session_id, {_session_id: _tool_summary})
        except Exception as e:
            logger.debug("[Agent] 保存工具结果失败: %s", e)

    record: StepRecord = {
        "step": state.get("loop_step", 0),
        "description": f"工具: {', '.join(tools)}" if tools else f"Skill: {skill}",
        "tools": tools, "skill": skill, "tool_strategy": state.get("current_tool_strategy", ""),
        "planner_reasoning": "", "step_content": content or "", "step_success": success,
        "steps_used": total_steps, "step_tokens": total_tokens,
        "tool_calls": tool_calls_log, "charts": charts,
        "shell_data": _shell_data, "shell_errors": _shell_errors,
        "shell_conclusion": _shell_conclusion,
    }

    prev_records = state.get("step_records", [])
    all_tool_calls = list(state.get("tool_calls_log", [])) + tool_calls_log
    all_charts = list(state.get("charts", [])) + charts

    return {
        "step_records": prev_records + [record],
        "tool_calls_log": all_tool_calls, "charts": all_charts,
        "total_steps": state.get("total_steps", 0) + total_steps,
        "total_tokens": state.get("total_tokens", 0) + total_tokens,
        "_shell_valid": _shell_valid,
    }


def route_after_agent(state: AgentState) -> str:
    loop_step = state.get("loop_step", 0)
    max_loop = state.get("max_loop_steps", 10)

    if loop_step >= max_loop:
        return "finish"

    step_records = state.get("step_records", [])
    if not step_records:
        return "finish"

    _shell_valid = state.get("_shell_valid", False)
    if not _shell_valid:
        logger.info("[Route] 无壳（中间步骤），继续循环 (step %d)", loop_step)
        return "continue"

    _conclusion = step_records[-1].get("shell_conclusion", True)
    if _conclusion:
        logger.info("[Route] 壳层 conclusion=true，结束循环 (step %d)", loop_step)
        return "finish"

    logger.info("[Route] 壳层 conclusion=false，继续循环 (step %d)", loop_step)
    return "continue"


def _has_conclusion(step_records: list) -> bool:
    if not step_records:
        return False
    return step_records[-1].get("shell_conclusion", True)


# ═══════════════════════════════════════════════════════════════
#  finalize_node — 后处理 + 自动上下文压缩（P2）
# ═══════════════════════════════════════════════════════════════

def finalize_node(state: AgentState) -> Dict[str, Any]:
    step_records = state.get("step_records", [])

    # 快速通道
    if state.get("final_output") and not step_records:
        _pop_collector(state.get("session_id", ""))
        return {
            "messages": [{"role": "assistant", "content": json.dumps(state["final_output"], ensure_ascii=False)}],
            "final_output": state["final_output"],
            "last_verb": state.get("intent_verb", ""), "last_noun": state.get("intent_noun", ""),
            "context_summary": state.get("context_summary", ""),
        }

    contents = [r.get("step_content", "") for r in step_records if r.get("step_content")]
    content = "\n\n".join(contents) if contents else ""

    final_output = {}
    display_content = content
    if step_records:
        _last_record = step_records[-1]
        _sd = _last_record.get("shell_data")
        if _sd:
            final_output = _sd
            display_content = _last_record.get("step_content", content)
        else:
            final_output = {"analysis": content}

    # 闭环
    _save_traces(state, content)
    _cleanup_stale_collectors()

    # ── P2: 自动上下文压缩（Consolidator）──────────────────
    messages = state.get("messages", [])
    new_context_summary = state.get("context_summary", "")

    try:
        from app.agent.consolidator import consolidate_session, should_consolidate
        if should_consolidate(messages):
            domain = state.get("domain", "")
            query = state.get("query", "")
            result = consolidate_session(
                session_id=state.get("session_id", ""),
                state_messages=messages,
                domain=domain,
                query=query,
            )
            if result["compressed"]:
                new_context_summary = result["summary"]
                # 裁剪：保留最近条 + 摘要注入 system
                from app.agent.consolidator import KEEP_RECENT
                trimmed = list(messages[-KEEP_RECENT:])
                trimmed.insert(0, {
                    "role": "system",
                    "content": f"[历史摘要] {result['summary']}",
                })
                messages = trimmed
                logger.info("[Finalize] Consolidator: %d 条 → %d 条 + 摘要",
                            result["messages_before"], result["messages_after"])
    except Exception as e:
        logger.debug("[Finalize] Consolidator 跳过: %s", e)

    # 构建最终 messages
    final_messages = list(messages)
    if display_content:
        final_messages.append({"role": "assistant", "content": display_content})

    # 兜底：如果 Consolidator 没触发，用 intent 的 context_summary
    if not new_context_summary:
        intent_data = state.get("intent", {})
        if isinstance(intent_data, dict):
            new_context_summary = intent_data.get("context_summary", "")
        # 再兜底：用 step_records 最后一步的 step_content 摘要
        if not new_context_summary and step_records:
            last_content = step_records[-1].get("step_content", "")
            if last_content:
                new_context_summary = last_content[:100]

    return {
        "messages": final_messages,
        "final_output": final_output, "all_phases_completed": True,
        "last_verb": state.get("intent_verb", ""), "last_noun": state.get("intent_noun", ""),
        "context_summary": new_context_summary,
    }


# ═══════════════════════════════════════════════════════════════
#  辅助函数
# ═══════════════════════════════════════════════════════════════

def _aggregate_tools_called(state: AgentState) -> list:
    tools = []
    for r in state.get("step_records", []):
        for tc in r.get("tool_calls", []):
            name = tc.get("tool", "")
            if name and name not in tools:
                tools.append(name)
    return tools


def _check_negative_feedback(state: AgentState) -> None:
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
    else:
        chain_name = f"{state.get('domain', '')}+{last_verb}+{last_noun}"
        trace = chain_store.query_latest_root_by_chain(chain_name)
        if trace:
            root_id = trace["id"]
            if chain_store.get_penalty_count_by_chain(chain_name) >= 3:
                chain_store.delete_tree(root_id)
            else:
                chain_store.mark_root_wrong(root_id)

    logger.info("[Feedback] %s: verb=%s noun=%s", severity, last_verb, last_noun)


def _detect_feedback_severity(message: str) -> Optional[str]:
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
        _verb = state.get('intent_verb', '')
        _noun = state.get('intent_noun', '')
        if not _verb and not _noun:
            return
        try:
            from app.agent.chain.schema import EvalNode, Layer, Status
            from datetime import date

            sc = state.get("stock_code", "")
            _sd = {}
            if state.get("step_records"):
                _sd = state["step_records"][-1].get("shell_data") or {}
            _domain = state.get('domain', '')
            root = EvalNode(
                layer=Layer.CHAIN.value,
                name=f"{_domain}+{_verb}+{_noun}",
                exec_date=date.today(), stock_code=sc, stock_name=state.get("stock_name", ""),
                score=_sd.get("score") if _sd else None,
                direction=_sd.get("direction", "") if _sd else "",
                action=_sd.get("action", "") if _sd else "",
                signal=_sd.get("signal", "") if _sd else "",
                analysis=content[:2000] if content else "",
                input_params={"user_query": state.get("query", "")},
                status=Status.OK.value if content else Status.FAILED.value,
                tools_called=_aggregate_tools_called(state),
            )
            from app.agent.chain.store import save_tree
            save_tree(root)
        except Exception as e:
            logger.warning("[Trace] 兜底失败: %s", e)


def _try_answer_from_history(state: AgentState) -> str:
    """从 Checkpointer 读取上轮 state 并格式化。"""
    session_id = state.get("session_id", "")
    if not session_id:
        return "暂无历史记录。"

    try:
        from app.agent.graph import get_previous_state
        prev_state = get_previous_state(session_id)
        if not prev_state or not isinstance(prev_state, dict):
            return "暂无历史记录。"

        prev_output = prev_state.get("final_output", {})
        prev_records = prev_state.get("step_records", [])
        prev_verb = prev_state.get("intent_verb", "")
        prev_noun = prev_state.get("intent_noun", "")
        prev_stock = prev_state.get("stock_name", "")

        parts = ["上次执行的操作："]
        if prev_verb or prev_noun:
            parts.append(f"- 意图：{prev_verb} {prev_noun}")
        if prev_stock:
            parts.append(f"- 标的：{prev_stock}")
        if prev_records:
            for r in prev_records:
                desc = r.get("description", "")
                content = r.get("step_content", "")[:200]
                if desc or content:
                    parts.append(f"- {desc}: {content}")
        if prev_output:
            reply = prev_output.get("reply", "") or prev_output.get("analysis", "")
            if reply:
                parts.append(f"- 结论：{reply[:200]}")

        return "\n".join(parts) if len(parts) > 1 else "暂无历史记录。"

    except Exception as e:
        logger.warning("[History] 查询历史失败: %s", e)
        return "暂无历史记录。"
