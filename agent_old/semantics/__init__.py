# -*- coding: utf-8 -*-
"""
Semantics Loader — 语义描述统一加载入口。

所有描述语义从 YAML 文件加载，代码层只引用不硬编码。

使用方式：
    from app.agent.semantics import (
        get_persona, get_domain_meta, get_intent_meta,
        get_skill_meta, get_all_skill_metas,
        get_tool_meta, get_all_tool_metas,
        get_route_metas, get_chain_meta, get_planner_meta,
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


@dataclass
class DomainMeta:
    name: str = ""
    description: str = ""
    instructions: str = ""
    skills: List[str] = field(default_factory=list)
    tool_categories: Optional[List[str]] = None


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
    domain: List[str] = field(default_factory=list)


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


@dataclass
class PlannerMeta:
    skill_catalog: List[Dict[str, str]] = field(default_factory=list)
    aliases: Dict[str, str] = field(default_factory=dict)
    planner_prompt: str = ""


# ═══════════════════════════════════════════════════════════════
# Cache
# ═══════════════════════════════════════════════════════════════

_persona: Optional[PersonaMeta] = None
_domains: Dict[str, DomainMeta] = {}
_intent: Optional[IntentMeta] = None
_skills: Dict[str, SkillMeta] = {}
_tools: Dict[str, ToolMeta] = {}
_routes: List[RouteMeta] = []
_chains: Dict[str, ChainMeta] = {}
_planner: Optional[PlannerMeta] = None
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
# Loader
# ═══════════════════════════════════════════════════════════════

def load_semantics():
    """加载所有语义描述文件（幂等，只加载一次）。"""
    global _loaded, _persona, _intent, _planner
    if _loaded:
        return
    _loaded = True

    # persona
    p = _load_yaml("persona.yaml")
    _persona = PersonaMeta(**p)

    # domains
    for name, cfg in _load_yaml("domains.yaml").get("domains", {}).items():
        if isinstance(cfg, dict):
            cfg.setdefault("name", name)
            _domains[name] = DomainMeta(**cfg)

    # intent
    i = _load_yaml("intent.yaml")
    _intent = IntentMeta(
        classifier_prompt=i.get("classifier_prompt", ""),
        rules=i.get("rules", []),
        quick_patterns=i.get("quick_patterns", {}),
        intent_tool_categories=i.get("intent_tool_categories", {}),
    )

    # skills (单文件 skills.yaml，skills 列表)
    for s in _load_yaml("skills.yaml").get("skills", []):
        if isinstance(s, dict) and s.get("name"):
            _skills[s["name"]] = SkillMeta(**s)

    # tools (单文件 tools.yaml，按 category 分组)
    for cat_name, cat_tools in _load_yaml("tools.yaml").get("categories", {}).items():
        if not isinstance(cat_tools, list):
            continue
        for t in cat_tools:
            if isinstance(t, dict) and t.get("name"):
                _tools[t["name"]] = ToolMeta(**t)

    # routes
    for r in _load_yaml("routes.yaml").get("routes", []):
        _routes.append(RouteMeta(**r))

    # chains
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

    # planner
    pl = _load_yaml("planner.yaml")
    _planner = PlannerMeta(
        skill_catalog=pl.get("skill_catalog", []),
        aliases=pl.get("aliases", {}),
        planner_prompt=pl.get("planner_prompt", ""),
    )

    logger.info(
        "[Semantics] 加载完成: %d domains, %d skills, %d tools, %d routes, %d chains",
        len(_domains), len(_skills), len(_tools), len(_routes), len(_chains),
    )


# ═══════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════

def get_persona() -> PersonaMeta:
    load_semantics()
    return _persona


def get_domain_meta(name: str) -> Optional[DomainMeta]:
    load_semantics()
    return _domains.get(name)


def get_all_domain_metas() -> Dict[str, DomainMeta]:
    load_semantics()
    return dict(_domains)


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


def get_planner_meta() -> PlannerMeta:
    load_semantics()
    return _planner


# ═══════════════════════════════════════════════════════════════
# Summary generators (for system prompt injection)
# ═══════════════════════════════════════════════════════════════

def get_skills_summary_xml() -> str:
    """生成 skills 摘要 XML，用于注入 system prompt（轻量，只有 name+description）。"""
    load_semantics()
    lines = ["<skills>"]
    for name, meta in sorted(_skills.items(), key=lambda x: x[1].priority, reverse=True):
        lines.append(f'  <skill name="{name}" priority="{meta.priority}">')
        lines.append(f'    <description>{meta.description}</description>')
        lines.append(f'  </skill>')
    lines.append("</skills>")
    return "\n".join(lines)


def get_tools_summary_xml() -> str:
    """生成 tools 摘要 XML，按 category 分组。"""
    load_semantics()
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
