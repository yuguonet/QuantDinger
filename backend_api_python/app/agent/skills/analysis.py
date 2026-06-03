# -*- coding: utf-8 -*-
"""
Analysis skill — 个股分析全流程专家。

负责：行情→技术面→形态→量能→情报→综合判断。
"""
from app.agent.skills.registry import skill


@skill(
    name="analysis_agent",
    description="股票分析专家。负责个股分析：行情→技术面→形态→量能→情报→综合判断。当用户询问某只股票的分析时调用。",
    instructions="你是技术分析专家。按行情→形态→情报→分析流程执行。优先用 get_indicator_snapshot 一次获取全部指标。必须调用工具获取真实数据。",
    tools=[
        "get_realtime_quote", "agent_get_kline", "get_stock_info",
        "generate_kline_chart",
        "analyze_trend", "get_indicator_snapshot",
        "calculate_ma", "get_volume_analysis", "analyze_pattern",
        "get_chip_distribution", "search_stock_news", "search_comprehensive_intel",
        "resolve_stock_name", "search_stock_by_name",
        "get_market_indices", "get_sector_rankings",
        "get_fund_flow",
    ],
    priority=10,
)
class AnalysisSkill:
    """股票分析专家子 Agent。"""
    pass
