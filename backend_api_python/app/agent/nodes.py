# -*- coding: utf-8 -*-
"""
nodes.py — Graph 节点定义

4 个节点 + plan 复盘循环：
  - chat_node：RAG + 实体解析 + 意图分类 + 简单问题直接回答
  - plan_node：生成任务描述 + step_budget（复盘时带前轮结果）
  - execute_node：单 CodeAgent 执行，跨轮复用实例
  - finalize_node：格式化汇总 + 保存 memory + trace.finish() 写入 qd_traces

每个节点签名为 async def node(state: dict) -> dict | None：
  - 输入：完整状态
  - 输出：partial state（只返回需要更新的字段）
"""
from __future__ import annotations

import inspect
import logging
import re
import time
from typing import Any, Dict, List, Optional, TypedDict

from llm.base import ChatMessage, LLMBase
from utils.tracing import AgentTraceRecorder

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
#  格式化函数
# ═══════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════
#  AgentState — 状态类型定义
# ═══════════════════════════════════════════════════════════════
class AgentState(TypedDict, total=False):
    """Graph 状态。节点之间通过它传递数据。"""

    # ── 输入 ──
    user_input: str
    session_id: str
    use_rag: bool

    # ── chat_node 输出（通用实体字段）──
    entity_code: str      # 实体代码（股票代码/商品代码/...）
    entity_name: str      # 实体名称
    entity_type: str      # 实体类型（stock/commodity/crypto/...）
    context: str          # RAG 上下文（仅 chat_node 检索一次）
    sources: list         # RAG 来源
    effective_input: str  # 扩写后的完整指令

    # ── chat_node 路由 ──
    needs_task: bool      # True=进 plan→execute 任务流程, False=直接回答
    task_type: str        # 任务子类型: analysis/screen/compare/query/general
    direct_answer: str    # 直接回答内容（needs_task=False 时有值）

    # ── plan_node 输出 ──
    task: str             # 完整任务描述
    selected_skill: str   # plan 选中的技能名（None=无技能）
    selected_domain: str  # plan 选中的领域名（空=仅通用工具）
    skill_body: str       # 选中技能的 SKILL.md 正文
    skill_tools: list     # 选中技能的工具列表（_SkillResourceTool + _SkillFuncTool）
    step_budget: int      # CodeAgent 本轮步数预算
    planning_interval: int

    # ── execute_node 输出 ──
    result_raw: str       # CodeAgent 执行结果
    hit_max_steps: bool   # True=max_steps 耗尽，需复盘
    replan_count: int     # 已复盘次数
    _code_agent: Any      # CodeAgent 实例（跨轮复用，不序列化）
    _failed_tools: list   # 失败工具列表
    _agent_plan: str      # smolagents 最终规划

    # ── finalize_node 输出 ──
    elapsed: float

    # ── 错误处理 ──
    error: str
    failed_node: str


# ═══════════════════════════════════════════════════════════════
#  Context — 非序列化运行时对象（不进 checkpoint）
# ═══════════════════════════════════════════════════════════════

class NodeContext:
    """节点共享的运行时对象。

    这些对象不可序列化，不存 checkpoint，通过闭包传给节点函数。
    """

    def __init__(
        self,
        llm: LLMBase,
        memory=None,
        retriever=None,
        skill_adapter=None,
        system_prompt: str = "",
        memory_window_size: int = 10,
        max_tool_rounds: int = 10,
        entity_resolver: EntityResolver | None = None,
    ):
        self.llm = llm
        self.memory = memory
        self.retriever = retriever
        self.skill_adapter = skill_adapter
        self.system_prompt = system_prompt
        self.memory_window_size = memory_window_size
        self.max_tool_rounds = max_tool_rounds
        self.entity_resolver = entity_resolver  # 由外部注入（如 StockResolver）

        # ToolProvider 运行时（惰性初始化）
        self.tool_provider = None
        self.model = None

        # TaskAgent 实例（用于调用 _build_code_agent 等方法）
        self.agent = None

        # TraceCollector（session 级）
        self.collectors: Dict[str, Any] = {}

    def init_tools(self):
        """初始化 ToolProvider（扫描 tools/ 目录）+ LLM 适配器。"""
        from tools.base import ToolProvider
        from agents.task_agent import _LLMAdapter
        from pathlib import Path

        tools_dir = Path(__file__).resolve().parent / "tools"
        provider = ToolProvider()
        # 扫描 tools/ 根目录（通用工具）
        provider.scan_directory(tools_dir, domain="common", package_prefix="tools")
        # 扫描 tools/ 子目录（领域工具）
        provider.scan_subdirectories(tools_dir, package_prefix="tools")

        self.tool_provider = provider
        self.model = _LLMAdapter(self.llm)

        # 设置全局默认 provider，供工具内部调用
        ToolProvider.set_default(provider)

        logger.info("[Context] ToolProvider 初始化完成: %d 个工具", len(provider))


# ═══════════════════════════════════════════════════════════════
#  节点函数
# ═══════════════════════════════════════════════════════════════




def _set_llm_timeout(agent, timeout_seconds: int):
    """设置 LLM 超时（直接改底层 OpenAI 客户端，而非 model 属性）。"""
    try:
        # smolagents _LLMAdapter → 内部 _llm (OpenAILLM) → _client (AsyncOpenAI)
        llm_adapter = getattr(agent, 'model', None)
        if llm_adapter and hasattr(llm_adapter, '_llm'):
            inner_llm = llm_adapter._llm
            client = getattr(inner_llm, '_client', None)
            if client:
                client.timeout = timeout_seconds
                logger.debug("[Execute] 已设置 OpenAI 客户端 timeout=%ds", timeout_seconds)
                return
        # 兜底：改 model 属性
        if llm_adapter and hasattr(llm_adapter, 'timeout'):
            llm_adapter.timeout = timeout_seconds
    except Exception as e:
        logger.debug("[Execute] 设置超时失败: %s", e)


def _record_tool_calls_to_trace(trace, agent):
    """从 smolagents agent memory 提取工具调用 + 推理链，写入 AgentTraceRecorder。

    提取内容：
      - ActionStep: tool_name, args, observations, model_output, code_action, token_usage
      - PlanningStep: 每轮规划文本（不只是最后一轮）
    """
    try:
        from smolagents.memory import ActionStep, PlanningStep

        # ── 1. 遍历 ActionStep，提取工具调用 + 推理链 ──
        for step in getattr(agent.memory, 'steps', []) or []:
            if not isinstance(step, ActionStep):
                continue

            tool_name = ""
            tool_args = {}
            if hasattr(step, 'tool_calls') and step.tool_calls:
                tc = step.tool_calls[0]
                tool_name = getattr(tc, 'name', '') or getattr(tc, 'function', {}).get('name', '')
                raw_args = getattr(tc, 'arguments', None) or getattr(tc, 'function', {}).get('arguments', {})
                if isinstance(raw_args, str):
                    try:
                        tool_args = json.loads(raw_args)
                    except Exception:
                        tool_args = {}
                elif isinstance(raw_args, dict):
                    tool_args = raw_args
            elif hasattr(step, 'tool_name') and step.tool_name:
                tool_name = step.tool_name
                raw_args = getattr(step, 'tool_arguments', None) or {}
                if isinstance(raw_args, dict):
                    tool_args = raw_args

            if not tool_name:
                continue

            observations = str(getattr(step, 'observations', '') or '')
            elapsed_ms = 0.0
            if hasattr(step, 'start_time') and hasattr(step, 'end_time'):
                if step.start_time and step.end_time:
                    elapsed_ms = (step.end_time - step.start_time) * 1000

            error = ""
            if "_failed_tool" in observations or "error" in observations.lower():
                m = re.search(r"'error'\s*:\s*'([^']*)'", observations)
                if m:
                    error = m.group(1)[:200]

            # ── 提取推理链（model_output / code_action / token_usage）──
            model_output = (getattr(step, 'model_output', '') or '')[:1000]
            code_action = (getattr(step, 'code_action', '') or '')[:500]
            token_usage = None
            raw_usage = getattr(step, 'token_usage', None)
            if raw_usage:
                token_usage = {
                    'input': getattr(raw_usage, 'input_tokens', 0),
                    'output': getattr(raw_usage, 'output_tokens', 0),
                    'total': getattr(raw_usage, 'total_tokens', 0),
                }

            trace.add_tool_call(
                tool_name=tool_name,
                arguments=tool_args,
                result={
                    'observations': observations[:2000] if observations else '',
                    'model_output': model_output,
                    'code_action': code_action,
                    'token_usage': token_usage,
                },
                elapsed_ms=elapsed_ms,
                error=error,
            )

        # ── 2. 遍历 PlanningStep，记录每轮规划 ──
        for step in getattr(agent.memory, 'steps', []) or []:
            if not isinstance(step, PlanningStep):
                continue
            plan_text = (getattr(step, 'plan', '') or '').strip()
            if plan_text:
                trace.record('planning', {
                    'plan': plan_text[:2000],
                    'token_usage': {
                        'input': getattr(getattr(step, 'token_usage', None), 'input_tokens', 0),
                        'output': getattr(getattr(step, 'token_usage', None), 'output_tokens', 0),
                    } if getattr(step, 'token_usage', None) else None,
                })

    except Exception as e:
        logger.debug("[Execute] trace.add_tool_call 提取失败: %s", e)


def _extract_failed_tools(agent, tool_provider=None) -> list:
    """从 agent memory 中提取失败工具。

    工具返回含 error 的 dict 时，observations 中会标记失败。
    这里从 observations 中提取失败工具名。
    """
    failed = []
    seen = set()
    try:
        from smolagents.memory import ActionStep
        for step in getattr(agent.memory, 'steps', []):
            if not isinstance(step, ActionStep):
                continue
            obs = str(getattr(step, 'observations', '') or '')
            # 从 observation 中提取 _failed_tool 字段值
            for m in re.finditer(r"'_failed_tool'\s*:\s*'(\w+)'", obs):
                name = m.group(1)
                if name and name not in seen:
                    seen.add(name)
                    desc = ""
                    if tool_provider:
                        func = tool_provider.get(name)
                        if func:
                            desc = (inspect.getdoc(func) or "").split("\n")[0][:60]
                    failed.append((name, desc))
    except Exception:
        pass
    return failed


def make_chat_node(ctx: NodeContext):
    """创建 chat_node（闭包捕获 ctx）。

    职责：
      1. RAG 检索（只做一次，结果贯穿后续链路）
      2. 实体解析（代码/名称/标识）
      3. 消息标准化（短指令 → 完整分析指令）
      4. 意图分类：简单问题直接回答，需要工具进 plan
    """

    async def chat_node(state: dict) -> dict:
        """对话层：RAG + 实体解析 + 意图判断 + 简单问题直接回答。"""
        user_input = state["user_input"]
        session_id = state.get("session_id", "default")
        use_rag = state.get("use_rag", True)
        trace = state.get("_trace")

        # ── 1. RAG 检索（仅此处执行一次）──
        sources = []
        context = ""
        docs = []
        if use_rag and ctx.retriever:
            try:
                docs = await ctx.retriever.retrieve(user_input)
                # 过滤低相关度文档（避免噪音污染任务）
                RAG_SCORE_THRESHOLD = 0.7
                docs = [d for d in docs if d.get("score", 0) >= RAG_SCORE_THRESHOLD]
                if docs:
                    from rag.retriever import Retriever
                    context = Retriever.format_context(docs)
                    sources = [{"content": d["content"][:200], "score": d.get("score", 0)} for d in docs]
                    logger.info("[Chat] RAG 检索到 %d 条文档（相关度>=%.2f）, %d 字符", len(docs), RAG_SCORE_THRESHOLD, len(context))
            except Exception as e:
                logger.warning("[Chat] RAG 检索失败: %s", e)

        # ── 2. 实体解析（RAG 上下文辅助）──
        entity_code = ""
        entity_name = ""
        entity_type = ""
        effective_input = user_input
        # RAG 辅助：用户消息无明确代码时，从 context 中提取最近分析的标的
        resolve_input = user_input
        if context and not re.search(r'(?<!\d)\d{6}(?!\d)', user_input):
            code_match = re.search(r'(?:代码|code|symbol)[：:]*\s*(\d{6})', context, re.IGNORECASE)
            if code_match:
                resolve_input = f"{user_input} {code_match.group(1)}"
                logger.info("[Chat] RAG 辅助实体解析: 注入代码 %s", code_match.group(1))
        if ctx.entity_resolver:
            try:
                entity = ctx.entity_resolver.resolve(resolve_input)
                if entity:
                    entity_code = entity.entity_code
                    entity_name = entity.entity_name
                    entity_type = entity.entity_type
                    # 直接用 resolver 生成的 effective_input（含实体注入+扩写）
                    if entity.effective_input:
                        effective_input = entity.effective_input
                    elif entity_code:
                        # resolver 没生成 effective_input，手动注入实体信息
                        entity_desc = f"{entity_name}({entity_code})" if entity_name else entity_code
                        effective_input = f"{user_input} 【实体】{entity_desc} [{entity_type}]"
                    logger.info("[Chat] 实体解析: %s → %s %s (%s)", user_input, entity_code, entity_name, entity_type)
            except Exception as e:
                logger.debug("[Chat] 实体解析跳过: %s", e)

        # ── 4. 意图分类：统一 LLM 分类 ──
        needs_task = True
        direct_answer = ""
        task_type = ""

        try:
            # 从文件加载意图分类器 prompt
            intent_system = ""
            try:
                import os
                intent_prompt_path = os.path.join(os.path.dirname(__file__), "prompts", "intent_classifier.txt")
                with open(intent_prompt_path, encoding="utf-8") as f:
                    intent_system = f.read()
            except Exception:
                # 兜底：硬编码
                intent_system = (
                    "你是意图分类器。判断用户消息的意图类型。\n"
                    "输出格式: 只回复一个类型词\n"
                    "类型说明：\n"
                    "- task: 需要工具完成任务（分析、查询、搜索、计算、对比等）\n"
                    "- chat: 不需要工具（闲聊、问候、简单知识问答、感谢等）\n"
                    "- analysis: 分析、评估、诊断\n"
                    "- screen: 筛选、选股、推荐、找\n"
                    "- compare: 对比、比较、PK\n"
                    "- query: 查询、查一下、获取数据\n"
                    "- code: 写代码、开发、编程、实现功能\n"
                    "- explain: 解释、说明、教学、教程\n"
                    "- general: 其他需要工具的任务\n"
                    "只回复一个类型词，不要解释"
                )
            if context:
                intent_system += f"\n\n【参考上下文】\n{context[:1000]}\n如果上下文中提到过具体标的或分析，优先判断为 task。"
            if entity_code:
                intent_system += f"\n\n【已识别实体】{entity_name}({entity_code}) [{entity_type}]\n该实体已解析完成，用户消息必然需要工具，优先判断为 task。"
            intent_messages = [
                ChatMessage(role="system", content=intent_system),
                ChatMessage(role="user", content=user_input),
            ]
            intent_resp = await ctx.llm.generate(messages=intent_messages)
            intent = (intent_resp.content or "").strip().lower()
            if "chat" in intent and "task" not in intent:
                needs_task = False
                logger.info("[Chat] 意图分类: chat（直接回答）")
            else:
                needs_task = True
                # 提取 task_type
                _ALL_TYPES = ["cron", "analysis", "screen", "compare", "query", "code", "explain", "general"]
                for tt in _ALL_TYPES:
                    if tt in intent:
                        task_type = tt
                        break
                if not task_type:
                    task_type = "general"
                logger.info("[Chat] 意图分类: task, 子类型=%s", task_type)
        except Exception as e:
            logger.warning("[Chat] 意图分类失败，默认走任务流程: %s", e)
            needs_task = True
            task_type = "general"

        # ── 4.5 Cron 意图拦截：直接创建定时任务，不走 plan/execute ──
        if task_type == "cron":
            try:
                from agents.task_agent import TaskAgent
                cron_result = TaskAgent._try_intercept_cron(user_input, session_id)
                if cron_result is not None:
                    logger.info("[Chat] Cron 意图拦截成功: %s", cron_result.content[:80])
                    return {
                        "needs_task": False,
                        "task_type": "cron",
                        "direct_answer": cron_result.content,
                    }
                else:
                    # 正则未匹配，降级为普通 task
                    task_type = "general"
                    logger.info("[Chat] Cron 意图但正则未匹配，降级为 task")
            except Exception as e:
                task_type = "general"
                logger.warning("[Chat] Cron 拦截异常，降级为 task: %s", e)

        # ── 5. 直接回答（不需要工具）──
        if not needs_task:
            messages = [ChatMessage(role="system", content=ctx.system_prompt)]
            if context:
                messages.append(ChatMessage(role="system", content=f"【参考资料】\n{context}"))
            if ctx.memory:
                history = await ctx.memory.get_history(session_id, limit=ctx.memory_window_size)
                for msg in history:
                    messages.append(ChatMessage(role=msg.role, content=msg.content))
            messages.append(ChatMessage(role="user", content=user_input))
            llm_response = await ctx.llm.generate(messages=messages)
            direct_answer = llm_response.content or ""
            logger.info("[Chat] 直接回答: %s 字符", len(direct_answer))

        # ── 6. 设置 trace 上下文 ──
        trace = state.get("_trace")
        if trace and entity_code:
            trace.set_stock(code=entity_code, name=entity_name)

        return {
            "entity_code": entity_code,
            "entity_name": entity_name,
            "entity_type": entity_type,
            "context": context,
            "sources": sources,
            "effective_input": effective_input,
            "needs_task": needs_task,
            "task_type": task_type,
            "direct_answer": direct_answer,
        }

    return chat_node


def make_plan_node(ctx: NodeContext):
    """创建 plan_node（闭包捕获 ctx）。"""

    async def plan_node(state: dict) -> dict:
        """规划：初始化 MCP + 生成任务描述。复盘时带前轮结果。"""
        trace = state.get("_trace")
        effective_input = state.get("effective_input", state["user_input"])
        original_input = state.get("user_input", "")
        context = state.get("context", "")
        entity_code = state.get("entity_code", "")
        entity_name = state.get("entity_name", "")
        entity_type = state.get("entity_type", "")
        task_type = state.get("task_type", "")

        # ToolProvider 延迟初始化（首次进入任务流程时才扫描，直接回答路径跳过）
        if not ctx.tool_provider:
            ctx.init_tools()
            # 同步到 TaskAgent，让 _plan() 能看到工具列表和域列表
            if ctx.tool_provider and ctx.agent:
                ctx.agent._tool_provider = ctx.tool_provider
        if not ctx.tool_provider:
            logger.error("[Plan] ToolProvider 不可用，退回直接回答")
            direct = await ctx.llm.generate(messages=[
                ChatMessage(role="system", content=ctx.system_prompt),
                ChatMessage(role="user", content=effective_input),
            ])
            return {"task": "", "step_budget": 0, "planning_interval": 6, "direct_answer": direct.content or ""}

        # 复盘时注入前轮结果
        prev_result = state.get("result_raw", "")
        prev_hit = state.get("hit_max_steps", False)
        replan_count = state.get("replan_count", 0)

        replan_context = ""
        if prev_hit and prev_result:
            replan_context = f"\n\n【前轮执行结果（步数耗尽）】\n{prev_result[:2000]}\n请基于上述进度继续完成任务。"
            logger.info("[Plan] 复盘第 %d 轮，前轮结果 %d 字符", replan_count, len(prev_result))

        # 加载历史对话
        history_text = ""
        if ctx.memory:
            try:
                history = await ctx.memory.get_history(state.get("session_id", "default"), limit=ctx.memory_window_size)
                if history:
                    history_lines = []
                    for msg in history[-10:]:  # 最近 10 条
                        role = "用户" if msg.role == "user" else "助手"
                        history_lines.append(f"{role}: {msg.content[:300]}")
                    history_text = "\n".join(history_lines)
            except Exception as e:
                logger.debug("[Plan] 加载历史对话失败: %s", e)

        # 意图类型映射
        _TASK_TYPE_DESC = {
            "analysis": "分析/评估/诊断",
            "screen": "筛选/选股/推荐",
            "compare": "对比/比较",
            "query": "查询/获取数据",
            "code": "写代码/开发/编程",
            "explain": "解释/说明/教学",
            "general": "通用任务",
        }

        # 设置分离的上下文变量，供 _plan() 模板使用
        entity_info_str = ""
        if entity_code:
            entity_desc = f"{entity_name}({entity_code})" if entity_name else entity_code
            entity_info_str = f"【实体】{entity_desc} [{entity_type}]"

        task_type_str = ""
        if task_type:
            task_type_str = f"【意图】{_TASK_TYPE_DESC.get(task_type, task_type)}"

        rag_context_str = ""
        if context:
            rag_context_str = f"【参考上下文】\n{context[:1500]}"

        history_context_str = ""
        if history_text:
            history_context_str = f"【历史对话】\n{history_text}"

        # 传递给 _plan() 的 agent 实例
        ctx.agent._plan_entity_info = entity_info_str
        ctx.agent._plan_task_type_info = task_type_str
        ctx.agent._plan_rag_context = rag_context_str
        ctx.agent._plan_history_context = history_context_str

        # 组装 plan 输入（保留拼接版本作为 task 的基础）
        plan_parts = [effective_input]
        if entity_info_str:
            plan_parts.append(entity_info_str)
        if task_type_str:
            plan_parts.append(task_type_str)
        if original_input != effective_input:
            plan_parts.append(f"【原始消息】{original_input}")
        if context:
            plan_parts.append(f"【参考上下文】\n{context[:1500]}")
        if history_text:
            plan_parts.append(f"【历史对话】\n{history_text}")
        if replan_context:
            plan_parts.append(replan_context)
        plan_input = "\n\n".join(plan_parts)

        # _plan() 内部已将所有技能名+描述注入到 plan prompt，由 LLM 选择
        plan = await ctx.agent._plan(plan_input, ctx.llm, trace)

        # ── 渐进式加载：plan 选中技能后，加载 SKILL.md body + 工具 ──
        selected_skill = plan.get("selected_skill")
        skill_body = ""
        skill_tools = []

        if selected_skill and ctx.skill_adapter:
            # 加载 SKILL.md 正文
            body = ctx.skill_adapter.load_body(selected_skill)
            if body:
                skill_body = body
                logger.info("[Plan] 渐进加载技能 '%s': SKILL.md %d 字符", selected_skill, len(body))

            # 加载技能工具（SkillResourceTool + SkillFuncTool）
            from agents.task_agent import _SkillResourceTool, _load_skill_functions
            skill_tools.append(_SkillResourceTool(ctx.skill_adapter, selected_skill))
            skill_tools.extend(_load_skill_functions(selected_skill))
            logger.info("[Plan] 渐进加载技能 '%s': %d 个工具", selected_skill, len(skill_tools))

            trace.record("skill_loaded", {
                "skill": selected_skill,
                "body_chars": len(skill_body),
                "tool_count": len(skill_tools),
            })

        return {
            "task": plan["task"],
            "selected_skill": selected_skill or "",
            "selected_domain": plan.get("selected_domain", ""),
            "skill_body": skill_body,
            "skill_tools": skill_tools,
            "step_budget": plan["step_budget"],
            "planning_interval": plan.get("planning_interval", 6),
            "replan_count": replan_count + (1 if prev_hit else 0),
        }

    return plan_node


def make_execute_node(ctx: NodeContext):
    """创建 execute_node（闭包捕获 ctx）。"""

    async def execute_node(state: dict) -> dict:
        """执行：单次 CodeAgent 跑完任务，planning_interval 内部进度检查。"""
        agent_instance = ctx.agent
        if not agent_instance:
            logger.error("[Execute] ctx.agent 未设置")
            return {"result_raw": "[错误] agent 未初始化", "hit_max_steps": True}

        task = state.get("task", "")
        if not task:
            return {"result_raw": "[错误] 无任务描述", "hit_max_steps": False}

        step_budget = state.get("step_budget", 10)
        planning_interval = state.get("planning_interval", 6)
        trace = state.get("_trace")

        # 构建上下文
        task_parts = []
        if state.get("entity_code"):
            entity_label = state.get("entity_type", "实体")
            entity_info = f"【{entity_label}】{state.get('entity_name', '')}({state['entity_code']})" if state.get('entity_name') else f"【{entity_label}】{state['entity_code']}"
            task_parts.append(entity_info)
        if state.get("context"):
            task_parts.append(f"【参考资料】\n{state['context']}")

        # 渐进式加载：从 state 读取 plan 阶段已选中的技能/域信息
        selected_skill = state.get("selected_skill", "")
        selected_domain = state.get("selected_domain", "")
        skill_body = state.get("skill_body", "")
        skill_tools = list(state.get("skill_tools", []))

        if selected_skill and skill_body:
            task_parts.append(f"【技能指令: {selected_skill}】\n{skill_body}")
            logger.info("[Execute] 注入技能 '%s': SKILL.md %d 字符, %d 个工具",
                        selected_skill, len(skill_body), len(skill_tools))

        # 注入原始用户输入（Planner 可能重新描述任务，丢失关键词如“深度”“明天”）
        original_input = state.get("user_input", "")
        if original_input and original_input != task:
            task_parts.append(f"【用户原始输入】{original_input}")

        task_parts.append(f"【任务】\n{task}")
        full_task = "\n\n".join(task_parts)

        # 复用已有 CodeAgent 实例（跨轮 memory 自然衔接）
        agent = state.get("_code_agent")
        if agent is None:

            # 技能模式下关闭 smolagents 内部 planning（外部 plan 已规划）
            # 非技能模式保持原样
            effective_interval = None if selected_skill else planning_interval

            agent = agent_instance._build_code_agent(
                model=ctx.model,
                provider=ctx.tool_provider,
                skill_tools=skill_tools,
                planning_interval=effective_interval,
                phase_id=0,
                domain=selected_domain,
            )
            logger.info("[Execute] 新建 CodeAgent 实例")
        else:
            logger.info("[Execute] 复用 CodeAgent 实例，memory 自然衔接")

        # 用 plan 的 step_budget 覆盖默认 max_steps
        agent.max_steps = step_budget

        # LLM 超时设 180s（上限，不是等待时间）
        _set_llm_timeout(agent, 180)

        # ── trace: 记录技能 ──
        trace = state.get("_trace")
        if trace and selected_skill:
            trace.set_skill(selected_skill)

        # 执行
        logger.info("[Execute] 开始执行，step_budget=%d, timeout=180s", step_budget)
        react_start = time.time()

        import signal as _signal
        import threading as _threading
        hit_max_steps = False
        _interrupted = False
        _is_main_thread = _threading.current_thread() is _threading.main_thread()

        def _sigint_handler(signum, frame):
            nonlocal _interrupted
            _interrupted = True
            logger.warning("[Execute] 收到 SIGINT，正在停止...")

        if _is_main_thread:
            old_handler = _signal.signal(_signal.SIGINT, _sigint_handler)
        try:
            result = agent.run(full_task)
            if _interrupted:
                result = "[中断] 用户中断"
                logger.warning("[Execute] 被用户中断")
            result = str(result) if result else ""
        except KeyboardInterrupt:
            logger.warning("[Execute] 被用户中断")
            result = "[中断] 用户中断"
        except Exception as e:
            error_str = str(e).lower()
            if "max_steps" in error_str or "maximum" in error_str:
                logger.info("[Execute] max_steps 耗尽，需复盘")
                hit_max_steps = True
                result = f"[max_steps 耗尽] {e}"
            else:
                logger.error("[Execute] 执行异常: %s", e)
                result = f"[错误] {e}"
        finally:
            if _is_main_thread:
                _signal.signal(_signal.SIGINT, old_handler)

        react_elapsed = round(time.time() - react_start, 2)
        logger.info("[Execute] 完成，耗时 %.1fs，hit_max_steps=%s", react_elapsed, hit_max_steps)

        # ── trace: 从 agent memory 提取工具调用 ──
        if trace:
            _record_tool_calls_to_trace(trace, agent)

        # 从 agent memory 提取失败的工具调用（不追加到 result，由 finalize_node 处理）
        failed_tools = _extract_failed_tools(agent, ctx.tool_provider)
        if failed_tools:
            logger.info("[Execute] 失败工具: %s", [n for n, _ in failed_tools])

        # 提取 smolagents 的最终规划
        agent_plan = ""
        try:
            from smolagents.memory import PlanningStep
            for step in reversed(getattr(agent.memory, 'steps', []) or []):
                if isinstance(step, PlanningStep):
                    agent_plan = step.plan or ""
                    break
        except Exception:
            pass

        if trace:
            trace.record("execute_done", {
                "elapsed_seconds": react_elapsed,
                "hit_max_steps": hit_max_steps,
                "result_preview": result[:200],
                "agent_plan": agent_plan[:500] if agent_plan else None,
            })

        return {
            "result_raw": result,
            "hit_max_steps": hit_max_steps,
            "replan_count": state.get("replan_count", 0),
            "_code_agent": agent,  # 保留实例，下轮复用
            "_failed_tools": failed_tools,  # 失败工具列表，由 finalize_node 追加到输出
            "_agent_plan": agent_plan,  # smolagents 最终规划
        }

    return execute_node


def make_finalize_node(ctx: NodeContext):
    """创建 finalize_node（闭包捕获 ctx）。"""

    async def finalize_node(state: dict) -> dict:
        """最终阶段：保存原始结果 → 追加错误信息 → 格式化输出 → trace.finish()。

        保存顺序设计：
          1. memory 存原始 result_raw（复盘时 plan_node 拿到真实进度）
          2. 追加失败工具信息
          3. 格式化（仅 task 模式 + 有工具产出时，只影响最终输出给用户）
          4. trace.finish() 写 JSONL + qd_traces（Evaluator/Feedback 用）
        """
        session_id = state.get("session_id", "default")
        direct_answer = state.get("direct_answer", "")
        result_raw = state.get("result_raw", "") or direct_answer or "[错误] 无执行结果"
        failed_tools = state.get("_failed_tools", [])
        agent_plan = state.get("_agent_plan", "")
        selected_skill = state.get("selected_skill", "")

        # 记录 smolagents 最终规划
        if agent_plan:
            logger.info("[Finalize] Agent 规划:\n%s", agent_plan[:500])

        # ── 1. 保存 memory（原始 result_raw，给复盘用）──
        if ctx.memory:
            try:
                await ctx.memory.add(session_id, "user", state["user_input"])
                await ctx.memory.add(session_id, "assistant", result_raw)
            except Exception as e:
                logger.warning("[Finalize] memory 保存失败: %s", e)

        # ── 4. 追加失败工具信息 ──
        if failed_tools:
            lines = []
            for name, desc in failed_tools:
                lines.append(f"{name} -- {desc[:60]}" if desc else name)
            missing = "\n".join(lines)
            result_raw = f"{result_raw}\n\n【数据完整性】以下工具未获取到数据:\n{missing}"

        # ── 5. 结果格式化（仅 task 模式 + 有工具产出时）──
        needs_task = state.get("needs_task", True)
        has_tool_output = bool(state.get("result_raw"))  # execute_node 产出过结果
        if needs_task and has_tool_output and not selected_skill:
            try:
                from formatters.base import get_formatter
                entity_type = state.get("entity_type", "")
                formatter = get_formatter(entity_type)
                fmt_context = {
                    "entity_type": entity_type,
                    "entity_code": state.get("entity_code", ""),
                    "entity_name": state.get("entity_name", ""),
                    "task": state.get("task", ""),
                    "user_input": state.get("user_input", ""),
                    "_llm": ctx.llm,
                }
                result_raw = await formatter.format(result_raw, fmt_context)
            except Exception as e:
                logger.warning("[Finalize] 格式化失败，使用原始数据: %s", e)

        # ── 6. trace.finish() 写 JSONL + qd_traces ──
        trace = state.get("_trace")
        if trace:
            try:
                root_id = trace.finish(
                    final_answer=state.get("result_raw", ""),  # 用原始结果提取结构化字段
                    status="success",
                    response={"content": result_raw},
                )
                if root_id:
                    from feedback import record_session_root
                    record_session_root(session_id, root_id)
            except Exception as e:
                logger.warning("[Finalize] trace.finish 失败: %s", e)

        # 计算耗时
        start_time = state.get("_start_time", 0)
        elapsed = round(time.time() - start_time, 2) if start_time else 0

        return {
            "result_raw": result_raw,
            "elapsed": elapsed,
            "final_output": {},
        }

    return finalize_node


# ═══════════════════════════════════════════════════════════════
#  路由函数
# ═══════════════════════════════════════════════════════════════

def route_after_chat(state: dict) -> str:
    """chat_node 之后的路由。"""
    if state.get("needs_task", True):
        return "plan"
    return "finalize"


def route_after_plan(state: dict) -> str:
    """plan_node 之后的路由。"""
    if state.get("task") and state.get("step_budget", 0) > 0:
        return "execute"
    return "finalize"


def route_after_execute(state: dict) -> str:
    """execute_node 之后的路由。"""
    MAX_REPLAN = 2

    if not state.get("hit_max_steps", False):
        return "finalize"        # CodeAgent 完成或正常结束
    if state.get("replan_count", 0) >= MAX_REPLAN:
        return "finalize"        # 复盘次数用完
    return "plan"                 # max_steps 耗尽，回 plan 复盘
