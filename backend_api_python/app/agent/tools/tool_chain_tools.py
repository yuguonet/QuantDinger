# -*- coding: utf-8 -*-
"""
Tool Chain Tools — agent 自主维护工具链的工具。

agent 执行完任务后，可通过这些工具：
- 读取当前工具链
- 验证执行效果
- 写回优化后的工具链
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from app.agent.tools.registry import tool

logger = logging.getLogger(__name__)


@tool(
    description="读取指定场景的工具链配置。用于查看当前场景建议使用哪些工具及顺序。",
    category="工作区",
    layer="支撑层",
)
def read_tool_chain(verb: str, noun: str) -> Dict[str, Any]:
    """读取指定场景的工具链。

    Args:
        verb: 动作类别（analyze/view/modify/create/filter/backtest/execute/explain/query）
        noun: 对象类别（stock/chart/market/code/project/indicator/strategy/fund_flow/trading/screener/concept）
    """
    from app.agent.router.tool_chains import get_tool_chain
    chain = get_tool_chain(verb, noun)
    return {
        "key": f"{verb}+{noun}",
        "chain": chain,
        "has_chain": len(chain) > 0,
    }


@tool(
    description="保存工具链配置。执行任务成功后，将验证过的工具调用顺序写回配置，供下次使用。",
    category="工作区",
    layer="支撑层",
)
def write_tool_chain(verb: str, noun: str, chain: List[Dict[str, str]]) -> Dict[str, Any]:
    """保存工具链（执行验证后写回）。

    Args:
        verb: 动作类别
        noun: 对象类别
        chain: 工具列表，格式 [{"tool": "工具名", "desc": "用途"}, ...]
    """
    from app.agent.router.tool_chains import save_tool_chain
    save_tool_chain(verb, noun, chain)
    return {
        "key": f"{verb}+{noun}",
        "saved": True,
        "steps": len(chain),
    }


@tool(
    description="列出所有已配置的工具链。用于查看系统支持哪些场景。",
    category="工作区",
    layer="支撑层",
)
def list_tool_chains() -> Dict[str, Any]:
    """列出所有已配置的工具链。"""
    from app.agent.router.tool_chains import list_all_chains
    all_chains = list_all_chains()
    return {
        "total": len(all_chains),
        "chains": {k: [s["tool"] for s in v] for k, v in all_chains.items()},
    }
