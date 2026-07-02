# -*- coding: utf-8 -*-
"""
Skill Catalog Tool — 获取可用技能列表。

遵循 Anthropic Agent Skills 标准：
  - Agent 需要使用 skill 时，先调用此工具获取技能列表
  - 然后调用 read_skill 工具加载具体指令
  - 最后按指令执行
"""
from __future__ import annotations

from smolagents import Tool


class GetSkillCatalogTool(Tool):
    """获取可用技能列表，Agent 按需调用。"""

    name = "get_skill_catalog"
    description = (
        "获取可用技能列表。"
        "如果需要使用 skill，请先调用此工具获取 skill 列表，"
        "然后调用 read_skill 工具加载具体指令。"
    )
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        from app.agent.skills.registry import get_skill_catalog_text
        catalog = get_skill_catalog_text()
        if not catalog:
            return "当前无可用技能。"
        return catalog


def get_skill_catalog_tool():
    """获取 get_skill_catalog 工具实例（供 agent.py 使用）。"""
    return GetSkillCatalogTool()
