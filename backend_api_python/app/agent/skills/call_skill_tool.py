# -*- coding: utf-8 -*-
"""
call_skill — Agent 调用 Skill 的统一入口（smolagents Tool 子类）。

继承 smolagents.Tool，description/inputs 作为类属性，不受 docstring 解析限制。
"""
from __future__ import annotations

from smolagents import Tool

from app.agent.skills.registry import run_skill


class CallSkillTool(Tool):
    """调用指定的分析技能（Skill），返回标准化分析报告。"""

    name = "call_skill"
    description = (
        "调用指定的分析技能（Skill），返回标准化分析报告。"
        "可用技能: technical_agent(技术面), indicator_agent(指标), "
        "intelligence_agent(情报面), market_screener(市场筛选), "
        "bb_screener(布林带), researcher(深度研究), backtest_agent(回测)"
    )
    inputs = {
        "skill_name": {
            "type": "string",
            "description": "技能名称，可选值: technical_agent, indicator_agent, intelligence_agent, market_screener, bb_screener, researcher, backtest_agent",
        },
        "stock_code": {
            "type": "string",
            "description": "股票代码（6位数字），部分技能可为空",
            "nullable": True,
        },
        "stock_name": {
            "type": "string",
            "description": "股票名称（可选）",
            "nullable": True,
        },
    }
    output_type = "object"

    def forward(self, skill_name: str, stock_code: str = "", stock_name: str = "") -> dict:
        return run_skill(skill_name, stock_code, stock_name)


def get_call_skill_tool():
    """获取 call_skill 工具实例（供 agent.py 使用）。"""
    return CallSkillTool()
