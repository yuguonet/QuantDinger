# -*- coding: utf-8 -*-
"""
State — LangGraph State 定义。

替代 checkpoint.py 的 LoopState / StepRecord。
LangGraph 的 State 是 TypedDict，通过 Annotated reducers 自动合并。
"""
from __future__ import annotations

from operator import add
from typing import Any, Dict, List, Optional, TypedDict, Annotated


class StepRecord(TypedDict, total=False):
    """单步执行记录。"""
    step: int
    skill: Optional[str]
    description: str
    tools: List[str]
    rules: str
    planner_reasoning: str
    step_content: str
    step_success: bool
    steps_used: int
    step_tokens: int
    tool_calls: List[Dict[str, Any]]
    charts: List[str]


class AgentState(TypedDict, total=False):
    """LangGraph 主状态。所有节点共享读写。"""

    # ── 对话历史（LangGraph Checkpointer 自动持久化）────────
    messages: Annotated[List[Dict[str, str]], add]  # [{role, content}, ...]

    # ── 用户输入 ──────────────────────────────────────────────
    query: str
    stock_code: str
    stock_name: str
    domain: str
    intent: Dict[str, Any]              # IntentResult 序列化

    # ── 执行状态 ──────────────────────────────────────────────
    step_records: List[StepRecord]  # 每轮覆盖（非追加），避免跨轮污染
    current_tools: List[str]            # Planner 当轮选的工具
    current_skill: Optional[str]        # Planner 当轮选的 skill
    cached_tools: Optional[List[str]]   # qd_traces 命中的缓存工具链（跳过 LLM#2）

    # ── 控制流 ────────────────────────────────────────────────
    loop_step: int                      # 当前循环步数
    max_loop_steps: int                 # 最大循环步数（默认 10）
    should_continue: bool               # 是否继续（快速通道跳转用）
    all_phases_completed: bool

    # ── 输出 ──────────────────────────────────────────────────
    final_output: Dict[str, Any]        # 最终结构化金融 JSON
    total_steps: int
    total_tokens: int
    tool_calls_log: List[Dict[str, Any]]  # 每轮覆盖
    charts: List[str]  # 每轮覆盖

    # ── 元数据 ────────────────────────────────────────────────
    session_id: str
    user_id: str
    strategy: str                       # direct / traced
    collector: Any                      # TraceCollector（运行时对象，不序列化）
    intent_verb: str
    intent_noun: str
    domain_instructions: str
    # ── 跨轮元数据（checkpointer 自动持久化）─────────────
    last_verb: str                      # 上一轮 verb（反馈闭环用）
    last_noun: str                      # 上一轮 noun（反馈闭环用）


class AgentResult:
    """执行结果（公开接口，供 evaluator / cron 等模块使用）。"""
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


def create_initial_state(
    query: str,
    session_id: str,
    user_id: str = "1",
    stock_code: str = "",
    stock_name: str = "",
    domain: str = "",
    strategy: str = "direct",
    max_loop_steps: int = 10,
    **kwargs,
) -> AgentState:
    """创建初始状态。"""
    return AgentState(
        query=query,
        stock_code=stock_code,
        stock_name=stock_name,
        domain=domain,
        intent={},
        step_records=[],
        current_tools=[],
        current_skill=None,
        cached_tools=None,

        loop_step=0,
        max_loop_steps=max_loop_steps,
        should_continue=True,
        all_phases_completed=False,
        final_output={},
        total_steps=0,
        total_tokens=0,
        tool_calls_log=[],
        charts=[],
        session_id=session_id,
        user_id=user_id,
        strategy=strategy,
        collector=None,
        intent_verb="",
        intent_noun="",
        domain_instructions="",
        messages=[],
        last_verb="",
        **kwargs,
    )
