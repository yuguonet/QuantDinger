# -*- coding: utf-8 -*-
"""
Intelligence Skill — 情报分析专家（A股事件驱动 + 政策分析特化）。

合并原 intelligence_agent + policy_analyst：
  新闻搜索、事件驱动分析、概念催化、公告解读、政策分析。
A股弱有效市场下，信息不对称是核心alpha来源。
"""
from app.agent.skills.registry import skill


@skill("intelligence_agent", auto_load=True)
class IntelligenceSkill:
    """情报分析专家（含政策分析）。"""
    pass
