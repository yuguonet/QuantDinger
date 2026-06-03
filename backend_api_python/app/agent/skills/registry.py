# -*- coding: utf-8 -*-
"""
Skill Registry — decorator-based self-registration for QuantDinger skills.

Usage:
    from app.agent.skills.registry import skill, skill_registry

    @skill(
        name="analysis_agent",
        description="股票分析专家。负责个股分析。",
        instructions="你是技术分析专家。按行情→形态→情报→分析流程执行。",
        tools=["get_realtime_quote", "agent_get_kline", "analyze_trend"],
    )
    class AnalysisAgent:
        ...

    # Discover all skills
    skill_registry.discover()

    # Build managed agents
    agents = skill_registry.build_managed_agents(smol_model, tool_map)
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Type

logger = logging.getLogger(__name__)


@dataclass
class SkillSpec:
    """Registered skill metadata."""
    name: str
    description: str
    instructions: str
    tools: List[str] = field(default_factory=list)
    max_steps: int = 8
    priority: int = 0
    extra_kwargs: Dict[str, Any] = field(default_factory=dict)
    cls: Optional[Type] = None  # The decorated class (if any)


class SkillRegistry:
    """Central registry for @skill-decorated entries.

    Lifecycle:
        1. Modules define @skill(...) decorated classes/functions
        2. skill_registry.discover() imports all modules in the skills package
        3. skill_registry.build_managed_agents() creates smolagents agents
    """

    def __init__(self):
        self._skills: Dict[str, SkillSpec] = {}
        self._discovered = False

    def register(self, spec: SkillSpec):
        """Register a skill. Called by the @skill decorator."""
        self._skills[spec.name] = spec

    def discover(self, package: str = "app.agent.skills"):
        """Import all modules in the package to trigger @skill registrations."""
        if self._discovered:
            return
        pkg = importlib.import_module(package)
        for importer, mod_name, is_pkg in pkgutil.iter_modules(
            getattr(pkg, "__path__", [])
        ):
            if mod_name.startswith("_"):
                continue
            try:
                importlib.import_module(f"{package}.{mod_name}")
            except Exception as e:
                logger.warning("[SkillRegistry] Failed to import %s.%s: %s",
                               package, mod_name, e)
        self._discovered = True
        logger.info("[SkillRegistry] Discovered %d skills from %s",
                    len(self._skills), package)

    def build_managed_agents(
        self,
        smol_model,
        tool_map: Dict[str, Any],
        agent_class=None,
        base_kwargs: Dict[str, Any] = None,
    ) -> list:
        """Build smolagents managed agents from registered skills.

        Args:
            smol_model: smolagents model instance
            tool_map: {tool_name: Tool} mapping from build_all_tools()
            agent_class: smolagents Agent class (CodeAgent or ToolCallingAgent)
            base_kwargs: shared kwargs for all agents (model, max_steps, etc.)
        """
        from smolagents import CodeAgent, LogLevel

        if agent_class is None:
            agent_class = CodeAgent
        if base_kwargs is None:
            base_kwargs = dict(
                model=smol_model,
                max_steps=8,
                verbosity_level=LogLevel.INFO,
                stream_outputs=True,
                provide_run_summary=True,
            )

        # Sort by priority (higher = first)
        sorted_skills = sorted(
            self._skills.values(), key=lambda s: s.priority, reverse=True
        )

        agents = []
        for spec in sorted_skills:
            # Resolve tools by name
            tools = [tool_map[name] for name in spec.tools if name in tool_map]

            kwargs = {**base_kwargs, **spec.extra_kwargs}
            try:
                agent = agent_class(
                    tools=tools,
                    name=spec.name,
                    description=spec.description,
                    instructions=spec.instructions,
                    **kwargs,
                )
                agents.append(agent)
            except Exception as e:
                logger.warning("[SkillRegistry] Failed to build agent '%s': %s",
                               spec.name, e)

        return agents

    def get(self, name: str) -> Optional[SkillSpec]:
        return self._skills.get(name)

    @property
    def all_names(self) -> List[str]:
        return list(self._skills.keys())

    def __len__(self):
        return len(self._skills)

    def __contains__(self, name: str):
        return name in self._skills


# ── Global singleton ──
skill_registry = SkillRegistry()


# ═══════════════════════════════════════════════════════════════
# @skill decorator
# ═══════════════════════════════════════════════════════════════

def skill(
    name: str,
    description: str,
    instructions: str,
    tools: List[str] = None,
    max_steps: int = 8,
    priority: int = 0,
    **extra_kwargs,
):
    """Decorator to register a managed agent skill.

    Can be applied to a class or used as a standalone decorator:

        @skill(
            name="analysis_agent",
            description="股票分析专家",
            instructions="你是技术分析专家...",
            tools=["get_realtime_quote", "agent_get_kline"],
        )
        class AnalysisAgent:
            pass

    The decorated class/function is stored in the registry but not wrapped.
    """
    def decorator(cls_or_fn) -> Callable:
        spec = SkillSpec(
            name=name,
            description=description,
            instructions=instructions,
            tools=tools or [],
            max_steps=max_steps,
            priority=priority,
            extra_kwargs=extra_kwargs,
            cls=cls_or_fn if isinstance(cls_or_fn, type) else None,
        )
        skill_registry.register(spec)
        return cls_or_fn
    return decorator
