# -*- coding: utf-8 -*-
"""
Concept Tracker — 已合并到 market_data_agent。

保留此文件作为别名，确保存量调用不会断裂。
新代码请直接使用 market_data_agent。
"""
from __future__ import annotations

from app.agent.skills.registry import skill
from app.agent.skills.market_data import MarketDataSkill


@skill(
    name="concept_tracker",
    description="【已合并到 market_data_agent】概念追踪。保留此别名确保存量调用兼容。",
    tools=[],
    priority=0,
    default_weight=0.9,
)
class ConceptTrackerSkill(MarketDataSkill):
    """别名：实际执行走 MarketDataSkill。"""
    pass
