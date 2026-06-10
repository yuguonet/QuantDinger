# -*- coding: utf-8 -*-
"""
Momentum Tracker — 已合并到 technical_agent。

保留此文件作为别名，确保存量调用不会断裂。
新代码请直接使用 technical_agent。
"""
from __future__ import annotations

from app.agent.skills.registry import skill
from app.agent.skills.technical import TechnicalSkill


@skill(
    name="momentum_tracker",
    description="【已合并到 technical_agent】动量追踪。保留此别名确保存量调用兼容。",
    tools=[],  # 工具由 technical_agent 统一管理
    priority=0,  # 低优先级，agent 不会主动选择
    default_weight=1.1,
)
class MomentumTrackerSkill(TechnicalSkill):
    """别名：实际执行走 TechnicalSkill。"""
    pass
