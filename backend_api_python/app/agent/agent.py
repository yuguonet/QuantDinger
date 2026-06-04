# -*- coding: utf-8 -*-
"""
Agent — smolagents Agent for QuantDinger.

Supports both CodeAgent (LLM writes Python code) and ToolCallingAgent (OpenAI function calling).
Agent type is configurable via AGENT_TYPE env var:
  - "code": CodeAgent — best for GPT-4o, Claude, DeepSeek etc.
  - "tool": ToolCallingAgent — best for local models (Ollama) that don't generate code blocks.
  - (default): Auto-detect — uses ToolCallingAgent for Ollama, CodeAgent otherwise.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional

from smolagents import (
    CodeAgent,
    ToolCallingAgent,
    ActionStep,
    PlanningStep,
    FinalAnswerStep,
    LogLevel,
)
from smolagents.memory import ToolCall

from app.agent.model import build_model
from app.agent.tool_adapter import build_all_tools, load_tools_from_module
from app.agent.tool_context import set_tool_context

logger = logging.getLogger(__name__)

# ── Legacy excluded tool names ────────────────────────────────
_EXCLUDED_TOOL_NAMES = {
    "screen_stocks", "smart_screen",
    "get_stock_fund_flow", "batch_get_stock_fund_flow",
    "get_dragon_tiger_stocks", "get_dragon_tiger_by_stock",
    "get_hot_rank_stocks", "get_zt_pool_stocks",
    "get_limit_down_stocks", "get_broken_board_stocks",
}

# ── Per-user agent cache (tools + managed agents only) ────────
_tools_cache_by_domain: Dict[str, List] = {}       # key: domain → tools list
_managed_agents_cache: Dict[str, list] = {}         # key: model_provider → managed agents
_tools_cache_lock = __import__("threading").Lock()


def _get_agent_class():
    """Return the agent class based on AGENT_TYPE env var.

    Auto-detection: if the model is served by Ollama (localhost:11434),
    default to ToolCallingAgent since local models don't generate code blocks.
    """
    agent_type = os.getenv("AGENT_TYPE", "").strip().lower()
    if agent_type == "tool":
        return ToolCallingAgent
    if agent_type == "code":
        return CodeAgent
    # Auto-detect: check if the LLM endpoint is Ollama
    try:
        from app.services.llm import LLMService
        svc = LLMService()
        base_url = svc.get_base_url(svc.provider)
        if any(k in base_url for k in ("localhost:11434", "127.0.0.1:11434", "ollama")):
            logger.info("[Agent] Detected Ollama endpoint (%s), using ToolCallingAgent", base_url)
            return ToolCallingAgent
    except Exception as e:
        logger.debug("[Agent] Ollama auto-detect failed: %s", e)
    return CodeAgent


# ═══════════════════════════════════════════════════════════════
# 1. Tool Catalog & Agent Instructions
# ═══════════════════════════════════════════════════════════════

def _generate_tool_catalog(tools, managed_agents) -> str:
    """从工具对象自动生成分类目录。按 layer → category 二级分组。"""
    from app.agent.tools.registry import registry as tool_registry

    tool_names = {t.name for t in tools}
    layered = tool_registry.layered_categories  # {layer: {category: [tool_names]}}

    lines = []
    categorized = set()
    for layer, cats in layered.items():
        layer_tools = []
        for cat, names in cats.items():
            available = [n for n in names if n in tool_names]
            if available:
                if len(cats) > 1:
                    layer_tools.append(f"  {cat}: {', '.join(available)}")
                else:
                    layer_tools.append(f"  {', '.join(available)}")
                categorized.update(available)
        if layer_tools:
            lines.append(f"**{layer}**")
            lines.extend(layer_tools)

    # 未分类的工具
    uncategorized = tool_names - categorized - {"final_answer"}
    if uncategorized:
        lines.append(f"**未分层**: {', '.join(sorted(uncategorized))}")

    # Managed agents
    if managed_agents:
        ma_info = [f"{ma.name}({ma.description[:30]})" for ma in managed_agents]
        lines.append(f"\n**子Agent**: {', '.join(ma_info)}")

    return "\n".join(lines)


# ── GUIDANCE loaded from skills.guidance ──
from app.agent.skills.guidance import GUIDANCE


def _load_preamble() -> str:
    """Load agent preamble from external .md file, with built-in fallback."""
    import pathlib
    candidates = [
        pathlib.Path(__file__).resolve().parent / "agent_preamble.md",
        pathlib.Path(__file__).resolve().parent.parent.parent / "agent_preamble.md",
    ]
    for p in candidates:
        if p.is_file():
            try:
                return p.read_text(encoding="utf-8").strip()
            except Exception:
                pass
    # Fallback — embedded default
    return (
        "你是 QuantDinger 量化分析助手。你的职责是基于真实数据为用户提供专业、客观、"
        "可执行的金融分析与交易建议。\n\n"
        "## 核心原则\n\n"
        "- **数据驱动** — 所有结论必须有工具返回的数据支撑，绝不编造数字\n"
        "- **风险优先** — 分析必须包含风险提示，投资决策前先排查风险\n"
        "- **直接了当** — 跳过客套，直接给结论和依据\n"
        "- **诚实透明** — 数据不足时明确告知，不猜测，不掩饰不确定性"
    )


def _build_instructions(user_message: str = "", skill_instructions: str = "",
                        language: str = "zh", tools=None, managed_agents=None,
                        domain: str = "", domain_instructions: str = "",
                        intent_context: str = "") -> str:
    if str(language or "").lower().startswith("en"):
        lang_section = "\n## Output Language\n- Reply in English.\n- All JSON values in English.\n"
    else:
        lang_section = "\n## 输出语言\n- 使用中文回答。\n- 所有面向用户的文本值使用中文。\n"

    skill_section = ""
    if skill_instructions:
        skill_section = f"\n## 激活的交易技能\n\n{skill_instructions}\n"

    scan_section = ""
    if os.getenv("AGENT_SCAN_PROJECT_READONLY", "true").lower() == "true":
        scan_section = """
## 源码扫描能力（只读）

可使用 list_project_files、read_project_file、grep_project 扫描项目源码。
当用户要求分析项目结构、查找代码问题时使用。

"""

    modify_section = ""
    if os.getenv("AGENT_TOOLS_SELF_MODIFY", "false").lower() == "true":
        modify_paths = os.getenv("AGENT_SELF_MODIFY_PATHS", "backend_api_python/app/agent/tools")
        modify_section = f"""
## 自修改能力

允许修改目录: {modify_paths}
工具: self_modify_list_dirs, self_modify_read, self_modify_write, self_modify_create, self_modify_diff, self_modify_rollback
安全约束: 每次修改自动备份，只能修改配置目录范围内的文件，先用 self_modify_read 理解代码再做最小改动。

"""

    preamble = _load_preamble()

    # 动态生成工具分类目录
    tool_catalog = ""
    if tools is not None:
        tool_catalog = f"\n## 工具分类\n\n{_generate_tool_catalog(tools, managed_agents)}\n"

    # 意图分析上下文（前置分析器的输出）
    intent_section = ""
    if intent_context:
        intent_section = f"\n## 意图分析\n\n{intent_context}\n"

    # 领域专属指令
    domain_section = ""
    if domain_instructions:
        domain_section = f"\n## 当前领域: {domain}\n\n{domain_instructions}\n"

    return f"""{preamble}

{GUIDANCE}
{tool_catalog}
{skill_section}{scan_section}{modify_section}{intent_section}{domain_section}## 规则

0. **⚠️ 必须用 final_answer() 返回结果** — 完成任务后，必须调用 `final_answer(你的回复)` 来结束。
1. **不需要工具的消息，第一步就 final_answer** — 打招呼、闲聊等直接调用 final_answer。
2. **必须调用工具获取真实数据** — 绝不编造数字。
3. **深度优先** — 分析深度不够时用 Python 代码做量化分析。
4. **风险优先** — 分析必须包含风险提示。
5. **工具失败处理** — 记录失败原因，用已有数据继续，不重复调用。
6. **多维验证** — 技术面结论至少 2 个指标相互验证。
7. **善用工具** — 可以组合工具做计算、处理数据。
8. **诚实透明** — 数据不足时明确告知，不猜测。
{lang_section}"""


# ═══════════════════════════════════════════════════════════════
# 2. Skill Instructions (from indicator IDE)
# ═══════════════════════════════════════════════════════════════

# ── Indicator skills loaded from skills.indicator_skills ──
from app.agent.skills.indicator_skills import get_indicator_skill_instructions


# ═══════════════════════════════════════════════════════════════
# 3. Final Answer Validation (final_answer_checks)
# ═══════════════════════════════════════════════════════════════

def _check_dashboard_json(answer, memory, agent) -> bool:
    """Validate that the final answer is non-empty.

    Previously enforced JSON dashboard structure in analysis mode.
    Now that the LLM decides its own approach, accept any non-empty answer.
    """
    if not answer or not isinstance(answer, str):
        return False
    return bool(answer.strip())


# ═══════════════════════════════════════════════════════════════
# 4. smolagents step → QuantDinger SSE events
# ═══════════════════════════════════════════════════════════════

def _step_to_events(step) -> List[Dict[str, Any]]:
    """Convert a smolagents step (or intermediate event) into QuantDinger SSE events.

    Handles all types yielded by agent.run(stream=True):
    - ChatMessageStreamDelta: LLM token-by-token output
    - ToolCall: tool call about to execute
    - ToolOutput: tool call result
    - ActionOutput: final output of a step
    - ActionStep: complete step object
    - PlanningStep: planning step
    - FinalAnswerStep: final answer
    """
    events = []

    # ── Intermediate streaming events ─────────────────────────
    if isinstance(step, ToolCall):
        events.append({
            "type": "tool_start",
            "tool": step.name,
            "display_name": step.name,
            "arguments": step.arguments if isinstance(step.arguments, dict) else {},
        })
        return events

    # ToolOutput
    if hasattr(step, "observation") and hasattr(step, "tool_call") and not isinstance(step, ActionStep):
        tool_name = step.tool_call.name if step.tool_call else ""
        events.append({
            "type": "tool_done",
            "tool": tool_name,
            "display_name": tool_name,
            "success": True,
        })
        tool_name = step.tool_call.name if step.tool_call else ""
        if step.observation:
            import re
            obs = step.observation
            # smolagents 可能返回 dict 而非 str
            if isinstance(obs, dict):
                # 优先取 message 字段（工具返回值的标准字段）
                obs = obs.get("message", "") or str(obs)
            elif not isinstance(obs, str):
                obs = str(obs)
            # 提取图表标记，单独发给前端
            chart_match = re.search(r'__CHART_B64__([A-Za-z0-9+/=]+)__END_CHART__', obs)
            if chart_match:
                events.append({
                    "type": "chart",
                    "tool": tool_name,
                    "b64": chart_match.group(1),
                })
                # 从模型可见的输出中移除图表标记
                obs = obs[:chart_match.start()] + obs[chart_match.end():]
                obs = obs.strip()
            if obs:
                events.append({
                    "type": "tool_info",
                    "tool": tool_name,
                    "message": obs[:2000],
                })
        return events

    # ActionOutput (step-level, skip — ActionStep follows with complete info)
    if hasattr(step, "is_final_answer") and not isinstance(step, (ActionStep, FinalAnswerStep)):
        return events

    # ChatMessageStreamDelta
    if hasattr(step, "content") and hasattr(step, "token_usage") and not isinstance(step, (ActionStep, PlanningStep, FinalAnswerStep)):
        if step.content:
            events.append({
                "type": "tool_stream",
                "tool": "",
                "output": step.content,
            })
        return events

    # ── Complete step objects ──────────────────────────────────
    if isinstance(step, ActionStep):
        if step.tool_calls:
            for tc in step.tool_calls:
                events.append({
                    "type": "tool_start",
                    "tool": tc.name,
                    "display_name": tc.name,
                })

        if hasattr(step, "code_action") and step.code_action:
            events.append({
                "type": "tool_info",
                "tool": step.tool_calls[0].name if step.tool_calls else "",
                "message": f"执行代码:\n{step.code_action[:500]}",
            })

        if step.tool_calls:
            for tc in step.tool_calls:
                events.append({
                    "type": "tool_done",
                    "tool": tc.name,
                    "display_name": tc.name,
                    "success": step.error is None,
                    "duration": step.timing.duration if step.timing else 0,
                })

        if step.observations:
            import re as _re
            obs_text = step.observations
            if not isinstance(obs_text, str):
                obs_text = str(obs_text)
            # ActionStep 也可能携带图表标记（code-based tool call 场景）
            _chart_m = _re.search(r'__CHART_B64__([A-Za-z0-9+/=]+)__END_CHART__', obs_text)
            if _chart_m:
                events.append({
                    "type": "chart",
                    "tool": step.tool_calls[0].name if step.tool_calls else "",
                    "b64": _chart_m.group(1),
                })
                obs_text = (obs_text[:_chart_m.start()] + obs_text[_chart_m.end():]).strip()
            if obs_text:
                events.append({
                    "type": "tool_info",
                    "tool": step.tool_calls[0].name if step.tool_calls else "",
                    "message": obs_text[:2000],
                })

        if step.error:
            events.append({
                "type": "tool_info",
                "tool": step.tool_calls[0].name if step.tool_calls else "",
                "message": f"⚠️ {step.error}",
            })

        if step.model_output:
            events.append({
                "type": "thinking",
                "step": step.step_number,
                "message": step.model_output[:1000],
            })

    elif isinstance(step, PlanningStep):
        events.append({
            "type": "thinking",
            "step": 0,
            "message": step.plan[:1000] if step.plan else "正在规划...",
        })

    elif isinstance(step, FinalAnswerStep):
        events.append({
            "type": "generating",
            "step": 0,
            "message": "正在生成最终分析...",
        })

    return events


# ═══════════════════════════════════════════════════════════════
# 5. Managed Agents (专业子 Agent)
# ═══════════════════════════════════════════════════════════════

def _build_managed_agents(smol_model) -> list:
    """Build specialized sub-agents from skill registry.

    Skills are auto-discovered from app/agent/skills/*.py modules
    that use the @skill decorator. No hardcoded agent definitions here.
    """
    from app.agent.skills.registry import skill_registry

    tools = build_all_tools()
    tool_map = {t.name: t for t in tools}

    AgentClass = _get_agent_class()
    base_kwargs = dict(
        model=smol_model,
        max_steps=8,
        verbosity_level=LogLevel.INFO,
        stream_outputs=True,
        provide_run_summary=True,
    )

    # Discover and build from registry
    skill_registry.discover()
    agents = skill_registry.build_managed_agents(
        smol_model=smol_model,
        tool_map=tool_map,
        agent_class=AgentClass,
        base_kwargs=base_kwargs,
    )
    logger.info("[Agent] Built %d managed agents from skill registry", len(agents))
    return agents


# ═══════════════════════════════════════════════════════════════
# 6. Agent Builder
# ═══════════════════════════════════════════════════════════════

def _filter_tools_by_categories(all_tools: List, categories: List[str]) -> List:
    """按工具分类过滤工具列表（已废弃，保留向后兼容）。

    新方案使用 domain 过滤，见 get_smolagent() 中的 registry.build({"domain": ...})。
    """
    return all_tools


def get_smolagent(
    skills: Optional[List[str]] = None,
    user_id: int = 1,
    model: str = None,
    provider: str = None,
    max_steps: int = 10,
    user_message: str = "",
    language: str = "zh",
    domain: str = "",
    domain_instructions: str = "",
    intent_context: str = "",
    tool_categories: Optional[List[str]] = None,
) -> "CodeAgent | ToolCallingAgent":
    """Build a fresh agent instance per call.

    Caches only the expensive parts (tools discovery, managed agents).
    Agent instance is always rebuilt to avoid cross-session state pollution.
    """
    skill_instructions = get_indicator_skill_instructions(skills, user_id)
    smol_model = build_model(model=model, provider=provider)

    # ── 按领域过滤工具（缓存） ────────────────────────────────
    domain_key = domain or "all"
    with _tools_cache_lock:
        if domain_key not in _tools_cache_by_domain:
            from app.agent.tools.registry import registry as tool_registry
            tool_registry.discover()
            if domain:
                tools = tool_registry.build({"domain": domain, "deny": list(_EXCLUDED_TOOL_NAMES)})
            else:
                tools = tool_registry.build({"deny": list(_EXCLUDED_TOOL_NAMES)})

            _tools_cache_by_domain[domain_key] = tools
        else:
            tools = _tools_cache_by_domain[domain_key]

    # Managed agents（按 model+provider+domain 缓存，避免不同领域互相污染）
    ma_key = f"{model}_{provider}_{domain_key}"
    if ma_key not in _managed_agents_cache:
        _managed_agents_cache[ma_key] = _build_managed_agents(smol_model)
    managed_agents = _managed_agents_cache[ma_key]

    instructions = _build_instructions(
        user_message, skill_instructions, language, tools, managed_agents,
        domain=domain, domain_instructions=domain_instructions,
        intent_context=intent_context,
    )
    AgentClass = _get_agent_class()

    # ── Always build fresh agent (avoid cross-session state pollution) ──
    _extra_kwargs = {}
    if AgentClass is CodeAgent:
        _extra_kwargs["additional_authorized_imports"] = [
            "pandas", "numpy", "json", "math", "statistics",
            "datetime", "collections", "itertools", "re",
        ]

    agent = AgentClass(
        tools=tools,
        model=smol_model,
        max_steps=max_steps,
        instructions=instructions,
        verbosity_level=LogLevel.INFO,
        return_full_result=True,
        stream_outputs=True,
        planning_interval=None,
        managed_agents=managed_agents,
        final_answer_checks=[_check_dashboard_json],
        **_extra_kwargs,
    )

    logger.info(
        "[Agent] Built %s for user=%s domain=%s: %d tools, %d managed agents, max_steps=%d",
        AgentClass.__name__, user_id, domain_key, len(tools), len(managed_agents), max_steps,
    )
    return agent


# ═══════════════════════════════════════════════════════════════
# 7. Result Wrapper & Executor
# ═══════════════════════════════════════════════════════════════

class AgentResult:
    def __init__(self, success=False, content="", tool_calls_log=None,
                 total_steps=0, total_tokens=0, model="", error=None, charts=None):
        self.success = success
        self.content = content
        self.tool_calls_log = tool_calls_log or []
        self.total_steps = total_steps
        self.total_tokens = total_tokens
        self.model = model
        self.error = error
        self.charts = charts or []


def build_agent_executor(
    skills=None, user_id=1, max_steps=10,
    timeout_seconds=None, model=None, provider=None,
):
    return _AgentExecutor(
        skills=skills, user_id=user_id, max_steps=max_steps,
        timeout_seconds=timeout_seconds, model=model, provider=provider,
    )


class _AgentExecutor:
    """Wraps smolagents CodeAgent with session management."""

    def __init__(self, skills=None, user_id=1, max_steps=10,
                 timeout_seconds=None, model=None, provider=None):
        self.skills = skills
        self.user_id = user_id
        self.max_steps = max_steps
        self.timeout_seconds = timeout_seconds
        self.model = model
        self.provider = provider
        # Agent instance — set by _prepare(), used for interrupt support
        self._current_agent = None
        import threading as _threading
        self._agent_ready_event = _threading.Event()

    def _prepare(self, message, session_id, context, user_id):
        from app.agent.session_store import get_session_store
        store = get_session_store()
        set_tool_context({
            "session_id": session_id,
            "user_id": user_id,
            "progress_callback": None,
        })

        # ── 前置意图分析 ──────────────────────────────────────
        intent_context = ""
        domain = ""
        domain_instructions = ""
        tool_categories = None
        # skip_agent: 意图明确且不需要工具时（如打招呼），直接返回，不进 agent
        skip_agent = False
        skip_agent_reply = ""
        intent = None  # 供后置评估提取 verb/noun

        if os.getenv("INTENT_ANALYSIS_ENABLED", "true").lower() == "true":
            try:
                from app.agent.intent_analyzer import analyze_intent, format_intent_for_agent
                from app.agent.domain_registry import init_builtin_domains
                init_builtin_domains()
                # 取最近 3 轮对话历史，用于指代消解
                history = store.get_history(session_id)[-6:]
                intent = analyze_intent(
                    message, model=self.model, provider=self.provider,
                    history=history,
                )
                domain = intent.domain
                domain_instructions = intent.domain_instructions
                tool_categories = intent.tool_categories or None
                intent_context = format_intent_for_agent(intent, message)
                logger.info(
                    "[Intent] domain=%s intent=%s confidence=%.2f categories=%s",
                    intent.domain, intent.intent, intent.confidence,
                    intent.tool_categories or [],
                )
                # Update tool context with domain for per-domain workspace isolation
                from app.agent.tool_context import get_tool_context
                _ctx = get_tool_context()
                _ctx["domain"] = domain
                set_tool_context(_ctx)

                # ── 闲聊/greeting 快速通道：跳过 agent，直接回复 ──
                # 本地模型（如 gemma4）经常忽略 final_answer 指令，
                # 把问候语包在 <code> 块里导致解析失败。
                # 对于置信度 >= 0.6 的 chat 意图，直接构造回复，不走 agent。
                if (domain == "chat"
                        and intent.confidence >= 0.6
                        and intent.intent in ("greeting", "farewell", "thanks", "empty")):
                    skip_agent = True
                    _quick_replies = {
                        "greeting": "你好！我是 QuantDinger 量化分析助手，可以帮你做股票分析、选股筛选、策略回测等。有什么需要帮忙的？",
                        "farewell": "再见！有问题随时找我。",
                        "thanks": "不客气！有需要随时找我。",
                        "empty": "请告诉我你需要什么帮助。",
                    }
                    skip_agent_reply = _quick_replies.get(intent.intent, "你好！有什么需要帮忙的？")
                    logger.info("[Intent] Quick-reply for %s, skipping agent", intent.intent)

            except Exception as e:
                import traceback
                logger.warning("[Intent] 分析失败，走默认流程: %s\n%s", e, traceback.format_exc())

        # ── 快速通道：不需要 agent 时直接返回 ────────────────
        if skip_agent:
            store.add_message(session_id, "user", message)
            # Signal "ready" with no agent (skip path)
            self._agent_ready_event.set()
            return store, None, message, {"skip_agent": True, "skip_agent_reply": skip_agent_reply}

        # ── 提取意图信息，供后置评估使用 ──────────────────────
        _eval_verb = ""
        _eval_noun = ""
        _eval_tool_chain = []
        if intent is not None:
            _eval_verb = getattr(intent, 'verb', '') or ""
            _eval_noun = getattr(intent, 'noun', '') or ""
            _eval_tool_chain = (getattr(intent, 'metadata', None) or {}).get("tool_chain", [])

        # ── 上下文拼接 ────────────────────────────────────────
        enriched = message
        ctx_parts = []

        # 压缩上下文（上轮分析摘要，领域切换时自动丢弃）
        context_summary = store.get_context_summary(session_id, current_domain=domain)
        if context_summary:
            ctx_parts.append(f"[上轮分析摘要]\n{context_summary}")

        if context:
            if context.get("stock_code"):
                ctx_parts.append(f"股票代码: {context['stock_code']}")
            if context.get("stock_name"):
                ctx_parts.append(f"股票名称: {context['stock_name']}")
            if context.get("realtime_quote"):
                ctx_parts.append(f"[已获取的实时行情]\n{json.dumps(context['realtime_quote'], ensure_ascii=False)[:2000]}")
            if context.get("chip_distribution"):
                ctx_parts.append(f"[已获取的筹码分布]\n{json.dumps(context['chip_distribution'], ensure_ascii=False)[:2000]}")
        if ctx_parts:
            enriched = "\n".join(ctx_parts) + "\n\n" + message

        agent = get_smolagent(
            skills=self.skills, user_id=user_id,
            model=self.model, provider=self.provider,
            max_steps=self.max_steps, user_message=message,
            language=(context or {}).get("report_language", "zh"),
            domain=domain, domain_instructions=domain_instructions,
            intent_context=intent_context,
            tool_categories=tool_categories,
        )

        store.add_message(session_id, "user", message)
        # 暂存当前 domain，供压缩线程读取
        if domain:
            store.save_context_summary(session_id, "", domain=domain)

        # Expose agent for interrupt support (set before agent.run starts)
        self._current_agent = agent
        self._agent_ready_event.set()

        return store, agent, enriched, {
            "skip_agent": False, "skip_agent_reply": "",
            "intent_verb": _eval_verb, "intent_noun": _eval_noun,
            "tool_chain": _eval_tool_chain,
            "domain": domain,
        }

    def chat(self, message, session_id, context=None,
             progress_callback=None, user_id=1) -> AgentResult:
        """Blocking chat — waits for full result.

        Per-session lock prevents concurrent requests on the same session
        from interleaving get_history / add_message calls.
        """
        store = get_session_store()
        with store.session_lock(session_id):
            return self._chat_locked(message, session_id, context, progress_callback, user_id)

    def _chat_locked(self, message, session_id, context, progress_callback, user_id) -> AgentResult:
        """Internal chat implementation — assumes session lock is held."""
        store, agent, enriched, meta = self._prepare(message, session_id, context, user_id)

        # ── 快速通道：不需要 agent 的简单回复 ────────────────
        if meta.get("skip_agent"):
            reply = meta["skip_agent_reply"]
            store.add_message(session_id, "assistant", reply)
            return AgentResult(
                success=True, content=reply,
                tool_calls_log=[], total_steps=0, total_tokens=0,
                model="intent-quick-reply", error=None,
            )

        # 保存意图信息，供后置评估使用
        _intent_verb = meta.get("intent_verb", "")
        _intent_noun = meta.get("intent_noun", "")
        _tool_chain = meta.get("tool_chain", [])
        _eval_domain = meta.get("domain", "")

        t0 = time.time()
        try:
            result = agent.run(enriched, max_steps=self.max_steps)

            if hasattr(result, "output"):
                content = str(result.output) if result.output else ""
                total_steps = len(result.steps) if result.steps else 0
                tu = result.token_usage
                total_tokens = (tu.input_tokens + tu.output_tokens) if tu else 0
                success = result.state == "success"
                tool_calls_log = []
                charts_b64 = []
                import re as _re_chat
                for sd in result.steps:
                    if sd.get("type") == "action":
                        for tc in sd.get("tool_calls", []):
                            tool_calls_log.append({
                                "tool": tc.get("name", ""),
                                "arguments": tc.get("arguments", {}),
                                "success": sd.get("error") is None,
                                "duration": sd.get("timing", {}).get("duration", 0),
                            })
                    # 从所有步骤的 observations 中提取图表标记
                    obs = sd.get("observations") or sd.get("observation") or ""
                    if obs and isinstance(obs, str):
                        for _cm in _re_chat.finditer(r'__CHART_B64__([A-Za-z0-9+/=]+)__END_CHART__', obs):
                            charts_b64.append(_cm.group(1))
            else:
                content = str(result) if result else ""
                total_steps = total_tokens = 0
                tool_calls_log = []
                charts_b64 = []
                # Extract chart markers from content even in fallback path
                import re as _re_fallback
                if content:
                    for _cm in _re_fallback.finditer(r'__CHART_B64__([A-Za-z0-9+/=]+)__END_CHART__', content):
                        charts_b64.append(_cm.group(1))
                    content = _re_fallback.sub(r'__CHART_B64__[A-Za-z0-9+/=]+__END_CHART__', '', content).strip()
                success = bool(content)

            store.add_message(session_id, "assistant", content)

            # ── 后置评估 + 工具链学习闭环 ─────────────────────
            agent_result_for_eval = AgentResult(
                success=success, content=content, tool_calls_log=tool_calls_log,
                total_steps=total_steps, total_tokens=total_tokens,
            )
            self._post_evaluate(agent_result_for_eval, _tool_chain, _intent_verb, _intent_noun, domain=_eval_domain)

            # 异步压缩上下文（不阻塞返回）
            if success and content:
                try:
                    from app.agent.context_compressor import compress_context
                    import threading
                    def _compress(c=content, tc=tool_calls_log, sid=session_id, m=self.model):
                        try:
                            summary = compress_context(c, tc, model=m)
                        except Exception as e:
                            logger.warning("[Compress] 压缩异常，降级截断: %s", e)
                            summary = c[:500]
                        if summary:
                            store.save_context_summary(sid, summary)
                    threading.Thread(target=_compress, daemon=True).start()
                except Exception:
                    pass

            return AgentResult(
                success=success, content=content, tool_calls_log=tool_calls_log,
                total_steps=total_steps, total_tokens=total_tokens,
                model=str(getattr(agent.model, "model_id", "")),
                error=None if success else "Agent did not produce a final answer",
                charts=charts_b64,
            )
        except Exception as e:
            logger.error("[Agent] chat failed: %s", e, exc_info=True)
            store.add_message(session_id, "assistant", f"[分析失败] {e}")
            return AgentResult(success=False, error=str(e))

    @staticmethod
    def _post_evaluate(agent_result, tool_chain, verb, noun, domain=""):
        """后置评估 + 工具链学习闭环（纯规则，不消耗 agent 步数）。"""
        if not verb and not noun:
            return  # 无意图信息，跳过评估
        try:
            from app.agent.evaluator import evaluate, learn_from_execution
            eval_result = evaluate(agent_result, tool_chain, verb, noun, domain=domain)
            learn_from_execution(eval_result, verb, noun)
        except Exception as e:
            logger.warning("[PostEval] 评估异常，不影响返回: %s", e)

    def chat_stream(self, message, session_id, context=None,
                    progress_callback=None, user_id=1):
        """Streaming chat — yields SSE event dicts as smolagents produces steps.

        Per-session lock prevents concurrent requests on the same session
        from interleaving get_history / add_message calls.
        """
        store = get_session_store()
        with store.session_lock(session_id):
            yield from self._chat_stream_locked(message, session_id, context, progress_callback, user_id)

    def _chat_stream_locked(self, message, session_id, context, progress_callback, user_id):
        """Internal streaming chat — assumes session lock is held."""
        store, agent, enriched, meta = self._prepare(message, session_id, context, user_id)

        # ── 快速通道：不需要 agent 的简单回复 ────────────────
        if meta.get("skip_agent"):
            reply = meta["skip_agent_reply"]
            store.add_message(session_id, "assistant", reply)
            yield {
                "type": "generating",
                "step": 0,
                "message": reply,
            }
            yield {
                "type": "done",
                "success": True,
                "content": reply,
                "error": None,
                "total_steps": 0,
                "model": "intent-quick-reply",
                "session_id": session_id,
            }
            return

        t0 = time.time()
        _stream_tool_calls = []  # 收集流式执行中的工具调用
        _stream_tool_call_counter = 0  # Unique ID for each tool call
        _pending_tool_ids: Dict[str, int] = {}  # tool_name → most recent index
        try:
            for step in agent.run(enriched, max_steps=self.max_steps, stream=True):
                events = _step_to_events(step)
                for ev in events:
                    if progress_callback:
                        progress_callback(ev)
                    yield ev
                    # 收集工具调用信息
                    if ev.get("type") == "tool_start":
                        _stream_tool_calls.append({
                            "tool": ev.get("tool", ""),
                            "success": True,  # 先假设成功
                            "_id": _stream_tool_call_counter,
                        })
                        _pending_tool_ids[ev.get("tool", "")] = _stream_tool_call_counter
                        _stream_tool_call_counter += 1
                    elif ev.get("type") == "tool_done":
                        # Update by unique ID, not name
                        tool_name = ev.get("tool", "")
                        pending_id = _pending_tool_ids.pop(tool_name, None)
                        if pending_id is not None:
                            for tc in _stream_tool_calls:
                                if tc.get("_id") == pending_id:
                                    tc["success"] = ev.get("success", True)
                                    break

                if isinstance(step, FinalAnswerStep):
                    content = str(step.output) if step.output else ""
                    store.add_message(session_id, "assistant", content)

                    # ── 后置评估 + 工具链学习闭环 ─────────────
                    _eval_result = AgentResult(
                        success=bool(content), content=content,
                        tool_calls_log=_stream_tool_calls,
                        total_steps=agent.step_number,
                    )
                    self._post_evaluate(
                        _eval_result,
                        meta.get("tool_chain", []),
                        meta.get("intent_verb", ""),
                        meta.get("intent_noun", ""),
                        domain=meta.get("domain", ""),
                    )

                    # 压缩上下文
                    if content:
                        try:
                            from app.agent.context_compressor import compress_context
                            import threading
                            def _compress(c=content, tc=_stream_tool_calls, sid=session_id, m=self.model):
                                try:
                                    summary = compress_context(c, tc, model=m)
                                except Exception as e:
                                    logger.warning("[Compress] 压缩异常，降级截断: %s", e)
                                    summary = c[:500]
                                if summary:
                                    store.save_context_summary(sid, summary)
                            threading.Thread(target=_compress, daemon=True).start()
                        except Exception:
                            pass

                    yield {
                        "type": "done",
                        "success": bool(content),
                        "content": content,
                        "error": None if content else "No final answer",
                        "total_steps": agent.step_number,
                        "model": str(getattr(agent.model, "model_id", "")),
                        "session_id": session_id,
                    }
        except Exception as e:
            logger.error("[Agent] chat_stream failed: %s", e, exc_info=True)
            store.add_message(session_id, "assistant", f"[分析失败] {e}")
            yield {"type": "error", "message": str(e)}
