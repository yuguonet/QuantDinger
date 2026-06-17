# -*- coding: utf-8 -*-
"""
Indicator skills — 用户自定义指标策略（indicator IDE 生成的交易信号代码）。

从 indicator_analyzer 加载用户配置的指标策略，注入到 Agent 的 instructions 中。
"""
from __future__ import annotations

import logging
from typing import List, Optional

logger = logging.getLogger(__name__)


def get_indicator_skill_instructions(skills: Optional[List[str]] = None, user_id: int = 1) -> str:
    """加载用户自定义指标策略指令。

    Args:
        skills: 技能 ID 列表（来自 indicator IDE）
        user_id: 用户 ID

    Returns:
        指标策略的指令文本，无则返回空串
    """
    if not skills:
        return ""
    indicator_ids = None
    try:
        indicator_ids = [int(s) for s in skills if str(s).isdigit()]
    except (ValueError, AttributeError):
        pass
    if not indicator_ids:
        return ""
    try:
        from app.services.indicator_analyzer import build_agent_skill_instructions
        return build_agent_skill_instructions(user_id=user_id, indicator_ids=indicator_ids)
    except Exception as e:
        logger.warning("[IndicatorSkills] IndicatorAnalyzer unavailable: %s", e)
        return ""
