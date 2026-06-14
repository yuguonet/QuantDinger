# -*- coding: utf-8 -*-
"""
Agent — smolagents Agent for QuantDinger.

核心入口：build_agent_executor() → _AgentExecutor → chat() / chat_stream()

架构：
  smolagents CodeAgent（默认）或 ToolCallingAgent（AGENT_TYPE=tool）
  + 15 个 Managed Agents（skills/ 目录，@skill 装饰器自动发现）
  + 40+ 工具（tools/ 目录，@tool 装饰器自动发现）
  + Chain 链路编排（chain/ 目录，verb+noun 触发）

执行流程：
  1. _prepare() — 意图分析 → 领域路由 → 工具过滤 → 上下文拼接
  2. 快速通道 — 闲聊/greeting 直接回复，不走 agent
  3. 链路触发 — _try_chain() 匹配 verb+noun → ChainExecutor 执行
  4. Agent 执行 — smolagents CodeAgent.run()（流式/阻塞）
  5. 后置评估 — _post_evaluate() → evaluator.learn_from_execution()
  6. 上下文压缩 — compress_context() 异步线程

配置：
  AGENT_TYPE=code|tool     — Agent 类型（默认 code）
  AGENT_MAX_STEPS=10       — 最大步数
  AGENT_TIMEOUT_SECONDS=180 — 超时
  INTENT_ANALYSIS_ENABLED=true — 意图分析开关

公开接口：
  build_agent_executor(skills, user_id, max_steps, timeout_seconds, model, provider) → _AgentExecutor
  _AgentExecutor.chat(message, session_id, context, progress_callback, user_id) → AgentResult
  _AgentExecutor.chat_stream(...) → Generator[dict]
  AgentResult(success, content, tool_calls_log, total_steps, total_tokens, model, error, charts)
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
from app.agent.trace_collector import TraceCollector

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
_tools_cache_lock = __import__("threading").Lock()


def _get_agent_class():
    """Return the agent class based on AGENT_TYPE env var.

    ⚠️ 确定性修复：不再自动检测。默认使用 CodeAgent，避免同一问题
    因 CodeAgent/ToolCallingAgent 切换导致结果不一致。
    用户可通过 AGENT_TYPE=tool 显式切换。
    """
    agent_type = os.getenv("AGENT_TYPE", "code").strip().lower()
    if agent_type == "tool":
        return ToolCallingAgent
    # 默认 CodeAgent，不再自动检测 Ollama
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

    # 子 agent 列表
    if managed_agents and managed_agents is not None:
        lines.append("\n**可用子 agent**")
        for ma in managed_agents:
            lines.append(f"  {ma.name} — {ma.description}")

    return "\n".join(lines)


# ── GUIDANCE loaded from skills.guidance ──
from app.agent.skills.guidance import GUIDANCE


def _load_preamble() -> str:
    """Load agent preamble from external .md file, with built-in fallback."""
    import pathlib
    p = pathlib.Path(__file__).resolve().parent / "agent_preamble.md"
    if p.is_file():
        try:
            return p.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return "你是 QuantDinger 量化分析助手。"


def _build_router_instructions(
    language: str = "zh",
    available_skills: str = "",
    domain: str = "",
    domain_instructions: str = "",
    intent_context: str = "",
) -> str:
    """构建极简路由 prompt — 主 Agent 只做分发，不做分析。"""
    preamble = _load_preamble()

    lang_section = (
        "\n## 输出语言\n"
        "- 使用中文回答。\n"
        "- 所有面向用户的文本值使用中文。\n"
        "- JSON 中 action/direction/confidence 保持英文枚举值。\n"
    ) if not str(language or "").lower().startswith("en") else (
        "\n## Output Language\n- Reply in English.\n- All JSON values in English.\n"
    )

    domain_section = ""
    if domain_instructions:
        domain_section = f"\n## 当前领域: {domain}\n\n{domain_instructions}\n"

    intent_section = ""
    if intent_context:
        intent_section = f"\n## 意图分析\n\n{intent_context}\n"

    return f"""{preamble}

## 你的角色

你是**路由器**。你的唯一职责是：
1. 理解用户意图
2. 选择正确的工具调用
3. 把工具返回的结果直接作为最终回复输出

**你不做分析，不做判断，不做验证。** 工具的结果就是最终结果。

## ⚠️ 你的可用工具

- **call_skill(skill_name, stock_code, stock_name)** — 调用单个技能分析
- **execute_chain(chain_id, stock_code, stock_name)** — 调用多技能链路分析
- **search_stock_by_name(keyword)** — 搜索股票代码（不确定代码时先搜）
- **final_answer(回复)** — 输出结果

## 可用技能

{available_skills}

## 可用链路

- **evaluate+stock** — 股票综合评估（游资→解禁→情报→技术→指标→选股→行情→回测→多空辩论）
- **screen+stock** — 选股筛选（条件选股→技术验证→情报过滤）
- **scan+market** — 市场全景扫描（大盘指数→板块排名→涨停池→资金流向）

## 规则

0. **⚠️ 必须用 final_answer() 返回结果**。
1. **不需要工具的消息，第一步就 final_answer**。
2. **分析股票 → call_skill 或 execute_chain**：
   - 单维度分析（如"技术面分析600519"）→ call_skill
   - 全面分析（如"分析贵州茅台"、"帮我看看600519"）→ execute_chain
3. **⚠️ 工具返回后立即 final_answer** — 不要再调任何工具或做额外分析。
4. **不确定股票代码时** → 先 search_stock_by_name，再 call_skill/execute_chain。
5. **工具返回的是完整分析报告，直接原样输出，不要修改、总结或重新格式化。**

{intent_section}{domain_section}{lang_section}"""


def _build_instructions(user_message: str = "", skill_instructions: str = "",
                        language: str = "zh", tools=None, managed_agents=None,
                        domain: str = "", domain_instructions: str = "",
                        intent_context: str = "") -> str:
    if str(language or "").lower().startswith("en"):
        lang_section = "\n## Output Language\n- Reply in English.\n- All JSON values in English.\n"
    else:
        lang_section = (
            "\n## 输出语言\n"
            "- 使用中文回答。所有输出内容（包括思考过程、代码注释）必须使用中文。\n"
            "- JSON 中以下字段**必须用英文枚举值**（代码解析需要）：\n"
            "  action: buy/sell/hold/skip\n"
            "  direction: bullish/bearish/neutral\n"
            "  confidence: high/medium/low\n"
            "  timeframe: T+1/T+3/T+5/1W/1M/3M/1Y\n"
            "- JSON 中以下字段**必须用中文**（给用户看）：\n"
            "  signal: 一句话信号摘要，如「多头排列,MACD金叉,放量突破」\n"
            "  factors[].name: 维度名，如「趋势」「量价」「指标」「形态」「筹码」\n"
            "  factors[].value: 状态描述，如「主升浪」「缩量回调」「RSI超买」\n"
            "  analysis: 完整分析文字，全部中文\n"
            "  timeframe_reason: 为什么选这个时间维度，中文\n"
            "- 专有名词（MACD/RSI/KDJ/BOLL/MA 等）保持英文即可。\n"
        )

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

    # 客观评分校准注入（如果有）
    calibration_section = ""
    # calibration_context 通过外部注入到 user_message 前部

    # 金融领域 JSON 标准化输出规范（按 AGENT_TYPE 区分格式）
    finance_json_section = ""
    if domain == "finance":
        _agent_cls = _get_agent_class()
        _json_fields = (
            '"action": "buy/sell/hold/skip",\n'
            '    "score": 0-100,\n'
            '    "direction": "bullish/bearish/neutral",\n'
            '    "confidence": "high/medium/low",\n'
            '    "timeframe": "T+1/T+3/T+5/1W/1M/3M/1Y",\n'
            '    "timeframe_reason": "为什么选这个时间维度",\n'
            '    "stock_code": "6位代码",\n'
            '    "stock_name": "股票名称",\n'
            '    "signal": "一句话信号摘要",\n'
            '    "factors": [\n'
            '        {"name": "维度名", "score": 0-100, "direction": "bullish/bearish/neutral"}\n'
            '    ],\n'
            '    "analysis": "你的完整分析文字"'
        )
        _timeframe_rules = """**timeframe 规则**：
- 用户给了时间（"明天"/"这周"）→ 按用户的来
- 用户没给时间 → **默认 T+3**（3个交易日短线），除非用户明确问中长期
- 禁止使用 1Y/1Y+ 等超长周期作为默认值，那等于没分析
- direction 和 score 只在你声明的时间维度内有效
- 不同时间维度方向可能相反，必须明确"""

        if _agent_cls is ToolCallingAgent:
            finance_json_section = f"""
## ⚠️ 输出格式（必须遵守）

你必须调用 final_answer 工具来返回结果。工具调用的 JSON 格式如下：

```json
{{
    "name": "final_answer",
    "arguments": {{
{_json_fields}
    }}
}}
```

{_timeframe_rules}

不要输出任何其他文字，只输出上述 JSON 工具调用。格式不对会被系统拒绝并要求重写。

"""
        else:
            finance_json_section = f"""
## ⚠️ 输出格式（必须遵守）

你的最终答案必须通过 Python 代码调用 `final_answer()` 函数来返回一个包含以下字段的字典。

在代码块的最后一行加上：

```py
final_answer({{
{_json_fields}
}})
```

{_timeframe_rules}

不要输出任何 ````json` 代码块。必须用 Python 的 `final_answer()` 返回。格式不对会被系统拒绝并要求重写。

"""

    # 金融领域权重注入
    weight_section = ""
    if domain == "finance":
        try:
            from app.agent.chain.store import get_skill_weights
            weights = get_skill_weights()
            if weights:
                weight_lines = ["| 技能 | 权重 |", "|------|------|"]
                for name, w in sorted(weights.items(), key=lambda x: -x[1]):
                    weight_lines.append(f"| {name} | {w:.2f} |")
                weight_section = f"\n## 技能权重（历史回溯数据）\n\n{'\\n'.join(weight_lines)}\n\n权重越高，该技能的历史预测越准确。\n"
        except Exception:
            pass  # 权重注入失败不影响主流程

    return f"""{preamble}

{GUIDANCE}
{tool_catalog}
{skill_section}{scan_section}{modify_section}{intent_section}{domain_section}{calibration_section}{weight_section}## 你的角色

你是**路由器**。意图分析已经给出了建议（见上方"意图分析"），你的工作是：
1. 根据建议选择调哪些子 Agent
2. 调用子 Agent
3. 把子 Agent 的结果**直接作为最终回复**输出

**你不做分析，不做判断，不做验证。** 子 Agent 的结果就是最终结果。

## ⚠️ 你的可用工具

- **数据层工具**（见"工具分类"中的"数据层"/"显示层"）— 查询数据
- **search_stock_by_name** — 搜索股票代码
- **子 agent**（见"可用子 agent"）— 分析任务
- **final_answer(回复)** — 输出结果

❌ "分析层"/"决策层"工具由子 agent 内部使用，你不能调用。

## 规则

0. **⚠️ 必须用 final_answer() 返回结果**。
1. **不需要工具的消息，第一步就 final_answer**。
2. **根据意图分析的建议选择子 Agent** — 见上方"意图分析"中的"工具分类"。
3. **⚠️ 调完就输出** — 子 Agent 返回结果后，**立即调用 final_answer()**，不要再调任何工具或子 Agent。
4. **子 Agent 结果 = 最终结果** — 不要修改、不要补充验证、不要重新分析。直接放进 final_answer。
5. **⚠️ 绝对禁止**：
   - 收到子 Agent 结果后继续调其他工具或子 Agent
   - 对子 Agent 数据做二次验证
   - 同一个子 Agent 调用两次
6. **子 agent 返回 JSON** — 用 `json.loads()` 解析后直接使用。
7. **诚实透明** — 子 Agent 返回失败时，**用你手里的数据层工具直接查数据**，能查到多少算多少，在结论中说明"XX子Agent失败，以下为直接查询的数据"。
{finance_json_section}{lang_section}"""


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
    if not answer:
        return False
    if isinstance(answer, dict):
        return True
    if isinstance(answer, str):
        return bool(answer.strip())
    return True


def _check_output_json(answer, memory, agent) -> bool:
    """校验金融领域 agent 输出是否为合法 JSON 且字段完整。

    兼容两种输入：
    - ToolCallingAgent → answer 是 dict（final_answer 工具调用的返回值）
    - CodeAgent → answer 是包含 ```json 块的字符串

    仅 domain=finance 时使用。不通过时 agent 会收到错误提示并重写 final_answer。
    """
    import json as _json
    import re as _re

    if not answer:
        return False

    data = None

    # 情况 1：answer 已经是 dict（ToolCallingAgent 的 final_answer 工具调用）
    if isinstance(answer, dict):
        data = answer

    # 情况 2：answer 是字符串，尝试从中提取 JSON 块
    elif isinstance(answer, str):
        patterns = [
            r'```json\s*\n?(.*?)\n?\s*```',
            r'(\{[^{}]*"action"[^{}]*"score"[^{}]*\})',
        ]
        for pat in patterns:
            m = _re.search(pat, answer, _re.DOTALL)
            if m:
                try:
                    data = _json.loads(m.group(1).strip())
                except (_json.JSONDecodeError, TypeError):
                    continue
                if isinstance(data, dict):
                    break

    if not isinstance(data, dict):
        return False

    # 校验必填字段
    required = {"action", "score", "direction", "confidence", "timeframe", "signal", "factors", "analysis"}
    missing = required - set(data.keys())
    if missing:
        return False

    try:
        # 校验 action 值
        if data["action"] not in ("buy", "sell", "hold", "skip"):
            return False

        # 校验 score 范围（可能为字符串如 "75" 或 None）
        score = data["score"]
        if not isinstance(score, (int, float)):
            try:
                score = float(score)
            except (TypeError, ValueError):
                return False
        if not (0 <= score <= 100):
            return False

        # 校验 direction 值
        if data["direction"] not in ("bullish", "bearish", "neutral"):
            return False

        return True
    except Exception:
        # 任何校验异常 → 视为校验不通过
        return False


# ═══════════════════════════════════════════════════════════════
# 3b. Finance Domain — Decision Card Formatter
# ═══════════════════════════════════════════════════════════════

TIMEFRAME_CN = {
    "T+1": "1天", "T+3": "3天", "T+5": "5天",
    "1W": "1周", "1M": "1月", "3M": "3月", "1Y": "1年",
}


def format_decision_card(data: dict) -> str:
    """将 agent 输出的 JSON 格式化为用户可见的标准卡片。"""
    action_cn = {"buy": "买入", "sell": "卖出", "hold": "观望", "skip": "跳过"}
    conf_cn = {"high": "高", "medium": "中", "low": "低"}
    dir_cn = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}
    tf = TIMEFRAME_CN.get(data.get("timeframe", ""), data.get("timeframe", ""))

    lines = [
        f"**{action_cn.get(data['action'], '观望')}** {data.get('stock_name', '')}({data.get('stock_code', '')})",
        f"维度:{tf} 评分:{data['score']:.0f} 方向:{dir_cn.get(data['direction'], '中性')} 置信:{conf_cn.get(data['confidence'], '中')}",
    ]

    # 因子明细
    if data.get("factors"):
        parts = []
        for f in data["factors"]:
            s = f"{f['score']:.0f}" if f.get("score") is not None else "—"
            parts.append(f"{f['name']}:{s}")
        lines.append(" | ".join(parts))

    # 信号
    if data.get("signal"):
        lines.append(f"信号: {data['signal']}")

    # 详细分析（折叠）
    if data.get("analysis"):
        lines.append(f"\n<details><summary>详细分析</summary>\n\n{data['analysis']}\n</details>")

    return "\n".join(lines)


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
# 5b. Managed Agent Builder
# ═══════════════════════════════════════════════════════════════


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
    collector=None,  # TraceCollector（金融领域注入）
) -> "CodeAgent | ToolCallingAgent":
    """Build a fresh agent instance per call.

    架构：主 Agent 极简（call_skill / execute_chain / search / final_answer），
    所有数据工具和分析逻辑封装在 Skill/Chain 内部，不暴露给主 Agent。
    """
    smol_model = build_model(model=model, provider=provider)

    # ── 发现技能 ──
    from app.agent.skills.registry import skill_registry as _skill_registry
    _skill_registry.discover()

    # ── 构建主 Agent 工具：极简 4 件套 ──
    from app.agent.skills.skill_executor import SkillExecutionTool
    from app.agent.chain_executor_tool import ChainExecutionTool
    from smolagents import FinalAnswerTool

    # 1) call_skill — 调用单个 Skill（内部 Python 函数调用 + 按需 LLM）
    call_skill_tool = SkillExecutionTool(
        skill=None,  # 不绑定单个 skill，由 forward 动态选择
        model=smol_model,
        collector=collector,
    )
    # 覆盖 inputs 让主 Agent 知道有哪些 skill 可用
    _available_skills = ", ".join(
        f"{s.name}({s.description})" for s in _skill_registry.all_specs
    )
    call_skill_tool.inputs = {
        "skill_name": {
            "type": "string",
            "description": f"技能名。可选值: {_available_skills}",
            "nullable": True,
        },
        "stock_code": {
            "type": "string",
            "description": "股票代码，如 600519、000858",
            "nullable": True,
        },
        "stock_name": {
            "type": "string",
            "description": "股票名称（可选），如 贵州茅台",
            "nullable": True,
        },
    }

    # 2) execute_chain — 调用 Chain 编排（内部 Python 函数调用 + 按需 LLM）
    chain_tool = ChainExecutionTool(
        model=smol_model,
        collector=collector,
        user_id=user_id,
    )

    # 3) search_stock_by_name — 轻量搜索
    search_tool = None
    from app.agent.tools.registry import registry as tool_registry
    tool_registry.discover()
    all_tools = tool_registry.build({"deny": list(_EXCLUDED_TOOL_NAMES)})
    for t in all_tools:
        if t.name == "search_stock_by_name":
            search_tool = t
            break

    # 4) final_answer — 输出
    final_tool = FinalAnswerTool()

    router_tools = [call_skill_tool, chain_tool, final_tool]
    if search_tool:
        router_tools.insert(2, search_tool)

    # ── 极简 prompt：只做路由，不做分析 ──
    instructions = _build_router_instructions(
        language=language,
        available_skills=_available_skills,
        domain=domain,
        domain_instructions=domain_instructions,
        intent_context=intent_context,
    )

    AgentClass = _get_agent_class()

    _extra_kwargs = {}
    if AgentClass is CodeAgent:
        _extra_kwargs["additional_authorized_imports"] = [
            "json", "datetime",
        ]
        _extra_kwargs["executor_kwargs"] = {"timeout_seconds": int(os.getenv("AGENT_EXEC_TIMEOUT", "300"))}

    # 金融领域用 JSON 校验，其他领域用宽松校验
    checks = [_check_output_json] if domain == "finance" else [_check_dashboard_json]

    agent = AgentClass(
        tools=router_tools,
        model=smol_model,
        max_steps=max_steps,
        instructions=instructions,
        verbosity_level=LogLevel.INFO,
        return_full_result=True,
        stream_outputs=True,
        planning_interval=None,
        final_answer_checks=checks,
        **_extra_kwargs,
    )

    logger.info(
        "[Agent] Built %s for user=%s domain=%s: %d router tools (call_skill/execute_chain/search/final_answer), max_steps=%d",
        AgentClass.__name__, user_id, domain or "all", len(router_tools), max_steps,
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
    skills=None, user_id=1, max_steps=6,
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

        # ── 金融域：从消息中提取 stock_code（_try_chain 被跳过，需要在此提取）──
        if domain == "finance" and not (context and context.get("stock_code")):
            import re as _re_stock
            # 1. 6位数字代码
            _m = _re_stock.search(r'\b(\d{6})\b', message)
            if _m:
                _stock_code = _m.group(1)
                if not (context and context.get("stock_code")):
                    context = context or {}
                    context["stock_code"] = _stock_code
                    logger.info("[Prepare] 从消息提取股票代码: %s", _stock_code)
                # 查股票名称（避免 LLM 瞎编）
                if not (context and context.get("stock_name")):
                    try:
                        from app.utils.basicinfo_db import get_stock_basic_db
                        _matches = get_stock_basic_db().search_stocks(_stock_code, limit=1)
                        if _matches:
                            context["stock_name"] = _matches[0].get("name", "")
                            logger.info("[Prepare] 代码→名称: %s → %s", _stock_code, context["stock_name"])
                    except Exception:
                        pass
            else:
                # 2. 中文名查找
                from app.agent.text_utils import extract_stock_from_message
                _code, _name = extract_stock_from_message(message)
                if _code:
                    context = context or {}
                    context["stock_code"] = _code
                    if _name:
                        context["stock_name"] = _name
                    logger.info("[Prepare] 中文名 → 代码 %s", _code)

        # ── 提取意图信息，供主 Agent 路由决策参考 ─────────────
        _eval_verb = ""
        _eval_noun = ""
        _eval_tool_chain = []
        if intent is not None:
            _eval_verb = getattr(intent, 'verb', '') or ""
            _eval_noun = getattr(intent, 'noun', '') or ""
            _eval_tool_chain = (getattr(intent, 'metadata', None) or {}).get("tool_chain", [])

        # ── 创建 TraceCollector（金融领域注入）─────────────────
        collector = None
        if domain == "finance":
            collector = TraceCollector(session_id=session_id, user_query=message)
            collector.intent_verb = _eval_verb
            collector.intent_noun = _eval_noun
            collector.domain = domain
            # 同步从消息中提取的 stock_code
            if context and context.get("stock_code"):
                collector.stock_code = context["stock_code"]
            if context and context.get("stock_name"):
                collector.stock_name = context["stock_name"]

        # ── 上下文拼接 ────────────────────────────────────────
        enriched = message
        ctx_parts = []

        # 压缩上下文（上轮分析摘要，领域切换时自动丢弃）
        context_summary, summary_age = store.get_context_summary(session_id, current_domain=domain, with_age=True)
        if context_summary:
            age_hint = f" (已存{summary_age}轮)" if summary_age > 0 else ""
            ctx_parts.append(f"[上轮分析摘要{age_hint}]\n{context_summary}")

        if context:
            if context.get("stock_code"):
                ctx_parts.append(f"股票代码: {context['stock_code']}")
            if context.get("stock_name"):
                ctx_parts.append(f"股票名称: {context['stock_name']}")
            if context.get("realtime_quote"):
                # 只注入摘要，不注入原始 JSON（避免 LLM 尝试 json.loads()）
                _q = context["realtime_quote"]
                _q_summary = f"最新价:{_q.get('price', _q.get('last', '?'))} 涨跌幅:{_q.get('change_pct', _q.get('pct_change', '?'))}%"
                ctx_parts.append(f"[已获取的实时行情摘要] {_q_summary}（如需完整数据请调用 get_realtime_quote）")
            if context.get("chip_distribution"):
                ctx_parts.append(f"[已获取的筹码分布数据]（如需完整数据请调用 get_chip_distribution）")

        # 数据完整性预检：标记缺失的数据维度
        _missing_data_hints = []
        if context:
            if not context.get("realtime_quote"):
                _missing_data_hints.append("实时行情")
            if not context.get("chip_distribution"):
                _missing_data_hints.append("筹码分布")
        if _missing_data_hints:
            ctx_parts.append(
                f"[数据完整性提示] 以下数据在请求时尚未提供，你需要在分析时主动获取: "
                f"{', '.join(_missing_data_hints)}。如果工具调用失败，必须在结论中说明。"
            )

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
            collector=collector,
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
            "collector": collector,
        }

    def chat(self, message, session_id, context=None,
             progress_callback=None, user_id=1) -> AgentResult:
        """Blocking chat — waits for full result.

        Per-session lock prevents concurrent requests on the same session
        from interleaving get_history / add_message calls.
        """
        from app.agent.session_store import get_session_store
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

        # ── 保存意图信息，供后置评估使用 ───────────────────
        _intent_verb = meta.get("intent_verb", "")
        _intent_noun = meta.get("intent_noun", "")
        _tool_chain = meta.get("tool_chain", [])
        _eval_domain = meta.get("domain", "")
        collector = meta.get("collector")

        t0 = time.time()
        try:
            result = agent.run(enriched, max_steps=self.max_steps)

            if hasattr(result, "output"):
                # ToolCallingAgent 返回 dict，直接保留（后续 JSON 提取需要）
                _raw_output = result.output
                if isinstance(_raw_output, dict):
                    content = _raw_output  # 保留 dict，不转 str
                else:
                    content = str(_raw_output) if _raw_output else ""
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

            store.add_message(session_id, "assistant", content if isinstance(content, str) else str(content))

            # ── 金融领域：JSON → TraceCollector 存库 → format_decision_card ──
            if success and content and collector and _eval_domain == "finance":
                try:
                    import json as _json_card
                    import re as _re_card
                    # 提取 JSON 块：content 可能已是 dict（ToolCallingAgent）或 str（CodeAgent）
                    _card_data = None
                    if isinstance(content, dict) and "action" in content:
                        _card_data = content
                    elif isinstance(content, str):
                        for _pat in [r'```json\s*\n?(.*?)\n?\s*```',
                                     r'(\{[^{}]*"action"[^{}]*"score"[^{}]*\})']:
                            _m = _re_card.search(_pat, content, _re_card.DOTALL)
                            if _m:
                                try:
                                    _card_data = _json_card.loads(_m.group(1).strip())
                                    if isinstance(_card_data, dict) and "action" in _card_data:
                                        break
                                except (_json_card.JSONDecodeError, TypeError):
                                    _card_data = None

                    # ── fallback：自由文本 → skip JSON ──
                    # agent 输出了非 JSON 内容，构造兜底结构化响应
                    if _card_data is None and isinstance(content, str) and content.strip():
                        _stock_code = ""
                        _stock_name = ""
                        if context:
                            _stock_code = context.get("stock_code", "")
                            _stock_name = context.get("stock_name", "")
                        if not _stock_code:
                            for _k in ("stock_code", "stock", "symbol", "code"):
                                if _k in (meta or {}) and meta[_k]:
                                    _stock_code = str(meta[_k])
                                    break
                        _card_data = {
                            "action": "skip",
                            "score": 0,
                            "direction": "neutral",
                            "confidence": "low",
                            "timeframe": "T+3",
                            "timeframe_reason": "agent未输出结构化JSON",
                            "stock_code": _stock_code or collector.stock_code if collector else "",
                            "stock_name": _stock_name or collector.stock_name if collector else "",
                            "signal": "分析未完成，输出格式异常",
                            "factors": [],
                            "analysis": content[:2000],
                        }
                        logger.warning("[Agent] JSON提取失败，fallback → skip。原始内容前200字: %s",
                                       content[:200])

                    # 先用原始 JSON 内容调用 on_agent_finish（提取字段 + 存库）
                    # TraceCollector 需要字符串输入，dict 转 JSON 字符串
                    _tc_answer = _json_card.dumps(content, ensure_ascii=False) if isinstance(content, dict) else content
                    tu = result.token_usage if hasattr(result, 'token_usage') else None
                    total_tok = (tu.input_tokens + tu.output_tokens) if tu else 0
                    root = collector.on_agent_finish(
                        final_answer=_tc_answer,
                        total_steps=total_steps,
                        total_tokens=total_tok,
                        model=str(getattr(agent.model, "model_id", "")),
                    )
                    logger.info("[Agent] TraceCollector 存库 root_id=%s stock=%s",
                                root.id, root.stock_code)

                    # 再用 JSON 格式化为 DecisionCard 给用户
                    if _card_data:
                        content = format_decision_card(_card_data)
                        store.add_message(session_id, "assistant", content)
                        logger.info("[Agent] DecisionCard: %s score=%s action=%s",
                                    _card_data.get("stock_code", ""),
                                    _card_data.get("score", ""),
                                    _card_data.get("action", ""))
                except Exception as e:
                    logger.warning("[Agent] DecisionCard/存库失败，保留原始输出: %s", e)

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
                    _compress_domain = _eval_domain
                    def _compress(c=content, tc=tool_calls_log, sid=session_id, m=self.model, d=_compress_domain):
                        try:
                            _, age = store.get_context_summary(sid, current_domain=d, with_age=True)
                            summary = compress_context(c, tc, model=m, domain=d, age_turns=age)
                        except Exception as e:
                            logger.warning("[Compress] 压缩异常，降级截断: %s", e)
                            summary = c[:500]
                        if summary:
                            store.save_context_summary(sid, summary, domain=d)
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

    def _try_chain(self, verb, noun, message, session_id, context, user_id):
        """尝试链路执行。匹配到链路时执行并返回 AgentResult，否则返回 None。

        流程：
          1. 查固定链路（verb+noun 精确匹配）
          2. 未匹配 → Planner 规划（LLM 选 Skill）
          3. 规划失败 → 返回 None（让 agent 直接处理）
        """
        # ── 前置拦截：无意图信号时直接返回，不浪费 token ──
        if not verb and not noun:
            logger.info("[Chain] 无 verb/noun 信号，跳过链路")
            return None

        # ── 提取股票代码（固定链路和规划都需要）──
        stock_code = ""
        stock_name = ""
        if context:
            stock_code = context.get("stock_code", "")
            stock_name = context.get("stock_name", "")
        if not stock_code:
            import re
            match = re.search(r'\b(\d{6})\b', message)
            if match:
                stock_code = match.group(1)
        if not stock_code:
            from app.agent.text_utils import extract_stock_from_message
            _code, _name = extract_stock_from_message(message)
            if _code:
                stock_code = _code
            if _name:
                stock_name = _name
                logger.info("[Chain] 中文名 → 代码 %s", stock_code)

        # ── Layer 0: 固定链路匹配 ──
        chain_def = None
        degraded = False
        degrade_reason = ""

        try:
            from app.agent.chain.chains import get_chain_for_intent
            chain_def = get_chain_for_intent(verb, noun)
        except Exception as e:
            logger.warning("[Chain] 查找链路异常: %s", e)

        # ── Layer 1: Planner 规划（无固定链路时）──
        if not chain_def:
            logger.info("[Chain] 无固定链路匹配 (verb=%s noun=%s)，尝试 Planner", verb, noun)
            try:
                from app.agent.planner import Planner
                smol_model = build_model(self.model, self.provider)

                def planner_llm(prompt: str) -> str:
                    messages = [{"role": "user", "content": prompt}]
                    response = smol_model(messages)
                    return response.content if hasattr(response, "content") else str(response)

                planner = Planner(call_llm=planner_llm)
                plan_result = planner.plan(
                    query=message,
                    stock_code=stock_code,
                    stock_name=stock_name,
                    verb=verb,
                    noun=noun,
                )

                if plan_result.success and plan_result.chain_def:
                    chain_def = plan_result.chain_def
                    degraded = plan_result.degraded
                    degrade_reason = plan_result.degrade_reason

                    if plan_result.from_cache:
                        logger.info("[Planner] 缓存命中: %s", chain_def.chain_id)
                    elif degraded:
                        logger.warning("[Planner] 降级: %s", degrade_reason)
                    else:
                        logger.info("[Planner] 规划成功: %d 步, reasoning=%s",
                                    len(chain_def.steps), plan_result.reasoning[:80])
                else:
                    logger.warning("[Planner] 规划失败")
            except Exception as e:
                logger.warning("[Planner] 规划异常: %s", e)

        # ── Layer 2: 无匹配 → 返回 None，让 agent 直接处理 ──
        if not chain_def:
            logger.info("[Chain] 无链路匹配 (verb=%s noun=%s)，交给 agent", verb, noun)
            return None

        # 非个股链路不需要股票代码
        if not stock_code:
            if chain_def.chain_id == "scan+market":
                stock_code = ""
            elif not degraded:
                logger.info("[Chain] 链路 %s 匹配但未找到股票代码，跳过", chain_def.chain_id)
                return None

        logger.info("[Chain] 执行链路 %s | 股票=%s | degraded=%s",
                     chain_def.chain_id, stock_code, degraded)

        # ── 构建 Skill 实例并执行 ──
        from app.agent.session_store import get_session_store
        store = get_session_store()

        try:
            from app.agent.skills.registry import skill_registry
            skill_registry.discover()
            smol_model = build_model(self.model, self.provider)
        except Exception as e:
            logger.warning("[Chain] 初始化失败: %s", e)
            return None

        # 工具只构建一次，所有 skill 共享
        from app.agent.tool_adapter import build_all_tools
        _all_tools = build_all_tools()
        _tool_map = {t.name: t for t in _all_tools}

        # call_llm：Skill 层和 Chain 层共用的 LLM 调用函数
        def call_llm(prompt: str) -> str:
            messages = [{"role": "user", "content": prompt}]
            response = smol_model(messages)
            return response.content if hasattr(response, "content") else str(response)

        # run_skill_fn：调用指定的 BaseSkill
        def run_skill_fn(skill_name: str, scode: str, sname: str, ctx: dict) -> tuple:
            sk = skill_registry.get(skill_name)
            if not sk:
                raise ValueError(f"Unknown skill: {skill_name}")

            def call_tool_fn(tool_name: str, **kwargs):
                t = _tool_map.get(tool_name)
                if not t:
                    raise ValueError(f"Unknown tool: {tool_name}")
                return t(**kwargs)

            return sk.run(
                stock_code=scode,
                stock_name=sname,
                context=ctx,
                call_llm=call_llm,
                call_tool_fn=call_tool_fn,
            )

        # 执行链路
        from app.agent.chain.executor import ChainExecutor
        executor = ChainExecutor(
            chain_id=chain_def.chain_id,
            stock_code=stock_code,
            stock_name=stock_name,
            user_id=user_id,
        )
        chain_result = executor.execute(
            run_skill_fn=run_skill_fn,
            context={"user_query": message},
            call_llm=call_llm,
        )

        # 转换为 AgentResult — 用 DecisionResult 的 content 属性
        content = chain_result.content
        if not content:
            content = "链路执行未产生决策。"

        # 降级告知用户（必须告知）
        if degraded:
            degrade_msg = f"⚠️ 当前为降级模式（{degrade_reason}），仅执行基础分析，结果可能不完整。"
            content = degrade_msg + "\n\n" + content
            logger.warning("[Chain] 降级告知: %s", degrade_reason)

        # 附加结构化 JSON 供前端解析
        import json as _json
        result_dict = chain_result.to_dict()
        if degraded:
            result_dict["degraded"] = True
            result_dict["degrade_reason"] = degrade_reason
        content += "\n\n<!-- decision_result:\n" + _json.dumps(result_dict, ensure_ascii=False, indent=2) + "\n-->"

        return AgentResult(
            success=chain_result.success,
            content=content,
            tool_calls_log=[],
            total_steps=len(chain_result.root_node.children) if chain_result.root_node else 0,
            total_tokens=0,
            model="chain-orchestrator",
            error=None if chain_result.success else "链路执行失败",
        )

    def chat_stream(self, message, session_id, context=None,
                    progress_callback=None, user_id=1):
        """Streaming chat — yields SSE event dicts as smolagents produces steps.

        Per-session lock prevents concurrent requests on the same session
        from interleaving get_history / add_message calls.
        """
        from app.agent.session_store import get_session_store
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

        # ── 链路执行：检测是否有匹配的编排链路 ───────────────
        # 金融领域走 Chain 编排（多 Skill 协作），非金融域 agent 自规划
        _intent_verb = meta.get("intent_verb", "")
        _intent_noun = meta.get("intent_noun", "")
        _stream_domain_check = meta.get("domain", "")
        if _stream_domain_check == "finance":
            chain_result = self._try_chain(
                _intent_verb, _intent_noun, message, session_id, context, user_id,
            )
            if chain_result is not None:
                store.add_message(session_id, "assistant", chain_result.content)
                yield {
                    "type": "generating",
                    "step": 0,
                    "message": chain_result.content,
                }
                yield {
                    "type": "done",
                    "success": chain_result.success,
                    "content": chain_result.content,
                    "error": chain_result.error,
                    "total_steps": chain_result.total_steps,
                    "model": chain_result.model,
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
                    # ToolCallingAgent 返回 dict，直接保留
                    _raw_stream_output = step.output
                    if isinstance(_raw_stream_output, dict):
                        content = _raw_stream_output
                    else:
                        content = str(_raw_stream_output) if _raw_stream_output else ""
                    store.add_message(session_id, "assistant", content if isinstance(content, str) else str(content))

                    # ── 金融领域：JSON → TraceCollector 存库 → format_decision_card ──
                    _stream_domain = meta.get("domain", "")
                    _stream_collector = meta.get("collector")
                    if content and _stream_collector and _stream_domain == "finance":
                        try:
                            import json as _json_sc
                            import re as _re_sc
                            # 提取 JSON 块：content 可能已是 dict（ToolCallingAgent）或 str（CodeAgent）
                            _sc_data = None
                            if isinstance(content, dict) and "action" in content:
                                _sc_data = content
                            elif isinstance(content, str):
                                for _pat in [r'```json\s*\n?(.*?)\n?\s*```',
                                             r'(\{[^{}]*"action"[^{}]*"score"[^{}]*\})']:
                                    _m = _re_sc.search(_pat, content, _re_sc.DOTALL)
                                    if _m:
                                        try:
                                            _sc_data = _json_sc.loads(_m.group(1).strip())
                                            if isinstance(_sc_data, dict) and "action" in _sc_data:
                                                break
                                        except (_json_sc.JSONDecodeError, TypeError):
                                            _sc_data = None

                            # 先用原始 JSON 内容调用 on_agent_finish（提取字段 + 存库）
                            # TraceCollector 需要字符串输入，dict 转 JSON 字符串
                            _tc_stream_answer = _json_sc.dumps(content, ensure_ascii=False) if isinstance(content, dict) else content
                            _tu = None
                            try:
                                _tu = agent.token_usage
                            except Exception:
                                pass
                            _total_tok = (_tu.input_tokens + _tu.output_tokens) if _tu else 0
                            _root = _stream_collector.on_agent_finish(
                                final_answer=_tc_stream_answer,
                                total_steps=agent.step_number,
                                total_tokens=_total_tok,
                                model=str(getattr(agent.model, "model_id", "")),
                            )
                            logger.info("[Agent] 流式TraceCollector 存库 root_id=%s stock=%s",
                                        _root.id, _root.stock_code)

                            # 再用 JSON 格式化为 DecisionCard 给用户
                            if _sc_data:
                                content = format_decision_card(_sc_data)
                                store.add_message(session_id, "assistant", content)
                                logger.info("[Agent] 流式DecisionCard: %s score=%s action=%s",
                                            _sc_data.get("stock_code", ""),
                                            _sc_data.get("score", ""),
                                            _sc_data.get("action", ""))
                        except Exception as e:
                            logger.warning("[Agent] 流式DecisionCard/存库失败，保留原始输出: %s", e)

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
                            _stream_domain = meta.get("domain", "")
                            def _compress(c=content, tc=_stream_tool_calls, sid=session_id, m=self.model, d=_stream_domain):
                                try:
                                    _, age = store.get_context_summary(sid, current_domain=d, with_age=True)
                                    summary = compress_context(c, tc, model=m, domain=d, age_turns=age)
                                except Exception as e:
                                    logger.warning("[Compress] 压缩异常，降级截断: %s", e)
                                    summary = c[:500]
                                if summary:
                                    store.save_context_summary(sid, summary, domain=d)
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
