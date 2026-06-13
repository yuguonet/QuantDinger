# -*- coding: utf-8 -*-
"""
工具链定义 — 根据 verb+noun 返回建议的 call_skill 调用顺序。
"""

# 工具链模板：(verb, noun) → [步骤列表]
_TOOL_CHAINS = {
    ("analyze", "stock"): [
        {"tool": "search_stock_by_name", "desc": "查找股票代码", "args": {}},
        {"tool": "call_skill", "desc": "技术面分析", "args": {"skill_name": "technical_agent"}},
        {"tool": "call_skill", "desc": "指标信号分析", "args": {"skill_name": "indicator_agent"}},
        {"tool": "call_skill", "desc": "情报分析", "args": {"skill_name": "intelligence_agent"}},
    ],
    ("analyze", "chart"): [
        {"tool": "search_stock_by_name", "desc": "查找股票代码", "args": {}},
        {"tool": "call_skill", "desc": "技术面分析", "args": {"skill_name": "technical_agent"}},
    ],
    ("filter", "market"): [
        {"tool": "call_skill", "desc": "市场数据分析", "args": {"skill_name": "market_data_agent"}},
    ],
    ("backtest", "stock"): [
        {"tool": "search_stock_by_name", "desc": "查找股票代码", "args": {}},
        {"tool": "call_skill", "desc": "回测分析", "args": {"skill_name": "backtest_agent"}},
    ],
    ("analyze", "indicator"): [
        {"tool": "search_stock_by_name", "desc": "查找股票代码", "args": {}},
        {"tool": "call_skill", "desc": "指标信号分析", "args": {"skill_name": "indicator_agent"}},
    ],
    ("query", "fund_flow"): [
        {"tool": "search_stock_by_name", "desc": "查找股票代码", "args": {}},
        {"tool": "call_skill", "desc": "市场资金数据", "args": {"skill_name": "market_data_agent"}},
    ],
}


def get_tool_chain(verb: str, noun: str) -> list:
    """根据动词+名词返回工具链。"""
    return _TOOL_CHAINS.get((verb, noun), [])
