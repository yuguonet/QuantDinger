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
from app.agent.tool_adapter import build_all_tools
from app.agent.tool_context import set_tool_context

logger = logging.getLogger(__name__)

# ── Singleton cache ───────────────────────────────────────────
_agent_cache = None  # type: ignore
_cached_tools_signature: str = ""


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
        if "localhost:11434" in base_url or "127.0.0.1:11434" in base_url:
            logger.info("[Agent] Detected Ollama endpoint (%s), using ToolCallingAgent", base_url)
            return ToolCallingAgent
    except Exception:
        pass
    return CodeAgent


# ═══════════════════════════════════════════════════════════════
# 1. Tool Catalog & Agent Instructions
# ═══════════════════════════════════════════════════════════════

def _generate_tool_catalog(tools, managed_agents) -> str:
    """从工具对象自动生成分类目录，替代硬编码 TOOL_CATALOG。"""
    categories = {
        "名称查询": ["resolve_stock_name", "search_stock_by_name"],
        "行情数据": ["get_realtime_quote", "agent_get_kline", "get_stock_info",
                    "get_market_indices", "get_sector_rankings"],
        "技术分析": ["analyze_trend", "get_indicator_snapshot", "calculate_ma",
                    "get_volume_analysis", "analyze_pattern", "get_chip_distribution"],
        "情报搜索": ["search_stock_news", "search_comprehensive_intel"],
        "选股": ["search_stocks", "get_screener_presets"],
        "指标策略": ["list_indicators", "get_indicator_params", "run_indicator_signal"],
        "回测": ["run_backtest", "get_backtest_history"],
        "交易": ["list_strategies", "get_strategy_detail", "start_strategy",
                "stop_strategy", "get_strategy_trades"],
        "龙虎榜/热榜": ["get_dragon_tiger", "get_hot_rank", "get_zt_pool",
                      "get_limit_down", "get_broken_board"],
        "资金流向": ["get_fund_flow", "get_sector_fund_flow", "get_concept_fund_flow"],
        "市场快照": ["get_market_overview"],
        "搜索": ["web_search", "visit_webpage", "wikipedia_search"],
        "工作区": ["save_script", "load_script", "list_workspace",
                  "shell_exec", "exec_script", "run_background", "poll_task"],
        "源码扫描(只读)": ["list_project_files", "read_project_file", "grep_project"],
        "自修改": ["self_modify_list_dirs", "self_modify_read", "self_modify_write",
                  "self_modify_create", "self_modify_diff", "self_modify_rollback"],
    }
    tool_names = {t.name for t in tools}
    lines = []
    for cat, names in categories.items():
        available = [n for n in names if n in tool_names]
        if available:
            lines.append(f"**{cat}**: {', '.join(available)}")
    # 列出未分类的工具
    categorized = set()
    for names in categories.values():
        categorized.update(names)
    uncategorized = tool_names - categorized - {"final_answer"}
    if uncategorized:
        lines.append(f"**其他**: {', '.join(sorted(uncategorized))}")

    # Managed agents
    if managed_agents:
        ma_info = []
        for ma in managed_agents:
            ma_info.append(f"{ma.name}({ma.description[:30]})")
        lines.append(f"\n**子Agent**: {', '.join(ma_info)}")

    return "\n".join(lines)


GUIDANCE = """## 核心规则

0. **⚠️ 必须用 final_answer() 返回结果** — 这是唯一能正确终止的方式。
1. **不需要工具的消息，第一步就 final_answer** — 打招呼、闲聊等直接回复。
2. **必须调用工具获取真实数据** — 绝不编造数字。
3. **深度优先** — 分析深度不够时用 Python 代码做更深入的量化分析。
4. **风险优先** — 分析必须包含风险提示。
5. **工具失败处理** — 记录失败原因，用已有数据继续，不重复调用。
6. **多维验证** — 技术面结论至少 2 个指标相互验证。
7. **诚实透明** — 数据不足时明确告知。

### 任务流程

**股票分析** — 行情→技术面→形态→量能→情报→综合判断。用 final_answer 返回。
**选股筛选** — 用 search_stocks 按条件筛选，再用 run_indicator_signal 验证。
**回测验证** — 用 list_strategies 发现策略，用 run_backtest 执行，分析绩效。
**交易执行** — 先确认行情和信号，再用 start_strategy 启动。

**重要提示：**
- 当用户只给中文名称没给代码时，必须先用 search_stock_by_name 查到代码。
- get_indicator_snapshot 一次获取全部技术指标，比多次调用 analyze_trend 更高效。
- search_stocks 支持自然语言条件，无需手动构建 filters 字典。
"""


def _load_preamble() -> str:
    """Load agent preamble from external .md file, with built-in fallback."""
    import pathlib
    # Try project root first, then backend_api_python/
    candidates = [
        pathlib.Path(__file__).resolve().parents[3] / "agent_preamble.md",
        pathlib.Path(__file__).resolve().parents[2] / "agent_preamble.md",
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

def _get_skill_instructions(skills: Optional[List[str]] = None, user_id: int = 1) -> str:
    if not skills:
        return ""
    indicator_ids = None
    try:
        indicator_ids = [int(s) for s in skills if str(s).isdigit()]
    except (ValueError, AttributeError):
        pass
    if not indicator_ids:
        return ""
    try:
        from app.services.indicator_analyzer import build_agent_skill_instructions
        return build_agent_skill_instructions(user_id=user_id, indicator_ids=indicator_ids)
    except Exception as e:
        logger.warning("[Agent] IndicatorAnalyzer unavailable: %s", e)
        return ""


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
            events.append({
                "type": "tool_info",
                "tool": tool_name,
                "message": step.observation[:2000],
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
            events.append({
                "type": "tool_info",
                "tool": step.tool_calls[0].name if step.tool_calls else "",
                "message": step.observations[:2000],
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
    """Build specialized sub-agents for different domains.

    Each managed agent focuses on one domain and is called by the main agent
    when the task falls into that domain.
    """
    tools = build_all_tools()
    tool_map = {t.name: t for t in tools}

    def pick(*names):
        return [tool_map[n] for n in names if n in tool_map]

    AgentClass = _get_agent_class()
    base_kwargs = dict(
        model=smol_model,
        max_steps=8,
        verbosity_level=LogLevel.INFO,
        stream_outputs=True,
        provide_run_summary=True,
    )

    # 1. Analysis specialist — 个股分析全流程
    analysis_agent = AgentClass(
        tools=pick(
            "get_realtime_quote", "agent_get_kline", "get_stock_info",
            "analyze_trend", "get_indicator_snapshot",
            "calculate_ma", "get_volume_analysis", "analyze_pattern",
            "get_chip_distribution", "search_stock_news", "search_comprehensive_intel",
            "resolve_stock_name", "search_stock_by_name",
            "get_market_indices", "get_sector_rankings",
            "get_fund_flow",
        ),
        name="analysis_agent",
        description="股票分析专家。负责个股分析：行情→技术面→形态→量能→情报→综合判断。当用户询问某只股票的分析时调用。",
        instructions="你是技术分析专家。按行情→形态→情报→分析流程执行。优先用 get_indicator_snapshot 一次获取全部指标。必须调用工具获取真实数据。",
        **base_kwargs,
    )

    # 2. Screening specialist — 选股和推荐
    screening_agent = AgentClass(
        tools=pick(
            "search_stocks", "get_screener_presets",
            "get_zt_pool", "get_dragon_tiger", "get_hot_rank",
            "get_limit_down", "get_broken_board",
            "list_indicators", "run_indicator_signal", "review_stocks_with_indicator",
            "get_realtime_quote", "agent_get_kline",
            "resolve_stock_name", "search_stock_by_name",
        ),
        name="screening_agent",
        description="选股专家。负责全市场筛选：条件选股→龙虎榜→涨停池→热榜→指标验证。当用户要求选股、筛选股票时调用。",
        instructions="你是选股专家。用 search_stocks 按条件筛选，再用 run_indicator_signal 验证信号。优先使用自然语言条件。",
        **base_kwargs,
    )

    # 3. Backtest specialist — 策略验证
    backtest_agent = AgentClass(
        tools=pick(
            "run_backtest", "get_backtest_history",
            "list_strategies", "get_strategy_detail",
            "list_indicators", "get_indicator_params", "run_indicator_signal",
        ),
        name="backtest_agent",
        description="回测专家。负责执行策略回测、分析历史绩效（收益率、胜率、最大回撤、夏普比率）。当用户要求回测、验证策略时调用。",
        instructions="你是回测专家。发现策略→执行回测→分析绩效。重点分析风险调整后收益。",
        **base_kwargs,
    )

    # 4. Data engineering specialist — 代码执行和数据处理
    data_agent = AgentClass(
        tools=pick(
            "save_script", "load_script", "list_workspace",
            "shell_exec", "exec_script", "run_background", "poll_task",
            "agent_get_kline", "get_realtime_quote",
        ),
        name="data_agent",
        description="数据工程专家。负责代码执行、数据清洗、自定义分析脚本、批量数据处理。当用户要求写代码、跑脚本、处理数据时调用。",
        instructions="你是数据工程专家。用工作区工具保存和执行脚本，支持迭代优化。长时间任务用 run_background 后台执行。",
        **base_kwargs,
    )

    return [analysis_agent, screening_agent, backtest_agent, data_agent]


# ═══════════════════════════════════════════════════════════════
# 6. Agent Builder
# ═══════════════════════════════════════════════════════════════

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
) -> "CodeAgent | ToolCallingAgent":
    global _agent_cache, _cached_tools_signature

    skill_instructions = _get_skill_instructions(skills, user_id)
    smol_model = build_model(model=model, provider=provider)
    tools = build_all_tools()
    managed_agents = _build_managed_agents(smol_model)
    instructions = _build_instructions(
        user_message, skill_instructions, language, tools, managed_agents,
        domain=domain, domain_instructions=domain_instructions,
        intent_context=intent_context,
    )
    AgentClass = _get_agent_class()

    sig = f"{len(tools)}_{model}_{provider}_{user_id}_{AgentClass.__name__}"
    if _agent_cache is not None and _cached_tools_signature == sig:
        _agent_cache.instructions = instructions
        _agent_cache._setup_managed_agents(managed_agents)
        _agent_cache.max_steps = max_steps
        _agent_cache.planning_interval = None
        return _agent_cache

    # additional_authorized_imports is only supported by CodeAgent, not ToolCallingAgent.
    # Passing it to ToolCallingAgent causes: MultiStepAgent.__init__() got an unexpected keyword argument
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
        stream_outputs=True,               # LLM token-by-token streaming
        planning_interval=None,            # Don't auto-inject planning steps — let the LLM decide
        managed_agents=managed_agents,     # Multi-agent dispatch
        final_answer_checks=[_check_dashboard_json],  # Validate output
        **_extra_kwargs,
    )

    _agent_cache = agent
    _cached_tools_signature = sig
    logger.info(
        "[Agent] Built %s: %d tools, %d managed agents, planning_interval=None, max_steps=%d",
        AgentClass.__name__, len(tools), len(managed_agents), max_steps,
    )
    return agent


# ═══════════════════════════════════════════════════════════════
# 7. Result Wrapper & Executor
# ═══════════════════════════════════════════════════════════════

class AgentResult:
    def __init__(self, success=False, content="", tool_calls_log=None,
                 total_steps=0, total_tokens=0, model="", error=None):
        self.success = success
        self.content = content
        self.tool_calls_log = tool_calls_log or []
        self.total_steps = total_steps
        self.total_tokens = total_tokens
        self.model = model
        self.error = error


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
        # skip_agent: 意图明确且不需要工具时（如打招呼），直接返回，不进 agent
        skip_agent = False
        skip_agent_reply = ""

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
                intent_context = format_intent_for_agent(intent, message)
                logger.info(
                    "[Intent] domain=%s intent=%s confidence=%.2f",
                    intent.domain, intent.intent, intent.confidence,
                )

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
                logger.warning("[Intent] 分析失败，走默认流程: %s", e)

        # ── 快速通道：不需要 agent 时直接返回 ────────────────
        if skip_agent:
            store.add_message(session_id, "user", message)
            return store, None, message, {"skip_agent": True, "skip_agent_reply": skip_agent_reply}

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
        )

        store.add_message(session_id, "user", message)
        # 暂存当前 domain，供压缩线程读取
        if domain:
            store.save_context_summary(session_id, "", domain=domain)

        return store, agent, enriched, {"skip_agent": False, "skip_agent_reply": ""}

    def chat(self, message, session_id, context=None,
             progress_callback=None, user_id=1) -> AgentResult:
        """Blocking chat — waits for full result."""
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
                for sd in result.steps:
                    if sd.get("type") == "action":
                        for tc in sd.get("tool_calls", []):
                            tool_calls_log.append({
                                "tool": tc.get("name", ""),
                                "arguments": tc.get("arguments", {}),
                                "success": sd.get("error") is None,
                                "duration": sd.get("timing", {}).get("duration", 0),
                            })
            else:
                content = str(result) if result else ""
                total_steps = total_tokens = 0
                tool_calls_log = []
                success = bool(content)

            store.add_message(session_id, "assistant", content)

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
            )
        except Exception as e:
            logger.error("[Agent] chat failed: %s", e, exc_info=True)
            store.add_message(session_id, "assistant", f"[分析失败] {e}")
            return AgentResult(success=False, error=str(e))

    def chat_stream(self, message, session_id, context=None,
                    progress_callback=None, user_id=1):
        """Streaming chat — yields SSE event dicts as smolagents produces steps."""
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
        try:
            for step in agent.run(enriched, max_steps=self.max_steps, stream=True):
                events = _step_to_events(step)
                for ev in events:
                    if progress_callback:
                        progress_callback(ev)
                    yield ev

                if isinstance(step, FinalAnswerStep):
                    content = str(step.output) if step.output else ""
                    store.add_message(session_id, "assistant", content)

                    # 压缩上下文
                    if content:
                        try:
                            from app.agent.context_compressor import compress_context
                            import threading
                            def _compress(c=content, sid=session_id, m=self.model):
                                try:
                                    summary = compress_context(c, model=m)
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
