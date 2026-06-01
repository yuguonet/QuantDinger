# -*- coding: utf-8 -*-
"""
Shared tool display-name mapping (Chinese labels for frontend & progress events).

Single source of truth — imported by both runner.py and agent_blueprint.py.
"""
from typing import Dict

TOOL_DISPLAY_NAMES: Dict[str, str] = {
    # ── 数据工具 ─────────────────────────────────────────
    "get_realtime_quote": "获取实时行情",
    "get_daily_history": "获取历史K线",
    "get_stock_info": "获取股票基本面",
    "get_market_indices": "获取市场指数",
    "get_sector_rankings": "分析行业板块",
    "get_market_overview": "获取市场快照",
    # ── 名称/代码互查 ───────────────────────────────────
    "resolve_stock_name": "代码查名称",
    "search_stock_by_name": "名称查代码",
    # ── 分析工具 ─────────────────────────────────────────
    "analyze_trend": "综合技术分析（MA+MACD+RSI+BOLL+KDJ）",
    "calculate_ma": "计算均线系统",
    "get_volume_analysis": "分析量能与量价关系",
    "analyze_pattern": "识别K线形态（15+种）",
    "get_chip_distribution": "分析筹码分布",
    "get_indicator_snapshot": "获取指标快照",
    # ── 搜索工具 ─────────────────────────────────────────
    "search_stock_news": "搜索股票新闻",
    "search_comprehensive_intel": "综合情报搜索",
    # ── 选股工具 ─────────────────────────────────────────
    "screen_stocks": "智能选股",
    "smart_screen": "综合选股",
    "get_screener_presets": "获取选股条件",
    "review_stocks_with_indicator": "指标批量审核",
    # ── 指标工具 ─────────────────────────────────────────
    "list_indicators": "列出指标",
    "get_indicator_params": "获取指标参数",
    "run_indicator_signal": "执行指标信号",
    # ── 回测工具 ─────────────────────────────────────────
    "run_backtest": "执行回测",
    "get_backtest_history": "查询回测历史",
    # ── 交易工具 ─────────────────────────────────────────
    "list_strategies": "列出策略",
    "get_strategy_detail": "策略详情",
    "start_strategy": "启动策略",
    "stop_strategy": "停止策略",
    "get_strategy_trades": "交易记录",
    # ── 龙虎榜 / 热榜 ──────────────────────────────────
    "get_dragon_tiger_stocks": "获取龙虎榜",
    "get_dragon_tiger_by_stock": "查询个股龙虎榜",
    "get_hot_rank_stocks": "获取热榜",
    "get_zt_pool_stocks": "获取涨停池",
    "get_limit_down_stocks": "获取跌停池",
    "get_broken_board_stocks": "获取炸板池",
    # ── 资金流 ───────────────────────────────────────────
    "get_stock_fund_flow": "获取个股资金流",
    "batch_get_stock_fund_flow": "批量获取资金流",
    "get_sector_fund_flow": "获取板块资金流",
    "get_concept_fund_flow": "获取概念资金流",
    # ── 自定义分析 ───────────────────────────────────────
    "python_exec": "执行自定义分析代码",
    # ── 工作区工具 ───────────────────────────────────────
    "shell_exec": "执行Shell命令",
    "save_script": "保存脚本到工作区",
    "load_script": "从工作区加载脚本",
    "list_workspace": "查看工作区文件",
    "write_file": "写入文件",
    "read_file": "读取文件",
    "exec_script": "执行工作区脚本",
    "list_versions": "查看脚本版本",
    "diff_versions": "对比脚本版本差异",
    "run_background": "后台执行脚本",
    "poll_task": "查询后台任务",
    "apply_template": "应用项目模板",
    "list_templates": "列出项目模板",
}
