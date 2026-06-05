# -*- coding: utf-8 -*-
"""
Trading skill — 交易执行专家。

负责：策略启停、持仓管理、交易执行、交易记录查询。
"""
from app.agent.skills.registry import skill


@skill(
    name="trading_agent",
    description="交易执行专家。负责策略启动/停止、持仓管理、交易记录查询。当用户要求启动策略、停止策略、查看持仓、执行交易时调用。",
    instructions="你是交易执行专家。启动策略前必须先确认行情和信号状态。先用 list_strategies 列出可用策略，get_strategy_detail 查看详情，确认后再 start_strategy。停止策略用 stop_strategy。始终优先考虑风险控制。",
    tools=[
        "list_strategies", "get_strategy_detail",
        "start_strategy", "stop_strategy",
        "get_strategy_trades",
        "get_realtime_quote",
    ],
    priority=5,
)
class TradingSkill:
    """交易执行专家子 Agent。"""
    pass
