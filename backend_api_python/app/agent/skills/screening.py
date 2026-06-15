# -*- coding: utf-8 -*-
"""
Screening skill — 选股专家（A股动量+概念筛选特化）。

负责：条件选股、动量筛选、概念选股、龙虎榜、涨停池、热榜。
A股选股核心：先看概念热度和资金方向，再用技术指标验证。
"""
from app.agent.skills.registry import skill


@skill("screening_agent", auto_load=True)

class ScreeningSkill:
    """选股专家子 Agent。"""
    pass
