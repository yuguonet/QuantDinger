# -*- coding: utf-8 -*-
"""
Agent — smolagents Agent for QuantDinger.

Supports both CodeAgent (LLM writes Python code) and ToolCallingAgent (OpenAI function calling).
Agent type is configurable via AGENT_TYPE env var:
  - "code" (default): CodeAgent — best for GPT-4o, Claude, DeepSeek etc.
  - "tool": ToolCallingAgent — best for local models (Ollama) that don't generate code blocks.
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
    """Return the agent class based on AGENT_TYPE env var."""
    agent_type = os.getenv("AGENT_TYPE", "code").strip().lower()
    if agent_type == "tool":
        return ToolCallingAgent
    return CodeAgent


# ═══════════════════════════════════════════════════════════════
# 1. Tool Catalog & Agent Instructions
# ═══════════════════════════════════════════════════════════════

TOOL_CATALOG = """## 可用工具

**名称/代码互查**: resolve_stock_name(代码→名称), search_stock_by_name(名称→代码，模糊搜索)
**行情数据**: get_realtime_quote(实时行情), agent_get_kline(多周期K线/OHLCV, 支持1m~1W), get_stock_info(基本面), get_market_indices(大盘指数), get_sector_rankings(板块排名)
**技术分析**: analyze_trend(五维技术分析:MA+MACD+RSI+BOLL+KDJ), calculate_ma(均线), get_volume_analysis(量价), analyze_pattern(15+K线形态), get_chip_distribution(筹码), get_indicator_snapshot(指标快照)
**情报搜索**: search_stock_news(新闻), search_comprehensive_intel(综合情报)
**选股**: screen_stocks(本地DB选股), smart_screen(综合选股), review_stocks_with_indicator(指标审核)
**指标策略**: list_indicators(列出指标), get_indicator_params(指标参数), run_indicator_signal(执行指标信号)
**回测**: run_backtest(跑回测), get_backtest_history(查历史回测记录)
**交易**: list_strategies(列出策略), get_strategy_detail(策略详情), start_strategy(启动策略), stop_strategy(停止策略), get_strategy_trades(交易记录)
**龙虎榜/热榜**: get_dragon_tiger_stocks, get_dragon_tiger_by_stock, get_hot_rank_stocks, get_zt_pool_stocks, get_limit_down_stocks, get_broken_board_stocks
**搜索**: web_search(DuckDuckGo), visit_webpage(访问网页), wikipedia_search(维基百科)
**代码执行**: Agent 原生 Python 代码（仅 CodeAgent），可组合多个工具返回值做自定义计算
**工作区**: save_script, load_script, list_workspace, shell_exec, run_background, poll_task
**源码扫描(只读)**: list_project_files, read_project_file, grep_project
**自修改**: self_modify_list_dirs, self_modify_read, self_modify_write, self_modify_create, self_modify_diff, self_modify_rollback

**使用提示：**
- 当用户只给中文股票名称没给代码时，必须先用 search_stock_by_name 查到代码再分析。
- agent_get_kline 是获取K线原始数据，get_backtest_history 是查询过去的回测记录。
- 你可以用 Python 代码组合多个工具的返回值，做自定义计算。
- 如需向用户提问，直接在回复文本中提问，用户会通过下一轮对话回答你。
"""

GUIDANCE = """## 工作指引

根据用户消息的性质，自主判断该怎么做：

**闲聊/打招呼** — 不需要调工具，直接友好回复。介绍自己是量化分析助手，提示用户可以问什么。

**股票分析** — 按需调用工具获取数据，建议流程：行情→技术面→形态→量能→情报→综合判断。
输出完整的分析结论和风险提示。当工具分析深度不够时，用 Python 代码做更深入的量化分析。

**选股筛选** — 用 screen_stocks 按条件筛选，再用 run_indicator_signal 验证信号，汇总推荐。

**回测验证** — 用 list_strategies/list_indicators 发现策略，用 run_backtest 执行，分析绩效指标。

**交易执行** — 先确认行情和信号，再用 start_strategy 启动，用 get_strategy_trades 监控。

**代码/数据分析** — 利用工作区工具保存和执行脚本，支持迭代优化。
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


def _build_instructions(user_message: str = "", skill_instructions: str = "", language: str = "zh") -> str:
    if str(language or "").lower().startswith("en"):
        lang_section = "\n## Output Language\n- Reply in English.\n- All JSON values in English.\n"
    else:
        lang_section = "\n## 输出语言\n- 使用中文回答。\n- 所有面向用户的文本值使用中文。\n"

    skill_section = ""
    if skill_instructions:
        skill_section = f"\n## 激活的交易技能\n\n{skill_instructions}\n"

    # 源码扫描能力提示
    scan_section = ""
    if os.getenv("AGENT_SCAN_PROJECT_READONLY", "true").lower() == "true":
        scan_section = """
## 源码扫描能力（只读）

你可以使用以下工具扫描项目源码，理解代码架构、查找 bug、分析数据流：
- `list_project_files` — 列出项目目录结构
- `read_project_file` — 读取源码文件内容
- `grep_project` — 搜索代码片段（支持正则）

这些工具是只读的，不会修改任何项目文件。
当用户要求分析项目结构、查找代码问题、理解数据流时，优先使用这些工具。

"""

    # 工具自修改能力提示
    modify_section = ""
    if os.getenv("AGENT_TOOLS_SELF_MODIFY", "false").lower() == "true":
        modify_paths = os.getenv("AGENT_SELF_MODIFY_PATHS", "backend_api_python/app/agent/tools")
        modify_section = f"""
## 自修改能力

你可以读写以下目录中的文件，实现 bug 修复、功能升级、新模块扩充：
允许的目录: {modify_paths}

可用工具：
- `self_modify_list_dirs` — 列出所有允许修改的目录及其文件
- `self_modify_read` — 读取文件完整源码
- `self_modify_diff_head` — 读取文件头部 N 行（快速预览）
- `self_modify_write` — 修改现有文件（自动备份原文件到 .agent_backups/）
- `self_modify_create` — 创建新文件
- `self_modify_diff` — 对比修改差异
- `self_modify_rollback` — 回滚到备份版本
- `self_modify_log` — 查看修改历史

安全约束：
- 每次修改自动备份原文件
- 只能修改配置的目录范围内的文件
- 修改后可能需重启 Agent 生效
- **先用 self_modify_read 理解现有代码，再做最小改动**
- 路径使用相对于项目根目录的格式（如 backend_api_python/app/services/llm.py）

"""

    preamble = _load_preamble()
    return f"""{preamble}

{GUIDANCE}

{TOOL_CATALOG}
{skill_section}{scan_section}{modify_section}## 规则

1. **必须调用工具获取真实数据** — 绝不编造数字，所有数据必须来自工具返回结果。
2. **深度优先** — 不要满足于工具的默认输出，当分析深度不够时直接写 Python 代码做更深入的量化分析。
3. **风险优先** — 分析必须包含风险提示，投资决策前先排查风险（股东减持、业绩预警、监管问题）。
4. **工具失败处理** — 记录失败原因，使用已有数据继续分析，不重复调用失败工具。
5. **多维验证** — 技术面结论应至少有 2 个以上指标相互验证，避免单一指标误判。
6. **善用代码** — 你是 CodeAgent，可以用 Python 代码组合工具、做计算、处理数据。不要局限于单次工具调用。
7. **诚实透明** — 数据不足时明确告知，不猜测，不掩饰不确定性。
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

    # Shared kwargs for all managed agents
    AgentClass = _get_agent_class()
    base_kwargs = dict(
        model=smol_model,
        max_steps=8,
        verbosity_level=LogLevel.INFO,
        stream_outputs=True,
        provide_run_summary=True,
    )

    # Analysis specialist
    analysis_tools = [t for t in tools if t.name in {
        "get_realtime_quote", "agent_get_kline", "analyze_trend",
        "analyze_pattern", "get_volume_analysis", "get_chip_distribution",
        "get_indicator_snapshot", "search_stock_news", "search_comprehensive_intel",
        "resolve_stock_name", "search_stock_by_name", 
    }]
    analysis_agent = AgentClass(
        tools=analysis_tools,
        name="analysis_agent",
        description="股票技术分析专家。负责获取行情数据、技术指标分析、K线形态识别、新闻搜索。当用户询问某只股票的分析时调用此Agent。",
        instructions="你是技术分析专家。严格按照：行情→形态→情报→分析 的四阶段流程执行。必须调用工具获取真实数据。",
        **base_kwargs,
    )

    # Screening specialist
    screening_tools = [t for t in tools if t.name in {
        "screen_stocks", "smart_screen", "get_screener_presets",
        "list_indicators", "run_indicator_signal", "review_stocks_with_indicator",
        "get_realtime_quote", "agent_get_kline", "resolve_stock_name",
        "search_stock_by_name", 
    }]
    screening_agent = AgentClass(
        tools=screening_tools,
        name="screening_agent",
        description="选股专家。负责从全市场筛选候选股、执行指标验证、给出推荐列表。当用户要求选股、筛选股票时调用此Agent。",
        instructions="你是选股专家。按条件筛选→指标验证→综合推荐的流程执行。优先使用 screen_stocks 做初筛。",
        **base_kwargs,
    )

    # Backtest specialist
    backtest_tools = [t for t in tools if t.name in {
        "run_backtest", "get_backtest_history", "list_strategies",
        "list_indicators", "get_indicator_params", 
    }]
    backtest_agent = AgentClass(
        tools=backtest_tools,
        name="backtest_agent",
        description="回测专家。负责执行策略回测、分析历史绩效（收益率、胜率、最大回撤、夏普比率）。当用户要求回测、验证策略时调用此Agent。",
        instructions="你是回测专家。发现策略→执行回测→分析绩效。重点分析风险调整后收益。",
        **base_kwargs,
    )

    return [analysis_agent, screening_agent, backtest_agent]


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
) -> "CodeAgent | ToolCallingAgent":
    global _agent_cache, _cached_tools_signature

    skill_instructions = _get_skill_instructions(skills, user_id)
    instructions = _build_instructions(user_message, skill_instructions, language)
    smol_model = build_model(model=model, provider=provider)
    tools = build_all_tools()
    managed_agents = _build_managed_agents(smol_model)
    AgentClass = _get_agent_class()

    sig = f"{len(tools)}_{model}_{provider}_{user_id}_{AgentClass.__name__}"
    if _agent_cache is not None and _cached_tools_signature == sig:
        _agent_cache.instructions = instructions
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
        planning_interval=3,               # Auto-plan every 3 steps
        managed_agents=managed_agents,     # Multi-agent dispatch
        final_answer_checks=[_check_dashboard_json],  # Validate output
        **_extra_kwargs,
    )

    _agent_cache = agent
    _cached_tools_signature = sig
    logger.info(
        "[Agent] Built %s: %d tools, %d managed agents, planning_interval=3, max_steps=%d",
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

        enriched = message
        ctx_parts = []
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
        )

        store.add_message(session_id, "user", message)
        return store, agent, enriched

    def chat(self, message, session_id, context=None,
             progress_callback=None, user_id=1) -> AgentResult:
        """Blocking chat — waits for full result."""
        store, agent, enriched = self._prepare(message, session_id, context, user_id)
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
        store, agent, enriched = self._prepare(message, session_id, context, user_id)
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
