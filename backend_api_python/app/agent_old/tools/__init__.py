# -*- coding: utf-8 -*-
"""
Tools — 工具包。

40+ 工具通过 @tool 装饰器自动注册，agent.py 运行时自动发现。
转换为 smolagents Tool 对象由 tool_adapter.py 处理。

工具分层（layer）：
  数据层 — data_tools / quote_tools / market_tools / market_data_tools
  分析层 — analysis_tools / indicator_tools / signal_tools / news_search_tools / research_tools / sector_analysis_tools
  决策层 — screening_tools / screener_tools / backtest_tools / capital_tools
  执行层 — trading_tools
  显示层 — chart_tools
  支撑层 — code_workspace_tools / iteration_tools / tool_chain_tools / scan_tools
"""
# 注册翻页工具
try:
    from app.agent.tools.pagination import register_page_tool
    register_page_tool()
except Exception as _e:
    import logging
    logging.getLogger(__name__).warning("[Tools] 注册翻页工具失败: %s", _e)
