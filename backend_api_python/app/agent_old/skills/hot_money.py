# -*- coding: utf-8 -*-
"""
Hot Money Tracker skill — A股游资追踪师。

负责：龙虎榜分析、大单流向、主力资金动态、游资席位追踪。
游资是A股短线定价的核心力量，追踪游资 = 追踪短线alpha。
"""
from app.agent.skills.registry import skill


@skill("hot_money_tracker", auto_load=True)

class HotMoneyTrackerSkill:
    """A股游资追踪师子 Agent。"""
    pass
