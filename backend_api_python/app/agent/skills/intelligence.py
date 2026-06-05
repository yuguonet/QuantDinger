# -*- coding: utf-8 -*-
"""
Intelligence skill — 情报分析专家。

负责：新闻搜索、综合情报、舆情分析。
"""
from app.agent.skills.registry import skill


@skill(
    name="intelligence_agent",
    description="情报分析专家。负责个股新闻搜索、综合情报收集、舆情分析。当用户问新闻、消息面、情报、舆情时调用。",
    instructions="你是情报分析专家。先用 search_stock_news 搜新闻，再用 search_comprehensive_intel 做综合情报分析。重点关注影响股价的政策、公告、行业动态。数据不足时明确告知，不编造。",
    tools=[
        "search_stock_news", "search_comprehensive_intel",
        "resolve_stock_name", "search_stock_by_name",
    ],
    priority=7,
)
class IntelligenceSkill:
    """情报分析专家子 Agent。"""
    pass
