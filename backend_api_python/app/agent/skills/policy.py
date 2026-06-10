# -*- coding: utf-8 -*-
"""
Policy Analyst — 已合并到 intelligence_agent。

保留此文件作为别名，确保存量调用不会断裂。
新代码请直接使用 intelligence_agent。
"""
from __future__ import annotations

from app.agent.skills.registry import skill
from app.agent.skills.intelligence import IntelligenceSkill


@skill(
    name="policy_analyst",
    description="【已合并到 intelligence_agent】政策分析。保留此别名确保存量调用兼容。",
    tools=[],
    priority=0,
    default_weight=0.7,
)
class PolicyAnalystSkill(IntelligenceSkill):
    """别名：实际执行走 IntelligenceSkill。"""
    pass
