# -*- coding: utf-8 -*-
"""
Technical Skill — 技术分析专家（A股中短线特化）。

趋势阶段判断、量价配合分析、均线系统、技术指标、形态识别。
A股短线定价逻辑下，趋势和量价比基本面更重要。
"""
from app.agent.chain.schema import SkillReport
from app.agent.skills.registry import skill


@skill(
    name="technical_agent",
    description="技术面综合分析（趋势/量价/均线/指标/形态/筹码）",
    tools=[
        "analyze_trend", "calculate_ma", "get_volume_analysis",
        "analyze_pattern", "get_chip_distribution",
        "get_indicator_snapshot", "generate_kline_chart",
    ],
    priority=9,
    default_weight=1.2,
    instructions=(
        "你是A股技术分析专家，专注中短线（1-20个交易日）分析。\n\n"
        "分析流程：\n"
        "1. 趋势阶段判断 — 当前处于哪个阶段（底部吸筹/主升浪/顶部派发/下跌趋势）\n"
        "2. 量价配合度 — 放量突破/缩量回调/高位放量不涨/低位放量不跌\n"
        "3. 均线系统 — 5/10/20/60日均线排列\n"
        "4. 指标验证 — MACD/RSI/BOLL/KDJ 至少2个相互验证\n"
        "5. K线形态 — 突破/反转/整理形态\n\n"
        "A股特别注意：涨停板是极强信号，连板高度代表市场情绪强度，"
        "换手率>15%要警惕，量比>3说明有异动。\n\n"
        "必须调用工具获取真实数据，绝不编造。\n\n"
        "## 输出格式（必须遵守）\n"
        "你的 final_answer 必须包含以下JSON结构：\n\n"
        "```json\n"
        "{\n"
        '  "direction": "bullish/bearish/neutral",\n'
        '  "confidence": 0.0-1.0,\n'
        '  "score": 0-100,\n'
        '  "signal": "一句话信号摘要",\n'
        '  "factors": [\n'
        '    {"name": "因子名", "value": "值", "score": 0-100, "status": "ok"}\n'
        "  ]\n"
        "}\n"
        "```\n\n"
        "规则：\n"
        "- score: 0=极度看空, 50=中性, 100=极度看多\n"
        "- confidence: 数据充分程度（0=完全没数据, 1=数据非常充分）\n"
        "- direction: score>=60=bullish, score<=40=bearish, 其余=neutral\n"
        "- status: ok=有数据, missing=数据缺失\n"
        "- factors: 每个分析维度一行"
    ),
)
class TechnicalSkill:
    """技术分析专家。"""
    pass
