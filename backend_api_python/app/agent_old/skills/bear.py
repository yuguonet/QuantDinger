# -*- coding: utf-8 -*-
"""
Bear Researcher skill — 空头研究员（A股中短线特化）。

负责：基于所有分析师报告，构建看跌论据。
A股散户天然偏多，空头角色尤其重要——帮用户管住手。
"""
from app.agent.skills.registry import skill


@skill("bear_researcher", auto_load=True)
class BearResearcherSkill:
    """空头研究员子 Agent。"""
    pass
