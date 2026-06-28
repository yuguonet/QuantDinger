# -*- coding: utf-8 -*-
"""
Agent Evaluator — 执行记录（精简版）。

原"编排路径学习闭环"已移除：
  tool_chains.json 的读写链路断了（Planner 从来不读），
  改用 qd_traces 的 correct 字段天然过滤。

当前职责：
  - 记录日志（verb+noun+success+tools）
  - TraceCollector 的 qd_traces 写入不受影响（闭环#1）

闭环#2（T+N 回测）在 chain/evaluator.py，独立运行。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def learn_from_execution(
    agent_result,
    verb: str,
    noun: str,
    chain_def=None,
    all_phases_completed=None,
):
    """记录执行结果日志。

    tool_chains.json 写入已移除。链路缓存改用 qd_traces（store.query_cached_tools），
    质量由 T+N 回测的 correct 字段保证。

    Args:
        agent_result: AgentResult 实例
        verb: 意图动词
        noun: 意图对象
        chain_def: 保留参数（兼容调用方）
        all_phases_completed: 保留参数（兼容调用方）
    """
    if not verb or not noun:
        return

    success = bool(agent_result.success)
    steps_taken = agent_result.total_steps or 0
    tool_calls_log = list(agent_result.tool_calls_log or [])

    actual_tools = []
    for tc in tool_calls_log:
        tool_name = tc.get("tool", "")
        if tool_name and tool_name != "final_answer":
            actual_tools.append(tool_name)

    logger.info(
        "[Learn] %s+%s success=%s steps=%d tools=%d: %s",
        verb, noun, success, steps_taken,
        len(actual_tools), actual_tools,
    )
