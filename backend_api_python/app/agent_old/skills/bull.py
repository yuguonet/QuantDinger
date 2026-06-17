# -*- coding: utf-8 -*-
"""
Bull Researcher skill — 多头研究员（A股中短线特化）。

负责：基于所有分析师报告，构建看涨论据。
注意：A股散户天然偏多，多头论据需要更强的数据支撑才可信。
"""
from app.agent.skills.registry import skill


@skill("bull_researcher", auto_load=True)
class BullResearcherSkill:
    """多头研究员子 Agent。"""
    pass
