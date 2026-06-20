# -*- coding: utf-8 -*-
"""
call_skill — Agent 调用 Skill 的统一入口（smolagents Tool 子类）。

继承 smolagents.Tool，description/inputs 作为类属性，不受 docstring 解析限制。
description 和 skill_name 可选值从 registry 动态生成，新增 skill 无需改此文件。
"""
from __future__ import annotations

from smolagents import Tool

from app.agent.skills.registry import run_skill, all_skills


def _build_description() -> str:
    """从 registry 动态生成工具描述。"""
    skills = all_skills()
    if not skills:
        return "调用指定的分析技能（Skill），返回标准化分析报告。当前无可用技能。"
    parts = [f"{name}({info.description[:20]})" for name, info in skills.items()]
    return "调用指定的分析技能（Skill），返回标准化分析报告。可用技能: " + ", ".join(parts)


def _build_skill_name_enum() -> str:
    """从 registry 动态生成 skill_name 可选值。"""
    skills = all_skills()
    return ", ".join(skills.keys()) if skills else "(无可用技能)"


class CallSkillTool(Tool):
    """调用指定的分析技能（Skill），返回标准化分析报告。"""

    name = "call_skill"
    description = _build_description()
    inputs = {
        "skill_name": {
            "type": "string",
            "description": f"技能名称，可选值: {_build_skill_name_enum()}",
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
