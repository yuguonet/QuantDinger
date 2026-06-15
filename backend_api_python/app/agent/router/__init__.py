# -*- coding: utf-8 -*-
"""
Router 包 — 工具链管理。

仅保留 tool_chains 模块，语义路由已废弃。
"""
from app.agent.router.tool_chains import (
    get_tool_chain,
    save_tool_chain,
    get_chain_stats,
    update_chain_stats,
    list_all_chains,
    detect_feedback_severity,
    penalize_chain,
)

__all__ = [
    "get_tool_chain",
    "save_tool_chain",
    "get_chain_stats",
    "update_chain_stats",
    "list_all_chains",
    "detect_feedback_severity",
    "penalize_chain",
]
