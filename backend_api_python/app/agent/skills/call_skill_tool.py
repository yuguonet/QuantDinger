# -*- coding: utf-8 -*-
"""
call_skill — Agent 调用 Skill 的统一入口（smolagents Tool）。

注册为 smolagents Tool 后，Agent 可以通过 tool_calls 协议调用任意 Skill。
符合 OpenAI Function Calling 标准（JSON Schema 声明）。
"""
from __future__ import annotations

from app.agent.skills.registry import all_skills, run_skill


def get_call_skill_tool():
    """获取 call_skill 工具实例（供 agent.py 使用）。

    延迟导入 smolagents，避免模块加载时就要求安装。
    """
    from smolagents import tool as smolagents_tool

    # 动态生成 description，包含可用 Skill 列表
    skills = all_skills()
    skill_lines = []
    for name, info in sorted(skills.items()):
        skill_lines.append(f"- {name}: {info.description}")
    skills_catalog = "\n".join(skill_lines) if skill_lines else "(无可用 Skill)"

    @smolagents_tool
    def call_skill(skill_name: str, stock_code: str = "", stock_name: str = "") -> dict:
        """调用指定的分析技能（Skill），返回标准化分析报告。

        可用技能：
        {catalog}

        Args:
            skill_name: 技能名称，必须是上述列表中的一个
            stock_code: 股票代码（6位数字），部分技能可为空
            stock_name: 股票名称（可选）

        Returns:
            标准化分析报告 dict，包含 skill/score/direction/confidence/factors/analysis 等字段
        """.format(catalog=skills_catalog)

        return run_skill(skill_name, stock_code, stock_name)

    return call_skill
