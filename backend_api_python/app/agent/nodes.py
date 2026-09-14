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
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, TypedDict

from llm.base import ChatMessage, LLMBase
from utils.tracing import AgentTraceRecorder

logger = logging.getLogger(__name__)

# F3 门控（2026-09-14）：代码/通用意图的语汇。命中即**不**做"RAG 历史标的 → 实体"的
# 辅助注入——那条通道的本意是补全"它/这只票"这类省略指代，对代码任务是纯噪声。
# 实证：「写一个跑马灯的代码并运行」被注入"西安银行(600928)"，迫使模型把股票分析
# 塞进跑马灯任务。新增领域只需在此补语汇。
_CODE_INTENT_RE = re.compile(
    r"代码|程序|脚本|函数|算法|排序|递归|写一段|写一个|跑马灯|示例|demo|python|运行",
    re.IGNORECASE)


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
    plan_tools: list      # 顶层附加点名的工具（单段路径与 domain 基调取并集；有 phases 时恒为 []）

    # ── phase 契约（B 阶段 2026-09-12）──
    phases: list          # 外部 planner 产出的阶段契约（[]=单段执行旧路径）
    phase_index: int      # 阶段游标（当前执行第几个，0 起）
    phase_retry: int      # 当前阶段已重试次数
    phase_replan_count: int  # 因阶段失败触发的重设计次数（route 上限 2）
    phase_results: list   # 阶段结果记录 [{id,name,status,note,elapsed,preview}]
    completed_phases_text: str  # 阶段摘要累积（正向通道，注入下一阶段任务书）
    phase_last_note: str  # 最近一次验收失败说明（重试任务书引用）
    _phase_agents: Any    # 各阶段 CodeAgent 实例（dict，非序列化）
    _phase_abort: bool    # 中断标记：跳过重试直接收尾

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

# 进程级 ToolProvider 缓存：tools/ 目录运行期不变，扫描一次全程复用（含全局 set_default）。
# 注：单进程假设——多 worker 模式（GUNICORN_WORKERS>1）下各进程各自扫描，互不共享。
_SHARED_TOOL_PROVIDER = None


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

        # 过程事件回调（2026-09-11 SSE 改造）：节点/工具/步骤事件经此上报，
        # 由 SSE 层注入。None = 零开销（CLI/定时任务等非流式路径不受影响）。
        self.event_cb = None

        # TaskAgent 实例（用于调用 _build_code_agent 等方法）
        self.agent = None

        # TraceCollector（session 级）
        self.collectors: Dict[str, Any] = {}

    def init_tools(self):
        """初始化 ToolProvider（扫描 tools/ 目录）+ LLM 适配器。

        扫描结果进程内缓存：tools/ 目录内容在运行期不变，每次请求重扫纯属浪费
        （且扫描会 import 全部工具模块，冷路径可达数秒）。进程首扫后直接复用。
        """
        from tools.base import ToolProvider
        from agents.task_agent import _LLMAdapter
        from pathlib import Path

        global _SHARED_TOOL_PROVIDER
        if _SHARED_TOOL_PROVIDER is None:
            tools_dir = Path(__file__).resolve().parent / "tools"
            provider = ToolProvider()
            # 扫描 tools/ 根目录（通用工具）
            provider.scan_directory(tools_dir, domain="common", package_prefix="tools")
            # 扫描 tools/ 子目录（领域工具）
            provider.scan_subdirectories(tools_dir, package_prefix="tools")
            # 能力发现层（A 阶段 2026-09-12）: admission.json 准入的只读函数 -> 来源层标记
            # 2026-09-14：能力层**默认屏蔽**，CAPABILITIES_ENABLED=1 才注册。
            # 理由（用户决策）：19 项准入全部**无审核留痕**（admission.json 没有
            # reviewed_by/at），来源还是一份并不存在的"扫描报告"；而这层带来的
            # 取数函数让工具面变大 → 模型更容易试探、上下文膨胀更快、沙箱冲突更多。
            # 补齐审核留痕并评估收益后，再决定是否重新打开。
            try:
                if os.getenv("CAPABILITIES_ENABLED", "0") == "1":
                    from capabilities import register_capabilities
                    _cap_n = register_capabilities(provider)
                    logger.info("[Context] 能力层注册: %d 个函数（来源层标记，非可选工具域）", _cap_n)
                else:
                    logger.info("[Context] 能力层已屏蔽（CAPABILITIES_ENABLED != 1），跳过注册")
            except Exception as e:
                logger.warning("[Context] 能力层注册失败（不阻断启动）: %s", e)
            _SHARED_TOOL_PROVIDER = provider
            ToolProvider.set_default(provider)  # 全局默认 provider 只在首扫时设置一次
            logger.info("[Context] ToolProvider 初始化完成: %d 个工具", len(provider))

        self.tool_provider = _SHARED_TOOL_PROVIDER
        self.model = _LLMAdapter(self.llm)


# ═══════════════════════════════════════════════════════════════
#  节点函数
# ═══════════════════════════════════════════════════════════════




def _set_llm_timeout(agent, timeout_seconds: int):
    """设置 LLM 超时（直接改底层 OpenAI 客户端，而非 model 属性）。"""
    try:
        # smolagents _LLMAdapter → 内部 _llm (OpenAILLM) → _client (AsyncOpenAI)
        llm_adapter = getattr(agent, 'model', None)
        if llm_adapter is not None:
            # 记录期望超时（2026-09-12）：同步客户端惰性创建/重建时补挂
            try:
                llm_adapter._desired_timeout = timeout_seconds
            except Exception:
                pass
        if llm_adapter and hasattr(llm_adapter, '_llm'):
            inner_llm = llm_adapter._llm
            client = getattr(inner_llm, '_client', None)
            if client:
                client.timeout = timeout_seconds
                logger.debug("[Execute] 已设置 OpenAI 客户端 timeout=%ds", timeout_seconds)
                return
        # 同步客户端分支（2026-09-11，审计 P2）：CodeAgent 实际走 _LLMAdapter 的独立
        # 同步 OpenAI 客户端（_get_sync_client），上面的 AsyncOpenAI 分支对它无效——
        # 不设则执行期 LLM 调用沿用工厂默认超时，180s 上限形同虚设。
        # 与 generate() 同款阈值判定，客户端未初始化时不主动建连接。
        if llm_adapter and getattr(llm_adapter, '_sync_client', None) is not None:
            try:
                llm_adapter._sync_client.timeout = timeout_seconds
                logger.debug("[Execute] 已设置同步 OpenAI 客户端 timeout=%ds", timeout_seconds)
            except Exception as e2:
                logger.debug("[Execute] 设置同步客户端超时失败: %s", e2)
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
            if getattr(step, 'tool_calls', None):
                # smolagents 的 ToolCall 是 dataclass(name, arguments)，没有 .function 属性；
                # 旧代码 fallback 到 tc.function.get('name') 会读出空字符串（审计 P2）。
                tc = step.tool_calls[0]
                tool_name = getattr(tc, 'name', '') or ""
                raw_args = getattr(tc, 'arguments', None)
                if isinstance(raw_args, str):
                    try:
                        tool_args = json.loads(raw_args)
                    except Exception:
                        tool_args = {}
                elif isinstance(raw_args, dict):
                    tool_args = raw_args
            elif getattr(step, 'tool_name', None):
                tool_name = step.tool_name
                raw_args = getattr(step, 'tool_arguments', None) or {}
                if isinstance(raw_args, dict):
                    tool_args = raw_args

            if not tool_name:
                continue

            # 在 _truncate_observations（保留近 2 步，其余截到 200 字符）破坏前提取完整观察；
            # 旧实现在 finalize 时才读，trace 里只剩二手截断文本（审计文档漂移）。
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


# 工具失败标记：工具返回 dict 带 error 键时由 _extract_failed_tools 识别。
# 旧版依赖 observations 中 "'_failed_tool': 'xxx'" 标记，但全后端无任何代码生产该
# 标记（无生产者的消费者），失败工具检测恒空（审计 P1-4）。现以 error 键为准，
# _failed_tool 正则保留兼容历史数据。
_FAILED_TOOL_KEYS = ("error", "err_msg", "error_msg", "_failed_tool")


def _extract_failed_tools(agent, tool_provider=None) -> list:
    """从 agent memory 中提取失败工具。

    判定通道：
      1. smolagents ToolCall 的目标工具名 + observations 含 'error' 关键结构；
      2. observations 中工具返回 dict 的 error 类键值（error/err_msg/...）；
      3. 历史 _failed_tool 标记（兼容）。
    """
    failed = []
    seen = set()

    def _add(name, obs):
        if not name or name in seen:
            return
        # 伪工具名过滤（2026-09-12）：smolagents 为代码执行步骤记录 python_interpreter
        # 等伪工具名，任务尾"数据完整性"误报来源于此（CLI 实测）。
        if name in ("python_interpreter", "final_answer"):
            return
        seen.add(name)
        desc = ""
        if tool_provider:
            func = tool_provider.get(name)
            if func:
                desc = (inspect.getdoc(func) or "").split("\n")[0][:60]
        failed.append((name, desc))

    try:
        from smolagents.memory import ActionStep
        for step in getattr(agent.memory, 'steps', []):
            if not isinstance(step, ActionStep):
                continue
            obs = str(getattr(step, 'observations', '') or '')
            if not obs:
                continue

            # 通道 2/3：error 键值 或 _failed_tool 标记（值即工具名）
            for m in re.finditer(r"'(?:error|err_msg|error_msg|_failed_tool)'\s*:\s*'([^']+)'", obs):
                val = m.group(1)
                # 值像工具名（标识符）→ 直接视为失败工具名；否则尝试从工具调用记录反查
                if re.fullmatch(r'[a-zA-Z_]\w{2,40}', val):
                    _add(val, obs)

            # 通道 1：本步有工具调用且 observation 报错 → 记录该工具
            if re.search(r"'error'\s*:", obs) or 'Error' in obs:
                for tc in getattr(step, 'tool_calls', None) or []:
                    _add(getattr(tc, 'name', '') or '', obs)
    except Exception:
        pass
    return failed


# 数据/工具层不可用的正向证据关键词（验收失败归因，区分"环境导致"vs"agent 可控"）
_TOOL_DATA_KW = (
    "接口未返回", "未返回", "数据缺失", "无数据", "获取失败", "调用失败", "拉取失败",
    "超时", "timeout", "time out", "api error", "http 5", "502", "503", "504",
    "无法获取", "未能获取", "暂无", "暂不可用", "服务异常", "rate limit", "限流",
    "返回空", "空值", "缺失", "不可用", "unavailable", "failed to fetch", "网络",
)


def _tool_data_evidence(result_text: str, failed_tools: list, run_error) -> bool:
    """验收失败的"环境归因"正向证据。

    仅当本阶段**确有**工具/数据层失败时，才允许把验收失败归因为 tool_data_fault；
    否则即便验收判官声称"工具/数据导致"，也回退为 agent_fault，避免模型手软放过
    本应重试的环节（验收的意义不能被绕过）。
    """
    if run_error:
        return True
    if failed_tools:
        return True
    low = (result_text or "").lower()
    return any(k in low for k in _TOOL_DATA_KW)


def _decide_phase_transition(passed, reason, on_fail, retry, max_retries, replan_count):
    """阶段验收后的推进决策（由 execute_node 调用；抽为独立函数便于回归测试）。

    返回 (kind, new_retry, do_replan, soft_pass)：
      kind="advance" → 游标 +1（通过 / 软通过 / 重试·重设计额度耗尽容忍失败推进）
      kind="retry"   → 游标不变，new_retry=retry+1（同阶段重试）
      kind="replan"  → 游标不变，do_replan=True（回外部 planner 重设计）
      soft_pass=True 仅当"未通过但缺失归因于工具/数据不可用"，此时仍 advance 但
      保留已产出工作、不重试、不放弃。
    """
    # 软通过：验收通过，或仅因工具/数据不可用（非 agent 可控）而部分缺失 →
    # 保留已产出的有效工作（如 80 分），直接推进管道。不重试（同一不可用工具必再
    # 失败）、不放弃（已产出部分有价值）。
    if passed or reason == "tool_data_fault":
        return "advance", 0, False, (not passed)
    # 仅 agent 可控失败才按 on_fail 重试 / 重设计
    if on_fail == "retry" and retry < max_retries:
        return "retry", retry + 1, False, False
    if on_fail == "replan" and replan_count < MAX_REPLAN:
        return "replan", retry, True, False
    # 重试 / 重设计额度耗尽：容忍单阶段失败，推进管道，让后续阶段继续产出
    return "advance", 0, False, False


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
                # 过滤低相关度文档（避免噪音污染任务）。
                # 分数尺度按来源分流：启用 Reranker 时输出 rerank_score∈[0,1] 用绝对阈值；
                # 未启用时输出 RRF 融合分（weight/(rrf_k+rank)，上限≈0.016），绝对阈值不可用，
                # 改为只保留 RRF 排名前 N 条（top_k 已由检索器按排名截断，这里防御性二次截断）。
                # 背景：旧实现统一用 0.7 阈值，RRF 尺度下过滤掉全部文档，RAG 静默失效（审计 P0-2）。
                rerank_used = any(d.get("rerank_score") is not None for d in docs)
                if rerank_used:
                    RAG_SCORE_THRESHOLD = 0.7
                    docs = [d for d in docs if (d.get("rerank_score") or 0) >= RAG_SCORE_THRESHOLD]
                else:
                    # RRF ????????2026-09-12 ???????? 0.01 ???
                    # ??????????RRF ????weight/(60+rank)??????0.016?
                    # 0.005 ? ?? rank>40 ???????????????
                    _rrf_min = float(os.getenv("RAG_RRF_MIN_SCORE", "0.005"))
                    _before = len(docs)
                    docs = [d for d in docs if (d.get("score") or 0) >= _rrf_min]
                    if len(docs) < _before:
                        logger.info("[Chat] RAG RRF ????: %d ? %d ? (min=%.4f)",
                                    _before, len(docs), _rrf_min)
                    docs = docs[: max(1, int(os.getenv("RAG_TOP_K", "5")))]
                if docs:
                    from rag.retriever import Retriever
                    context = Retriever.format_context(docs)
                    sources = [{"content": d["content"][:200], "score": d.get("score", 0)} for d in docs]
                    logger.info("[Chat] RAG 检索到 %d 条文档（rerank=%s）, %d 字符", len(docs), rerank_used, len(context))
            except Exception as e:
                logger.warning("[Chat] RAG 检索失败: %s", e)

        # ── 2. 实体解析（RAG 上下文辅助）──
        entity_code = ""
        entity_name = ""
        entity_type = ""
        effective_input = user_input
        # RAG 辅助：用户消息无明确代码时，从 context 中提取最近分析的标的
        #
        # F3 门控（2026-09-14 修复）：**代码/通用意图不得被历史标的污染**。
        # 实证：问「写一个跑马灯的代码并运行」，RAG 捞到历史分析记录里的 600928，
        # 于是给纯代码任务注入"西安银行(600928)，周期 T+3，深度：标准"——模型把股票
        # 分析塞进跑马灯，5 步才收尾（原本 1~2 步），单步最长 150s。
        # 该通道本意是"用户说'它'/'这只票'时补上标的"，对代码任务是纯粹噪声。
        resolve_input = user_input
        if context and not re.search(r'(?<!\d)\d{6}(?!\d)', user_input) \
                and not _CODE_INTENT_RE.search(user_input):
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
                    # 澄清契约（2026-09-13 通用，域无关）：解析器无法准确判断时
                    # 必须填 clarify_question 而不是猜默认值；这里直接把问题反问
                    # 用户且**不进入执行**，用户答复作为新消息重新解析，拿到准确
                    # 信息后才执行。任何 *_clarify（time / entity / 未来领域）都走
                    # 本通道，新增领域无需改本文件。
                    _q = str(getattr(entity, "clarify_question", "") or "")
                    if not _q and entity_type.endswith("_clarify"):
                        # 向后兼容：仍支持仅通过 entities 标记传递的旧写法
                        for _e in entity.entities or []:
                            if isinstance(_e, dict) and str(_e.get("type", "")).endswith("_clarify"):
                                _q = str(_e.get("question", ""))
                                break
                    if _q:
                        logger.info("[Chat] 澄清反问(%s): %s", entity_type or "clarify", _q[:80])
                        return {
                            "needs_task": False,
                            "task_type": "query",
                            "direct_answer": _q,
                        }
                    # 直接用 resolver 生成的 effective_input（含实体注入+扩写）
                    if entity.effective_input:
                        effective_input = entity.effective_input
                    elif entity_code:
                        # resolver 没生成 effective_input，手动注入实体信息
                        entity_desc = f"{entity_name}({entity_code})" if entity_name else entity_code
                        effective_input = f"{user_input} 【实体】{entity_desc} [{entity_type}]"
                    logger.info("[Chat] 实体解析: %s → %s %s (%s)", user_input, entity_code, entity_name, entity_type)
                    if effective_input != user_input:
                        # 2026-09-14：让"时间/实体到底注入成什么样"在日志里直接可见。
                        # 只打印原始输入时，排查者会误判解析器没生效（实测即如此）。
                        logger.info("[Chat] 指令扩写: %s", effective_input)
            except Exception as e:
                # 提升为 warning（2026-09-13）：此处原先 debug，把"注入对象不满足
                # EntityResolver 契约"这类接线错误整体吞掉了——实体解析与澄清反问
                # 因此静默失效很久。解析失败是真实降级，必须可见。
                logger.warning("[Chat] 实体解析失败（已跳过，不影响主流程）: %s", e)

        # ── 4. 意图分类：统一 LLM 分类 ──
        needs_task = True
        direct_answer = ""
        task_type = ""

        try:
            # 从文件加载意图分类器 prompt
            intent_system = ""
            try:
                # 易错点（2026-09-12 修复）: 不可在函数内 `import os`——会让整个 chat_node 的
                # os 变量成为局部变量, 上方 RAG 段（os.getenv）先于本行执行会抛 UnboundLocalError,
                # RAG 检索静默失败。模块顶部已有 import os, 此处直接复用。
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
                if trace:
                    trace.set_intent(verb="chat")
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
                if trace:
                    trace.set_intent(verb=task_type or "general")
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
                    if trace:
                        trace.set_intent(verb="cron")
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

        # plan 上下文挂本次请求的 ctx（并发安全）：
        # 旧实现写在共享 TaskAgent 单例的实例属性上，多 worker 并发时会话间互相覆盖（审计 P1-7）。
        ctx._plan_entity_info = entity_info_str
        ctx._plan_task_type_info = task_type_str
        ctx._plan_rag_context = rag_context_str
        ctx._plan_history_context = history_context_str
        # 已完成阶段摘要（phase 模式复盘时非空；_plan 模板 {completed_phases_text} 读取）
        ctx._completed_phases_text = state.get("completed_phases_text", "") or ""

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
        plan = await ctx.agent._plan(plan_input, ctx.llm, trace, plan_ctx=ctx)

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

        # phase 契约透传（B 阶段）：游标归零重走新序列；completed_phases_text / phase_results
        # 保留累积（复盘时已完成阶段的摘要仍然注入任务书）；_phase_agents 重置——
        # 重设计后契约已更换，旧 per-phase 记忆作废（重试走 execute 自身，不经过本节点）。
        phases = plan.get("phases") or []
        if phases:
            logger.info("[Plan] phase 契约: %d 个阶段: %s", len(phases),
                        ", ".join(f"#{p['id']}{p['name']}" for p in phases))

        return {
            "task": plan["task"],
            "selected_skill": selected_skill or "",
            "selected_domain": plan.get("selected_domain", ""),
            "skill_body": skill_body,
            "skill_tools": skill_tools,
            "step_budget": plan["step_budget"],
            "planning_interval": plan.get("planning_interval", 6),
            "replan_count": replan_count + (1 if prev_hit else 0),
            "phases": phases,
            "plan_tools": plan.get("plan_tools") or [],
            "phase_index": 0,
            "phase_retry": 0,
            "_phase_agents": {},
        }

    return plan_node


# agent.run 墙钟上限（秒；2026-09-12 卡死事故防线）：
# 非主线程执行时，超时按执行失败上报并隔离卡死线程（Python 无法强杀线程）。
AGENT_RUN_WALL_TIMEOUT = int(os.getenv("AGENT_RUN_WALL_TIMEOUT", "600"))


def _run_agent_with_guard(agent, full_task: str):
    """带 SIGINT 守卫 / 墙钟守卫执行 agent.run。返回 (result_str, run_error, interrupted)。

    主线程场景临时接管 SIGINT（Ctrl+C 不炸进程，标记中断后由调用方收尾）。
    非主线程（消息队列 worker / gunicorn 请求线程）无法接管信号——除 SIGINT 外，
    还需防"底层调用/调试器挂起导致线程永久阻塞拖死整条流水线"（2026-09-12 事故：
    后台线程未捕获异常被调试器 do_wait_suspend 挂起 → smolagents 代码执行
    ThreadPoolExecutor.shutdown(wait=True) 永久 join → SSE 无限等待、无错无超时）。
    对策：放入守护线程执行 + 限时等待，超时按失败上报（验收/on_fail 策略接管），
    卡死线程被隔离丢弃。
    设计点：单段执行（旧路径）与 phase 单阶段执行共用本函数，避免两处行为漂移。
    """
    import signal as _signal
    import threading as _threading
    _interrupted = False
    _is_main_thread = _threading.current_thread() is _threading.main_thread()

    def _sigint_handler(signum, frame):
        nonlocal _interrupted
        _interrupted = True
        logger.warning("[Execute] 收到 SIGINT，正在停止...")

    if not _is_main_thread:
        # ── 非主线程：墙钟守卫（守护线程 + 限时等待）──
        _holder = {}

        def _guarded_run():
            try:
                _holder["result"] = agent.run(full_task)
            except BaseException as _e:  # noqa: BLE001 - 一切异常带回主线程判定
                _holder["error"] = _e

        _gt = _threading.Thread(target=_guarded_run, daemon=True, name="agent-run-guard")
        _gt.start()
        _gt.join(AGENT_RUN_WALL_TIMEOUT)
        if _gt.is_alive():
            logger.error("[Execute] agent.run 超过 %ds 未返回，判定卡死并放弃等待（线程被隔离）",
                         AGENT_RUN_WALL_TIMEOUT)
            return (f"[run_error] agent 执行超时（超过 {AGENT_RUN_WALL_TIMEOUT}s 无响应）",
                    TimeoutError(f"agent run wall timeout >{AGENT_RUN_WALL_TIMEOUT}s"), False)
        if "error" in _holder:
            err = _holder["error"]
            if isinstance(err, KeyboardInterrupt):
                logger.warning("[Execute] 被用户中断")
                return "[run_error] 用户中断", RuntimeError("user interrupted (KeyboardInterrupt)"), False
            logger.error("[Execute] 执行异常: %s", err)
            # 机器可读错误标记：finalize_node 据此判定本次 run 失败，
            # trace 不再作为"成功分析"落库（否则污染 score/direction 统计，见 root_id=1737 事故）。
            return f"[run_error] {err}", err, False
        result = _holder.get("result")
        return (str(result) if result else ""), None, False

    old_handler = _signal.signal(_signal.SIGINT, _sigint_handler)
    run_error = None
    try:
        result = agent.run(full_task)
        if _interrupted:
            result = "[run_error] 用户中断"
            run_error = RuntimeError("user interrupted")
            logger.warning("[Execute] 被用户中断")
        result = str(result) if result else ""
    except KeyboardInterrupt:
        logger.warning("[Execute] 被用户中断")
        result = "[run_error] 用户中断"
        run_error = RuntimeError("user interrupted (KeyboardInterrupt)")
    except Exception as e:
        logger.error("[Execute] 执行异常: %s", e)
        # 机器可读错误标记：finalize_node 据此判定本次 run 失败，
        # trace 不再作为"成功分析"落库（否则污染 score/direction 统计，见 root_id=1737 事故）。
        result = f"[run_error] {e}"
        run_error = e
    finally:
        if old_handler is not None:
            _signal.signal(_signal.SIGINT, old_handler)
    return result, run_error, _interrupted


async def _check_phase_acceptance(ctx: NodeContext, phase: dict, result, run_error) -> tuple:
    """轮询通道：外部 planner 的阶段验收判定（B 阶段）。

    返回四元组 (passed, note, reason, score)：
      - passed: 是否达到验收标准
      - note: 未通过时缺失项说明（通过时留空）
      - reason: 失败归因 ——
          "pass"            验收通过
          "agent_fault"     缺失项属于 agent 可控（分析错误/交付物不完整/可修正错误）→ 应重试
          "tool_data_fault" 缺失项因工具不可用/外部接口未返回/数据缺失/网络超时等
                           **agent 无法自行解决**的环境因素 → 不应重试，应软通过保留已产出工作
      - score: 判官给的阶段完成度评分（0-100，供 telemetry；fail-open 时为 None）

    设计要点（用户定调 2026-09-14）：验收是好的，但当结果已大部分达标（如 80 分）、
    缺失的 20 分**纯因工具/数据不可用**时，系统不应无谓重试（同一工具必再失败）也不应
    放弃已产出的有效工作。故验收必须区分"环境导致"与"agent 可控"，由调用方据此决定
    重试 or 软通过。本函数只负责判定与归因，路由决策在 _run_phase_step / _decide_phase_transition。

    - 执行异常 / 结果为空 → 直接 fail（无有效产出，保持原重试行为）
    - 无 acceptance 条目 → pass（无标准即放行）
    - 有标准 → 轻量 LLM 逐条核对，输出 JSON {passed, score, reason, note}
    - 判定通道不可用（LLM 错误/解析失败）→ fail-open 放行（宁松勿卡：判定故障不阻断主链路）
    """
    text = str(result or "")
    if run_error is not None:
        return False, f"执行异常: {str(run_error)[:200]}", "agent_fault", None
    if not text.strip():
        return False, "结果为空", "agent_fault", None
    acc = phase.get("acceptance") or []
    if not acc:
        return True, "", "pass", None
    import asyncio
    _accept_timeout = float(os.getenv("PHASE_ACCEPT_TIMEOUT", "60"))
    try:
        from utils.json_parser import safe_parse_json
        criteria = "\n".join(f"{i + 1}. {a}" for i, a in enumerate(acc))
        prompt = (
            f"【阶段目标】{phase.get('goal', '')}\n"
            f"【验收标准】\n{criteria}\n"
            f"【阶段结果】\n{text[:4000]}\n\n"
            "请逐条核对阶段结果是否满足验收标准。只输出 JSON：\n"
            '{"passed": true/false, "score": 0-100, '
            '"reason": "pass 或 agent_fault 或 tool_data_fault", '
            '"note": "未通过时说明缺失项与原因（≤100字），通过时留空"}\n'
            "reason 取值规则：passed 为 true 时填 pass；为 false 时——"
            "若缺失项是由于工具不可用 / 外部接口未返回 / 数据缺失 / 网络超时等"
            "**agent 无法自行解决**的环境因素，填 tool_data_fault；"
            "若缺失项属于分析错误、交付物不完整、agent 本可修正的错误，填 agent_fault。"
        )
        # 验收判定自带上限（2026-09-12）：无独立超时时，一次慢判定会烧掉整个
        # 任务预算，阶段结果连同主链路一起被 wait_for 取消（TimeoutError 事故实测）。
        # 2026-09-13 修复：Python 3.11+ 的 wait_for 超时会级联取消整个协程树，
        # 必须捕获 CancelledError 并恢复到稳定状态。
        try:
            resp = await asyncio.wait_for(ctx.llm.generate(messages=[
                ChatMessage(role="system", content="你是严格的阶段验收员。只输出 JSON，并为失败归因于 agent_fault 或 tool_data_fault。"),
                ChatMessage(role="user", content=prompt),
            ]), timeout=_accept_timeout)
        except asyncio.CancelledError:
            # CancelledError 可能是 timeout 触发，也可能是外层取消
            # 优先按 timeout 处理（放行），避免级联崩溃
            logger.warning("[Execute] 验收判定被取消（可能是超时），放行通过")
            return True, "验收判定被取消（放行）"[:200], "pass", None
        obj = safe_parse_json((resp.content or "").strip(), default={})
        passed = bool(obj.get("passed", True))
        note = str(obj.get("note") or "")[:300]
        try:
            score = int(obj.get("score")) if obj.get("score") is not None else None
        except Exception:
            score = None
        reason = str(obj.get("reason") or ("pass" if passed else "agent_fault"))
        if reason not in ("pass", "agent_fault", "tool_data_fault"):
            reason = "pass" if passed else "agent_fault"
        return passed, note, reason, score
    except asyncio.TimeoutError:
        logger.warning("[Execute] 验收判定超时 %.0fs（放行）", _accept_timeout)
        return True, f"验收判定超时（{_accept_timeout:.0f}s）"[:200], "pass", None
    except asyncio.CancelledError:
        # 兜底：确保 CancelledError 不外泄
        logger.warning("[Execute] 验收判定 CancelledError 兜底（放行）")
        return True, "验收判定取消（兜底放行）"[:200], "pass", None
    except Exception as e:
        logger.warning("[Execute] 验收判定失败（放行）: %s", e)
        return True, f"验收判定不可用: {e}"[:200], "pass", None


def _phase_summary(phase: dict, result) -> str:
    """反向通道：阶段结果摘要（截断 300 字符，仅入 state 记录与 trace）。

    2026-09-14 三轮演进后定稿（用户定调：阶段交接**零内联**）——下阶段任务书
    不再引用本摘要（改用 phase_results 清单 + stage_read 指引），因此这里不需要
    任何提炼/截断花活：短截断仅供 state 记录与日志，不再进模型上下文。
    """
    text = str(result or "").strip()
    if len(text) <= 300:
        return text
    return f"{text[:300]}…(共 {len(text)} 字符)"


def _select_phase_skill_tools(all_tools: list, phase_tools: list) -> list:
    """按阶段契约收窄技能工具（2026-09-12 阶段清单升级）。

    - phase_tools 非空：只保留被点名的技能工具；read_skill_* 文档类工具始终保留
      （渐进式文档访问不设门槛，成本低且利于收敛）；
    - phase_tools 为空：保持全量（回退语义，兼容未列工具的旧契约）。
    """
    if not all_tools:
        return []
    names = set(phase_tools or [])
    if not names:
        return list(all_tools)
    out = []
    for t in all_tools:
        n = getattr(t, "name", "")
        if n.startswith("read_skill_") or n in names:
            out.append(t)
    return out


def _detect_max_steps(agent) -> bool:
    """smolagents>=1.27 到达 max_steps 不抛异常，仅在 memory 末步标记错误。"""
    try:
        from smolagents.utils import AgentMaxStepsError
        steps = getattr(getattr(agent, "memory", None), "steps", []) or []
        last = steps[-1] if steps else None
        return isinstance(getattr(last, "error", None), AgentMaxStepsError)
    except Exception:
        return False


def _has_final_answer(agent) -> bool:
    """引擎级 final_answer 检测：任一 ActionStep 置位 is_final_answer 即视为正常收尾。

    模型偶发忘调 final_answer 时，smolagents 只会走 _handle_max_steps_reached() 强制生成
    末步（该步仅有 error=AgentMaxStepsError，无 is_final_answer 标记）——通过扫描标记
    即可在引擎层判定"未正常收尾"，不再依赖 error 类型/版本行为。
    """
    try:
        from smolagents.memory import ActionStep
        steps = getattr(getattr(agent, "memory", None), "steps", []) or []
        return any(getattr(s, "is_final_answer", False) for s in steps
                   if isinstance(s, ActionStep))
    except Exception:
        return True


def _auto_stage_phase_result(scope: str, phase: dict, result, min_chars: int = 800) -> str:
    """框架侧自动落盘阶段完整结果（强制机制，2026-09-12）。

    摘要通道会丢明细，且跨阶段重数据原依赖模型自觉 stage_write——
    本函数在框架层代做：阶段结果超过 min_chars 即自动写入暂存区并返回文件名，
    下阶段任务书据此提示可 stage_read 读取完整数据。落盘失败不影响主流程。
    2026-09-14：min_chars 2000 → 800，与 `_phase_summary` 的截断上限对齐——
    凡被摘要截掉的全文，必然已在暂存区（内联摘要 ⊆ 暂存区全文）。
    """
    text = str(result or "")
    if len(text) <= min_chars:
        return ""
    try:
        # 2026-09-13 修正：infra 拆分后 staging 已迁至 infra/（见 DESIGN v2.0 架构行），
        # 此处遗留 `from tools.staging import ...` → ImportError 被下面的 except 静默吞掉，
        # 本函数（"框架代做落盘"的强制机制）实际从未执行过，且无任何告警。
        from infra.staging import stage_write
        base = re.sub(r"[^A-Za-z0-9_.-]", "_",
                      f"phase_{phase.get('id', '')}_{phase.get('name', '')}")[:76]
        name = f"{base}.md"
        stage_write(scope, name, text)
        return name
    except Exception as e:
        # 接线错误必须可见（同 v2.2 对 L8 的处置）：降级可以，静默不行。
        logger.warning("[Execute] 阶段结果自动落盘失败: %s", e)
        return ""


def _extract_clean_phase_result(agent, result: str, limit: int = 4000) -> str:
    """步数耗尽时，优先取最后一段"干净的执行输出"（打印内容）作为阶段结果。

    原因（run3 实测）：强制 final answer 常是半成品——<code> 包裹的工具调用 JSON，
    原样下传会污染下一阶段任务书。跳过含执行错误标记的观察，取最后一段干净输出。
    """
    bad_marks = ("Code execution failed", "InterpreterError", "Traceback",
                 "is not among", "is not allowed", "KeyError")
    try:
        from smolagents.memory import ActionStep
        for step in reversed(getattr(agent.memory, "steps", []) or []):
            if not isinstance(step, ActionStep):
                continue
            obs = str(getattr(step, "observations", "") or "").strip()
            if not obs or any(k in obs for k in bad_marks):
                continue
            return obs[:limit]
    except Exception:
        pass
    return result


async def _run_phase_step(ctx: NodeContext, state: dict, phases: list) -> dict:
    """执行单个 phase（B 阶段 2026-09-12 单阶段执行器）。

    正向通道：任务书 = 用户原始需求 + 已完成阶段摘要 + phase.goal/deliverable/acceptance。
    反向通道：阶段摘要 ≤2000 字符累积进 completed_phases_text。
    轮询通道：_check_phase_acceptance 验收，失败由 route_after_execute 按 on_fail 处置。
    每 phase 独立 CodeAgent（_phase_agents 缓存；重试同 phase 复用实例 = 记忆衔接；
    adapter.close() 只关同步客户端，复用时会惰性重建，安全）。
    """
    agent_instance = ctx.agent
    if not agent_instance:
        logger.error("[Execute] ctx.agent 未设置")
        return {"result_raw": "[run_error] agent 未初始化", "phase_index": len(phases),
                "_phase_abort": True}
    trace = state.get("_trace")
    idx = int(state.get("phase_index", 0) or 0)
    if idx >= len(phases):
        return {"result_raw": state.get("result_raw", "")}
    phase = phases[idx]
    phase_id = int(phase.get("id", idx + 1))
    retry = int(state.get("phase_retry", 0) or 0)

    # ── 正向通道：组装阶段任务书 ──
    # 暂存区 scope（2026-09-12）：一次 run 内稳定，跨阶段读写同一区
    run_scope = "run_" + re.sub(r"[^A-Za-z0-9]", "",
                                str(state.get("_start_time", ""))[-8:]) or "run_default"
    task_parts = []
    task_parts.append(f"【暂存区 scope】{run_scope}")
    # 2026-09-14：用 effective_input（resolver 已把时间/实体事实**内联**进原文），
    # 而不是裸 user_input。只用原始消息时，阶段任务书开头的"用户原始需求"没有日期，
    # 与下方 goal 里的标定日期不一致，模型可能以无日期的那份为准；日志上看也像
    # "时间解析没生效"（实测即被这样误读）。effective_input 是原文的严格扩写，
    # 保底作用不减反增。
    original_input = state.get("effective_input") or state.get("user_input", "")
    if original_input:
        task_parts.append(f"【用户原始需求】{original_input[:800]}")
    # 2026-09-14（用户定调）：已完成阶段**不内联任何内容**，只给一行式清单 +
    # 暂存区取用方式。此前把上一阶段交付物（原文截断/行筛选摘要）拼进任务书，
    # 内联进后续每阶段每一步 ⇒ 平方级 token 膨胀。stage_* 是常驻工具，各阶段
    # 都可用；模型需要明细时自己 stage_read（用户接受这一次调用）。
    prev_results = list(state.get("phase_results") or [])
    if prev_results:
        lines = []
        for r in prev_results:
            fields = (r.get("deliverable") or "")[:120]
            staged = r.get("staged") or ""
            if staged:
                detail = f"交付物含[{fields}]；完整结果: stage_read('{run_scope}', '{staged}')"
            else:
                detail = f"交付物含[{fields}]；结果较短未落盘"
            lines.append(f"- 阶段{r.get('id')} {r.get('name', '')}："
                         f"{'✓通过' if r.get('status') == 'pass' else '✗未通过'}，{detail}")
        task_parts.append("【已完成阶段（本清单不含内容；需要明细时用 stage_read 自取）】\n"
                          + "\n".join(lines))
    task_parts.append(f"【当前阶段 {phase_id}/{len(phases)}：{phase.get('name', '')}】\n{phase.get('goal', '')}")
    deliverable = phase.get("deliverable")
    if deliverable:
        if not isinstance(deliverable, str):
            deliverable = json.dumps(deliverable, ensure_ascii=False)
        task_parts.append(f"【交付物要求】{deliverable}")
    phase_tools = phase.get("tools") or []
    if phase_tools:
        # 带签名（2026-09-12 E2E 实证：只给名字会诱发参数猜测——build_keyword_from_filters
        # (industry=)、bb_screener_scan(keyword=) 连环 TypeError 烧步数）
        def _tool_sig(_n):
            try:
                _fn = ctx.tool_provider.get(_n) if ctx.tool_provider else None
                if _fn is not None:
                    _ps = [p.name for p in inspect.signature(_fn).parameters.values()
                           if not p.name.startswith("_")]
                    return "%s(%s)" % (_n, ", ".join(_ps))
            except Exception:
                pass
            return _n
        task_parts.append("【本阶段可用工具（仅限；括号内为参数名）】" +
                          ", ".join(_tool_sig(n) for n in phase_tools))
    # 技能工具按阶段收窄（2026-09-12 阶段清单升级）：点到名才注入（read_skill_* 常驻）
    _skill_tools = _select_phase_skill_tools(list(state.get("skill_tools", [])), phase_tools)
    if _skill_tools:
        # 带参数签名（2026-09-12）：执行器曾因不见签名连环猜错参数（CLI 实测
        # pre_screen(strategy=) / deep_analyze(candidates=) 连环 TypeError 烧步数）
        _skill_sigs = []
        for _st in _skill_tools:
            _ps = list((getattr(_st, "inputs", None) or {}).keys())
            _skill_sigs.append(f"{getattr(_st, 'name', '?')}({', '.join(_ps)})")
        task_parts.append("【技能工具（可直接调用；括号内为参数名）】" + "；".join(_skill_sigs))
    acceptance = phase.get("acceptance") or []
    if acceptance:
        task_parts.append("【验收标准】\n" + "\n".join(f"- {a}" for a in acceptance))
    if retry:
        task_parts.append(f"【重试提示】上一轮未通过验收：{state.get('phase_last_note', '')}；"
                          "本轮聚焦补齐缺失项，不要重复已完成的工作。")
    task_parts.append("【数据纪律】多标的同类数据一次拉全：支持批量的工具用逗号分隔 codes 一次调用，"
                      "或在单个代码块内循环完成（只打印提炼后的关键字段）；禁止逐只分步取数。"
                      "跨阶段重数据用暂存区工具 stage_write(scope, name, content) / "
                      "stage_read(scope, name)（本阶段 scope 见上）：content 传**原对象**（dict/list）"
                      "即可，stage_read 直接返回该对象、无需 json.loads；纯文本（.md）照旧传字符串。"
                      "沙箱内只存在【本阶段可用工具】清单所列函数——不存在 create_file/read_file 等任何"
                      "文件工具，调用必然失败；代码类任务的交付物=完整代码文本（直接在最终答复中给出）。")
    full_task = "\n\n".join(task_parts)

    # ── 每 phase 独立 CodeAgent（重试复用实例）──
    agents = dict(state.get("_phase_agents") or {})
    agent = agents.get(str(phase_id))
    # 阶段级预算（2026-09-13 契约补全）：优先取契约 phases[].step_budget，缺失回退全局。
    # 旧实现所有阶段共用全局 step_budget → "取数 3 步 / 分析 8 步"这类差异无法表达（审计 G3）。
    phase_budget = int(phase.get("step_budget") or 0) or int(state.get("step_budget", 10) or 10)
    # 内部 planner 开关（2026-09-13）：由外部 planner 在契约里显式声明 internal_plan。
    # 旧实现按 selected_skill 判断——技能层清空后该判据恒真，内部 planner 全程常开，
    # 与"简单指令关闭内部 planner"的设计意图正好相反（审计 G2）。未声明时按预算判复杂度。
    _need_internal = phase.get("internal_plan")
    if _need_internal is None:
        _need_internal = phase_budget >= 8
    if agent is None:
        effective_interval = max(2, min(phase_budget // 2, 6)) if _need_internal else None
        agent = agent_instance._build_code_agent(
            model=ctx.model,
            provider=ctx.tool_provider,
            skill_tools=list(_skill_tools),
            planning_interval=effective_interval,
            phase_id=phase_id,
            domain=state.get("selected_domain", ""),
            tools=(phase.get("tools") or None),
            step_event_cb=getattr(ctx, "event_cb", None),
            run_scope=run_scope,
        )
        agents[str(phase_id)] = agent
        logger.info("[Execute] phase #%d 新建 CodeAgent（白名单 %d 个工具，内部规划 %s）",
                    phase_id, len(phase.get("tools") or []),
                    "on" if effective_interval else "off")
    else:
        logger.info("[Execute] phase #%d 复用 CodeAgent（重试）", phase_id)

    agent.max_steps = phase_budget
    _set_llm_timeout(agent, 180)

    logger.info("[Execute] phase #%d/%d '%s' 开始，step_budget=%d，内部规划=%s",
                phase_id, len(phases), phase.get("name", ""), agent.max_steps,
                "on" if _need_internal else "off")
    if trace:
        trace.record("phase_start", {
            "phase_id": phase_id, "name": phase.get("name", ""),
            "retry": retry, "tools": phase.get("tools") or [],
            # 规划分工决策留痕（2026-09-13）：事后可对账"该阶段有没有开内部 planner、
            # 预算多少"，否则 G2 那类判据失效的问题无法从 trace 里发现。
            "step_budget": agent.max_steps, "internal_plan": bool(_need_internal),
        })
    react_start = time.time()
    result, run_error, interrupted = _run_agent_with_guard(agent, full_task)
    react_elapsed = round(time.time() - react_start, 2)
    logger.info("[Execute] phase #%d 完成，耗时 %.1fs", phase_id, react_elapsed)

    # 步数耗尽的强制答案常是半成品（run3 实测：脏代码污染下阶段任务书）
    # → 改取最后一段干净执行输出作为阶段结果（验收与交接都用它）
    # 引擎级兜底：未置位 final_answer 标记同样按"未正常收尾"处理（模型偶发忘调）
    if not interrupted and (_detect_max_steps(agent) or not _has_final_answer(agent)):
        _clean = _extract_clean_phase_result(agent, str(result))
        if _clean and _clean != str(result):
            logger.info("[Execute] phase #%d 未正常收尾，改用最后干净输出（%d 字符）",
                        phase_id, len(_clean))
            result = _clean

    # 收尾：关闭 adapter 同步客户端（防 httpx 连接泄漏；复用实例时惰性重建）
    try:
        model_adapter = getattr(agent, "model", None)
        if model_adapter is not None and hasattr(model_adapter, "close"):
            model_adapter.close()
    except Exception as e:
        logger.debug("[Execute] 关闭 LLM adapter 客户端失败: %s", e)

    # 收尾：清理 CodeAgent.executor/python_executor（2026-09-13：smolagents
    # LocalPythonExecutor 内部 ThreadPoolExecutor(max_workers=1) 的工作线程在超时时
    # 不被杀，只抛 TimeoutError 给主线程，导致进程不退出。显式清空引用触发资源回收。）
    try:
        for _attr in ("executor", "python_executor"):
            _exec = getattr(agent, _attr, None)
            if _exec is None:
                continue
            if hasattr(_exec, "custom_tools"):
                _exec.custom_tools.clear()
            if hasattr(_exec, "state") and isinstance(_exec.state, dict):
                for _k in list(_exec.state.keys()):
                    if not _k.startswith("__"):
                        _exec.state.pop(_k, None)
            if hasattr(_exec, "cleanup"):
                _exec.cleanup()
            elif hasattr(_exec, "shutdown"):
                _exec.shutdown(wait=False)
            logger.debug("[Execute] phase #%d %s 已清理", phase_id, _attr)
    except Exception as e:
        logger.debug("[Execute] 清理 executor 失败: %s", e)

    if trace:
        _record_tool_calls_to_trace(trace, agent)
    failed_tools = _extract_failed_tools(agent, ctx.tool_provider)

    # ── 轮询通道：验收判定 ──
    if interrupted:
        passed, note, reason, score = False, "用户中断", "agent_fault", None
    else:
        passed, note, reason, score = await _check_phase_acceptance(ctx, phase, result, run_error)
        # 护栏（防误判/手软）：判官称"工具/数据导致"但本阶段无任何工具/数据层失败的
        # 实锤证据时，回退为 agent_fault，保住验收意义——不因而放过该重试的环节。
        if not passed and reason == "tool_data_fault" \
                and not _tool_data_evidence(str(result), failed_tools, run_error):
            reason = "agent_fault"

    logger.info("[Execute] phase #%d 验收: %s（归因 %s，评分 %s） %s", phase_id,
                "通过" if passed else "未通过", reason, score, note[:120])

    # 推进决策：验收通过 或"工具/数据不可用导致部分缺失(软通过)"→ 推进管道；
    # 仅 agent 可控失败才按 on_fail 重试/重设计。决策抽为可测函数（见 _decide_phase_transition）。
    _kind, new_retry, do_replan, soft_pass = _decide_phase_transition(
        passed, reason,
        phase.get("on_fail", "retry"),
        retry,
        int(phase.get("max_retries", 1) or 1),
        int(state.get("phase_replan_count", 0) or 0),
    )
    new_idx = idx + 1 if _kind == "advance" else idx
    replan_count = int(state.get("phase_replan_count", 0) or 0)
    if _kind == "replan":
        replan_count = replan_count + 1
    effective_pass = passed or reason == "tool_data_fault"

    summary = _phase_summary(phase, result)
    # 暂存区引用回填：结果中提到 stage_write 的文件 → 在摘要尾部列出（供下阶段 stage_read）
    try:
        if "stage_write" in str(result):
            import re as _re
            _hits = sorted(set(_re.findall(r"([\w.\-]+\.(?:json|md|txt))", str(result)[:6000])))
            if _hits:
                summary += f"\n（暂存区文件: {', '.join(_hits[:8])}——下阶段可用 stage_read(scope, name) 读取）"
    except Exception:
        pass
    # 强制机制：摘要截断前的重数据由框架自动落盘，不依赖模型自觉 stage_write
    _staged = _auto_stage_phase_result(run_scope, phase, result)
    if _staged:
        summary += (f"\n（阶段完整结果已自动存入暂存区 {_staged}，"
                    f"下阶段可用 stage_read('{run_scope}', '{_staged}') 直接读取）")
    entry = {
        "id": phase_id, "name": phase.get("name", ""),
        "status": "pass" if effective_pass else "fail",
        "partial": soft_pass,
        "reason": reason,
        "score": score,
        "note": note[:300], "elapsed": react_elapsed,
        "preview": str(result)[:300],
        # 2026-09-14：供下阶段任务书生成"已完成阶段清单"——只给文件名与字段说明，
        # 不内联内容（用户定调）。deliverable = planner 写的交付物描述（字段说明）。
        "staged": _staged,
        "deliverable": str(phase.get("deliverable", ""))[:200],
    }
    results = list(state.get("phase_results") or []) + [entry]
    line = f"[阶段{phase_id} {phase.get('name', '')}] {'✓通过' if passed else '✗未通过'}：{summary}"
    # 2026-09-14 回归修复：任务书组装处不再读 completed_phases_text（零内联改造时
    # 误删了 done_text 定义），但 state 累积本身保留——复盘/重规划通道可能消费。
    done_text = str(state.get("completed_phases_text", "") or "")
    done_new = (done_text + "\n\n" + line).strip() if done_text else line

    # 注：new_idx / new_retry / replan_count / do_replan / soft_pass / effective_pass
    # 已在上方"推进决策"段据 _decide_phase_transition 计算，此处不再重复。

    if trace:
        trace.record("phase_done", {
            "phase_id": phase_id, "name": phase.get("name", ""),
            "status": entry["status"], "retries": retry,
            "accept_reason": reason, "accept_score": score, "partial": soft_pass,
            "on_fail": phase.get("on_fail", "retry"),
            "acceptance_note": note[:500], "elapsed_seconds": react_elapsed,
            "tools_whitelist": phase.get("tools") or [],
            "result_preview": str(result)[:200],
        })

    return {
        "result_raw": str(result),
        "hit_max_steps": False,
        "phase_index": new_idx,
        "phase_retry": new_retry,
        "phase_replan_count": replan_count,
        "phase_results": results,
        "completed_phases_text": done_new,
        "phase_last_note": "" if passed else (note[:500] or "未通过验收"),
        "_phase_agents": {k: v for k, v in agents.items()
                          if k.isdigit() and int(k) >= new_idx},
        "_phase_abort": bool(interrupted),
        "_phase_replan_request": do_replan,
        "_failed_tools": failed_tools,
        "_agent_plan": "",
        "_run_error": repr(run_error) if run_error else "",
    }


def make_execute_node(ctx: NodeContext):
    """创建 execute_node（闭包捕获 ctx）。"""

    async def execute_node(state: dict) -> dict:
        """执行：单次 CodeAgent 跑完任务，planning_interval 内部进度检查。

        phase 模式（B 阶段 2026-09-12）：state.phases 非空时本节点降为"单阶段执行器"——
        每次执行一个 phase（任务书 = goal + deliverable + acceptance + 已完成摘要），
        验收后由 route_after_execute 循环推进；全部完成才进 finalize。
        """
        phases = state.get("phases") or []
        if phases:
            return await _run_phase_step(ctx, state, phases)

        agent_instance = ctx.agent
        if not agent_instance:
            logger.error("[Execute] ctx.agent 未设置")
            return {"result_raw": "[run_error] agent 未初始化", "hit_max_steps": True, "_run_error": "agent not initialized"}

        task = state.get("task", "")
        if not task:
            return {"result_raw": "[run_error] 无任务描述", "hit_max_steps": False, "_run_error": "empty task"}

        step_budget = state.get("step_budget", 10)
        planning_interval = state.get("planning_interval", 6)
        trace = state.get("_trace")

        # 构建上下文
        # 实体不在此重复注入（2026-09-11 去冗余，审计 P2）：resolver 的 effective_input
        # 已含实体 → plan task 基于 effective_input 生成 → 【任务】段已带实体；原始关键词
        # 由下方【用户原始输入】段保底。旧实现同一标的信息最多出现 3 次，纯 token 浪费。
        # state.entity_code 仍保留给 trace/finalize 写 qd_traces 使用，勿删字段。
        task_parts = []
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
        # 2026-09-14：改用 effective_input（原文的严格扩写，已内联时间/实体事实）。
        # 注释所述"Planner 可能重新描述任务、丢失关键词"的保底作用完全保留，
        # 且不会给出无日期的版本（与阶段模式任务书口径一致）。
        original_input = state.get("effective_input") or state.get("user_input", "")
        if original_input and original_input != task:
            task_parts.append(f"【用户原始输入】{original_input}")

        task_parts.append(f"【任务】\n{task}")

        # 附加点名工具（2026-09-13）：给"名字 + 参数签名"而不是裸名字。与阶段任务书同规则
        # （2026-09-12 E2E 实证：只给名字会诱发参数猜测，连环 TypeError 烧步数）——两条
        # 执行路径若在此处不一致，行为就会漂移。这里是单段任务接触 capabilities 的唯一入口。
        plan_tools = list(state.get("plan_tools") or [])
        if plan_tools:
            _pt_sigs = []
            for _n in plan_tools:
                _ft = ctx.tool_provider.get(_n) if ctx.tool_provider else None
                _sig = _n
                if _ft is not None:
                    try:
                        _ps = [p.name for p in inspect.signature(_ft).parameters.values()
                               if not p.name.startswith("_")]
                        _sig = "%s(%s)" % (_n, ", ".join(_ps))
                    except Exception:
                        pass
                _pt_sigs.append(_sig)
            task_parts.append("【附加点名工具（可直接调用；括号内为参数名）】" + ", ".join(_pt_sigs))
            logger.info("[Execute] 附加点名工具 %d 个: %s", len(_pt_sigs), plan_tools[:12])

        full_task = "\n\n".join(task_parts)

        # 复用已有 CodeAgent 实例（跨轮 memory 自然衔接）——但实例是与"工具契约"
        # （域 + 附加点名）绑定的：复盘重规划若改变了二者，复用旧实例会导致沙箱里
        # 根本没有新点名的工具，而任务书却明写着"可直接调用"→ 模型照说明调用即
        # Forbidden，被误报成"幻觉调用"（与 stage_write 漏注入是同一类坑）。
        # 契约变了就重建：牺牲一次跨轮 memory，换取工具面与任务书一致。
        _contract = (selected_domain, tuple(plan_tools))
        agent = state.get("_code_agent")
        if agent is not None and getattr(agent, "_tool_contract", None) != _contract:
            logger.info("[Execute] 工具契约变化 %s → %s，重建 CodeAgent 实例",
                        getattr(agent, "_tool_contract", None), _contract)
            agent = None
        if agent is None:

            # 内部 planner 开关（2026-09-13）：单段路径无阶段契约，按预算判复杂度——
            # 预算小视为"简单指令"，直接关闭 smolagents 内部 planner（外部 plan 已定范围）。
            # 旧实现按 selected_skill 判断，技能层清空后判据恒真、内部 planner 恒开（审计 G2）。
            effective_interval = planning_interval if step_budget >= 8 else None

            # 本次 run 的暂存区 scope（与 _run_phase_step 同规则派生）
            run_scope = "run_" + re.sub(r"[^A-Za-z0-9]", "",
                                        str(state.get("_start_time", ""))[-8:]) or "run_default"
            agent = agent_instance._build_code_agent(
                model=ctx.model,
                provider=ctx.tool_provider,
                skill_tools=skill_tools,
                planning_interval=effective_interval,
                phase_id=0,
                domain=selected_domain,
                step_event_cb=getattr(ctx, "event_cb", None),
                run_scope=run_scope,
                extra_tools=plan_tools or None,
            )
            agent._tool_contract = _contract
            logger.info("[Execute] 新建 CodeAgent 实例（工具契约 %s）", _contract)
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

        hit_max_steps = False
        result, run_error, _interrupted = _run_agent_with_guard(agent, full_task)

        # smolagents>=1.27 到达 max_steps 不再抛异常：_handle_max_steps_reached()
        # 会强制生成 final answer 正常返回，仅在 memory 最后一步标记 AgentMaxStepsError。
        # 旧实现靠捕获异常字符串 "max_steps"/"maximum" 判定，在该版本下永不触发，
        # 复盘循环（hit_max_steps → plan 重规划）因此成为死代码（审计 P1-1）。
        try:
            from smolagents.utils import AgentMaxStepsError
            last_err = getattr(getattr(agent, "memory", None), "steps", [None])[-1]
            last_err = getattr(last_err, "error", None)
            if isinstance(last_err, AgentMaxStepsError):
                logger.info("[Execute] max_steps 耗尽（memory 标记），需复盘")
                hit_max_steps = True
                result = f"[max_steps 耗尽] 已执行 {getattr(agent, 'step_number', '?')} 步，交由复盘循环继续"
        except ImportError:
            # 旧版 smolagents 仍走异常路径
            if run_error is not None:
                error_str = str(run_error).lower()
                if "max_steps" in error_str or "maximum" in error_str:
                    logger.info("[Execute] max_steps 耗尽（异常捕获），需复盘")
                    hit_max_steps = True
                    result = f"[max_steps 耗尽] {run_error}"

        react_elapsed = round(time.time() - react_start, 2)
        logger.info("[Execute] 完成，耗时 %.1fs，hit_max_steps=%s", react_elapsed, hit_max_steps)

        # ── 收尾：关闭 _LLMAdapter 的同步 OpenAI 客户端（防 httpx 连接泄漏）──
        # 注意只关 adapter 层客户端；底层共享 LLM 实例的生命周期由调用方管理，这里不动。
        try:
            model_adapter = getattr(agent, "model", None)
            if model_adapter is not None and hasattr(model_adapter, "close"):
                model_adapter.close()
        except Exception as e:
            logger.debug("[Execute] 关闭 LLM adapter 客户端失败: %s", e)

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
            "_run_error": repr(run_error) if run_error else "",  # 执行异常标记（含 LLM 5xx），finalize 据此判定 run 失败
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
        # 缓存格式化前的原始 CodeAgent 输出：trace 结构化字段提取必须用原始结果，
        # 否则 LLM 格式化版式一变，score/direction 的 regex 提取随之失效（审计 P1-3）。
        raw_agent_output = state.get("result_raw", "") or direct_answer or ""
        # run 失败判定（P1-3 配套）：执行异常（含 LLM 网关 5xx）的 run 只留错误痕迹，
        # 不作为"成功分析"进入 qd_traces 提取/回测统计——否则一次网关故障会造出一条
        # direction="neutral"、confidence=0.5 的伪决策记录参与权重训练（root_id=1737 事故）。
        # 直接回答（chat）路径无 result_raw，不算失败。
        # 双通道判定：_run_error 显式标记 + 错误前缀兜底（防未来新分支漏标）。
        _RUN_ERROR_PREFIXES = ("[run_error]", "[错误]", "[max_steps 耗尽]")
        result_text = state.get("result_raw", "") or ""
        run_failed = (
            (bool(state.get("_run_error")) and bool(result_text))
            or (result_text.startswith(_RUN_ERROR_PREFIXES) and not direct_answer)
        )
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

        # ── 4b. 阶段部分达标说明（工具/数据不可用，未无谓重试/放弃）──
        _phase_results = state.get("phase_results") or []
        _partial = [p for p in _phase_results if p.get("partial")]
        if _partial:
            _p_lines = []
            for _p in _partial:
                _sc = _p.get("score")
                _sc_txt = f"（评分 {_sc}/100）" if isinstance(_sc, int) else ""
                _p_lines.append(f"- 阶段{_p.get('id')} {_p.get('name', '')}{_sc_txt}：{str(_p.get('note', ''))[:200]}")
            result_raw = (result_raw + "\n\n【阶段部分达标】以下阶段因工具/数据不可用仅部分完成，"
                          "已保留已产出结果并继续（未无谓重试/放弃）：\n" + "\n".join(_p_lines))

        # ── 5. 结果格式化（仅 task 模式 + 有工具产出时）──
        needs_task = state.get("needs_task", True)
        has_tool_output = bool(state.get("result_raw")) and not run_failed  # 错误结果不需要 LLM 格式化
        if needs_task and has_tool_output and not selected_skill:
            try:
                from formatters.base import get_formatter
                entity_type = state.get("entity_type", "")
                # domain 优先匹配（领域级标准输出，多领域共用模板），
                # entity_type 为次级键（领域内单实体定制）。2026-09-13 修复：
                # 此前只传 entity_type="stock"，而注册方用领域名 "finance" → 永不命中。
                formatter = get_formatter(entity_type,
                                          domain=state.get("selected_domain", ""))
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
                    final_answer=raw_agent_output if not run_failed else None,  # 失败 run 不做决策提取
                    status="error" if run_failed else "success",
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

        # 任务收尾：清空本次 run 的暂存区（内存对象区 + 字符串区），防长流程内存泄漏
        try:
            from infra.staging import stage_clear
            _scope = "run_" + re.sub(r"[^A-Za-z0-9]", "",
                                     str(state.get("_start_time", ""))[-8:]) or "run_default"
            stage_clear(_scope)
        except Exception as e:
            logger.debug("[Finalize] 清空暂存区失败: %s", e)

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


MAX_REPLAN = 2


def route_after_execute(state: dict) -> str:
    """execute_node 之后的路由。

    phase 模式（B 阶段）：state.phases 非空时，是否重试同阶段 / 推进下一阶段 /
    回 plan 重设计，已由 execute_node 内部根据 (passed / retry 额度 / on_fail)
    判定好，本函数只做收口——
      - 中断 → finalize
      - 收到重设计请求(_phase_replan_request) → plan
      - 游标越过末尾(phase_index >= len(phases)) → finalize（全部阶段完成）
      - 否则 → execute（跑 phases[phase_index]）
    关键修正：单阶段验收失败且重试/重设计额度耗尽时，execute_node 选择"推进下一
    阶段"而非 abort——否则任一阶段验收不可达就会吞掉后续所有阶段的产出，整个多
    阶段管道在第一个阶段就死掉（实测"明天买什么股?" 卡在阶段1 直接 finalize）。
    旧路径（无 phases）：保持 max_steps 复盘逻辑不变。
    """
    phases = state.get("phases") or []
    if phases:
        if state.get("_phase_abort"):
            return "finalize"
        if state.get("_phase_replan_request"):
            return "plan"
        idx = int(state.get("phase_index", 0) or 0)
        if idx >= len(phases):
            return "finalize"    # 全部阶段完成
        return "execute"         # 执行 phases[idx]（重试/推进由节点内部判定）

    if not state.get("hit_max_steps", False):
        return "finalize"        # CodeAgent 完成或正常结束
    if state.get("replan_count", 0) >= MAX_REPLAN:
        return "finalize"        # 复盘次数用完
    return "plan"                 # max_steps 耗尽，回 plan 复盘
