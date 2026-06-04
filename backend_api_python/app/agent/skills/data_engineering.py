# -*- coding: utf-8 -*-
"""
Data engineering skill — 代码执行和数据处理专家。

负责：代码执行、数据清洗、自定义分析脚本、批量数据处理。
"""
from app.agent.skills.registry import skill


@skill(
    name="data_agent",
    description="数据工程专家。负责代码执行、数据清洗、自定义分析脚本、批量数据处理。当用户要求写代码、跑脚本、处理数据时调用。",
    instructions="""你是数据工程专家。用工作区工具保存和执行脚本，支持迭代优化。

工作流：
1. 先用 workspace_code_review 检查代码质量
2. 用 workspace_write_file 写入代码
3. 用 workspace_edit_file 精确修改（支持正则替换，无需全量重写）
4. 用 workspace_exec_script 执行
5. 长时间任务用 run_background 后台执行

执行时自动注入整个后端 app/ 的公开 API（introspection 动态发现，无需手动导入）。

按目录自动加前缀避免命名冲突：
  无前缀  → DataSourceFactory 方法（get_kline, get_ticker 等）、ChinaData（gdp, cpi 等）、china_market（get_fear_greed 等）、StockBasicDB（search_stocks 等）
  util_   → app/utils/ （cn_stock_info, trading_calendar 等）
  svc_    → app/services/ （fast_analyze, search_stock_news, indicator_analyzer 等）
  route_  → app/routes/ （路由层函数）
  ds_     → app/data_sources/ （数据源层函数）
  mkt_    → app/market_cn/ （市场分析函数）
  bt_     → app/backtest/ （回测函数）
  iface_  → app/interfaces/ （接口层函数）
  card_   → market_cn cards 仪表盘卡片

黑名单跳过：auth, credentials, security, billing, email, oauth, payment,
  trading_executor, exchange_execution, ibkr_trading, mt5_trading, live_trading,
  logger, config_loader, agent（避免自引用）

每个用户的代码空间按 user_id + domain 隔离，可安全迭代。""",
    tools=[
        "workspace_save_script", "workspace_load_script", "workspace_list",
        "workspace_write_file", "workspace_read_file", "workspace_edit_file",
        "workspace_code_review", "workspace_exec_script",
        "shell_exec", "run_background", "poll_task",
        "agent_get_kline", "get_realtime_quote",
    ],
    priority=4,
)
class DataEngineeringSkill:
    """数据工程专家子 Agent。"""
    pass
