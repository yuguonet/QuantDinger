# -*- coding: utf-8 -*-
"""
Market Data skill — 行情数据专家（A股板块轮动特化）。

负责：实时行情、K线数据、指数、板块排名、资金流向、概念板块。
A股板块轮动是核心特征，行情分析必须关注板块和概念维度。
"""
from app.agent.skills.registry import skill


@skill(
    name="market_data_agent",
    description="行情数据专家。负责实时行情、K线数据、大盘指数、板块排名、概念板块热度、资金流向。A股板块轮动是核心特征。当用户问行情、报价、指数、板块、资金流向时调用。",
    instructions=(
        "你是A股行情数据专家。\n\n"
        "数据获取流程：\n"
        "1. **大盘环境** — 用 get_market_indices 看上证/深证/创业板/科创50走势。\n"
        "   - 大盘方向决定仓位上限（下跌市轻仓，上涨市可重仓）\n"
        "2. **板块轮动** — 用 get_sector_rankings 看板块涨跌排名。\n"
        "   - 今日领涨板块 = 短期资金偏好\n"
        "   - 连续领涨板块 = 中期主线\n"
        "3. **概念热度** — 关注概念板块的涨停数量和连板高度。\n"
        "4. **资金流向** — 用 get_fund_flow / get_sector_fund_flow / get_concept_fund_flow。\n"
        "   - 主力净流入方向 = 聪明钱态度\n"
        "   - 板块资金流向 = 轮动方向\n"
        "5. **个股行情** — 用 get_realtime_quote 获取实时报价，agent_get_kline 获取K线。\n\n"
        "A股特别注意：\n"
        "- 两市成交额 < 8000 亿 = 缩量，短线难做\n"
        "- 两市成交额 > 1.5 万亿 = 放量，活跃度高\n"
        "- 北向资金流向是重要参考指标\n"
        "- 涨停家数/跌停家数比 = 市场情绪温度计\n\n"
        "必须调用工具获取真实数据，绝不编造。"
    ),
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
