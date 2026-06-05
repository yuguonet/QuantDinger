# -*- coding: utf-8 -*-
"""
Screening skill — 选股专家（A股动量+概念筛选特化）。

负责：条件选股、动量筛选、概念选股、龙虎榜、涨停池、热榜。
A股选股核心：先看概念热度和资金方向，再用技术指标验证。
"""
from app.agent.skills.registry import skill


@skill(
    name="screening_agent",
    description="选股专家。负责条件选股、动量筛选、概念选股、龙虎榜、涨停池、热榜、指标验证。A股选股先看概念和资金，再验证技术面。当用户要求选股、筛选股票时调用。",
    instructions=(
        "你是A股选股专家。\n\n"
        "选股策略（按A股有效性排序）：\n"
        "1. **概念/题材选股** — 先确定当前热门概念，再找概念内的强势股。\n"
        "   - 关注连续涨停的龙头股（辨识度高、资金共识强）\n"
        "   - 概念内补涨股（涨幅落后但逻辑一致）\n"
        "2. **动量选股** — 短线强势股筛选：\n"
        "   - 近 N 日涨幅排名\n"
        "   - 连续放量上涨\n"
        "   - 突破关键均线或前高\n"
        "3. **资金选股** — 跟踪聪明钱：\n"
        "   - 龙虎榜机构席位净买入（get_dragon_tiger / get_dragon_tiger_detail 看机构专用席位）\n"
        "   - 主力资金净流入（get_fund_flow / get_fund_flow_minute 看盘中实时 / get_fund_flow_120d 看中长期趋势）\n"
        "   - 涨停池（get_zt_pool）看市场最强股\n"
        "4. **条件选股** — 用 search_stocks 按自然语言条件筛选。\n"
        "5. **指标验证** — 用 run_indicator_signal 验证筛选结果的技术信号。\n\n"
        "用 get_hot_rank 看市场关注度排名，用 get_limit_down / get_broken_board 看情绪面。\n"
        "   用 get_valuation_metrics 获取PE/PB/市值做估值筛选，用 get_holder_count 看筹码集中度。\n"
        "   用 get_stock_sector_info 查个股所属行业/概念，配合热门板块做概念选股。\n\n"
        "必须调用工具获取真实数据，绝不编造。"
    ),
    tools=[
        "search_stocks", "get_screener_presets",
        "get_zt_pool", "get_dragon_tiger", "get_hot_rank",
        "get_limit_down", "get_broken_board", "get_market_overview",
        "list_indicators", "run_indicator_signal", "review_stocks_with_indicator",
        "get_realtime_quote", "agent_get_kline",
        "search_stock_by_name", "search_stock_by_name",
    ],
    priority=8,
)
class ScreeningSkill:
    """选股专家子 Agent。"""
    pass
