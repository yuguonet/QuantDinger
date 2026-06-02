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
import re
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
# 1. Workflow Templates & Intent Detection
# ═══════════════════════════════════════════════════════════════

INTENT_KEYWORDS = {
    "screening": ["选股", "筛选", "选股票", "找股票", "初选", "股票池", "screen", "filter", "scan"],
    "backtest": ["回测", "回验", "验证", "历史表现", "过去表现", "backtest", "test"],
    "trading": ["买入", "卖出", "交易", "下单", "启动策略", "执行", "buy", "sell", "trade", "execute"],
    "full_pipeline": ["全流程", "完整流程", "一站式", "从头到尾", "pipeline", "full"],
    "code_analysis": ["写脚本", "写代码", "代码分析", "迭代", "工作区", "脚本", "数据分析",
                      "write script", "code", "iterate", "workspace", "script"],
    "analysis": ["分析", "行情", "走势", "技术面", "基本面", "怎么看", "analyze", "analysis"],
}

WORKFLOW_TEMPLATES = {
    "analysis": """## 分析工作流程（必须严格按阶段执行，每阶段等工具结果返回后再进入下一阶段）

**第一阶段 · 行情与技术面**（首先执行）
- `get_realtime_quote` 获取实时行情
- `agent_get_kline` 获取历史K线（建议 days=120，短线分析可用 15m/1H 等短周期）
- `analyze_trend` 综合技术分析（MA + MACD + RSI + BOLL + KDJ 五维共振）

**第二阶段 · 形态与量能**（等第一阶段结果返回后执行）
- `analyze_pattern` 识别K线形态（15+ 种经典形态）
- `get_volume_analysis` 分析量能与量价关系
- `get_chip_distribution` 获取筹码分布（仅A股）

**第三阶段 · 情报搜索**（等前两阶段完成后执行）
- `search_stock_news` 搜索最新资讯、减持、业绩预告等风险信号

**第四阶段 · 深度分析与报告**（所有数据就绪后，输出完整决策仪表盘 JSON）
- 当预设工具分析深度不够时，直接写 Python 代码做量化分析
- 可以用代码组合多个工具的返回值，做自定义计算和可视化""",

    "screening": """## 选股筛选工作流程

**第一步 · 条件筛选**
- 使用 `screen_stocks` 按行业、概念、涨跌幅、换手率等条件从全市场筛选候选股

**第二步 · 指标验证**（对候选股逐只执行）
- 使用 `list_indicators` 查看可用指标策略
- 使用 `run_indicator_signal` 对每只候选股执行指标策略，检查是否出现买入信号

**第三步 · 综合推荐**
- 汇总有买入信号的股票，分析信号强度
- 给出推荐列表和理由""",

    "backtest": """## 回测验证工作流程

**第一步 · 发现策略**
- 使用 `list_strategies` 列出用户所有交易策略
- 使用 `list_indicators` 列出可用指标策略

**第二步 · 执行回测**
- 使用 `run_backtest` 对指定策略在指定股票和时间范围内跑回测

**第三步 · 分析绩效**
- 分析回测结果：收益率、胜率、最大回撤、夏普比率
- 使用 `get_backtest_history` 查看历史回测记录做对比""",

    "trading": """## 交易执行工作流程

**第一步 · 确认信号**
- 使用 `get_realtime_quote` 确认当前行情
- 使用 `run_indicator_signal` 确认是否出现交易信号

**第二步 · 确认策略**
- 使用 `list_strategies` 列出可用策略
- 使用 `get_strategy_detail` 确认策略配置

**第三步 · 执行交易**
- 使用 `start_strategy` 启动策略执行
- 使用 `get_strategy_trades` 监控最近交易记录""",

    "full_pipeline": """## 完整量化交易流水线

**第一步 · 选股初筛**
- 使用 `screen_stocks` 从全市场筛选候选股

**第二步 · 指标精筛**
- 使用 `run_indicator_signal` 对候选股执行指标策略

**第三步 · 回测验证**
- 使用 `run_backtest` 对筛选出的标的跑历史回测

**第四步 · 交易执行**
- 使用 `start_strategy` 对通过验证的标的启动策略""",

    "code_analysis": """## 迭代代码分析工作流程

你拥有一个持久化的工作区，可以保存脚本、读写文件、执行代码并迭代优化。

**核心循环（按需重复）：**

1. **规划** — 明确分析目标，拆解为可执行的步骤
2. **编写代码** — 用 `save_script` 保存脚本到工作区
3. **执行** — 用 Python 代码直接执行（支持文件I/O）
4. **检查结果** — 用 Python 代码读取输出文件，分析执行结果
5. **迭代优化** — 根据结果修改代码，重新保存和执行
6. **沉淀** — 最终版本用 `save_script` 保存，附带清晰的 description

**可用变量（自动注入）：**
- `WORKSPACE` — 工作区根目录路径
- `pd` — pandas, `np` — numpy, `Path` — pathlib.Path
- `data` — 上下文数据（K线、行情等）

CodeAgent 可以直接用 Python 代码读写文件、执行分析，无需额外工具。""",
}

TOOL_CATALOG = """## 可用工具分类

**名称/代码互查**: resolve_stock_name(代码→名称), search_stock_by_name(名称→代码，模糊搜索)
**数据工具**: get_realtime_quote(实时行情), agent_get_kline(多周期K线/OHLCV, 支持1m~1W, 可指定market), get_stock_info(基本面), get_market_indices(大盘指数), get_sector_rankings(板块排名)
**分析工具**: analyze_trend(五维技术分析), calculate_ma(均线), get_volume_analysis(量价), analyze_pattern(15+形态), get_chip_distribution(筹码), get_indicator_snapshot(指标快照)
**搜索工具**: search_stock_news(新闻), search_comprehensive_intel(综合情报)
**选股工具**: screen_stocks(本地DB选股), smart_screen(综合选股), review_stocks_with_indicator(指标审核)
**指标工具**: list_indicators(列出指标), get_indicator_params(指标参数), run_indicator_signal(执行指标信号)
**回测工具**: run_backtest(跑回测), get_backtest_history(查历史回测记录)
**交易工具**: list_strategies(列出策略), get_strategy_detail(策略详情), start_strategy(启动策略), stop_strategy(停止策略), get_strategy_trades(交易记录)
**龙虎榜/热榜**: get_dragon_tiger_stocks, get_dragon_tiger_by_stock, get_hot_rank_stocks, get_zt_pool_stocks, get_limit_down_stocks, get_broken_board_stocks
**内置工具**: web_search(DuckDuckGo搜索), visit_webpage(访问网页), wikipedia_search(维基百科)
⚠️ user_input 工具已禁用（Web 环境不可用）。如需向用户提问，直接在回复文本中提问，用户会通过下一轮对话回答你。
**自定义分析**: Agent 原生代码执行(LLM 直接写 Python 代码，仅 CodeAgent 模式)
**工作区工具**: save_script, load_script, list_workspace, shell_exec, run_background, poll_task, apply_template, list_templates
**源码扫描(只读)**: list_project_files(目录结构), read_project_file(读源码), grep_project(搜索代码)
**自修改**: self_modify_list_dirs(目录列表), self_modify_read(读源码), self_modify_diff_head(快速预览), self_modify_write(修改文件), self_modify_create(新建文件), self_modify_diff(对比差异), self_modify_rollback(回滚), self_modify_log(修改日志)

⚠️ 当用户只给中文股票名称没给代码时，必须先用 search_stock_by_name 查到代码再分析。
⚠️ agent_get_kline 是获取K线原始数据，get_backtest_history 是查询过去的回测记录。
⚠️ 你可以用 Python 代码组合多个工具的返回值，做自定义计算。例如：
```python
quote = get_realtime_quote("600519")
history = agent_get_kline("600519", timeframe="1D", days=120)
# 短线分析可以用分钟级周期
# history_15m = agent_get_kline("600519", timeframe="15m", days=5)
# 然后用 pandas 做自定义分析
```"""


def _detect_intent(message: str) -> str:
    msg_lower = message.lower()
    for intent, keywords in INTENT_KEYWORDS.items():
        for kw in keywords:
            if kw in msg_lower:
                return intent
    return "analysis"


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
    intent = _detect_intent(user_message)
    workflow = WORKFLOW_TEMPLATES.get(intent, WORKFLOW_TEMPLATES["analysis"])

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

{workflow}

{TOOL_CATALOG}
{skill_section}{scan_section}{modify_section}## 规则

1. **必须调用工具获取真实数据** — 绝不编造数字，所有数据必须来自工具返回结果。
2. **系统化分析** — 严格按工作流程分阶段执行。
3. **深度优先** — 不要满足于工具的默认输出，当分析深度不够时直接写 Python 代码做更深入的量化分析。
4. **风险优先** — 必须排查风险（股东减持、业绩预警、监管问题）。
5. **工具失败处理** — 记录失败原因，使用已有数据继续分析，不重复调用失败工具。
6. **多维验证** — 技术面结论应至少有 2 个以上指标相互验证，避免单一指标误判。
7. **善用代码** — 你是 CodeAgent，可以用 Python 代码组合工具、做计算、处理数据。不要局限于单次工具调用。
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
    """Validate that the final answer contains a valid dashboard JSON when in analysis mode.

    Only enforces JSON structure when the intent is 'analysis' (dashboard mode).
    For other intents (chat, screening, etc.), any non-empty answer is accepted.
    """
    if not answer or not isinstance(answer, str):
        return False

    # Try to extract JSON from the answer
    answer_stripped = answer.strip()

    # Check if it's a JSON object (dashboard mode)
    if answer_stripped.startswith("{"):
        try:
            obj = json.loads(answer_stripped)
            # Validate required dashboard fields
            required = ["stock_name", "sentiment_score", "trend_prediction", "operation_advice"]
            missing = [f for f in required if f not in obj]
            if missing:
                logger.warning("[FinalAnswerCheck] Dashboard JSON missing fields: %s", missing)
                # Don't reject — the agent might have a good reason for partial output
                return True
            return True
        except json.JSONDecodeError:
            pass

    # Check markdown-wrapped JSON
    json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", answer_stripped, re.DOTALL)
    if json_match:
        try:
            obj = json.loads(json_match.group(1))
            return isinstance(obj, dict)
        except json.JSONDecodeError:
            pass

    # Non-JSON answer is fine for chat/non-dashboard intents
    return bool(answer_stripped)


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
