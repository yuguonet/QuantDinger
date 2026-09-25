# -*- coding: utf-8 -*-
"""finance 域分类学（DomainSpec）。

只放纯数据/正则，禁止 import 核心图（nodes/task_agent/tracing）——会成环。
工具注册仍在 tools/finance/*.py；本文件只回答「金融域如何被识别/归类」。
"""
from __future__ import annotations

import re

from domain_registry import DomainSpec

# verb → noun（追溯链名 domain+verb+noun 的缺省补齐）
_VERB_NOUNS = {
    "screen": "stock",
    "analysis": "stock",
    "compare": "stock",
    "query": "stock",
    "code": "strategy",
    "explain": "indicator",
}

# 需要拉金融工具面的任务动词（刻意不含 code/explain：纯推理任务不该拉金融工具）
_INTENT_VERBS = frozenset({"screen", "analysis", "compare", "query"})

# 行情/交易语境词（出现才考虑交易日口径与语义异常；纯闲聊不搅）
# 与 resolvers/time.py 历史口径保持一致，勿擅自增删（差一词就可能给错日期）
_MARKET_WORDS = re.compile(
    r"行情|开盘|收盘|涨停|跌停|大盘|指数|个股|股票|资金|龙虎榜|板块|题材|"
    r"买入|卖出|建仓|减仓|仓位|止损|止盈|K线|均线|MACD|RSI|KDJ|涨跌|做多|做空|看多|看空|"
    r"涨幅|跌幅|振幅|换手|成交|量比|市盈率|市值|涨速|跌速|封板|炸板|打板"
)

# plan_linter R1 数据域词典：(数据域, 触发关键词, 候选工具[0]=首选)
# 工具名必须逐字来自 provider 注册表（CI 断言见 tests/test_wiring.py plan_linter 段）
_DATA_DOMAINS = (
    ("realtime_quote",
     ("实时", "现价", "最新价", "盘口", "快照", "分时", "realtime", "报价", "现在多少钱"),
     ("get_realtime_quote", "quote", "get_realtime_quote", "get_order_book",
      "minute_live", "get_realtime_quote")),
    ("kline",
     ("日线", "周线", "月线", "k线", "历史行情", "走势", "复权", "分钟线", "均线"),
     ("agent_get_kline", "daily", "daily_live", "agent_get_kline", "index_daily",
      "get_sector_history_data", "day_series")),
    ("fund_flow",
     ("资金流", "主力", "净流入", "净流出", "大单", "北向", "外资", "资金面", "fund_flow"),
     ("get_fund_flow", "get_fund_flow", "get_fund_flow_daily",
      "get_market_fund_flow", "get_market_fund_flow", "get_capital_summary")),
    ("financials",
     ("财务", "业绩", "营收", "净利润", "财报", "毛利率", "roe", "基本面", "财报数据"),
     ("get_capital_summary", "get_stock_info", "get_stock_info", "get_stock_info")),
    ("valuation",
     ("估值", "市盈", "市净", "市值", "贵不贵", "值多少钱", "dcf", "目标价", "定价"),
     ("batch_valuation_compare", "get_stock_info")),
    ("sector",
     ("板块", "行业", "概念", "题材", "产业链", "板块转动"),
     ("get_hot_sectors", "get_sector_stocks", "get_industry_ranking",
      "get_sector_trend_analysis", "get_sector_fund_flow", "get_stock_sector_info")),
    ("dragon_tiger",
     ("龙虎榜", "席位", "游资", "营业部", "机构专用"),
     ("get_dragon_tiger", "get_dragon_tiger_detail", "lhb", "query_dragon_tiger")),
    ("hot_rank",
     ("人气", "热度", "关注度", "人气榜", "热搜"),
     ("get_hot_rank", "query_hot_rank", "get_hot_stocks_with_reason")),
    ("limit_pool",
     ("涨停", "跌停", "炸板", "连板", "封板", "打板", "首板", "接力", "晋级"),
     ("get_limit_pool", "get_limit_pool", "get_dragon_tiger", "get_limit_pool")),
    ("screen",
     ("筛选", "选股", "选出", "找出", "挑出", "有哪些", "股票池", "排行"),
     ("search_stocks", "get_screener_presets", "build_keyword_from_filters")),
    ("technical",
     ("技术面", "技术分析", "指标", "macd", "kdj", "rsi", "金叉", "死叉", "形态",
      "布林", "背驰", "背离", "量能"),
     ("technical_analysis", "analyze_trend", "calculate_ma", "indicator_analysis",
      "analyze_chart_patterns", "get_obv_analysis", "get_volume_analysis",
      "list_indicators")),
    ("intel_news",
     ("消息面", "新闻", "公告", "研报", "舆情", "利好", "利空", "政策", "事件驱动", "传闻"),
     ("search_stock_intel", "search_sector_intel", "search_policy_intel",
      "search_comprehensive_intel", "search_policy_intel")),
    ("market_overview",
     ("大盘", "指数", "市场概览", "沪深", "上证", "创业板指", "行情总览"),
     ("get_market_overview", "get_market_indices", "market_snapshot", "get_market_indices")),
    ("sentiment",
     ("情绪", "恐慌", "赚钱效应", "亏钱效应", "情绪周期", "高潮", "冰点"),
     ("get_market_overview", "get_market_overview", "fear_greed_index", "get_market_overview")),
    ("chip",
     ("筹码", "成本分布", "获利盘", "套牢盘", "筹码集中度"),
     ("get_chip_distribution",)),
    ("backtest",
     ("回测", "胜率", "收益率", "绩效", "策略表现", "历史表现"),
     ("run_backtest", "get_backtest_history", "list_strategies", "get_strategy_detail")),
    ("lockup",
     ("解禁", "限售"),
     ("get_lockup_expiry",)),
    ("dividend",
     ("分红", "股息", "派息"),
     ("get_capital_summary",)),
    ("signals",
     ("信号", "买点", "卖点", "触发条件"),
     ("search_stock_intel", "run_indicator_signal", "list_strategies", "strategy_keys")),
)

# R2 依赖登记
_LIST_PRODUCERS = frozenset({
    "search_stocks", "get_limit_pool", "get_dragon_tiger",
    "get_hot_rank", "query_hot_rank", "get_hot_stocks_with_reason",
    "query_dragon_tiger", "lhb",
    "get_hot_sectors", "get_sector_stocks", "get_industry_ranking",
    "list_strategies", "search_stock_intel", "all_codes",
})
_LIST_CONSUMERS = frozenset({
    "agent_get_kline", "get_realtime_quote", "quote", "technical_analysis",
    "analyze_trend", "analyze_pattern", "analyze_chart_patterns", "calculate_ma",
    "indicator_analysis", "get_obv_analysis", "get_volume_analysis",
    "get_fund_flow", "get_fund_flow_daily", "get_chip_distribution",
    "get_stock_info", "get_stock_sector_info", "get_stock_concept_blocks",
    "search_stock_intel", "batch_valuation_compare", "get_capital_summary",
    "resolve_stock", "get_lockup_expiry", "run_indicator_signal",
})

SPEC = DomainSpec(
    name="finance",
    primary=True,
    entity_types=("stock",),
    trading_calendar=True,
    verb_nouns=_VERB_NOUNS,
    intent_verbs=_INTENT_VERBS,
    word_pattern=_MARKET_WORDS,
    data_domains=_DATA_DOMAINS,
    list_producers=_LIST_PRODUCERS,
    list_consumers=_LIST_CONSUMERS,
)
