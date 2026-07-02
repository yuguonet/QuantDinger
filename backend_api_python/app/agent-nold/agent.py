# -*- coding: utf-8 -*-
"""
Agent — smolagents CodeAgent/ToolCallingAgent 构建器。

公开接口：
  get_smolagent(...) → CodeAgent | ToolCallingAgent
"""
from __future__ import annotations

from app.agent.log import logger
import os
from typing import Dict, List, Optional

from smolagents import (
    CodeAgent,
    ToolCallingAgent,
    LogLevel,
)

from app.agent.model import build_model
from app.agent.tools.registry import build_smolagent_tools, get_local_registry as _get_local_registry
local_registry = _get_local_registry()

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
                        stock_code: str = "",
                        is_tool_mode: bool = False,
                        user_id: str = "1",
                        context_summary: str = "") -> str:
    """用 ContextBuilder 统一组装 instructions（Agent-Template 模式）。"""
    from app.agent.context_builder import ContextBuilder

    # 总 token 预算
    total_budget = int(os.getenv("INSTRUCTIONS_TOKEN_BUDGET", "6000"))
    ctx = ContextBuilder(total_budget=total_budget)

    # Layer 1: Persona
    preamble = _load_preamble()
    ctx.set_persona(preamble)

    # Layer 2: Rules
    agent_rules_text = ""
    try:
        from app.agent.semantics import get_agent_rules_text
        _r = get_agent_rules_text()
        if _r:
            agent_rules_text = _r
    except Exception:
        pass
    ctx.set_rules(agent_rules_text)

    # Layer 2b: Domain instructions
    if domain and domain_instructions:
        ctx.set_domain(domain, domain_instructions)

    # Layer 2c: Language
    if str(language or "").lower().startswith("en"):
        ctx.set_rules("\n## Output Language\n- Reply in English.\n- All JSON values in English.")
    else:
        ctx.set_rules("\n## 输出语言\n- 使用中文回答。\n- 所有面向用户的文本值使用中文。")

    # Layer 3: Skills 摘要（渐进加载，不注入完整内容）
    try:
        from app.agent.semantics import get_skills_summary_xml
        ctx.set_skills_summary(get_skills_summary_xml())
    except Exception:
        pass

    # Layer 4: Tools
    tool_catalog = ""
    if tools is not None:
        tool_catalog = f"## 工具分类\n\n{_generate_tool_catalog(tools)}"
    ctx.set_tools(tool_catalog)

    # Layer 5: History（context_summary 直接注入，指代消解）
    if context_summary:
        ctx.set_history(f"## 上文摘要\n{context_summary}\n\n用户追问时请基于以上上下文理解。")

    # Layer 6: Memory
    try:
        from app.agent.memory_store import get_memory
        memory = get_memory(user_id)
        memory_summary = memory.get_summary()
        if memory_summary:
            ctx.set_memory(f"## 长期记忆（用户偏好与历史知识）\n{memory_summary}")
    except Exception:
        pass

    # 附加：技能使用说明、源码扫描、自修改、权重等
    extras = []
    extras.append("## 技能使用说明\n\n如果需要使用 skill，请按以下步骤：\n"
                   "1. 调用 get_skill_catalog 工具获取可用技能列表\n"
                   "2. 选择合适的技能\n"
                   "3. 调用 read_skill 工具加载具体指令\n"
                   "4. 按指令执行")

    if os.getenv("AGENT_SCAN_PROJECT_READONLY", "true").lower() == "true":
        extras.append("## 源码扫描能力（只读）\n\n"
                       "可使用 list_project_files、read_project_file、grep_project 扫描项目源码。")

    if os.getenv("AGENT_TOOLS_SELF_MODIFY", "false").lower() == "true":
        modify_paths = os.getenv("AGENT_SELF_MODIFY_PATHS", "backend_api_python/app/agent/tools")
        extras.append(f"## 自修改能力\n\n允许修改目录: {modify_paths}")

    if domain == "finance":
        try:
            from app.agent.chain.store import get_skill_weights
            weights = get_skill_weights()
            if weights:
                weight_lines = ["| 技能 | 权重 |", "|------|------|"]
                for name, w in sorted(weights.items(), key=lambda x: -x[1]):
                    weight_lines.append(f"| {name} | {w:.2f} |")
                extras.append(f"## 技能权重\n\n{'chr(10)'.join(weight_lines)}")
        except Exception:
            pass

    for extra in extras:
        ctx.set_rules(extra)

    return ctx.build()
def get_smolagent(
    user_id: int = 1,
    model: str = None,
    provider: str = None,
    max_steps: int = 10,
    user_message: str = "",
    language: str = "zh",
    domain: str = "",
    domain_instructions: str = "",
    stock_code: str = "",
    stock_name: str = "",
    tool_categories: Optional[List[str]] = None,
    collector=None,  # TraceCollector（金融领域注入）
    strategy: str = "direct",  # §15: 执行策略
    is_tool_mode: bool = False,  # 是否是 tool 模式
    context_summary: str = "",  # 上下文摘要（指代消解）
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

    # ── 工具级权重过滤：移除低权重工具 ────────────────────────
    try:
        from app.agent.chain.store import query_low_weight_tools
        low_weight = query_low_weight_tools()
        if low_weight:
            before = len(tools)
            removed_names = {t.name for t in tools} & low_weight
            tools = [t for t in tools if t.name not in low_weight]
            if removed_names:
                logger.info("[Agent] 低权重工具过滤: 移除 %d 个 %s", len(removed_names), removed_names)
    except Exception as e:
        logger.debug("[Agent] 工具权重查询跳过: %s", e)

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
        stock_code=stock_code,
        is_tool_mode=is_tool_mode,
        user_id=str(user_id),
        context_summary=context_summary,
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
