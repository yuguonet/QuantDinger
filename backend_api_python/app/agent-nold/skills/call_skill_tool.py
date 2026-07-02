# -*- coding: utf-8 -*-
"""
read_skill — Agent 读取 Skill 的 SKILL.md body（smolagents Tool 子类）。

遵循 Anthropic Agent Skills 标准：
  - Agent 看到 catalog（system prompt 中）后，按需调用此工具加载完整 SKILL.md body
  - 此工具只返回 body 内容，不执行代码
  - Agent 读取 body 后，用自身工具（code-run、bash 等）执行

与旧版 call_skill_tool 的区别：
  - 旧版：CallSkillTool.forward() → importlib → mod.run() → 直接执行 Python 代码
  - 新版：ReadSkillTool.forward() → 返回 SKILL.md body → Agent 自行执行
"""
from __future__ import annotations

from smolagents import Tool

from app.agent.skills.registry import all_skills, get_skill_body, get_skill_dir


def _build_description() -> str:
    """从 registry 动态生成工具描述。"""
    skills = all_skills()
    if not skills:
        return "读取指定 Skill 的 SKILL.md 完整指令。当前无可用技能。"
    parts = [f"{info.display_name}" for info in skills.values()]
    return (
        "读取指定 Skill 的 SKILL.md 完整指令（含执行方式）。"
        "加载后按 body 中的指令执行。可用技能: " + ", ".join(parts)
    )


def _build_skill_name_enum() -> str:
    """从 registry 动态生成 skill_name 可选值。"""
    skills = all_skills()
    return ", ".join(info.display_name for info in skills.values()) if skills else "(无可用技能)"


class ReadSkillTool(Tool):
    """读取指定 Skill 的 SKILL.md 完整指令，Agent 按指令自行执行。"""

    name = "read_skill"
    description = _build_description()
    inputs = {
        "skill_name": {
            "type": "string",
            "description": f"技能名称，可选值: {_build_skill_name_enum()}",
        },
    }
    output_type = "string"

    def forward(self, skill_name: str) -> str:
        body = get_skill_body(skill_name)
        if body is None:
            return f"错误: 未找到技能 '{skill_name}'。可用技能: {', '.join(info.display_name for info in all_skills().values())}"

        dir_path = get_skill_dir(skill_name)
        header = f"<!-- Skill: {skill_name} | Dir: {dir_path} -->\n\n"
        return header + body


def get_read_skill_tool():
    """获取 read_skill 工具实例（供 agent.py 使用）。"""
    return ReadSkillTool()
