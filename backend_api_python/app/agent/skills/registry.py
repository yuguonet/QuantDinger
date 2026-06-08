# -*- coding: utf-8 -*-
"""
Skill Registry — @skill 装饰器自注册 + 自动发现 + Managed Agent 构建。

生命周期：
  1. 各 skill 模块用 @skill(...) 装饰器注册（technical.py, momentum.py 等）
  2. skill_registry.discover() 导入 skills/ 包下所有模块，触发注册
  3. skill_registry.build_managed_agents() 构建 smolagents Managed Agent 列表

已注册的 15 个 Skill：
  technical_agent    — 技术分析（趋势/量价/均线/指标/形态）
  momentum_tracker   — 动量追踪（趋势强度/突破/择时）
  intelligence_agent — 情报分析（新闻/事件驱动/概念催化）
  screening_agent    — 选股筛选（条件/动量/概念/龙虎榜）
  backtest_agent     — 策略回测（A股规则：T+1/涨跌停/印花税）
  trading_agent      — 交易执行（策略启停/持仓管理）
  policy_analyst     — 政策面分析
  hot_money_tracker  — 游资追踪
  lockup_watcher     — 解禁监控
  concept_tracker    — 概念追踪
  market_data_agent  — 市场数据
  indicator_agent    — 指标信号
  bull_researcher    — 多头论证
  bear_researcher    — 空头反驳
  data_engineer      — 数据工程

被调用方：
  agent.py → _build_managed_agents() → skill_registry.discover() + build_managed_agents()

公开接口：
  skill_registry.discover(package) → None
  skill_registry.build_managed_agents(smol_model, tool_map, agent_class, base_kwargs) → list
  skill_registry.get(name) → Optional[SkillSpec]
  skill_registry.all_names → List[str]
  @skill(name, description, instructions, tools, max_steps, priority, **extra_kwargs) → decorator
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
