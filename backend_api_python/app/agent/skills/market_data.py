# -*- coding: utf-8 -*-
"""
Market Data skill — 行情数据专家。

负责：实时行情、K线数据、指数、板块排名、资金流向。
"""
from app.agent.skills.registry import skill


@skill(
    name="market_data_agent",
    description="行情数据专家。负责实时行情查询、K线数据获取、大盘指数、板块排名、资金流向。当用户问行情、报价、指数、板块、资金流向时调用。",
    instructions="你是行情数据专家。快速准确地获取和呈现市场数据。优先用 get_realtime_quote 获取实时报价，agent_get_kline 获取K线。大盘走势先看 get_market_indices，板块动向看 get_sector_rankings。",
    tools=[
        "get_realtime_quote", "agent_get_kline", "get_stock_info",
        "resolve_stock_name", "search_stock_by_name",
        "get_market_indices", "get_sector_rankings",
        "get_market_overview",
        "get_fund_flow", "get_sector_fund_flow", "get_concept_fund_flow",
    ],
    priority=10,
)
class MarketDataSkill:
    """行情数据专家子 Agent。"""
    pass
