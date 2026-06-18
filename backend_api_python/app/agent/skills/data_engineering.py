# -*- coding: utf-8 -*-
"""
Data engineering skill — 代码执行和数据处理专家。

负责：代码执行、数据清洗、自定义分析脚本、批量数据处理。
"""
from app.agent.skills.registry import skill


@skill("data_agent", auto_load=True)
class DataEngineeringSkill:
    """数据工程专家子 Agent。"""
    pass
