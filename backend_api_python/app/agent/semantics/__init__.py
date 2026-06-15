# -*- coding: utf-8 -*-
"""
Semantics Loader — 语义描述统一加载入口（v4，对齐 Nanobot 两段加载）。

v4 变更：
  - 删除 DomainMeta 和 domains.yaml 加载
  - 删除 PlannerMeta 和 planner.yaml 加载
  - persona.yaml 扩展，吸收通用行为规范（behaviors）
  - skills 从单文件 skills.yaml 迁移到 skills/*/SKILL.md（YAML frontmatter + Markdown body）
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
        get_route_metas, get_chain_meta,
        get_skills_summary_xml, get_tools_summary_xml,
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
    quick_patterns: Dict[str, str] = field(default_factory=dict)
    intent_tool_categories: Dict[str, List[str]] = field(default_factory=dict)


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


@dataclass
class RouteMeta:
    name: str = ""
    description: str = ""
    domain: str = ""
    intent: str = ""
    verb: str = ""
    noun: str = ""
    tool_categories: List[str] = field(default_factory=list)
    utterances: List[str] = field(default_factory=list)


@dataclass
class ChainStepMeta:
    name: str = ""
    agent: str = ""
    order: int = 0
    description: str = ""
    required: bool = True
    extract_fn: str = ""


@dataclass
class ChainMeta:
    name: str = ""
    description: str = ""
    trigger_verbs: List[str] = field(default_factory=list)
    trigger_nouns: List[str] = field(default_factory=list)
    steps: List[ChainStepMeta] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════
# Cache
# ═══════════════════════════════════════════════════════════════

_persona: Optional[PersonaMeta] = None
_intent: Optional[IntentMeta] = None
_skills: Dict[str, SkillMeta] = {}
_tools: Dict[str, ToolMeta] = {}
_routes: List[RouteMeta] = []
_chains: Dict[str, ChainMeta] = {}
_loaded = False


def _load_yaml(relative_path: str) -> dict:
    """Load a YAML file relative to the semantics directory."""
    path = _SEMANTICS_DIR / relative_path
    if not path.exists():
        logger.warning("[Semantics] 文件不存在: %s", path)
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


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


# ═══════════════════════════════════════════════════════════════
# Loader
# ═══════════════════════════════════════════════════════════════

def load_semantics():
    """加载所有语义描述文件（幂等，只加载一次）。"""
    global _loaded, _persona, _intent
    if _loaded:
        return
    _loaded = True

    # ── persona（v4: 扩展版，包含 behaviors）──
    p = _load_yaml("persona.yaml")
    _persona = PersonaMeta(
        role=p.get("role", ""),
        identity=p.get("identity", ""),
        mission=p.get("mission", ""),
        behaviors=p.get("behaviors", {}),
    )

    # ── skills（从 SKILL.md 加载，YAML frontmatter + Markdown body）──
    skills_dir = _SEMANTICS_DIR / "skills"
    if skills_dir.exists():
        for skill_dir in skills_dir.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue
            content = skill_md.read_text(encoding="utf-8")
            meta, body = _parse_skill_md(content)
            if meta.get("name"):
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

    # ── tools（单文件 tools.yaml，按 category 分组）──
    for cat_name, cat_tools in _load_yaml("tools.yaml").get("categories", {}).items():
        if not isinstance(cat_tools, list):
            continue
        for t in cat_tools:
            if isinstance(t, dict) and t.get("name"):
                _tools[t["name"]] = ToolMeta(
                    name=t["name"],
                    description=t.get("description", ""),
                    category=t.get("category", cat_name),
                    layer=t.get("layer", ""),
                    tags=t.get("tags", t.get("domain", [])),
                )

    # ── routes ──
    for r in _load_yaml("routes.yaml").get("routes", []):
        _routes.append(RouteMeta(**r))

    # ── chains ──
    for name, cfg in _load_yaml("chains.yaml").get("chains", {}).items():
        steps = []
        for s in cfg.get("steps", []):
            steps.append(ChainStepMeta(**s))
        _chains[name] = ChainMeta(
            name=cfg.get("name", name),
            description=cfg.get("description", ""),
            trigger_verbs=cfg.get("trigger_verbs", []),
            trigger_nouns=cfg.get("trigger_nouns", []),
            steps=steps,
        )

    # ── intent ──
    intent_data = _load_yaml("intent.yaml")
    _intent = IntentMeta(
        classifier_prompt=intent_data.get("classifier_prompt", ""),
        rules=intent_data.get("rules", []),
        quick_patterns=intent_data.get("quick_patterns", {}),
        intent_tool_categories=intent_data.get("intent_tool_categories", {}),
    )

    logger.info(
        "[Semantics] 加载完成: %d skills (SKILL.md), %d tools, %d routes, %d chains",
        len(_skills), len(_tools), len(_routes), len(_chains),
    )


# ═══════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════

def get_persona() -> PersonaMeta:
    load_semantics()
    return _persona


def get_intent_meta() -> IntentMeta:
    load_semantics()
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


def get_route_metas() -> List[RouteMeta]:
    load_semantics()
    return list(_routes)


def get_chain_meta(name: str) -> Optional[ChainMeta]:
    load_semantics()
    return _chains.get(name)


def get_all_chain_metas() -> Dict[str, ChainMeta]:
    load_semantics()
    return dict(_chains)


# ═══════════════════════════════════════════════════════════════
# Summary generators (for system prompt injection)
# ═══════════════════════════════════════════════════════════════

def get_skills_summary_xml() -> str:
    """生成 skills 摘要 XML（轻量，只有 name+description+tags）。

    用于第一段加载：system prompt 只放摘要，完整 instructions 按需加载。
    """
    load_semantics()
    lines = ["<skills>"]
    for name, meta in sorted(_skills.items(), key=lambda x: x[1].priority, reverse=True):
        tags_str = ",".join(meta.tags) if meta.tags else ""
        lines.append(f'  <skill name="{name}" tags="{tags_str}" priority="{meta.priority}">')
        lines.append(f'    <description>{meta.description}</description>')
        lines.append(f'  </skill>')
    lines.append("</skills>")
    return "\n".join(lines)


def get_tools_summary_xml(tags_filter: Optional[List[str]] = None) -> str:
    """生成 tools 摘要 XML，按 category 分组。可选按 tags 过滤。"""
    load_semantics()
    by_cat: Dict[str, List[ToolMeta]] = {}
    for meta in _tools.values():
        # §15: 用 tags 过滤（tags 优先，降级到无过滤）
        if tags_filter and meta.tags and not any(t in meta.tags for t in tags_filter):
            continue
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
