# -*- coding: utf-8 -*-
"""
nodes.py — Graph 节点定义

4 个节点 + plan 复盘循环：
  - chat_node：RAG + 实体解析 + 意图分类 + 简单问题直接回答
  - plan_node：生成任务描述 + step_budget（复盘时带前轮结果）
  - execute_node：单 CodeAgent 执行，跨轮复用实例
  - finalize_node：保存 memory + TraceCollector 存库

每个节点签名为 async def node(state: dict) -> dict | None：
  - 输入：完整状态
  - 输出：partial state（只返回需要更新的字段）
"""
from __future__ import annotations

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

def format_stock_report(data: dict) -> str:
    """将结构化股票分析数据格式化为人类可读文本。"""
    if not data:
        return ""
    parts = []
    name = data.get("stock_name", "")
    code = data.get("stock_code", "")
    if name or code:
        parts.append(f"**股票名称**: {name} ({code})" if name and code else f"**股票名称**: {name or code}")
    field_map = [
        ("action", "操作建议"),
        ("score", "评    分"),
        ("direction", "方    向"),
        ("confidence", "置 信 度"),
        ("time_window", "时间窗口"),
        ("signal", "信    号"),
        ("analysis", "分    析"),
    ]
    for key, label in field_map:
        val = data.get(key)
        if val is not None and val != "":
            parts.append(f"**{label}**: {val}")
    return "\n".join(parts)


# markdown 字段标签 → 结构化 key
_FIELD_MAP = {
    "股票名称": "stock_name", "股票代码": "stock_code",
    "操作建议": "action",
    "评分": "score", "评    分": "score",
    "方向": "direction", "方    向": "direction",
    "置信度": "confidence", "置 信 度": "confidence",
    "时间窗口": "time_window",
    "信号": "signal", "信    号": "signal",
    "分析": "analysis", "分    析": "analysis",
}


def extract_stock_data(content: str) -> dict | None:
    """从 LLM 输出中提取结构化股票分析数据。支持 markdown 和 JSON。"""
    if not content:
        return None

    # 方式1: 尝试 JSON 解析
    from json_extractor import extract_json
    parsed = extract_json(content)
    if parsed and isinstance(parsed, dict):
        # 壳格式：data 字段里有结构化数据
        inner = parsed.get("data")
        if isinstance(inner, dict) and any(inner.get(k) for k in ("action", "score", "direction", "signal")):
            return inner
        # 直接格式：顶层有结构化数据
        if any(parsed.get(k) for k in ("action", "score", "direction", "signal")):
            return parsed

    # 方式2: 从 markdown 格式提取
    data = {}
    for m in re.finditer(r"\*\*(.+?)\*\*\s*[:：]\s*(.+)", content):
        label = m.group(1).strip()
        value = m.group(2).strip()
        key = _FIELD_MAP.get(label)
        if key:
            data[key] = value

    if not any(data.get(k) for k in ("action", "score", "direction", "signal")):
        return None

    # 从 stock_name 中拆出 code
    if "stock_name" in data and not data.get("stock_code"):
        code_match = re.search(r"(\d{6})", data["stock_name"])
        if code_match:
            data["stock_code"] = code_match.group(1)
            data["stock_name"] = data["stock_name"].replace(code_match.group(0), "").strip(" ()")

    # score 转 int
    if "score" in data:
        try:
            data["score"] = int(re.search(r"\d+", str(data["score"])).group())
        except (ValueError, AttributeError):
            pass

    return data


# ═══════════════════════════════════════════════════════════════
#  AgentState — 状态类型定义
# ═══════════════════════════════════════════════════════════════

class EntityResolver:
    """实体解析器接口（通用）。

    不同领域实现不同的解析逻辑：
      - 股票：解析股票代码/名称
      - 商品：解析商品代码
      - 加密货币：解析币种
    """

    def resolve(self, user_input: str) -> dict | None:
        """从用户输入中解析实体。

        Returns:
            {"code": str, "name": str, "type": str} 或 None
        """
        raise NotImplementedError


class StockResolver(EntityResolver):
    """股票实体解析器（金融领域）。"""

    def resolve(self, user_input: str) -> dict | None:
        try:
            from tools.data_tools import resolve_stock
            code_match = re.search(r'\b(\d{6})\b', user_input)
            if code_match:
                code = code_match.group(1)
                return {"code": code, "name": "", "type": "stock"}

            clean_input = re.sub(r'分析|看看|查一下|怎么样|什么股|股票|推荐|选|买|卖', '', user_input).strip()
            if clean_input and len(clean_input) >= 2:
                result = resolve_stock(clean_input, limit=1)
                if isinstance(result, dict) and not result.get('error'):
                    if result.get('code'):
                        return {"code": result['code'], "name": result.get('name', ''), "type": "stock"}
                    elif result.get('data'):
                        first = result['data'][0]
                        return {"code": first.get('code', ''), "name": first.get('name', ''), "type": "stock"}
        except Exception as e:
            logger.debug("[StockResolver] 解析跳过: %s", e)
        return None


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
    direct_answer: str    # 直接回答内容（needs_task=False 时有值）

    # ── plan_node 输出 ──
    task: str             # 完整任务描述
    step_budget: int      # CodeAgent 本轮步数预算
    planning_interval: int

    # ── execute_node 输出 ──
    result_raw: str       # CodeAgent 执行结果
    hit_max_steps: bool   # True=max_steps 耗尽，需复盘
    replan_count: int     # 已复盘次数
    _code_agent: Any      # CodeAgent 实例（跨轮复用，不序列化）

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
        self.entity_resolver = entity_resolver or StockResolver()  # 默认股票解析器

        # MCP 运行时（惰性初始化）
        self.mcp_tool_list = []
        self.model = None

        # TaskAgent 实例（用于调用 _build_code_agent 等方法）
        self.agent = None

        # TraceCollector（session 级）
        self.collectors: Dict[str, Any] = {}

    def init_mcp(self):
        """初始化 MCP 连接。"""
        from agents.task_agent import _mcp, _LLMAdapter

        if _mcp.available:
            self.mcp_tool_list = _mcp.tools
            self.model = _LLMAdapter(self.llm)
            logger.info("[Context] MCP 初始化完成: %d 个工具", len(self.mcp_tool_list))
        else:
            logger.error("[Context] MCP 不可用")


# ═══════════════════════════════════════════════════════════════
#  节点函数
# ═══════════════════════════════════════════════════════════════

def _expand_entity_query(user_input: str, entity_name: str, entity_code: str, entity_type: str) -> str:
    """将简短的实体分析指令扩写为完整的分析指令。"""
    return f"分析{entity_name}({entity_code}): {user_input}"


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


def _extract_failed_tools(agent, mcp_tool_list: list = None) -> list:
    """从 CodeAgent memory 提取失败的工具调用，返回 [(name, description), ...]。

    smolagents CodeAgent 用 python_interpreter 作为外层 executor，
    内部的 mcp(action='call', tool_name='xxx') 调用失败时，
    tool_calls 只记录 python_interpreter，实际失败工具名在 observations 里。

    两种提取方式：
    1. observations 中出现 tool_name='xxx' ... error 格式
    2. observations 中打印的字典包含 'error' 字段，从代码中按顺序匹配工具名
    """
    tool_desc = {}
    tool_names = set()
    if mcp_tool_list:
        for t in mcp_tool_list:
            tool_desc[t.name] = getattr(t, 'description', '') or ''
            tool_names.add(t.name)

    # 从代码块中提取 mcp 调用的工具名（按顺序）
    _MCP_CALL_RE = re.compile(r"mcp\s*\(.*?tool_name\s*=\s*['\"]?(\w+)['\"]?", re.IGNORECASE)

    failed = []  # [(name, desc)]
    seen = set()
    try:
        from smolagents.memory import ActionStep
        for step in getattr(agent.memory, 'steps', []):
            if not isinstance(step, ActionStep):
                continue
            obs = str(getattr(step, 'observations', '') or '')
            if not obs.strip():
                continue

            # ── 方式 1: 直接匹配 tool_name='xxx' ... error ──
            if 'error' in obs.lower() or '失败' in obs or '超时' in obs:
                for m in re.finditer(
                    r"tool_name\s*[=:]\s*['\"]?(\w+)['\"]?\b.*?(?:error|失败)",
                    obs, re.IGNORECASE | re.DOTALL
                ):
                    name = m.group(1)
                    if name and name in tool_names and name not in seen:
                        seen.add(name)
                        failed.append((name, tool_desc.get(name, '')))

            # ── 方式 2: 从代码中提取工具调用顺序，匹配含 error 的返回值 ──
            # 代码中 mcp 调用的顺序 = print 输出的顺序
            code = str(getattr(step, 'code', '') or '')
            tool_calls_in_code = _MCP_CALL_RE.findall(code)

            # 将 observation 按 Python dict 边界拆分
            # 每个 dict 以 '{' 开头，对应一个 print() 输出
            obs_dicts = []
            depth = 0
            start = -1
            for i, c in enumerate(obs):
                if c == '{':
                    if depth == 0:
                        start = i
                    depth += 1
                elif c == '}':
                    depth -= 1
                    if depth == 0 and start >= 0:
                        obs_dicts.append(obs[start:i+1])
                        start = -1

            # 按顺序匹配：第 i 个 dict 对应第 i 个 mcp 调用
            for i, dict_str in enumerate(obs_dicts):
                if i >= len(tool_calls_in_code):
                    break
                tname = tool_calls_in_code[i]
                if tname in seen or tname not in tool_names:
                    continue
                # 检查这个 dict 是否包含 error
                if "'error'" in dict_str or '"error"' in dict_str:
                    seen.add(tname)
                    failed.append((tname, tool_desc.get(tname, '')))

            # ── 方式 3: 兜底，已知工具名直接出现在 observation 附近有 error ──
            if not seen:
                for tname in tool_names:
                    if tname in obs and tname not in seen and tname not in {'python_interpreter', 'final_answer', 'mcp'}:
                        idx = obs.find(tname)
                        nearby = obs[max(0, idx-20):idx+len(tname)+200].lower()
                        if 'error' in nearby or '失败' in nearby or '超时' in nearby:
                            seen.add(tname)
                            failed.append((tname, tool_desc.get(tname, '')))
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
        if use_rag and ctx.retriever:
            try:
                docs = await ctx.retriever.retrieve(user_input)
                if docs:
                    from rag.retriever import Retriever
                    context = Retriever.format_context(docs)
                    sources = [{"content": d["content"][:200], "score": d.get("score", 0)} for d in docs]
                    logger.info("[Chat] RAG 检索到 %d 条文档, %d 字符", len(docs), len(context))
            except Exception as e:
                logger.warning("[Chat] RAG 检索失败: %s", e)

        # ── 2. 实体解析 ──
        entity_code = ""
        entity_name = ""
        entity_type = ""
        if ctx.entity_resolver:
            try:
                entity = ctx.entity_resolver.resolve(user_input)
                if entity:
                    entity_code = entity.get("code", "")
                    entity_name = entity.get("name", "")
                    entity_type = entity.get("type", "")
                    logger.info("[Chat] 实体解析: %s → %s %s (%s)", user_input, entity_code, entity_name, entity_type)
            except Exception as e:
                logger.debug("[Chat] 实体解析跳过: %s", e)

        # ── 3. 消息标准化 ──
        effective_input = user_input
        if entity_code and entity_name:
            effective_input = _expand_entity_query(user_input, entity_name, entity_code, entity_type)

        # ── 4. 意图分类：是否需要任务流程 ──
        needs_task = True
        direct_answer = ""

        # 有实体 → 必须走任务流程
        if entity_code:
            needs_task = True
        else:
            # 无实体，用 LLM 快速判断意图
            try:
                intent_messages = [
                    ChatMessage(role="system", content=(
                        "你是意图分类器。判断用户消息是否需要调用工具完成任务。\n"
                        "需要工具（分析、查询、搜索、计算、对比等）回复: task\n"
                        "不需要工具（闲聊、问候、简单知识问答、感谢等）回复: chat\n"
                        "只回复一个词: task 或 chat"
                    )),
                    ChatMessage(role="user", content=user_input),
                ]
                intent_resp = await ctx.llm.generate(messages=intent_messages)
                intent = (intent_resp.content or "").strip().lower()
                if "chat" in intent and "task" not in intent:
                    needs_task = False
                    logger.info("[Chat] 意图分类: chat（直接回答）")
                else:
                    logger.info("[Chat] 意图分类: task（进入任务流程）")
            except Exception as e:
                logger.warning("[Chat] 意图分类失败，默认走任务流程: %s", e)
                needs_task = True

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

        # ── 6. TraceCollector ──
        try:
            from trace_collector import TraceCollector
            from agents.task_agent import _collectors
            collector = TraceCollector(session_id=session_id, user_query=user_input)
            _collectors[session_id] = collector
        except Exception as e:
            logger.debug("[Chat] TraceCollector 创建失败: %s", e)

        return {
            "entity_code": entity_code,
            "entity_name": entity_name,
            "entity_type": entity_type,
            "context": context,
            "sources": sources,
            "effective_input": effective_input,
            "needs_task": needs_task,
            "direct_answer": direct_answer,
        }

    return chat_node


def make_plan_node(ctx: NodeContext):
    """创建 plan_node（闭包捕获 ctx）。"""

    async def plan_node(state: dict) -> dict:
        """规划：初始化 MCP + 生成任务描述。复盘时带前轮结果。"""
        trace = state.get("_trace")
        effective_input = state.get("effective_input", state["user_input"])

        # MCP 延迟初始化（首次进入任务流程时才启动，直接回答路径跳过）
        if not ctx.mcp_tool_list:
            ctx.init_mcp()
        if not ctx.mcp_tool_list:
            logger.error("[Plan] MCP 不可用，无法执行任务流程")
            return {"task": "", "step_budget": 0, "planning_interval": 6}

        # 复盘时注入前轮结果
        prev_result = state.get("result_raw", "")
        prev_hit = state.get("hit_max_steps", False)
        replan_count = state.get("replan_count", 0)

        replan_context = ""
        if prev_hit and prev_result:
            replan_context = f"\n\n【前轮执行结果（步数耗尽）】\n{prev_result[:2000]}\n请基于上述进度继续完成任务。"
            logger.info("[Plan] 复盘第 %d 轮，前轮结果 %d 字符", replan_count, len(prev_result))

        plan_input = effective_input + replan_context

        # 第一层：只注入 name + description（简历），不注入 SKILL.md body
        if ctx.skill_adapter:
            for s in ctx.skill_adapter.list_skills():
                name = s['name']
                if name in plan_input or name.replace('-', ' ') in plan_input:
                    desc = s.get('description', '')[:200]
                    plan_input += f"\n\n【可用技能】{name}: {desc}"
                    logger.info("[Plan] 第一层：技能 '%s' 简历已注入", name)
                    break

        plan = await ctx.agent._plan(plan_input, ctx.llm, trace)

        return {
            "task": plan["task"],
            "step_budget": plan["step_budget"],
            "planning_interval": plan["planning_interval"],
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

        # 第二层：SKILL.md body → 注入 task context
        # 第三层：_SkillResourceTool → CodeAgent 按需读取资源
        skill_tools = []
        if ctx.skill_adapter:
            from agents.task_agent import _load_skill_functions, _SkillResourceTool
            for s in ctx.skill_adapter.list_skills():
                name = s['name']
                if name in task or name.replace('-', ' ') in task:
                    body = ctx.skill_adapter.load_body(name)
                    if body:
                        task_parts.append(f"【技能指令: {name}】\n{body}")
                        logger.info("[Execute] 第二层：SKILL.md '%s'，%d 字符", name, len(body))
                    skill_tools.append(_SkillResourceTool(ctx.skill_adapter, name))
                    skill_tools.extend(_load_skill_functions(name))
                    break

        task_parts.append(f"【任务】\n{task}")
        full_task = "\n\n".join(task_parts)

        # 复用已有 CodeAgent 实例（跨轮 memory 自然衔接）
        agent = state.get("_code_agent")
        if agent is None:

            agent = agent_instance._build_code_agent(
                model=ctx.model,
                mcp_tool_list=ctx.mcp_tool_list,
                skill_tools=skill_tools,
                planning_interval=planning_interval,
                phase_id=0,
            )
            # 注入工具列表到 planning prompt
            all_tools = list(ctx.mcp_tool_list) + skill_tools
            if all_tools:
                agent_instance._inject_tools_to_planning(agent, all_tools)
            logger.info("[Execute] 新建 CodeAgent 实例")
        else:
            logger.info("[Execute] 复用 CodeAgent 实例，memory 自然衔接")

        # 用 plan 的 step_budget 覆盖默认 max_steps
        agent.max_steps = step_budget

        # LLM 超时设 180s（上限，不是等待时间）
        _set_llm_timeout(agent, 180)

        # 执行
        logger.info("[Execute] 开始执行，step_budget=%d, planning_interval=%d, timeout=180s", step_budget, planning_interval)
        react_start = time.time()

        hit_max_steps = False
        try:
            result = agent.run(full_task)
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

        react_elapsed = round(time.time() - react_start, 2)
        logger.info("[Execute] 完成，耗时 %.1fs，hit_max_steps=%s", react_elapsed, hit_max_steps)

        # 从 agent memory 提取失败的工具调用（不追加到 result，由 finalize_node 处理）
        failed_tools = _extract_failed_tools(agent, ctx.mcp_tool_list)
        if failed_tools:
            logger.info("[Execute] 失败工具: %s", [n for n, _ in failed_tools])

        if trace:
            trace.record("execute_done", {
                "elapsed_seconds": react_elapsed,
                "hit_max_steps": hit_max_steps,
                "result_preview": result[:200],
            })

        return {
            "result_raw": result,
            "hit_max_steps": hit_max_steps,
            "replan_count": state.get("replan_count", 0),
            "_code_agent": agent,  # 保留实例，下轮复用
            "_failed_tools": failed_tools,  # 失败工具列表，由 finalize_node 追加到输出
        }

    return execute_node


def _finalize_domain(ctx: NodeContext, state: dict, result_raw: str) -> int | None:
    """领域特化后处理（可扩展）。

    Returns:
        root_id 如果存库成功，否则 None
    """
    entity_type = state.get("entity_type", "")

    if entity_type == "stock":
        stock_data = extract_stock_data(result_raw)
        if stock_data:
            from chain.schema import EvalNode, Layer, Status
            from chain.store import save_tree
            from datetime import date

            root = EvalNode(
                layer=Layer.CHAIN.value,
                name=f"stock+analyze+{state.get('entity_code', '')}",
                exec_date=date.today(),
                stock_code=state.get("entity_code", ""),
                stock_name=state.get("entity_name", ""),
                score=stock_data.get("score"),
                direction=stock_data.get("direction", ""),
                action=stock_data.get("action", ""),
                signal=stock_data.get("signal", ""),
                analysis=result_raw[:2000],
                input_params={"user_query": state.get("user_input", "")},
                status=Status.OK.value,
            )
            root_id = save_tree(root)
            logger.info("[Finalize:stock] EvalNode 已存储: root_id=%s score=%s action=%s",
                        root_id, stock_data.get("score"), stock_data.get("action"))
            return root_id

    # 扩展点：elif entity_type == "crypto": ...
    return None


def make_finalize_node(ctx: NodeContext):
    """创建 finalize_node（闭包捕获 ctx）。"""

    async def finalize_node(state: dict) -> dict:
        """最终阶段：领域提取 → 存库 → 追加错误信息 → 保存 memory。"""
        from agents.task_agent import _collectors

        session_id = state.get("session_id", "default")
        direct_answer = state.get("direct_answer", "")
        result_raw = state.get("result_raw", "") or direct_answer or "[错误] 无执行结果"
        failed_tools = state.get("_failed_tools", [])

        # ── 1. 领域特化：从干净的 result_raw 提取数据 + 存库 ──
        domain_root_id = None
        try:
            domain_root_id = _finalize_domain(ctx, state, result_raw)
        except Exception as e:
            logger.debug("[Finalize] 领域后处理跳过: %s", e)

        # ── 2. TraceCollector 存库（仅当领域后处理未存时） ──
        collector_root_id = None
        collector = _collectors.get(session_id)
        if collector:
            try:
                collector.on_agent_finish(
                    final_answer=result_raw,
                    total_steps=1,
                    total_tokens=0,
                    model=getattr(ctx.llm, "model", "unknown"),
                )
                if not domain_root_id:
                    # 领域后处理未存，由 collector 存
                    collector_root_id = collector.flush()
            except Exception as e:
                logger.warning("[Finalize] TraceCollector 存库失败: %s", e)
            finally:
                _collectors.pop(session_id, None)

        # ── 3. 记录 root_id 供反馈闭环使用 ──
        root_id = domain_root_id or collector_root_id
        if root_id:
            from feedback import record_session_root
            record_session_root(session_id, root_id)

        # ── 4. 追加失败工具信息（在领域提取之后，不污染提取） ──
        if failed_tools:
            tool_desc_map = {}
            if ctx.mcp_tool_list:
                for t in ctx.mcp_tool_list:
                    tool_desc_map[t.name] = getattr(t, 'description', '') or ''

            lines = []
            for name, desc in failed_tools:
                desc = desc or tool_desc_map.get(name, '')
                lines.append(f"{name} -- {desc[:60]}" if desc else name)
            missing = "\n".join(lines)
            result_raw = f"{result_raw}\n\n【数据完整性】以下工具未获取到数据:\n{missing}"

        # ── 5. 保存 memory ──
        if ctx.memory:
            try:
                await ctx.memory.add(session_id, "user", state["user_input"])
                await ctx.memory.add(session_id, "assistant", result_raw)
            except Exception as e:
                logger.warning("[Finalize] memory 保存失败: %s", e)

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
