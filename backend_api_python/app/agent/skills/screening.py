# -*- coding: utf-8 -*-
"""
Screening skill — 选股和推荐专家。

负责：条件选股→龙虎榜→涨停池→热榜→指标验证。
"""
from app.agent.skills.registry import skill


@skill(
    name="screening_agent",
    description="选股专家。负责全市场筛选：条件选股→龙虎榜→涨停池→热榜→指标验证。当用户要求选股、筛选股票时调用。",
    instructions="你是选股专家。用 search_stocks 按条件筛选，再用 run_indicator_signal 验证信号。优先使用自然语言条件。",
    tools=[
        "search_stocks", "get_screener_presets",
        "get_zt_pool", "get_dragon_tiger", "get_hot_rank",
        "get_limit_down", "get_broken_board",
        "list_indicators", "run_indicator_signal", "review_stocks_with_indicator",
        "get_realtime_quote", "agent_get_kline",
        "resolve_stock_name", "search_stock_by_name",
    ],
    priority=8,
)
class ScreeningSkill:
    """选股专家子 Agent。"""
    pass
