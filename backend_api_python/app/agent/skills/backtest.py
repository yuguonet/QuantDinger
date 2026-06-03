# -*- coding: utf-8 -*-
"""
Backtest skill — 策略回测验证专家。

负责：策略回测、历史绩效分析（收益率、胜率、最大回撤、夏普比率）。
"""
from app.agent.skills.registry import skill


@skill(
    name="backtest_agent",
    description="回测专家。负责执行策略回测、分析历史绩效（收益率、胜率、最大回撤、夏普比率）。当用户要求回测、验证策略时调用。",
    instructions="你是回测专家。发现策略→执行回测→分析绩效。重点分析风险调整后收益。",
    tools=[
        "run_backtest", "get_backtest_history",
        "list_strategies", "get_strategy_detail",
        "list_indicators", "get_indicator_params", "run_indicator_signal",
    ],
    priority=6,
)
class BacktestSkill:
    """回测专家子 Agent。"""
    pass
