# -*- coding: utf-8 -*-
"""
nodes.py — Graph 节点定义

将 _chat_plan() 拆分为 5 个独立节点：
  - prepare_node：股票解析 + RAG + TraceCollector
  - plan_node：_plan() 调用
  - execute_node：执行单个阶段（direct/skill/execute）
  - eval_node：评估阶段结果 + 重试
  - finalize_node：汇总 + memory + TraceCollector 存库

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

    # ── prepare_node 输出（通用实体字段）──
    entity_code: str      # 实体代码（股票代码/商品代码/...）
    entity_name: str      # 实体名称
    entity_type: str      # 实体类型（stock/commodity/crypto/...）
    context: str          # RAG 上下文
    sources: list         # RAG 来源
    effective_input: str  # 扩写后的完整指令

    # ── plan_node 输出 ──
    phases: list          # [{id, name, type, skill?, goal}]
    planning_interval: int
    expanded_query: str

    # ── execute_node 状态 ──
    current_phase_index: int
    phase_results: list   # [{phase, result, passed}]
    shared_state: dict    # 跨阶段变量（executor state 注入）

    # ── finalize_node 输出 ──
    result_raw: str
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

def _expand_stock_query(user_input: str, stock_name: str, stock_code: str) -> str:
    """将简短的股票分析指令扩写为完整的分析指令。"""
    import re as _re

    msg = user_input.strip()

    # ── 检测分析深度 ──
    is_deep = any(kw in msg for kw in ("深度", "详细", "全面", "中线", "中长线", "综合"))

    # ── 检测时间周期 ──
    if any(kw in msg for kw in ("短线", "日内", "超短")):
        period = "T+1"
    elif any(kw in msg for kw in ("中线", "中长线", "月线", "深度")):
        period = "1M"
    elif any(kw in msg for kw in ("长线", "半年", "年线")):
        period = "1M"
    else:
        period = "T+3"

    # ── 检测用户指定的周期覆盖 ──
    period_match = _re.search(r'[Tt]\+\d+', msg)
    if period_match:
        period = period_match.group().upper()

    # ── 构建分析维度 ──
    if is_deep:
        dimensions = "技术指标,基本面,新闻,资金流向,政策,公司财务"
    else:
        dimensions = "技术指标,基本面,资金流向"

    return f"用户需要从{dimensions}等方面分析{stock_name} ({stock_code}) 股票,周期:{period},给出标准评估结果"


def make_prepare_node(ctx: NodeContext):
    """创建 prepare_node（闭包捕获 ctx）。"""

    async def prepare_node(state: dict) -> dict:
        """准备阶段：实体解析 + RAG + TraceCollector。"""
        user_input = state["user_input"]
        session_id = state.get("session_id", "default")
        use_rag = state.get("use_rag", True)
        trace = state.get("_trace")

        # 实体解析（通用接口，由 ctx.entity_resolver 决定领域）
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
                    logger.info("[Prepare] 实体解析: %s → %s %s (%s)", user_input, entity_code, entity_name, entity_type)
            except Exception as e:
                logger.debug("[Prepare] 实体解析跳过: %s", e)

        # ── 消息扩写：将简短指令扩写为完整分析指令 ──
        effective_input = user_input
        if entity_code and entity_type == "stock" and entity_name:
            effective_input = _expand_stock_query(user_input, entity_name, entity_code)

        # RAG 检索
        sources = []
        context = ""
        if use_rag and ctx.retriever:
            try:
                docs = await ctx.retriever.retrieve(user_input)
                if docs:
                    from rag.retriever import Retriever
                    context = Retriever.format_context(docs)
                    sources = [{"content": d["content"][:200], "score": d.get("score", 0)} for d in docs]
                    logger.info("[Prepare] RAG 检索到 %d 条文档, %d 字符", len(docs), len(context))
                    for i, d in enumerate(docs[:3]):
                        logger.info("[Prepare] RAG[%d] score=%.2f content=%s", i, d.get("score", 0), d["content"][:100])
            except Exception as e:
                logger.warning("[Prepare] RAG 检索失败: %s", e)

        # TraceCollector
        try:
            from trace_collector import TraceCollector
            from agents.task_agent import _collectors
            collector = TraceCollector(session_id=session_id, user_query=user_input)
            _collectors[session_id] = collector
        except Exception as e:
            logger.debug("[Prepare] TraceCollector 创建失败: %s", e)

        return {
            "entity_code": entity_code,
            "entity_name": entity_name,
            "entity_type": entity_type,
            "context": context,
            "sources": sources,
            "effective_input": effective_input,
        }

    return prepare_node


def make_plan_node(ctx: NodeContext):
    """创建 plan_node（闭包捕获 ctx）。"""

    async def plan_node(state: dict) -> dict:
        """规划阶段：RAG 检索 + 选择技能 + 划分阶段。"""
        trace = state.get("_trace")
        effective_input = state.get("effective_input", state["user_input"])

        # RAG 检索结果注入到规划输入
        rag_context = state.get("context", "")
        if rag_context:
            logger.info("[Plan] RAG 检索到 %d 字符上下文", len(rag_context))
            effective_input = f"{effective_input}\n\n【参考资料】\n{rag_context}"

        plan = await ctx.agent._plan(effective_input, ctx.llm, trace)

        return {
            "phases": plan["phases"],
            "planning_interval": plan["planning_interval"],
            "expanded_query": plan["expanded_query"],
            "current_phase_index": 0,
            "phase_results": [],
        }

    return plan_node


def make_execute_node(ctx: NodeContext):
    """创建 execute_node（闭包捕获 ctx）。"""

    async def execute_node(state: dict) -> dict:
        """执行阶段：执行 phases[current_phase_index]。"""
        # 通过 ctx.agent 调用 TaskAgent 的方法
        agent_instance = ctx.agent
        if not agent_instance:
            logger.error("[Execute] ctx.agent 未设置")
            return {"error": "agent 未初始化", "current_phase_index": state.get("current_phase_index", 0) + 1}

        from agents.task_agent import _load_skill_functions, _SkillSectionTool, _SkillResourceTool

        phases = state["phases"]
        idx = state.get("current_phase_index", 0)
        phase = phases[idx]
        phase_id = phase.get("id", idx)
        phase_type = phase.get("type", "execute")
        phase_skill = phase.get("skill")
        expanded_query = state.get("expanded_query", state["user_input"])
        planning_interval = state.get("planning_interval", 5)

        trace = state.get("_trace")

        # 构建共享上下文（通用：只注入实体信息，不做领域判断）
        shared_parts = []
        if state.get("entity_code"):
            entity_label = state.get("entity_type", "实体")
            entity_info = f"【{entity_label}】{state.get('entity_name', '')}({state['entity_code']})" if state.get('entity_name') else f"【{entity_label}】{state['entity_code']}"
            shared_parts.append(entity_info)
        if state.get("context"):
            shared_parts.append(f"【参考资料】\n{state['context']}")

        # direct 类型：LLM 直接回答
        if phase_type == "direct":
            logger.info("[Execute] 阶段 %d: direct — %s", phase_id, phase.get("name"))
            messages = [ChatMessage(role="system", content=ctx.system_prompt)]
            if state.get("context"):
                messages.append(ChatMessage(role="system", content=f"【参考资料】\n{state['context']}"))
            if ctx.memory:
                history = await ctx.memory.get_history(state.get("session_id", "default"), limit=ctx.memory_window_size)
                for msg in history:
                    messages.append(ChatMessage(role=msg.role, content=msg.content))
            messages.append(ChatMessage(role="user", content=state["user_input"]))
            llm_response = await ctx.llm.generate(messages=messages)
            result = llm_response.content or ""
            phase_results = state.get("phase_results", [])
            phase_results.append({"phase": phase, "result": result, "passed": bool(result.strip())})
            return {
                "phase_results": phase_results,
                "current_phase_index": idx + 1,
            }

        # skill / execute 类型
        logger.info("[Execute] 阶段 %d: %s — %s (skill=%s)", phase_id, phase_type, phase.get("name"), phase_skill)

        phase_task_parts = list(shared_parts)

        # skill 类型：加载技能指令和工具
        skill_tools = []
        if phase_type == "skill" and phase_skill and ctx.skill_adapter:
            loader = ctx.skill_adapter
            try:
                body = loader.load_body(phase_skill)
                if body:
                    if len(body.split()) > 500:
                        headings = loader.get_section_headings(phase_skill)
                        catalog = "\n".join(f"  - {h}" for h in headings)
                        phase_task_parts.append(
                            f"【技能: {phase_skill}】\n"
                            f"使用 read_skill_section 工具按需加载指令段落。\n"
                            f"可用段落:\n{catalog}"
                        )
                    else:
                        phase_task_parts.append(f"【技能指令: {phase_skill}】\n{body}")
            except Exception as e:
                logger.warning("[Execute] skill %s 加载失败: %s", phase_skill, e)

            func_tools = _load_skill_functions(phase_skill)
            skill_tools.extend(func_tools)
            skill_tools.append(_SkillSectionTool(loader, phase_skill))
            skill_tools.append(_SkillResourceTool(loader, phase_skill))

        # 构建阶段任务
        # 阶段路线图（旧版 _build_phase_roadmap 的逻辑）
        phases = state.get("phases", [])
        roadmap_lines = ["【执行路线图】（仅作参考，不要跳过当前阶段）"]
        for p in phases:
            pid = p.get("id", 0)
            name = p.get("name", "")
            goal = p.get("goal", "")
            if pid == phase_id:
                roadmap_lines.append(f"  → 阶段{pid}: {name} — {goal} ← 你在这里")
            elif pid < phase_id:
                roadmap_lines.append(f"  ✓ 阶段{pid}: {name}")
            else:
                roadmap_lines.append(f"  ○ 阶段{pid}: {name} — {goal}")
        roadmap_lines.append("\n【约束】完成当前阶段后输出结果，不要自行进入下一阶段。")
        phase_task_parts.append("\n".join(roadmap_lines))

        phase_task_parts.append(f"【当前阶段】{phase.get('name', '执行')} — {phase.get('goal', '')}")
        phase_task_parts.append(f"【任务】\n{expanded_query}")

        # 注入前序阶段变量摘要
        shared_state = state.get("shared_state", {})
        if shared_state:
            var_summary = "【前序阶段变量】\n"
            for k, v in shared_state.items():
                var_summary += f"- {k}: {agent_instance._infer_var_type(v)}\n"
            var_summary += "这些变量可直接使用，无需重新获取。\n"
            phase_task_parts.append(var_summary)

        phase_task = "\n\n".join(phase_task_parts)

        # 构建 CodeAgent
        agent = agent_instance._build_code_agent(
            model=ctx.model,
            mcp_tool_list=ctx.mcp_tool_list,
            skill_tools=skill_tools,
            planning_interval=planning_interval,
            phase_id=phase_id,
        )

        # 注入工具列表到 planning prompt（作用域仅 planning，执行层看不见）
        all_tools = list(ctx.mcp_tool_list) + skill_tools
        if all_tools:
            agent_instance._inject_tools_to_planning(agent, all_tools)

        # 注入前序阶段变量到 executor state
        if shared_state:
            try:
                executor = getattr(agent, 'python_executor', None)
                if executor and hasattr(executor, 'state'):
                    executor.state.update(shared_state)
            except Exception as e:
                logger.debug("[Execute] executor state 注入跳过: %s", e)

        # 执行
        result = await agent_instance._execute_phase(phase_task, agent, phase, trace)

        # ── 提取结构化数据 ──
        stock_data = extract_stock_data(result) if result else None
        if stock_data:
            logger.info("[Execute] 提取到结构化数据: %s", list(stock_data.keys()))

        # 捕获 executor state（阶段变量）→ 传给下一阶段
        new_shared_state = dict(shared_state)
        try:
            executor = getattr(agent, 'python_executor', None)
            if executor and hasattr(executor, 'state'):
                for k, v in executor.state.items():
                    if not k.startswith('_') and agent_instance._is_serializable(v):
                        new_shared_state[k] = v
        except Exception as e:
            logger.debug("[Execute] executor state 捕获跳过: %s", e)

        # 规则 eval
        passed = bool(result and result.strip())
        phase_results = state.get("phase_results", [])
        phase_results.append({
            "phase": phase,
            "result": result,
            "passed": passed,
            "stock_data": stock_data,
        })

        return {
            "phase_results": phase_results,
            "current_phase_index": idx + 1,
            "shared_state": new_shared_state,
        }

    return execute_node


def make_finalize_node(ctx: NodeContext):
    """创建 finalize_node（闭包捕获 ctx）。"""

    async def finalize_node(state: dict) -> dict:
        """最终阶段：汇总结果 + 提取结构化数据 + 保存 memory + TraceCollector 存库。"""
        from agents.task_agent import _collectors

        session_id = state.get("session_id", "default")
        phase_results = state.get("phase_results", [])

        # ── 提取结构化数据（最后一个有效阶段）──
        stock_data = {}
        for pr in reversed(phase_results):
            if pr.get("stock_data"):
                stock_data = pr["stock_data"]
                break

        # ── 生成显示文本 ──
        if stock_data:
            result_raw = format_stock_report(stock_data)
        elif not phase_results:
            result_raw = "[错误] 没有执行任何阶段"
        elif len(phase_results) == 1:
            result_raw = phase_results[0]["result"]
        else:
            result_parts = []
            for pr in phase_results:
                status = "✅" if pr["passed"] else "❌"
                result_parts.append(f"{status} 阶段: {pr['phase'].get('name', '')}\n{pr['result']}")
            result_raw = "\n\n".join(result_parts)

        # 全部失败时提示
        all_failed = all(not pr.get("passed") for pr in phase_results)
        if all_failed and phase_results:
            result_raw = "所有阶段执行失败，请检查网络连接或稍后重试。\n\n" + result_raw

        # ── 构建 final_output ──
        final_output = {"data": stock_data} if stock_data else {}

        # 保存 memory
        if ctx.memory:
            try:
                await ctx.memory.add(session_id, "user", state["user_input"])
                await ctx.memory.add(session_id, "assistant", result_raw)
            except Exception as e:
                logger.warning("[Finalize] memory 保存失败: %s", e)

        # ── EvalNode 存库（结构化追踪） ──
        if stock_data:
            try:
                from chain.schema import EvalNode, Layer, Status
                from chain.store import save_tree
                from datetime import date

                entity_code = state.get("entity_code", "")
                entity_name = state.get("entity_name", "")
                tools_called = []
                for pr in phase_results:
                    phase = pr.get("phase", {})
                    if phase.get("skill"):
                        tools_called.append(f"skill:{phase['skill']}")

                root = EvalNode(
                    layer=Layer.CHAIN.value,
                    name=f"{state.get('entity_type', 'stock')}+analyze+{entity_code}",
                    exec_date=date.today(),
                    stock_code=entity_code,
                    stock_name=entity_name,
                    score=stock_data.get("score"),
                    direction=stock_data.get("direction", ""),
                    action=stock_data.get("action", ""),
                    signal=stock_data.get("signal", ""),
                    analysis=result_raw[:2000],
                    input_params={"user_query": state.get("user_input", "")},
                    status=Status.OK.value,
                    tools_called=tools_called,
                )
                save_tree(root)
                logger.info("[Finalize] EvalNode 已存储: score=%s action=%s", stock_data.get("score"), stock_data.get("action"))
            except Exception as e:
                logger.warning("[Finalize] EvalNode 存库失败: %s", e)

        # TraceCollector 存库
        collector = _collectors.get(session_id)
        if collector:
            try:
                collector.on_agent_finish(
                    final_answer=result_raw,
                    total_steps=len(phase_results),
                    total_tokens=0,
                    model=getattr(ctx.llm, "model", "unknown"),
                )
                collector.flush()
            except Exception as e:
                logger.warning("[Finalize] TraceCollector 存库失败: %s", e)
            finally:
                _collectors.pop(session_id, None)

        # 计算耗时
        start_time = state.get("_start_time", 0)
        elapsed = round(time.time() - start_time, 2) if start_time else 0

        return {
            "result_raw": result_raw,
            "elapsed": elapsed,
            "final_output": final_output,
        }

    return finalize_node


# ═══════════════════════════════════════════════════════════════
#  路由函数
# ═══════════════════════════════════════════════════════════════

def route_after_plan(state: dict) -> str:
    """plan_node 之后的路由。"""
    phases = state.get("phases", [])
    if not phases:
        return "finalize"
    # 如果只有一个 direct 阶段，直接 finalize
    if len(phases) == 1 and phases[0].get("type") == "direct":
        return "execute"  # direct 也在 execute_node 处理
    return "execute"


def route_after_execute(state: dict) -> str:
    """execute_node 之后的路由。"""
    phases = state.get("phases", [])
    idx = state.get("current_phase_index", 0)

    # 还有更多阶段
    if idx < len(phases):
        return "execute"

    # 所有阶段完成
    return "finalize"
