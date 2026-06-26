# -*- coding: utf-8 -*-
"""
Agent — smolagents CodeAgent/ToolCallingAgent 构建器。

公开接口：
  get_smolagent(...) → CodeAgent | ToolCallingAgent
"""
from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional

from smolagents import (
    CodeAgent,
    ToolCallingAgent,
    LogLevel,
)

from app.agent.model import build_model
from app.agent.tools.registry import build_smolagent_tools
from app.agent.tools import registry as local_registry

logger = logging.getLogger(__name__)

# ── Per-user agent cache (tools + managed agents only) ────────
_tools_cache_by_domain: Dict[str, List] = {}
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
    agent = AgentClass(
        tools=tools,
        model=smol_model,
        max_steps=max_steps,
        instructions=instructions,
        verbosity_level=LogLevel.INFO,
        return_full_result=True,
        stream_outputs=True,
        planning_interval=None,
        **_extra_kwargs,
    )

    logger.info(
        "[Agent] Built %s for user=%s domain=%s: %d tools, max_steps=%d, collector=%s",
        AgentClass.__name__, user_id, domain_key, len(tools), max_steps,
        "yes" if collector else "no",
    )
    return agent


