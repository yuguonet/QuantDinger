# -*- coding: utf-8 -*-
"""
Agent Evaluator — 执行记录（精简版）。

当前职责：
  - 记录日志（verb+noun+success+tools）

闭环#2（T+N 回测）在 chain/evaluator.py，独立运行。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def learn_from_execution(
    agent_result,
    verb: str,
    noun: str,
    chain_def=None,
    all_phases_completed=None,
):
    """记录执行结果日志。

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
