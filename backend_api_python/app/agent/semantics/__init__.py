# -*- coding: utf-8 -*-
"""
Semantics Loader — 语义描述统一加载入口（v4）。

v4 变更：
  - 删除 DomainMeta 和 domains.md 加载
  - 删除 PlannerMeta 和 planner.md 加载
  - persona.md 扩展，吸收通用行为规范（behaviors）
  - skills 从单文件 skills.md 迁移到 skills/*/SKILL.md（YAML frontmatter + Markdown body）
  - 领域特定指令移入各 SKILL.md body

职责分离：
  - strategy = 路由决策（IntentAnalyzer 计算）
  - tags = 能力标签（@skill/@tool 注册）
  - semantics = 描述来源（YAML/MD 单一信源）

使用方式：
     from app.agent.semantics import (
        get_persona, get_intent_meta,
        get_skill_meta, get_all_skill_metas,
        get_tool_meta, get_all_tool_metas,
    
        get_skills_summary_xml, get_tools_summary_xml,
        get_agent_rules_text,
        load_semantics,
    )
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

_SEMANTICS_DIR = Path(__file__).parent


# ═══════════════════════════════════════════════════════════════
# Data classes
# ═══════════════════════════════════════════════════════════════

@dataclass
class PersonaMeta:
    role: str = ""
    identity: str = ""
    mission: str = ""
    behaviors: Dict[str, List[str]] = field(default_factory=dict)


@dataclass
class IntentMeta:
    classifier_prompt: str = ""
    rules: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class SkillMeta:
    name: str = ""
    description: str = ""
    tags: List[str] = field(default_factory=list)
    priority: int = 0
    default_weight: float = 1.0
    tools: List[str] = field(default_factory=list)
    instructions: str = ""
    triggers: List[str] = field(default_factory=list)
    standard_output: bool = False


@dataclass
class ToolMeta:
    name: str = ""
    description: str = ""
    category: str = ""
    layer: str = ""
    tags: List[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════
# Cache
# ═══════════════════════════════════════════════════════════════

_persona: Optional[PersonaMeta] = None
_intent: Optional[IntentMeta] = None
_skills: Dict[str, SkillMeta] = {}
_tools: Dict[str, ToolMeta] = {}

_loaded = False


def _load_frontmatter(relative_path: str) -> dict:
    """从 .md 文件的 YAML frontmatter 加载元数据。"""
    md_path = _SEMANTICS_DIR / relative_path.replace(".yaml", ".md")
    if md_path.exists():
        content = md_path.read_text(encoding="utf-8")
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                try:
                    return yaml.safe_load(parts[1]) or {}
                except yaml.YAMLError:
                    pass
    return {}


# ═══════════════════════════════════════════════════════════════
# SKILL.md parser
# ═══════════════════════════════════════════════════════════════

def _parse_skill_md(content: str) -> tuple:
    """解析 SKILL.md，分离 YAML frontmatter 和 Markdown body。

    Returns:
        (meta_dict, body_str) — meta 为 frontmatter 解析结果，body 为 Markdown 正文
    """
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


def _parse_behaviors_from_md(body: str) -> Dict[str, List[str]]:
    """从 Markdown body 中解析行为规范。

    格式：
        ## 类别名
        - 规则1
        - 规则2

    Returns:
        {"类别名": ["规则1", "规则2"], ...}
    """
    behaviors: Dict[str, List[str]] = {}
    current_key = ""
    for line in body.split("\n"):
        stripped = line.strip()
        if stripped.startswith("## "):
            current_key = stripped[3:].strip().lower()
            # 中文类别名映射
            _cn_map = {
                "工作流程": "workflow",
                "安全原则": "safety",
                "代码修改": "coding",
                "迭代原则": "iteration",
                "交易执行": "trading",
                "系统管理": "system",
                "金融分析": "finance",
            }
            current_key = _cn_map.get(current_key, current_key)
            if current_key not in behaviors:
                behaviors[current_key] = []
        elif stripped.startswith("- ") and current_key:
            behaviors[current_key].append(stripped[2:].strip())
    return behaviors


# ═══════════════════════════════════════════════════════════════
# Loader
# ═══════════════════════════════════════════════════════════════

def load_semantics():
    """加载所有语义描述文件（幂等，只加载一次）。"""
    global _loaded, _persona, _intent
    if _loaded:
        return
    _loaded = True

    # ── persona（从 persona.md 加载）──
    persona_path = _SEMANTICS_DIR / "persona.md"
    if persona_path.exists():
        content = persona_path.read_text(encoding="utf-8")
        meta, body = _parse_skill_md(content)
        _persona = PersonaMeta(
            role=meta.get("role", ""),
            identity=meta.get("identity", ""),
            mission=meta.get("mission", ""),
            behaviors=_parse_behaviors_from_md(body),
        )

    # ── skills（从 agent/skills/*/SKILL.md 加载）──
    skills_dir = _SEMANTICS_DIR.parent / "skills"
    if skills_dir.exists():
        for skill_dir in skills_dir.iterdir():
            if not skill_dir.is_dir():
                continue
            md_file = skill_dir / "SKILL.md"
            if not md_file.exists():
                continue
            content = md_file.read_text(encoding="utf-8")
            meta, body = _parse_skill_md(content)
            if not meta or not meta.get("name"):
                continue
            _skills[meta["name"]] = SkillMeta(
                name=meta["name"],
                description=meta.get("description", ""),
                tags=meta.get("tags", []),
                priority=meta.get("priority", 5),
                default_weight=meta.get("default_weight", 1.0),
                tools=meta.get("tools", []),
                instructions=body,
                standard_output=meta.get("standard_output", False),
            )

    # ── intent（intent.md frontmatter + body 作为 classifier_prompt）──
    intent_md = _SEMANTICS_DIR / "intent.md"
    if intent_md.exists():
        content = intent_md.read_text(encoding="utf-8")
        meta, body = _parse_skill_md(content)
        _intent = IntentMeta(
            classifier_prompt=body or meta.get("classifier_prompt", ""),
            rules=meta.get("rules", []),
        )

    # ── tools（从 agent/tools/ 目录扫描工具函数）──
    tools_dir = _SEMANTICS_DIR.parent / "tools"
    if tools_dir.exists():
        from app.agent.tools.registry import ToolRegistry
        registry = ToolRegistry()
        registry.discover()
        for name, spec in registry._tools.items():
            _tools[name] = ToolMeta(
                name=name,
                description=spec.description or "",
                category="工具",
                layer="tool",
            )

    # ── 全量缓存 XML 摘要 ──
    global _skills_summary_xml_cache, _tools_summary_xml_cache
    _skills_summary_xml_cache = _build_skills_summary_xml()
    _tools_summary_xml_cache = _build_tools_summary_xml()

    logger.info(
        "[Semantics] 加载完成: %d skills, %d tools, XML 缓存已生成",
        len(_skills), len(_tools),
    )


# ═══════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════

def get_persona() -> PersonaMeta:
    load_semantics()
    if _persona is None:
        return PersonaMeta(role="量化分析助手", identity="", mission="", behaviors={})
    return _persona


def get_intent_meta() -> IntentMeta:
    load_semantics()
    if _intent is None:
        # load_semantics 失败时返回空默认值，防止调用方 NoneType 崩溃
        return IntentMeta(
            classifier_prompt="",
            rules=[],
        )
    return _intent


def get_skill_meta(name: str) -> Optional[SkillMeta]:
    load_semantics()
    return _skills.get(name)


def get_all_skill_metas() -> Dict[str, SkillMeta]:
    load_semantics()
    return dict(_skills)


def get_tool_meta(name: str) -> Optional[ToolMeta]:
    load_semantics()
    return _tools.get(name)


def get_all_tool_metas() -> Dict[str, ToolMeta]:
    load_semantics()
    return dict(_tools)


def get_skill_body(name: str) -> Optional[str]:
    """返回指定 skill 的 SKILL.md body（执行指令）。兼容 skills/registry.py 接口。"""
    load_semantics()
    # 尝试原始名（下划线）
    meta = _skills.get(name)
    if not meta:
        # 尝试连字符名
        for m in _skills.values():
            if m.name == name:
                meta = m
                break
    return meta.instructions if meta else None


def get_skill_dir(name: str) -> Optional[str]:
    """返回指定 skill 的目录路径。兼容 skills/registry.py 接口。"""
    load_semantics()
    skills_dir = _SEMANTICS_DIR.parent / "skills"
    # 尝试原始名（下划线）
    candidate = skills_dir / name
    if candidate.is_dir() and (candidate / "SKILL.md").exists():
        return str(candidate)
    # 尝试连字符名 → 下划线目录
    for d in skills_dir.iterdir():
        if d.is_dir() and (d / "SKILL.md").exists():
            content = (d / "SKILL.md").read_text(encoding="utf-8")
            meta_part, _ = _parse_skill_md(content)
            if meta_part.get("name") == name:
                return str(d)
    return None


def get_skill_catalog_text() -> str:
    """生成 skill catalog XML。兼容 skills/registry.py 接口。"""
    load_semantics()
    if not _skills:
        return ""
    lines = ["<available_skills>"]
    for name, meta in sorted(_skills.items(), key=lambda x: x[1].priority, reverse=True):
        dir_path = get_skill_dir(name) or ""
        lines.append(f'  <skill name="{meta.name}">')
        lines.append(f'    <description>{meta.description}</description>')
        lines.append(f'    <location>{dir_path}/SKILL.md</location>')
        lines.append(f'  </skill>')
    lines.append("</available_skills>")
    return "\n".join(lines)


def all_skills_compat() -> Dict[str, Any]:
    """返回兼容 skills/registry.SkillInfo 接口的字典。"""
    load_semantics()
    # 动态创建兼容对象
    class _CompatSkill:
        def __init__(self, meta: SkillMeta, dir_path: str = ""):
            self.name = meta.name
            self.display_name = meta.name
            self.description = meta.description
            self.tags = meta.tags
            self.priority = meta.priority
            self.tools = meta.tools
            self.body = meta.instructions
            self.dir_path = dir_path
    result = {}
    for name, meta in _skills.items():
        dir_path = get_skill_dir(name) or ""
        result[name] = _CompatSkill(meta, dir_path)
    return result


def get_persona_body() -> str:
    """返回 persona.md 的 Markdown body（不含 frontmatter）。"""
    load_semantics()
    persona_path = _SEMANTICS_DIR / "persona.md"
    if not persona_path.exists():
        return ""
    content = persona_path.read_text(encoding="utf-8")
    _, body = _parse_skill_md(content)
    return body


def get_agent_rules_text() -> str:
    """返回 agent_rules.md 的完整 Markdown body（核心规则 + 执行流程）。"""
    path = _SEMANTICS_DIR / "agent_rules.md"
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8")
    # 用 frontmatter 后的 body
    _, body = _parse_skill_md(content)
    return body


def get_planner_text() -> str:
    """返回 planner.md 的 Markdown body。"""
    path = _SEMANTICS_DIR / "planner.md"
    if not path.exists():
        return ""
    content = path.read_text(encoding="utf-8")
    _, body = _parse_skill_md(content)
    return body






# ═══════════════════════════════════════════════════════════════
# Summary generators (for system prompt injection)
# ═══════════════════════════════════════════════════════════════

# ── 全量缓存（启动时生成，后续直接读取）──
_skills_summary_xml_cache: str = ""
_tools_summary_xml_cache: str = ""


def _build_skills_summary_xml() -> str:
    """生成 skills 摘要 XML（内部，启动时调用一次）。"""
    lines = ["<skills>"]
    for name, meta in sorted(_skills.items(), key=lambda x: x[1].priority, reverse=True):
        tags_str = ",".join(meta.tags) if meta.tags else ""
        lines.append(f'  <skill name="{name}" tags="{tags_str}" priority="{meta.priority}">')
        lines.append(f'    <description>{meta.description}</description>')
        lines.append(f'  </skill>')
    lines.append("</skills>")
    return "\n".join(lines)


def _build_tools_summary_xml() -> str:
    """生成 tools 摘要 XML（内部，启动时调用一次）。"""
    by_cat: Dict[str, List[ToolMeta]] = {}
    for meta in _tools.values():
        by_cat.setdefault(meta.category or "其他", []).append(meta)

    lines = ["<tools>"]
    for cat, tools in sorted(by_cat.items()):
        lines.append(f'  <category name="{cat}">')
        for t in sorted(tools, key=lambda x: x.name):
            desc_short = t.description[:80] if t.description else ""
            lines.append(f'    <tool name="{t.name}">{desc_short}</tool>')
        lines.append(f'  </category>')
    lines.append("</tools>")
    return "\n".join(lines)


def get_skills_summary_xml() -> str:
    """返回 skills 摘要 XML（全量缓存）。"""
    load_semantics()
    return _skills_summary_xml_cache


def get_tools_summary_xml(tags_filter: Optional[List[str]] = None) -> str:
    """返回 tools 摘要 XML（全量缓存，tags_filter 参数保留兼容但不再支持过滤）。"""
    load_semantics()
    return _tools_summary_xml_cache
