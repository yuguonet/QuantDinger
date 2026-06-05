# -*- coding: utf-8 -*-
"""
Technical Analysis skill — 技术分析专家。

负责：技术指标计算、趋势分析、形态识别、量能分析、筹码分布。
"""
from app.agent.skills.registry import skill


@skill(
    name="technical_agent",
    description="技术分析专家。负责技术指标（MACD/RSI/BOLL/KDJ）、趋势判断、K线形态识别、量能分析、筹码分布。当用户问技术面、指标、趋势、形态时调用。",
    instructions="你是技术分析专家。按趋势→指标→形态→量能→筹码的流程分析。优先用 get_indicator_snapshot 一次获取全部指标。技术面结论至少 2 个指标相互验证。必须调用工具获取真实数据，绝不编造。",
    tools=[
        "analyze_trend", "calculate_ma", "get_volume_analysis",
        "analyze_pattern", "get_chip_distribution",
        "get_indicator_snapshot",
        "generate_kline_chart",
    ],
    priority=9,
)
class TechnicalSkill:
    """技术分析专家子 Agent。"""
    pass
