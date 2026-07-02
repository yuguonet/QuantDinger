# -*- coding: utf-8 -*-
"""
Skill Registry — 发现和管理 skills/*/SKILL.md。

遵循 Anthropic Agent Skills 标准：
  - 扫描 skills/*/SKILL.md，解析元数据（name, description）
  - 生成 catalog 供 agent system prompt 使用
  - 提供 SKILL.md body 供 agent 读取并自行执行
  - 不负责执行 skill —— 执行由 agent 用自身工具完成

职责：
  - discover(): 扫描并缓存 skill 元数据
  - get_skill_catalog_text(): 生成 catalog XML 注入 system prompt
  - get_skill_body(name): 返回 SKILL.md body 供 agent 读取
"""
from __future__ import annotations

from app.agent.log import logger
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

_SKILLS_DIR = Path(__file__).parent
@dataclass
class SkillInfo:
    """Skill 元数据（从 SKILL.md frontmatter 解析）。"""
    name: str                    # 目录名（下划线，Python 模块名）
    display_name: str            # SKILL.md 中的 name（连字符，Anthropic 标准）
    description: str = ""
    tags: List[str] = field(default_factory=list)
    priority: int = 5
    tools: List[str] = field(default_factory=list)
    body: str = ""               # SKILL.md 的 Markdown body（执行指令）
    dir_path: str = ""           # 目录绝对路径
# 缓存：name（下划线）→ SkillInfo
_skills: Dict[str, SkillInfo] = {}
_loaded = False
def _parse_skill_md(content: str) -> tuple:
    """解析 SKILL.md，分离 YAML frontmatter 和 Markdown body。"""
    meta = {}
    body = content
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            try:
                meta = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                meta = {}
            body = parts[2].strip()
    return meta, body
def _validate_skill(skill_dir: Path, meta: dict) -> Optional[str]:
    """校验 skill 是否合规。返回 None 表示通过，否则返回拒绝原因。"""
    name = meta.get("name", "")
    if not name:
        return "缺少 name 字段"

    if len(name) > 64:
        return f"name 超过64字符: {len(name)}"
    if not re.match(r'^[a-z0-9]+(-[a-z0-9]+)*$', name):
        return f"name 格式不合规（需小写+数字+连字符）: {name}"

    desc = meta.get("description", "")
    if not desc:
        return "缺少 description 字段"
    if len(desc) > 1024:
        return f"description 超过1024字符: {len(desc)}"

    return None
def discover():
    """扫描 skills/*/SKILL.md，加载元数据。幂等，只加载一次。

    校验规则：
      1. 目录下必须有 SKILL.md
      2. SKILL.md 必须有合法的 YAML frontmatter
      3. name 必填，格式：小写+数字+连字符，1-64字符
      4. description 必填，最长1024字符
    """
    global _loaded
    if _loaded:
        return
    _loaded = True

    for skill_dir in sorted(_SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue

        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            logger.debug("[SkillRegistry] 跳过 %s: 缺少 SKILL.md", skill_dir.name)
            continue

        try:
            content = skill_md.read_text(encoding="utf-8")
            meta, body = _parse_skill_md(content)
        except Exception as e:
            logger.warning("[SkillRegistry] 跳过 %s: SKILL.md 解析失败: %s", skill_dir.name, e)
            continue

        reason = _validate_skill(skill_dir, meta)
        if reason:
            logger.warning("[SkillRegistry] 跳过 %s: %s", skill_dir.name, reason)
            continue

        info = SkillInfo(
            name=skill_dir.name,
            display_name=meta.get("name", skill_dir.name),
            description=meta.get("description", ""),
            tags=meta.get("tags", []),
            priority=meta.get("priority", 5),
            tools=meta.get("tools", []),
            body=body,
            dir_path=str(skill_dir),
        )
        _skills[info.name] = info
        logger.debug("[SkillRegistry] 发现: %s (%s)", info.name, info.display_name)

    logger.info("[SkillRegistry] 已发现 %d 个 Skill", len(_skills))
# ═══════════════════════════════════════════════════════════════
#  公开接口
# ═══════════════════════════════════════════════════════════════

def get(name: str) -> Optional[SkillInfo]:
    """按名称查找 Skill。"""
    discover()
    return _skills.get(name)
def all_skills() -> Dict[str, SkillInfo]:
    """返回全部已发现的 Skill。"""
    discover()
    return dict(_skills)
def all_names() -> List[str]:
    """返回全部 Skill 名称列表。"""
    discover()
    return list(_skills.keys())
def get_skill_catalog_text() -> str:
    """生成 skill catalog XML，供注入 agent system prompt。

    只包含 name + description（轻量，~50-100 tokens/skill）。
    Agent 看到 catalog 后，按需调用 read_skill 工具加载完整 body。
    """
    discover()
    if not _skills:
        return ""

    lines = ["<available_skills>"]
    for name, info in sorted(_skills.items(), key=lambda x: x[1].priority, reverse=True):
        lines.append(f'  <skill name="{info.display_name}">')
        lines.append(f'    <description>{info.description}</description>')
        lines.append(f'    <location>{info.dir_path}/SKILL.md</location>')
        lines.append(f'  </skill>')
    lines.append("</available_skills>")
    return "\n".join(lines)
def get_skill_body(name: str) -> Optional[str]:
    """返回指定 skill 的 SKILL.md body（执行指令）。

    Agent 读取 body 后，用自身工具（code-run、bash 等）执行。
    """
    discover()
    info = _skills.get(name)
    if not info:
        # 尝试用连字符名查找
        for skill_info in _skills.values():
            if skill_info.display_name == name:
                return skill_info.body
        return None
    return info.body
def get_skill_dir(name: str) -> Optional[str]:
    """返回指定 skill 的目录路径。"""
    discover()
    info = _skills.get(name)
    if not info:
        for skill_info in _skills.values():
            if skill_info.display_name == name:
                return skill_info.dir_path
        return None
    return info.dir_path
