# -*- coding: utf-8 -*-
"""
Tool Chain Tools — 已废弃，chain 系统已移除。
保留空壳避免 import 错误。
"""
from __future__ import annotations
from typing import Any, Dict, List


def read_tool_chain(verb: str, noun: str) -> Dict[str, Any]:
    """读取指定 verb+noun 的工具链配置。

    Args:
        verb: 动作类别，如 "分析"、"查询"、"推荐"
        noun: 对象类别，如 "股票"、"行情"、"板块"
    """
    return {"key": f"{verb}+{noun}", "chain": [], "has_chain": False}


def write_tool_chain(verb: str, noun: str, chain: List[Dict[str, str]]) -> Dict[str, Any]:
    """保存指定 verb+noun 的工具链配置。

    Args:
        verb: 动作类别
        noun: 对象类别
        chain: 工具链步骤列表
    """
    return {"key": f"{verb}+{noun}", "saved": False, "reason": "chain system removed"}


def list_tool_chains() -> Dict[str, Any]:
    return {"total": 0, "chains": {}}
