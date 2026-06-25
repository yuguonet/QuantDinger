# -*- coding: utf-8 -*-
"""
Agent — smolagents Agent for QuantDinger.

核心入口：build_agent_executor() → _AgentExecutor → chat() / chat_stream()

架构：
  smolagents CodeAgent（默认）或 ToolCallingAgent（AGENT_TYPE=tool）
  + Skills（skills/ 目录，SKILL.md + run.py）
  + 40+ 工具（tools/ 目录，@tool 装饰器自动发现）
  + Chain 链路编排（chain/ 目录，verb+noun 触发）

执行流程：
  1. _prepare() — 意图分析 → 领域路由→ 上下文拼接
  2. 快速通道 — 闲聊/greeting 直接回复，不走 agent
  3. 链路触发 — _try_chain() 匹配 verb+noun → 注入执行计划到 Agent 上下文
  4. Agent 执行 — smolagents CodeAgent.run()（流式/阻塞）
  5. 后置学习 — _post_evaluate() → evaluator.learn_from_execution()
  6. 上下文压缩 — compress_context() 异步线程

配置：
  AGENT_TYPE=code|tool     — Agent 类型（默认 code）
  AGENT_MAX_STEPS=6       — 最大步数
  AGENT_TIMEOUT_SECONDS=180 — 超时
  INTENT_ANALYSIS_ENABLED=true — 意图分析开关

公开接口：
  build_agent_executor(user_id, max_steps, timeout_seconds, model, provider) → _AgentExecutor
  _AgentExecutor.chat(message, session_id, context, progress_callback, user_id) → AgentResult
  _AgentExecutor.chat_stream(...) → Generator[dict]
  AgentResult(success, content, tool_calls_log, total_steps, total_tokens, model, error, charts)
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from dataclasses import dataclass, field

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
from app.agent.tools.registry import build_smolagent_tools
from app.agent.tools import registry as local_registry
from app.agent.tool_context import set_tool_context
from app.agent.trace_collector import TraceCollector

logger = logging.getLogger(__name__)

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

def _generate_tool_catalog(tools) -> str:
    """从工具对象自动生成目录，按模块分组。"""
    try:
        local_registry.discover()
        tool_names = {t.name for t in tools}
    except Exception as e:
        logger.debug("[ToolCatalog] 生成失败: %s", e)
        return ""

    # 按模块分组
    by_module: Dict[str, List[str]] = {}
    for name in sorted(tool_names):
        spec = local_registry.get(name)
        if spec is None:
            continue
        # 从函数所属模块推断分组
        module = getattr(spec.fn, '__module__', '') or ''
        # 取最后两段: app.agent.tools.data_tools → data_tools
        parts = module.split('.')
        group = parts[-1] if len(parts) >= 2 else module
        by_module.setdefault(group, []).append(name)

    lines = []
    for group, names in sorted(by_module.items()):
        lines.append(f"**{group}**: {', '.join(names)}")

    return "\n".join(lines)





def _load_preamble() -> str:
    """从 persona.md 加载前导词（人设 + 行为规范）。"""
    from app.agent.semantics import get_persona_body
    body = get_persona_body()
    if body:
        return body
    # fallback
    from app.agent.semantics import get_persona
    persona = get_persona()
    if persona and persona.role:
        parts = [f"你是{persona.role}。"]
        if persona.identity:
            parts.append(persona.identity)
        if persona.mission:
            parts.append(f"使命：{persona.mission}")
        return "\n".join(parts)
    return "你是 QuantDinger 量化分析助手。"


def _build_instructions(user_message: str = "",
                        language: str = "zh", tools=None,
                        domain: str = "", domain_instructions: str = "",
                        intent_context: str = "", stock_code: str = "",
                        is_tool_mode: bool = False) -> str:
    if str(language or "").lower().startswith("en"):
        lang_section = "\n## Output Language\n- Reply in English.\n- All JSON values in English.\n"
    else:
        lang_section = "\n## 输出语言\n- 使用中文回答。\n- 所有面向用户的文本值使用中文。\n"

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
工具: workspace_read_file, workspace_write_file, workspace_edit_file
安全约束: 只能修改配置目录范围内的文件，先用 workspace_read_file 理解代码再做最小改动。

"""

    preamble = _load_preamble()

    # 动态生成工具分类目录
    tool_catalog = ""
    if tools is not None:
        tool_catalog = f"\n## 工具分类\n\n{_generate_tool_catalog(tools)}\n"

    # Anthropic Agent Skills catalog - 已改为工具，不再注入 instructions
    # agent 需要时会调用 get_skill_catalog 工具获取 skill 列表

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
    # 仅当有具体个股且需要输出买卖信号时才注入，否则用自然语言回复
    finance_json_section = ""
    if domain == "finance" and stock_code:
        _agent_cls = _get_agent_class()
        try:
            from app.agent.semantics import get_agent_rules_text
            _of_text = get_agent_rules_text()
            if _of_text:
                # 根据 agent 类型选择对应段落
                if _agent_cls is ToolCallingAgent:
                    # 提取 ToolCallingAgent 段落
                    import re as _of_re
                    _m = _of_re.search(r'## ToolCallingAgent 输出格式\n(.*?)(?=## CodeAgent|$)', _of_text, _of_re.DOTALL)
                    finance_json_section = f"\n## ⚠️ 输出格式（必须遵守）\n\n{_m.group(1).strip()}\n\n" if _m else ""
                else:
                    # 提取 CodeAgent 段落
                    import re as _of_re
                    _m = _of_re.search(r'## CodeAgent 输出格式\n(.*?)$', _of_text, _of_re.DOTALL)
                    finance_json_section = f"\n## ⚠️ 输出格式（必须遵守）\n\n{_m.group(1).strip()}\n\n" if _m else ""
        except Exception as e:
            logger.debug("[Instructions] 输出格式加载失败: %s", e)

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
                weight_section = f"\n## 技能权重（历史回溯数据）\n\n{'chr(10)'.join(weight_lines)}\n\n权重越高，该技能的历史预测越准确。\n"
        except Exception as e:
            logger.debug("[Instructions] 权重注入失败: %s", e)

    # 从 semantics 加载统一 agent 规则
    agent_rules_text = ""
    try:
        from app.agent.semantics import get_agent_rules_text
        _r = get_agent_rules_text()
        if _r:
            agent_rules_text = f"\n{_r}\n"
    except Exception as e:
        logger.debug("[Instructions] agent_rules 加载失败: %s", e)

    return f"""{preamble}
{agent_rules_text}

## 技能使用说明

如果需要使用 skill，请按以下步骤：
1. 调用 get_skill_catalog 工具获取可用技能列表
2. 选择合适的技能
3. 调用 read_skill 工具加载具体指令
4. 按指令执行

{tool_catalog}
{scan_section}{modify_section}{intent_section}{domain_section}{calibration_section}{weight_section}{finance_json_section}{lang_section}"""


# ═══════════════════════════════════════════════════════════════
# 2. Final Answer Validation (final_answer_checks)
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
    if not answer:
        return False

    from app.agent.json_extractor import extract_decision
    data = extract_decision(answer)
    if not data:
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
    except Exception as e:
        logger.debug("[CheckOutput] 校验异常: %s", e)
        return False


def _make_retry_once_checker():
    """包裹 _check_output_json，最多容忍一次重试。

    第一次校验失败 → 返回 False，smolagents 触发 agent 重试。
    第二次（重试后）→ 无论是否通过都放行，避免无限重试循环。
    """
    _has_retried = False

    def _checker(answer, memory, agent) -> bool:
        nonlocal _has_retried
        if _has_retried:
            # 已重试过一次，直接放行
            agent._json_output_fallback = True
            return True
        if not _check_output_json(answer, memory, agent):
            _has_retried = True
            return False  # 触发 smolagents 重试
        return True

    return _checker


# ═══════════════════════════════════════════════════════════════
# 3. Finance Domain — Decision Card Formatter
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


# ── Skill 执行 ───────────────────────────────────────────────
# 新架构：Skill → skills/<name>/run.py → 直接调用 tools/ 中的工具函数
# _try_chain 获取执行计划，注入 Agent 上下文


# ═══════════════════════════════════════════════════════════════
# 5. Agent Builder
# ═══════════════════════════════════════════════════════════════

def get_smolagent(
    user_id: int = 1,
    model: str = None,
    provider: str = None,
    max_steps: int = 10,
    user_message: str = "",
    language: str = "zh",
    domain: str = "",
    domain_instructions: str = "",
    intent_context: str = "",
    stock_code: str = "",
    tool_categories: Optional[List[str]] = None,
    collector=None,  # TraceCollector（金融领域注入）
    strategy: str = "direct",  # §15: 执行策略
    is_tool_mode: bool = False,  # 是否是 tool 模式
) -> "CodeAgent | ToolCallingAgent":
    """Build a fresh agent instance per call.

    Caches only the expensive parts (tools discovery, managed agents).
    Agent instance is always rebuilt to avoid cross-session state pollution.
    """
    smol_model = build_model(model=model, provider=provider)

    # ── 按领域过滤工具（缓存） ────────────────────────────────
    domain_key = domain or "all"
    with _tools_cache_lock:
        if domain_key not in _tools_cache_by_domain:
            # 本地 registry 自动发现 + smolagents 桥接
            tools = build_smolagent_tools({
                "domain": domain,
            })
            _tools_cache_by_domain[domain_key] = tools
        # 始终拷贝，避免修改缓存原始列表
        tools = list(_tools_cache_by_domain[domain_key])

    # ── per-phase 工具过滤（用于 per-phase agent 重建）──
    if tool_categories:
        # 只保留 tool_categories 中指定的工具
        _allow_set = set(tool_categories)
        tools = [t for t in tools if t.name in _allow_set]
        logger.info("[Agent] per-phase 工具过滤，保留 %d 个工具: %s", len(tools), tool_categories)

    # ── 注册 read_skill 和 get_skill_catalog 工具（Anthropic Agent Skills 标准）──
    try:
        from app.agent.skills.call_skill_tool import get_read_skill_tool
        from app.agent.tools.skill_catalog_tool import get_skill_catalog_tool
        read_skill = get_read_skill_tool()
        skill_catalog = get_skill_catalog_tool()
        tools.append(read_skill)
        tools.append(skill_catalog)
    except Exception as e:
        logger.warning("[Agent] read_skill/skill_catalog 工具加载失败: %s", e)

    # ── 金融领域：用 TracedTool 包装所有工具 ──────────────────
    if collector:
        from app.agent.traced_tool import TracedTool
        tools = [TracedTool(t, collector) for t in tools]

    instructions = _build_instructions(
        user_message, language, tools,
        domain=domain, domain_instructions=domain_instructions,
        intent_context=intent_context, stock_code=stock_code,
        is_tool_mode=is_tool_mode,
    )

    AgentClass = _get_agent_class()

    # ── 确保项目根目录在 sys.path（沙箱 import 需要）──
    import sys as _sys
    _backend_root = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
    if _backend_root not in _sys.path:
        _sys.path.insert(0, _backend_root)

    # ── Always build fresh agent (avoid cross-session state pollution) ──
    _extra_kwargs = {}
    if AgentClass is CodeAgent:
        _extra_kwargs["additional_authorized_imports"] = [
            "pandas", "numpy", "json", "math", "statistics",
            "datetime", "collections", "itertools", "re",
            # 项目模块（app.* 通配符放行所有子模块）
            "app.*",
        ]
        # 代码执行超时（默认 30s 太短，批量工具调用会超时）
        _code_exec_timeout = int(os.getenv("CODE_EXECUTION_TIMEOUT", "120"))
        _extra_kwargs["executor_kwargs"] = {"timeout_seconds": _code_exec_timeout}

    # §15: 用 strategy 替代 domain 做 JSON 校验决策
    # traced 策略 + 有 stock_code → 强制 JSON 校验；其他 → 宽松校验
    # 无 stock_code 时 system prompt 不会注入 JSON 格式指令（见 _build_instructions），
    # Agent 不再输出结构化 JSON，只需非空检查
    checks = [_check_dashboard_json]

    agent = AgentClass(
        tools=tools,
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
        "[Agent] Built %s for user=%s domain=%s: %d tools, max_steps=%d, collector=%s",
        AgentClass.__name__, user_id, domain_key, len(tools), max_steps,
        "yes" if collector else "no",
    )
    return agent


# ═══════════════════════════════════════════════════════════════
# 6. Result Wrapper & Executor
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
    user_id=1, max_steps=10,
    timeout_seconds=None, model=None, provider=None,
    domain=None,
):
    return _AgentExecutor(
        user_id=user_id, max_steps=max_steps,
        timeout_seconds=timeout_seconds, model=model, provider=provider,
    )


# ═══════════════════════════════════════════════════════════════
# _prepare_intent 返回结构
# ═══════════════════════════════════════════════════════════════

@dataclass
class _IntentPrepResult:
    """_prepare_intent() 的结构化返回结果。"""
    skip_agent: bool = False
    skip_agent_reply: str = ""
    intent_context: str = ""
    domain: str = ""
    intent: Optional[Any] = None  # IntentResult
    strategy: str = "direct"
    domain_instructions: str = ""
    tool_categories: Optional[List[str]] = None
    verb: str = ""
    noun: str = ""
    tool_chain: List[str] = field(default_factory=list)
    intent_meta: Dict[str, Any] = field(default_factory=dict)  # 额外元数据
    # §17.2: Planner 前置后新增字段
    collector: Optional[Any] = None  # TraceCollector
    enriched: str = ""  # 上下文拼接结果
    chain_def: Optional[Any] = None  # ChainDef 对象（per-phase 执行用）


class _AgentExecutor:
    """Wraps smolagents CodeAgent with session management."""

    def __init__(self, user_id=1, max_steps=10,
                 timeout_seconds=None, model=None, provider=None):
        self.user_id = user_id
        self.max_steps = max_steps
        self.timeout_seconds = timeout_seconds
        self.model = model
        self.provider = provider
        # Agent instance — set by _prepare(), used for interrupt support
        self._current_agent = None
        import threading as _threading
        self._agent_ready_event = _threading.Event()

    def _prepare_intent(self, message, session_id, context, user_id=1):
        """快速通道 → 意图分析(LLM) → 提取 stock_code，返回结构化结果。

        流程：
        0. 负面反馈检测 — 惩罚上一轮 chain
        1. 快速通道 — 正则匹配闲聊（0 LLM 调用）
        2. 意图分析 — LLM #1（仅非闲聊时）
        3. 提取 stock_code — 从消息解析个股代码
        """
        from app.agent.intent_analyzer import _quick_intent_check, analyze_intent, format_intent_for_agent
        from app.agent.session_store import get_session_store
        store = get_session_store()

        result = _IntentPrepResult()

        # ── 0. 负面反馈检测：惩罚上一轮 chain ──────────────────
        self._check_negative_feedback(message, session_id)

        # ── 1. 快速通道（极低成本正则，零 LLM 调用）───────────
        quick = _quick_intent_check(message)
        if quick:
            _quick_replies = {
                "greeting": "你好！我是 QuantDinger 量化分析助手，可以帮你做股票分析、选股筛选、策略回测等。有什么需要帮忙的？",
                "farewell": "再见！有问题随时找我。",
                "thanks": "不客气！有需要随时找我。",
                "empty": "请告诉我你需要什么帮助。",
            }
            result.skip_agent = True
            result.skip_agent_reply = _quick_replies.get(quick.intent, "你好！有什么需要帮忙的？")
            result.intent = quick
            logger.info("[Intent] Pre-LLM quick path: %s", quick.intent)
            return result

        # ── 2. 意图分析（LLM #1）──────────────────────────────
        if os.getenv("INTENT_ANALYSIS_ENABLED", "true").lower() == "true":
            try:
                history = store.get_history(session_id)[-6:]
                intent = analyze_intent(
                    message, model=self.model, provider=self.provider,
                    history=history,
                )
                domain = intent.domain
                result.intent = intent
                result.domain = domain
                result.strategy = intent.strategy
                result.domain_instructions = intent.domain_instructions

                result.intent_context = format_intent_for_agent(intent, message)
                result.verb = getattr(intent, 'verb', '') or ""
                result.noun = getattr(intent, 'noun', '') or ""
                result.tool_chain = (getattr(intent, 'metadata', None) or {}).get("tool_chain", [])
                result.intent_meta = {
                    "intent": intent.intent,
                    "confidence": intent.confidence,
                    "source": intent.source,
                }

                logger.info(
                    "[Intent] domain=%s strategy=%s intent=%s confidence=%.2f",
                    domain, result.strategy, intent.intent, intent.confidence,
                )
                logger.debug(
                    "[Intent] domain=%s strategy=%s intent=%s confidence=%.2f verb=%s noun=%s",
                    domain, result.strategy, intent.intent, intent.confidence,
                    result.verb, result.noun,
                )

                # Update tool context with domain
                from app.agent.tool_context import get_tool_context
                _ctx = get_tool_context()
                _ctx["domain"] = domain
                _ctx["strategy"] = result.strategy
                set_tool_context(_ctx)

                # ── Post-LLM 闲聊快速通道（LLM 也识别为 chat 时）──
                if (domain == "chat"
                        and intent.confidence >= 0.6
                        and intent.intent in ("greeting", "farewell", "thanks", "empty")):
                    _quick_replies = {
                        "greeting": "你好！我是 QuantDinger 量化分析助手，可以帮你做股票分析、选股筛选、策略回测等。有什么需要帮忙的？",
                        "farewell": "再见！有问题随时找我。",
                        "thanks": "不客气！有需要随时找我。",
                        "empty": "请告诉我你需要什么帮助。",
                    }
                    result.skip_agent = True
                    result.skip_agent_reply = _quick_replies.get(intent.intent, "你好！有什么需要帮忙的？")
                    logger.info("[Intent] Post-LLM quick-reply for %s, skipping agent", intent.intent)
                    return result

                # ── 未知意图：反问用户，不瞎猜 ──
                _is_cron = context and context.get("source") == "cron"
                if (domain == "unknown" or intent.intent == "unknown") and intent.confidence <= 0.4:
                    if _is_cron:
                        result.skip_agent = True
                        result.skip_agent_reply = ""
                        logger.info("[Intent] Cron unknown intent (conf=%.2f), skipping agent", intent.confidence)
                    else:
                        result.skip_agent = True
                        result.skip_agent_reply = "没太明白你的意思，能说得具体一点吗？比如是要分析股票、看行情、设提醒，还是其他什么？"
                        logger.info("[Intent] Unknown intent (conf=%.2f, domain=%s), asking for clarification", intent.confidence, domain)
                    return result

            except Exception as e:
                import traceback
                logger.debug("[Intent] 分析异常: %s", e)
                logger.warning("[Intent] 分析失败，走默认流程: %s\n%s", e, traceback.format_exc())
                return result

        # ── 3. 提取 stock_code（金融领域才提取，否则跳过）───
        if result.domain in ("finance", "trading"):
            stock_code = (context or {}).get("stock_code", "")
            stock_name = (context or {}).get("stock_name", "")
            if not stock_code:
                import re as _re_stock
                _m = _re_stock.search(r'(?<!\d)(\d{6})(?!\d)', message)
                if _m:
                    stock_code = _m.group(1)
                    # 从 DB 取股票名称
                    try:
                        from app.utils.basicinfo_db import get_stock_basic_db
                        _stock = get_stock_basic_db().get_stock(stock_code)
                        if _stock:
                            stock_name = _stock.get("name", "")
                    except Exception as e:
                        logger.debug("[Prepare] stock DB 查询失败: %s", e)
            if not stock_code:
                from app.agent.text_utils import extract_stock_from_message
                _code, _name = extract_stock_from_message(message)
                if _code:
                    stock_code = _code
                    if _name:
                        stock_name = _name
                        logger.info("[Prepare] 中文名 → 代码 %s", stock_code)
            if stock_code:
                # 用 DB 权威名称覆盖 LLM 猜测的名称
                try:
                    from app.utils.basicinfo_db import get_stock_basic_db
                    _db_stock = get_stock_basic_db().get_stock(stock_code)
                    if _db_stock:
                        _db_name = _db_stock.get("name", "")
                        if _db_name:
                            if stock_name and stock_name != _db_name:
                                logger.info("[Prepare] stock_name 校正: LLM='%s' → DB='%s' (code=%s)", stock_name, _db_name, stock_code)
                            stock_name = _db_name
                except Exception as e:
                    logger.debug("[Prepare] stock_name 校正失败: %s", e)
                result.intent_meta["stock_code"] = stock_code
                if stock_name:
                    result.intent_meta["stock_name"] = stock_name
                if session_id:
                    try:
                        store.update_session(session_id, stock_code=stock_code)
                    except Exception as e:
                        logger.debug("[Prepare] update_session 失败: %s", e)

        # ── 话题切换：非个股意图时清除遗留 stock_code ────────
        _is_stock_intent = result.noun in ("stock", "chart") or result.intent in (
            "stock_analysis", "chart_view", "backtest", "indicator", "fund_flow")
        if not _is_stock_intent and context and context.get("stock_code"):
            logger.info("[Prepare] 意图=%s 非个股，清除遗留 stock_code=%s", result.intent, context.get("stock_code"))
            context.pop("stock_code", None)
            context.pop("stock_name", None)

        # ── 合并 intent 提取的股票代码到 context ──────────────
        if result.intent_meta.get("stock_code") and not (context and context.get("stock_code")):
            context = context or {}
            context["stock_code"] = result.intent_meta["stock_code"]
            if result.intent_meta.get("stock_name"):
                context["stock_name"] = result.intent_meta["stock_name"]

        # ── 4. 创建 TraceCollector（策略触发，非领域绑定）──────
        collector = None
        if result.strategy == "traced":
            collector = TraceCollector(session_id=session_id, user_query=message)
            collector.intent_verb = result.verb
            collector.intent_noun = result.noun
            collector.domain = result.domain
            if context and context.get("stock_code"):
                collector.stock_code = context["stock_code"]
            if context and context.get("stock_name"):
                collector.stock_name = context["stock_name"]
        result.collector = collector

        # ── 5. 上下文拼接 ────────────────────────────────────
        from app.agent.session_store import get_session_store
        store = get_session_store()
        enriched = message
        ctx_parts = []
        context_summary, total_rounds = store.get_context_summary(
            session_id, current_domain=result.domain, with_age=True)
        if context_summary:
            ctx_parts.append(context_summary)
            round_num = total_rounds + 1
        else:
            round_num = 1

        _DOMAIN_CTX = {
            "finance": {
                "fields": {"stock_code": "股票代码", "stock_name": "股票名称"},
                "json_fields": {"realtime_quote": "实时行情", "chip_distribution": "筹码分布"},
                "missing_hints": ["realtime_quote", "chip_distribution"],
            },
            "trading": {
                "fields": {"stock_code": "股票代码", "stock_name": "股票名称"},
                "json_fields": {},
                "missing_hints": [],
            },
        }
        _domain_cfg = _DOMAIN_CTX.get(result.domain)
        _current_round_parts = []
        if _domain_cfg and context:
            for key, label in _domain_cfg["fields"].items():
                if context.get(key):
                    _current_round_parts.append(f"{label}: {context[key]}")
            for key, label in _domain_cfg["json_fields"].items():
                if context.get(key):
                    _current_round_parts.append(f"[{label}]\n{json.dumps(context[key], ensure_ascii=False)[:2000]}")
            _missing = [label for key, label in _domain_cfg["json_fields"].items()
                        if key in _domain_cfg["missing_hints"] and not context.get(key)]
            if _missing:
                _current_round_parts.append(f"需获取: {', '.join(_missing)}")
        if context_summary:
            sep = f"--- R{round_num} ---"
            if _current_round_parts:
                sep += "\n" + "\n".join(_current_round_parts)
            ctx_parts.append(sep)
        elif _current_round_parts:
            ctx_parts.append(f"--- R{round_num} ---\n" + "\n".join(_current_round_parts))
        if ctx_parts:
            enriched = "\n".join(ctx_parts) + "\n\n" + message
        result.enriched = enriched

        # ── 6. 获取执行计划（始终调用，verb/noun 为空时由 Planner 从消息解析）──
        chain_def = None
        try:
            chain_def = self._try_chain(
                result, message,
                session_id, context, user_id,
            )
        except Exception as e:
            logger.warning("[Planner] 链路获取异常，降级到 agent: %s", e)
        result.chain_def = chain_def

        return result

    def _prepare(self, message, session_id, context, user_id):
        from app.agent.session_store import get_session_store
        store = get_session_store()
        set_tool_context({
            "session_id": session_id,
            "user_id": user_id,
            "progress_callback": None,
        })

        # ── §17.2: _prepare_intent() 已完成步骤 1-6 ───────────
        # 1. 快速通道  2. 意图分析  3. stock_code
        # 4. TraceCollector  5. 上下文拼接  6. Planner
        _ipr = self._prepare_intent(message, session_id, context, user_id)

        # ── 快速通道：不需要 agent 时直接返回 ────────────────
        if _ipr.skip_agent:
            store.add_message(session_id, "user", message)
            self._agent_ready_event.set()
            return store, None, message, {"skip_agent": True, "skip_agent_reply": _ipr.skip_agent_reply}

        # ── §17.2 步骤 7.5: 同步 stock_code/stock_name 到 context ──
        _ipr_stock_code = (_ipr.intent_meta or {}).get("stock_code", "")
        _ipr_stock_name = (_ipr.intent_meta or {}).get("stock_name", "")
        if _ipr_stock_code:
            if context is None:
                context = {}
            if not context.get("stock_code"):
                context["stock_code"] = _ipr_stock_code
            if _ipr_stock_name and not context.get("stock_name"):
                context["stock_name"] = _ipr_stock_name

        # ── §17.2 步骤 8: 构建精简 Agent ──────────────────────
        stock_code = (context or {}).get("stock_code", "")
        agent = get_smolagent(
            user_id=user_id,
            model=self.model, provider=self.provider,
            max_steps=self.max_steps, user_message=message,
            language=(context or {}).get("report_language", "zh"),
            domain=_ipr.domain, domain_instructions=_ipr.domain_instructions,
            intent_context=_ipr.intent_context,
            stock_code=stock_code,
            tool_categories=None,
            collector=_ipr.collector,
            strategy=_ipr.strategy,
        )

        store.add_message(session_id, "user", message)
        if _ipr.domain:
            store.save_context_summary(session_id, "", domain=_ipr.domain)

        # Expose agent for interrupt support
        self._current_agent = agent
        self._agent_ready_event.set()

        enriched = _ipr.enriched

        return store, agent, enriched, {
            "skip_agent": False, "skip_agent_reply": "",
            "intent_verb": _ipr.verb, "intent_noun": _ipr.noun,
            "tool_chain": _ipr.tool_chain,
            "domain": _ipr.domain,
            "strategy": _ipr.strategy,
            "collector": _ipr.collector,
            "enriched": enriched,
            "chain_def": _ipr.chain_def,
            "stock_code": (context or {}).get("stock_code", ""),
            "stock_name": (context or {}).get("stock_name", ""),
        }

    # ═══════════════════════════════════════════════════════════════
    # §17.2 阶段 2: 执行循环（可多轮，工具失效时快速退出）
    # ═══════════════════════════════════════════════════════════════

    def _execute_phase(self, agent, enriched, max_steps, context, meta, store, session_id):
        """执行单个阶段，返回 (success, content, tool_calls_log, total_steps, total_tokens, charts_b64, result_obj)。

        §3.1 步骤 9 错误检测：
          - 成功 → 返回 success
          - 工具失效 + steps ≤ PLAN_PHASE_FAST_EXIT → 快速退出，返回错误供 LLM #2 决策
        """
        fast_exit_steps = int(os.getenv("PLAN_PHASE_FAST_EXIT_STEPS", "3"))

        t0 = time.time()
        # ── 调试日志：agent.run() 前 ──────────────────────────
        logger.debug(
            "[Phase] enriched_len=%d max_steps=%d agent_type=%s domain=%s strategy=%s",
            len(enriched) if enriched else 0, max_steps, type(agent).__name__,
            (context or {}).get('domain', ''), (meta or {}).get('strategy', ''),
        )
        result = agent.run(enriched, max_steps=max_steps)

        # ── §3.1 步骤 9: 错误检测 + 快速退出 ──────────────────
        # 注意：result.steps 是 ActionStep 对象列表，不是 dict！
        # 必须用 step.error / step.tool_calls 等属性访问，不能用 step.get()
        total_steps = len(result.steps) if hasattr(result, 'steps') and result.steps else 0
        if hasattr(result, 'steps') and result.steps:
            # 连续工具失败达到阈值 → 快速退出
            consecutive_failures = 0
            max_consecutive = 0
            _not_found_tool = False
            for step in result.steps:
                if isinstance(step, ActionStep):
                    if step.error:
                        consecutive_failures += 1
                        max_consecutive = max(max_consecutive, consecutive_failures)
                        # 检测工具不存在的情况
                        if "not among the explicitly allowed tools" in str(step.error) or "Forbidden function" in str(step.error):
                            _not_found_tool = True
                    else:
                        consecutive_failures = 0

            # 工具不存在 → 立即退出
            if _not_found_tool:
                logger.warning("[Phase] 检测到调用不存在的工具，立即退出")
                error_content = "调用不存在的工具，立即退出。最后错误: "
                for step in reversed(result.steps):
                    if isinstance(step, ActionStep) and step.error:
                        error_content += str(step.error)[:200]
                        break
                return False, error_content, [], total_steps, 0, [], result

            if max_consecutive >= fast_exit_steps and total_steps <= max_steps:
                logger.warning(
                    "[Phase] 工具连续失败 %d 步（阈值 %d），快速退出",
                    max_consecutive, fast_exit_steps
                )
                error_content = f"工具连续失败 {max_consecutive} 步，快速退出。最后错误: "
                for step in reversed(result.steps):
                    if isinstance(step, ActionStep) and step.error:
                        error_content += str(step.error)[:200]
                        break
                return False, error_content, [], total_steps, 0, [], result

            # ── 重复工具调用检测（同一工具+同一参数调用 ≥ 3 次 → 快速退出）──
            _tool_call_counter: Dict[str, int] = {}
            _REPEAT_THRESHOLD = 3
            for step in result.steps:
                if isinstance(step, ActionStep) and step.tool_calls:
                    for tc in step.tool_calls:
                        _tc_name = getattr(tc, 'name', '') or ''
                        if not _tc_name or _tc_name == 'final_answer':
                            continue
                        _tc_args = getattr(tc, 'arguments', {}) or {}
                        _args_key = str(sorted(_tc_args.items())) if isinstance(_tc_args, dict) else str(_tc_args)
                        _tc_key = f"{_tc_name}:{_args_key[:80]}"
                        _tool_call_counter[_tc_key] = _tool_call_counter.get(_tc_key, 0) + 1

            _repeated_tools = {k: v for k, v in _tool_call_counter.items() if v >= _REPEAT_THRESHOLD}
            if _repeated_tools:
                _rep_desc = "; ".join(f"{k} ×{v}" for k, v in _repeated_tools.items())
                logger.warning(
                    "[Phase] 检测到重复工具调用（阈值 %d），快速退出: %s",
                    _REPEAT_THRESHOLD, _rep_desc
                )
                return False, f"工具重复调用 {_REPEAT_THRESHOLD}+ 次，快速退出。重复工具: {_rep_desc}", \
                    tool_calls_log, total_steps, total_tokens, charts_b64, result

            # ── 连续空结果检测（工具成功但无数据，LLM 换关键词重试场景）──
            _EMPTY_RESULT_THRESHOLD = 3
            _consecutive_empty = 0
            for step in result.steps:
                if not isinstance(step, ActionStep) or step.error:
                    _consecutive_empty = 0
                    continue
                obs = getattr(step, 'observations', None) or getattr(step, 'observation', None) or ""
                if not isinstance(obs, str):
                    obs = str(obs) if obs else ""
                if not obs:
                    continue
                # 检测空结果模式：count=0 / news=[] / results=[] / 无数据
                _is_empty = False
                try:
                    import json as _json_check
                    _json_match = _json_check.loads(obs) if obs.strip().startswith('{') else None
                    if isinstance(_json_match, dict):
                        if _json_match.get('count', -1) == 0:
                            _is_empty = True
                        for _key in ('results', 'news', 'data', 'items', 'stocks'):
                            _val = _json_match.get(_key)
                            if isinstance(_val, list) and len(_val) == 0:
                                _is_empty = True
                except (ValueError, TypeError):
                    pass
                if not _is_empty and ('无数据' in obs or '未找到' in obs or '无结果' in obs or 'not found' in obs.lower()):
                    _is_empty = True
                if _is_empty:
                    _consecutive_empty += 1
                else:
                    _consecutive_empty = 0
            if _consecutive_empty >= _EMPTY_RESULT_THRESHOLD:
                logger.warning(
                    "[Phase] 连续 %d 次空结果，快速退出",
                    _consecutive_empty
                )
                return False, \
                    f"工具连续返回空数据 {_consecutive_empty} 次，该股票可能无此数据源。请基于已有数据给出分析，注明数据缺失。", \
                    tool_calls_log, total_steps, total_tokens, charts_b64, result

        # ── 解析 agent 输出 ──
        if hasattr(result, "output"):
            _raw_output = result.output
            # 统一转为字符串，避免 dict 内容在后续 [:300] 切片时崩溃
            if isinstance(_raw_output, dict):
                content = json.dumps(_raw_output, ensure_ascii=False)
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
                if isinstance(sd, ActionStep) and sd.tool_calls:
                    for tc in sd.tool_calls:
                        tool_calls_log.append({
                            "tool": getattr(tc, 'name', ''),
                            "arguments": getattr(tc, 'arguments', {}) or {},
                            "success": sd.error is None,
                            "duration": sd.timing.duration if hasattr(sd, 'timing') and sd.timing else 0,
                        })
                obs = getattr(sd, 'observations', None) or getattr(sd, 'observation', None) or ""
                if obs and isinstance(obs, str):
                    for _cm in _re_chat.finditer(r'__CHART_B64__([A-Za-z0-9+/=]+)__END_CHART__', obs):
                        charts_b64.append(_cm.group(1))
        else:
            content = str(result) if result else ""
            total_steps = total_tokens = 0
            tool_calls_log = []
            charts_b64 = []

        # ── 快速退出：工具失效时不满 max_steps 就退出 ──
        if not success and total_steps <= fast_exit_steps and tool_calls_log:
            _error_tools = [tc["tool"] for tc in tool_calls_log if not tc.get("success", True)]
            if _error_tools:
                logger.warning("[Phase] 工具失效快速退出: %s (steps=%d)", _error_tools, total_steps)
                return False, f"工具执行失败: {', '.join(_error_tools)}", tool_calls_log, total_steps, total_tokens, charts_b64, result

        # ── 兜底：agent 跑满 max_steps 未调 final_answer ──
        if not success and tool_calls_log and not content:
            _last_output = ""
            for sd in (result.steps if hasattr(result, "steps") and result.steps else []):
                obs = getattr(sd, 'observations', None) or getattr(sd, 'observation', None) or ""
                if obs and isinstance(obs, str) and len(obs) > len(_last_output):
                    _last_output = obs
            if _last_output:
                content = _last_output
                success = True
                logger.warning("[Agent] 未调 final_answer，从最后 tool 输出恢复")
            import re as _re_fallback
            if content:
                for _cm in _re_fallback.finditer(r'__CHART_B64__([A-Za-z0-9+/=]+)__END_CHART__', content):
                    charts_b64.append(_cm.group(1))
                content = _re_fallback.sub(r'__CHART_B64__[A-Za-z0-9+/=]+__END_CHART__', '', content).strip()
            success = bool(content)

        return success, content, tool_calls_log, total_steps, total_tokens, charts_b64, result

    def _execute_step_loop_blocking(self, agent, message, context, meta, store, session_id,
                                     planner, intent, stock_code, stock_name):
        """单步决策循环（阻塞版，返回元组）。"""
        logger.info("[StepLoop] 入口（阻塞）| stock=%s %s", stock_code, stock_name)

        _agent_config = {
            "user_id": meta.get("user_id", 1),
            "model": meta.get("model"),
            "provider": meta.get("provider"),
            "max_steps": self.max_steps,
            "language": (context or {}).get("report_language", "zh"),
            "domain": meta.get("domain", ""),
            "domain_instructions": meta.get("domain_instructions", ""),
            "intent_context": meta.get("intent_context", ""),
            "stock_code": stock_code,
            "collector": meta.get("collector"),
            "strategy": meta.get("strategy", "direct"),
        }

        all_content = []
        all_tool_calls = []
        all_charts = []
        total_steps = 0
        total_tokens = 0
        last_result = None
        max_loop_steps = 10

        from app.agent.checkpoint import get_checkpoint_manager
        ckpt_mgr = get_checkpoint_manager(session_id)

        from app.agent.judge import Judge
        judge = Judge(call_llm=planner._call_llm)
        judge_context = ""
        judge_summaries = []

        all_phases_completed = False
        for loop_step in range(max_loop_steps):
            logger.info("[StepLoop] 决策下一步 (loop_step=%d)", loop_step)

            step_result = planner.plan_next_step(
                query=message, judge_context=judge_context, intent=intent,
                stock_code=stock_code, stock_name=stock_name, context_summary="",
            )
            if not step_result.success:
                logger.warning("[StepLoop] Planner 决策失败: %s", step_result.reasoning)
                break

            logger.info("[StepLoop] 执行步骤: skill=%s, tools=%s", step_result.skill, step_result.tools)

            ckpt_mgr.save(
                step=loop_step + 1, skill=step_result.skill, description=step_result.description,
                tools=step_result.tools, rules=step_result.rules,
                step_content="", step_success=False, steps_used=0,
                all_content=list(all_content), previous_results=[],
                total_steps=total_steps, total_tokens=total_tokens,
                stock_code=stock_code, stock_name=stock_name,
                intent_data={"verb": getattr(intent, 'verb', ''), "noun": getattr(intent, 'noun', ''),
                             "domain": getattr(intent, 'domain', '')},
            )

            step_context = self._build_step_context(step_result, stock_code, stock_name, judge_context, message)
            step_agent = self._build_step_agent(step_result, _agent_config, message)

            step_success, step_content, step_tool_calls, step_steps, step_tokens, step_charts, step_result_obj = \
                self._execute_phase(step_agent, step_context, self.max_steps, context, meta, store, session_id)

            all_content.append(step_content or "")
            all_tool_calls.extend(step_tool_calls or [])
            all_charts.extend(step_charts or [])
            total_steps += step_steps
            total_tokens += step_tokens
            last_result = step_result_obj

            _cp = ckpt_mgr.load(loop_step + 1)
            if _cp:
                _cp.step_content = step_content or ""
                _cp.step_success = step_success
                _cp.steps_used = step_steps

            if not step_result.tools and step_success:
                logger.info("[StepLoop] Planner 无工具可选且上一步成功，任务完成")
                break

            remaining = max_loop_steps - loop_step - 1
            judge_result = judge.judge_step(
                query=message, step_number=loop_step + 1,
                step_description=step_result.description, step_content=step_content or "",
                step_success=step_success, previous_summaries=judge_summaries,
                step_queue_remaining=remaining, intent=intent,
                stock_code=stock_code, stock_name=stock_name,
            )
            judge_summaries.append(judge_result.summary)
            judge_context = judge_result.next_context
            logger.info("[Judge] step=%d continue=%s summary=%s", loop_step + 1, judge_result.continue_loop, judge_result.summary[:50])

            if not judge_result.continue_loop:
                logger.info("[Judge] 决定停止: %s", judge_result.reasoning)
                break
        else:
            all_phases_completed = True

        final_result = judge.judge_final(
            query=message, all_summaries=judge_summaries, all_contents=all_content,
            intent=intent, stock_code=stock_code, stock_name=stock_name,
        )
        final_output = final_result.output
        content = json.dumps(final_output, ensure_ascii=False) if final_output else "\n\n".join(c for c in all_content if c)
        success = bool(content)

        if final_result.need_rerun:
            logger.info("[Judge] 需要补跑: %s", final_result.rerun_hint)

        ckpt_mgr.clear()

        self._post_learn_and_compress(
            content, success, all_tool_calls,
            total_steps, total_tokens, meta, session_id, store, agent, last_result,
            chain_def=meta.get("chain_def"), all_phases_completed=all_phases_completed,
        )
        self._save_root_to_traces(content, success, context, meta, message, store)
        return success, content, all_tool_calls, total_steps, total_tokens, all_charts, last_result, all_phases_completed

    def _execute_step_loop_core(self, agent, message, context, meta, store, session_id,
                                 planner, intent, stock_code, stock_name,
                                 stream=False,
                                 _stream_tool_calls=None, _stream_tool_call_counter=0, _pending_tool_ids=None):
        """单步决策循环（流式 generator）。"""
        from smolagents import FinalAnswerStep as _FAS
        _stream_tool_calls = _stream_tool_calls if _stream_tool_calls is not None else []
        _pending_tool_ids = _pending_tool_ids if _pending_tool_ids is not None else {}

        logger.info("[StepLoop] 入口（流式）| stock=%s %s", stock_code, stock_name)

        _agent_config = {
            "user_id": meta.get("user_id", 1),
            "model": meta.get("model"),
            "provider": meta.get("provider"),
            "max_steps": self.max_steps,
            "language": (context or {}).get("report_language", "zh"),
            "domain": meta.get("domain", ""),
            "domain_instructions": meta.get("domain_instructions", ""),
            "intent_context": meta.get("intent_context", ""),
            "stock_code": stock_code,
            "collector": meta.get("collector"),
            "strategy": meta.get("strategy", "direct"),
        }

        all_content = []
        all_tool_calls = []
        all_charts = []
        total_steps = 0
        total_tokens = 0
        last_result = None
        max_loop_steps = 10

        # ── 断点管理器 ────────────────────────────────────────
        from app.agent.checkpoint import get_checkpoint_manager
        ckpt_mgr = get_checkpoint_manager(session_id)

        # ── LLM#4 Judge ───────────────────────────────────────
        from app.agent.judge import Judge
        judge = Judge(call_llm=planner._call_llm)  # 复用同一个 LLM
        judge_context = ""        # Judge 产出的上下文摘要，传给 Planner
        judge_summaries = []       # 所有步骤的摘要

        all_phases_completed = False
        for loop_step in range(max_loop_steps):
            logger.info("[StepLoop] 决策下一步 (loop_step=%d)", loop_step)
            if stream:
                yield {"type": "tool_info", "tool": "", "message": f"── 决策第 {loop_step + 1} 步 ──"}

            # ── LLM#2 Planner：选工具（只看 Judge 摘要，不看原始数据）──
            step_result = planner.plan_next_step(
                query=message, judge_context=judge_context, intent=intent,
                stock_code=stock_code, stock_name=stock_name, context_summary="",
            )

            if not step_result.success:
                logger.warning("[StepLoop] Planner 决策失败: %s", step_result.reasoning)
                if stream:
                    yield {"type": "tool_info", "tool": "", "message": f"❌ 决策失败: {step_result.reasoning}"}
                break

            # ── 执行当前步骤 ──
            logger.info("[StepLoop] 执行步骤: skill=%s, tools=%s", step_result.skill, step_result.tools)
            if stream:
                yield {"type": "tool_info", "tool": "", "message": f"── 执行: {step_result.description} ──"}

            # 保存 checkpoint（执行前状态）
            ckpt_mgr.save(
                step=loop_step + 1,
                skill=step_result.skill,
                description=step_result.description,
                tools=step_result.tools,
                rules=step_result.rules,
                step_content="",
                step_success=False,
                steps_used=0,
                all_content=list(all_content),
                previous_results=[],
                total_steps=total_steps,
                total_tokens=total_tokens,
                stock_code=stock_code,
                stock_name=stock_name,
                intent_data={"verb": getattr(intent, 'verb', ''), "noun": getattr(intent, 'noun', ''),
                             "domain": getattr(intent, 'domain', '')},
            )

            step_context = self._build_step_context(step_result, stock_code, stock_name, judge_context, message)
            step_agent = self._build_step_agent(step_result, _agent_config, message)

            if stream:
                content = ""
                for s in step_agent.run(step_context, max_steps=self.max_steps, stream=True):
                    events = _step_to_events(s)
                    for ev in events:
                        yield ev
                        if ev.get("type") == "tool_start":
                            _stream_tool_calls.append({"tool": ev.get("tool", ""), "success": True, "_id": _stream_tool_call_counter})
                            _pending_tool_ids[ev.get("tool", "")] = _stream_tool_call_counter
                            _stream_tool_call_counter += 1
                        elif ev.get("type") == "tool_done":
                            tool_name = ev.get("tool", "")
                            pending_id = _pending_tool_ids.pop(tool_name, None)
                            if pending_id is not None:
                                for tc in _stream_tool_calls:
                                    if tc.get("_id") == pending_id:
                                        tc["success"] = ev.get("success", True)
                                        break
                    if isinstance(s, _FAS):
                        raw = s.output
                        content = json.dumps(raw, ensure_ascii=False) if isinstance(raw, dict) else (str(raw) if raw else "")
                step_success = bool(content)
                step_content = content
                steps_used = step_agent.step_number if hasattr(step_agent, 'step_number') else 0
                step_tool_calls = []
                step_tokens = 0
                step_charts = []
                step_result_obj = step_agent
            else:
                step_success, step_content, step_tool_calls, step_steps, step_tokens, step_charts, step_result_obj = \
                    self._execute_phase(step_agent, step_context, self.max_steps, context, meta, store, session_id)

            all_content.append(step_content or "")
            if not stream:
                all_tool_calls.extend(step_tool_calls or [])
                all_charts.extend(step_charts or [])
                total_steps += step_steps
                total_tokens += step_tokens
            last_result = step_result_obj

            # 更新 checkpoint 的执行结果
            _cp = ckpt_mgr.load(loop_step + 1)
            if _cp:
                _cp.step_content = step_content or ""
                _cp.step_success = step_success if not stream else bool(step_content)
                _cp.steps_used = steps_used if stream else step_steps

            # ── Planner 无工具可选 → 任务完成 ──
            step_success_actual = step_success if not stream else bool(step_content)
            if not step_result.tools and step_success_actual:
                logger.info("[StepLoop] Planner 无工具可选且上一步成功，任务完成")
                if stream:
                    yield {"type": "tool_info", "tool": "", "message": "✅ 任务完成"}
                break

            # ── LLM#4 Judge：总结 + 纠错 + 控循环 ──
            remaining = max_loop_steps - loop_step - 1
            judge_result = judge.judge_step(
                query=message,
                step_number=loop_step + 1,
                step_description=step_result.description,
                step_content=step_content or "",
                step_success=step_success_actual,
                previous_summaries=judge_summaries,
                step_queue_remaining=remaining,
                intent=intent,
                stock_code=stock_code,
                stock_name=stock_name,
            )
            judge_summaries.append(judge_result.summary)
            judge_context = judge_result.next_context

            logger.info("[Judge] step=%d continue=%s summary=%s", loop_step + 1, judge_result.continue_loop, judge_result.summary[:50])
            if stream:
                yield {"type": "tool_info", "tool": "", "message": f"📊 {judge_result.summary}"}
                if judge_result.corrections:
                    yield {"type": "tool_info", "tool": "", "message": f"⚠️ 纠错: {judge_result.corrections}"}

            # Judge 决定是否继续
            if not judge_result.continue_loop:
                logger.info("[Judge] 决定停止: %s", judge_result.reasoning)
                if stream:
                    yield {"type": "tool_info", "tool": "", "message": f"✅ {judge_result.reasoning}"}
                break
        else:
            all_phases_completed = True

        # ── 最终输出：LLM#4 Judge 读取全量数据，输出结构化结果 ──
        # 保持 summaries 和 contents 一一对应（不过滤空值）
        final_result = judge.judge_final(
            query=message,
            all_summaries=judge_summaries,
            all_contents=all_content,
            intent=intent,
            stock_code=stock_code,
            stock_name=stock_name,
        )
        final_output = final_result.output
        content = json.dumps(final_output, ensure_ascii=False) if final_output else "\n\n".join(c for c in all_content if c)
        success = bool(content)

        # 如需补跑（Judge 发现数据缺失）
        if final_result.need_rerun:
            logger.info("[Judge] 需要补跑: %s", final_result.rerun_hint)

        # 清理 checkpoint
        ckpt_mgr.clear()

        if stream:
            store.add_message(session_id, "assistant", content if isinstance(content, str) else str(content))
            self._post_learn_and_compress(
                content, success, _stream_tool_calls,
                agent.step_number, 0, meta, session_id, store, agent, agent,
                chain_def=meta.get("chain_def"), all_phases_completed=all_phases_completed,
            )
            self._save_root_to_traces(content, success, context, meta, message, store)
            yield {
                "type": "done", "success": success, "content": content,
                "error": None if content else "No final answer",
                "total_steps": agent.step_number,
                "model": str(getattr(agent.model, "model_id", "")),
                "session_id": session_id,
            }
        else:
            self._post_learn_and_compress(
                content, success, all_tool_calls,
                total_steps, total_tokens, meta, session_id, store, agent, last_result,
                chain_def=meta.get("chain_def"), all_phases_completed=all_phases_completed,
            )
            self._save_root_to_traces(content, success, context, meta, message, store)
            return success, content, all_tool_calls, total_steps, total_tokens, all_charts, last_result, all_phases_completed

    def _build_step_context(self, step_result, stock_code, stock_name, judge_context, message):
        """构建当前步骤的上下文。"""
        parts = [
            f"[步骤] {step_result.description}",
            f"标的: {stock_name or '未知'}（{stock_code}）" if stock_code else "",
        ]

        # 注入 Judge 上下文摘要（替代前序结果）
        if judge_context:
            parts.append(f"\n上一步结论: {judge_context}")

        # 注入规则
        if step_result.rules:
            parts.append(f"\n规则: {step_result.rules}")

        # 注入 skill 指令
        if step_result.skill:
            parts.append(f"\n请用 read_skill 加载 {step_result.skill} 的指令并执行。")

        return "\n".join(p for p in parts if p)

    def _build_step_agent(self, step_result, _agent_config, message):
        """构建当前步骤的 agent。"""
        # 确定工具列表
        phase_tools = []
        is_tool_mode = True

        if step_result.skill:
            # skill 模式
            try:
                from app.agent.semantics import get_all_skill_metas
                skill_metas = get_all_skill_metas()
                meta_skill = skill_metas.get(step_result.skill)
                if meta_skill and meta_skill.tools:
                    phase_tools = meta_skill.tools + ["get_skill_catalog", "read_skill"]
                    is_tool_mode = False
            except Exception as e:
                logger.debug("[StepAgent] skill 元数据加载失败: %s", e)
        elif step_result.tools:
            # 工具模式
            phase_tools = step_result.tools

        # 重建 agent
        step_agent = get_smolagent(
            user_id=_agent_config["user_id"],
            model=_agent_config["model"],
            provider=_agent_config["provider"],
            max_steps=_agent_config["max_steps"],
            user_message=message,
            language=_agent_config["language"],
            domain=_agent_config["domain"],
            domain_instructions=_agent_config["domain_instructions"],
            intent_context=_agent_config["intent_context"],
            stock_code=_agent_config["stock_code"],
            tool_categories=phase_tools,
            collector=_agent_config["collector"],
            strategy=_agent_config["strategy"],
            is_tool_mode=is_tool_mode,
        )
        logger.info("[StepLoop] 重建 agent，加载 %d 个工具: %s", len(phase_tools), phase_tools)

        return step_agent

    def _execute_plan(self, agent, chain_def, message, context, meta, store, session_id):
        """执行计划（阻塞版）。"""
        return self._execute_plan_core(
            agent, chain_def, message, context, meta, store, session_id,
        )

    def _execute_plan_stream(self, agent, chain_def, message, context, meta, store, session_id,
                             _stream_tool_calls, _stream_tool_call_counter, _pending_tool_ids):
        """执行计划（流式版）。"""
        yield from self._execute_plan_core(
            agent, chain_def, message, context, meta, store, session_id,
            stream=True,
            _stream_tool_calls=_stream_tool_calls,
            _stream_tool_call_counter=_stream_tool_call_counter,
            _pending_tool_ids=_pending_tool_ids,
            resume_from=resume_from,
        )

    def _execute_plan_core(self, agent, chain_def, message, context, meta, store, session_id,
                           stream=False,
                           _stream_tool_calls=None, _stream_tool_call_counter=0, _pending_tool_ids=None):
        """执行计划（单步决策模式，阻塞/流式统一）。"""
        logger.info("[Plan] 入口 | chain_id=%s", chain_def.chain_id)

        stock_code = (context or {}).get("stock_code", "") or meta.get("stock_code", "")
        stock_name = (context or {}).get("stock_name", "") or meta.get("stock_name", "")
        intent = meta.get("intent")
        from app.agent.planner import Planner
        smol_model = build_model(self.model, self.provider)

        def planner_llm(prompt: str) -> str:
            messages = [{"role": "user", "content": prompt}]
            response = smol_model(messages)
            return response.content if hasattr(response, "content") else str(response)

        planner = Planner(call_llm=planner_llm)
        if stream:
            yield from self._execute_step_loop_core(
                agent, message, context, meta, store, session_id,
                planner, intent, stock_code, stock_name, stream=True,
                _stream_tool_calls=_stream_tool_calls,
                _stream_tool_call_counter=_stream_tool_call_counter,
                _pending_tool_ids=_pending_tool_ids,
            )
        else:
            return self._execute_step_loop_blocking(
                agent, message, context, meta, store, session_id,
                planner, intent, stock_code, stock_name,
            )

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
        """Internal chat implementation — assumes session lock is held.

        §17.2 三阶段架构：
          阶段 1: _prepare_intent() — 快速通道 + 意图分析 + stock_code + TraceCollector + 上下文 + 获取执行计划
          阶段 2: _execute_plan() 或 _execute_phase()
                 — 有链路 → per-phase 循环，每步 agent.run() 只有 1 步内容
                 — 无链路 → agent 自由执行
          阶段 3: _handle_decision_card + _post_learn_and_compress + _save_root_to_traces
        """
        # ── 阶段 1: 准备意图 ──────────────────────────────────
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

        # ── 阶段 2: 执行阶段 ──
        chain_def = meta.get("chain_def")

        logger.debug(
            "[Chat] 阶段2 | chain_def=%s verb=%s noun=%s domain=%s strategy=%s",
            '有' if chain_def else 'None', meta.get('intent_verb', ''),
            meta.get('intent_noun', ''), meta.get('domain', ''), meta.get('strategy', ''),
        )

        if chain_def:
            # per-phase 执行：每个 phase 单独一次 agent.run()，只看到这 1 步
            success, content, tool_calls_log, total_steps, total_tokens, charts_b64, result, all_phases_completed = \
                self._execute_plan(agent, chain_def, message, context, meta, store, session_id)
        else:
            # 无链路：Agent 自由执行（一次调用，内部自循环）
            success, content, tool_calls_log, total_steps, total_tokens, charts_b64, result = \
                self._execute_phase(agent, enriched, self.max_steps, context, meta, store, session_id)
            all_phases_completed = None

        # ── 阶段 3: 结果处理 ──────────────────────────────────
        store.add_message(session_id, "assistant", content if isinstance(content, str) else str(content))

        # DecisionCard + TraceCollector 存库（traced 策略）
        content = self._handle_decision_card(
            content, success, agent, result,
            meta.get("collector"), meta.get("strategy", "direct"),
            store, session_id, context, meta, total_steps, tool_calls_log,
        )

        # 后置学习闭环 + 异步压缩上下文
        _all_phases_completed = all_phases_completed if chain_def else None
        self._post_learn_and_compress(
            content, success, tool_calls_log, total_steps, total_tokens,
            meta, session_id, store, agent, result,
            chain_def=meta.get("chain_def"), all_phases_completed=_all_phases_completed,
        )

        # 非 traced 策略兜底根节点写入
        self._save_root_to_traces(content, success, context, meta, message, store)

        # JSON 校验重试失败后标记
        if getattr(agent, '_json_output_fallback', False):
            if isinstance(content, str):
                content += "\n\n> 输出内容未做标准化处理"
            elif isinstance(content, dict):
                content["_warning"] = "输出内容未做标准化处理"

        return AgentResult(
            success=success, content=content, tool_calls_log=tool_calls_log,
            total_steps=total_steps, total_tokens=total_tokens,
            model=str(getattr(agent.model, "model_id", "")),
            error=None if success else "Agent did not produce a final answer",
            charts=charts_b64,
        )

    # ═══════════════════════════════════════════════════════════════
    # 公共后处理函数（chat / chat_stream 复用）
    # ═══════════════════════════════════════════════════════════════

    def _save_root_to_traces(self, content, success, context, meta, message, store):
        """非 traced 策略的兜底根节点写入 qd_traces。"""
        if meta.get("collector"):
            return
        try:
            from app.agent.chain.schema import EvalNode, Layer, Status
            from app.agent.json_extractor import extract_decision
            from datetime import date as _date

            _sc = (context or {}).get("stock_code", "")
            _sn = (context or {}).get("stock_name", "")
            _dec = extract_decision(content) if content else None

            root = EvalNode(
                layer=Layer.CHAIN.value,
                name=f"{meta.get('intent_verb', '')}+{meta.get('intent_noun', '')}" or "agent",
                exec_date=_date.today(),
                stock_code=_sc, stock_name=_sn,
                score=_dec.get("score") if _dec else None,
                direction=_dec.get("direction", "") if _dec else "",
                action=_dec.get("action", "") if _dec else "",
                signal=_dec.get("signal", "") if _dec else "",
                confidence=_dec.get("confidence") if _dec else None,
                timeframe=_dec.get("timeframe", "") if _dec else "",
                analysis=str(content)[:2000] if content else "",
                input_params={"user_query": message},
                status=Status.OK.value if success else Status.FAILED.value,
                elapsed_ms=0,
            )
            store.save_tree(root)
            logger.info("[Agent] 根节点存库 stock=%s action=%s", _sc, root.action)
        except Exception as _e:
            logger.warning("[Agent] 根节点存库失败（不影响返回）: %s", _e)

    def _handle_decision_card(self, content, success, agent, result,
                              collector, strategy, store, session_id,
                              context, meta, total_steps, tool_calls_log):
        """traced 策略: JSON → TraceCollector 存库 → DecisionCard 格式化。"""
        if not (success and content and collector and strategy == "traced"):
            return content
        try:
            from app.agent.json_extractor import extract_decision as _extract_dec
            _card_data = _extract_dec(content)

            if _card_data is None and isinstance(content, str) and content.strip():
                _stock_code = (context or {}).get("stock_code", "")
                _stock_name = (context or {}).get("stock_name", "")
                if not _stock_code:
                    for _k in ("stock_code", "stock", "symbol", "code"):
                        if _k in (meta or {}) and meta[_k]:
                            _stock_code = str(meta[_k])
                            break
                _card_data = {
                    "action": "skip", "score": 0, "direction": "neutral",
                    "confidence": "low", "timeframe": "T+3",
                    "timeframe_reason": "agent未输出结构化JSON",
                    "stock_code": _stock_code or (collector.stock_code if collector else ""),
                    "stock_name": _stock_name or (collector.stock_name if collector else ""),
                    "signal": "分析未完成，输出格式异常",
                    "factors": [], "analysis": content[:2000],
                }
                logger.warning("[Agent] JSON提取失败，fallback → skip。原始内容前200字: %s", content[:200])

            _tc_answer = json.dumps(content, ensure_ascii=False) if isinstance(content, dict) else content
            tu = result.token_usage if hasattr(result, 'token_usage') else None
            total_tok = (tu.input_tokens + tu.output_tokens) if tu else 0
            root = collector.on_agent_finish(
                final_answer=_tc_answer, total_steps=total_steps,
                total_tokens=total_tok,
                model=str(getattr(agent.model, "model_id", "")),
            )
            logger.info("[Agent] TraceCollector 存库 root_id=%s stock=%s", root.id, root.stock_code)

            if _card_data:
                content = format_decision_card(_card_data)
                store.add_message(session_id, "assistant", content)
                logger.info("[Agent] DecisionCard: %s score=%s action=%s",
                            _card_data.get("stock_code", ""), _card_data.get("score", ""), _card_data.get("action", ""))
        except Exception as e:
            logger.warning("[Agent] DecisionCard/存库失败，保留原始输出: %s", e)
        return content

    def _post_learn_and_compress(self, content, success, tool_calls_log,
                                 total_steps, total_tokens, meta, session_id,
                                 store, agent, result, chain_def=None,
                                 all_phases_completed=None):
        """后置学习闭环 + 异步压缩上下文。"""
        agent_result_for_eval = AgentResult(
            success=success, content=content, tool_calls_log=tool_calls_log,
            total_steps=total_steps, total_tokens=total_tokens,
        )
        self._post_evaluate(
            agent_result_for_eval,
            meta.get("tool_chain", []),
            meta.get("intent_verb", ""), meta.get("intent_noun", ""),
            domain=meta.get("domain", ""),
            session_id=session_id,
            chain_def=chain_def,
            all_phases_completed=all_phases_completed,
        )

        if success and content:
            try:
                from app.agent.context_compressor import compress_context
                import threading
                _d = meta.get("domain", "")
                def _compress(c=content, tc=tool_calls_log, sid=session_id, m=self.model, d=_d):
                    try:
                        _, age = store.get_context_summary(sid, current_domain=d, with_age=True)
                        summary = compress_context(c, tc, model=m, domain=d, age_turns=age)
                    except Exception as e:
                        logger.warning("[Compress] 压缩异常，降级截断: %s", e)
                        summary = c[:500]
                    if summary:
                        store.save_context_summary(sid, summary, domain=d)
                threading.Thread(target=_compress, daemon=True).start()
            except Exception as e:
                logger.debug("[Compress] 异步压缩启动失败: %s", e)

    @staticmethod
    def _post_evaluate(agent_result, tool_chain, verb, noun, domain="", session_id=None, chain_def=None, all_phases_completed=None):
        """后置学习闭环（纯规则，不消耗 agent 步数）。

        Args:
            all_phases_completed: None=不适用，True=全部phase完成，False=phase被中断
        """
        # 存储本轮 verb/noun 到 session，供下一轮负面反馈检测使用
        if session_id and (verb or noun):
            try:
                from app.agent.session_store import get_session_store
                get_session_store().update_session(
                    session_id, last_verb=verb, last_noun=noun,
                )
            except Exception as e:
                logger.debug("[PostEval] session 更新失败: %s", e)

        if not verb and not noun:
            return
        try:
            from app.agent.evaluator import learn_from_execution
            learn_from_execution(agent_result, verb, noun, chain_def=chain_def, all_phases_completed=all_phases_completed)
        except Exception as e:
            logger.warning("[PostEval] 学习闭环异常，不影响返回: %s", e)

    @staticmethod
    def _check_negative_feedback(message: str, session_id: str) -> None:
        """检测用户负面反馈，同时惩罚 trace 和 tool_chains。

        每次反馈：
          - trace: 标 correct=False + calibration 重校准
          - tool_chains: success_count 扣减（轻度-1，重度-2）

        累计 2-4 次负面反馈后：
          - trace: 删除整棵 trace 树
          - tool_chains: success_count <= 0 或 success_rate < 0.2 时删除链路
        """
        try:
            from app.agent.session_store import get_session_store
            store = get_session_store()
            from app.agent.chain.tool_chains import detect_feedback_severity, penalize_chain
            from app.agent.chain import store as chain_store

            severity = detect_feedback_severity(message)
            if not severity:
                return

            # 从 session 取上一轮的 verb/noun
            session = store.get_session(session_id)
            if not session:
                return
            last_verb = session.get("last_verb", "")
            last_noun = session.get("last_noun", "")
            if not last_verb or not last_noun:
                return

            # ── trace 层惩罚 ──
            stock_code = session.get("stock_code", "")
            if stock_code:
                trace = chain_store.query_latest_root(stock_code)
                if trace:
                    root_id = trace["id"]
                    penalty_count = chain_store.get_penalty_count(stock_code)

                    if penalty_count >= 3:
                        # 累计 >=3 次，删除整棵 trace 树
                        chain_store.delete_tree(root_id)
                        logger.info("[Feedback] 删除 trace 树 root_id=%d (累计 %d 次)", root_id, penalty_count + 1)
                    else:
                        # 标记 wrong + 重校准
                        chain_store.mark_root_wrong(root_id)
                        logger.info("[Feedback] 标记 trace root_id=%d correct=False", root_id)

            # ── tool_chains 层惩罚 ──
            penalize_chain(last_verb, last_noun, severity)

            logger.info("[Feedback] %s 负面反馈: verb=%s noun=%s stock=%s msg=%s",
                        severity, last_verb, last_noun, stock_code, message[:50])
        except Exception as e:
            logger.warning("[Feedback] 负面反馈处理异常: %s", e)

    def _try_chain(self, intent, message, session_id, context, user_id):
        """返回执行计划。统一使用 Planner 单步决策模式。"""
        from app.agent.chain.chains import ChainDef, register_chain

        chain_def = ChainDef(
            chain_id="step_loop_mode",
            name="单步决策模式",
            description="使用 Planner 单步决策，逐步执行",
            steps=[],
            trigger_verbs=[],
            trigger_nouns=[],
            context={},
            progressive=True,
        )
        register_chain(chain_def)
        logger.info("[Chain] 使用单步决策模式 (verb=%s noun=%s)", intent.verb, intent.noun)
        return chain_def

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
        # ── 负面反馈检测：惩罚上一轮 chain ────────────────────
        self._check_negative_feedback(message, session_id)

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

        # §17.2: chain_def 已在 _prepare_intent() → _try_chain() 中获取
        chain_def = meta.get("chain_def")
        _stream_tool_calls = []
        _stream_tool_call_counter = 0
        _pending_tool_ids: Dict[str, int] = {}

        # ── per-phase 流式执行 ──
        if chain_def:
            yield from self._execute_plan_stream(
                agent, chain_def, message, context, meta, store, session_id,
                _stream_tool_calls, _stream_tool_call_counter, _pending_tool_ids,
            )
            return

        # ── 无链路：自由执行 ──
        t0 = time.time()
        # ── 调试日志：agent.run() 前（Stream 自由执行）───────
        logger.debug(
            "[Chat-Stream] enriched_len=%d max_steps=%d agent_type=%s domain=%s strategy=%s",
            len(enriched) if enriched else 0, self.max_steps, type(agent).__name__,
            meta.get('domain', ''), meta.get('strategy', ''),
        )
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

                    # DecisionCard + TraceCollector 存库（traced 策略）
                    content = self._handle_decision_card(
                        content, bool(content), agent, agent,
                        meta.get("collector"), meta.get("strategy", "direct"),
                        store, session_id, context, meta,
                        agent.step_number, _stream_tool_calls,
                    )

                    # JSON 校验重试失败后标记
                    if getattr(agent, '_json_output_fallback', False):
                        if isinstance(content, str):
                            content += "\n\n> 输出内容未做标准化处理"
                        elif isinstance(content, dict):
                            content["_warning"] = "输出内容未做标准化处理"

                    # 后置学习闭环 + 异步压缩上下文
                    self._post_learn_and_compress(
                        content, bool(content), _stream_tool_calls,
                        agent.step_number, 0, meta, session_id, store, agent, agent,
                        chain_def=meta.get("chain_def"),
                    )

                    # 非 traced 策略兜底根节点写入
                    self._save_root_to_traces(content, bool(content), context, meta, message, store)

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
