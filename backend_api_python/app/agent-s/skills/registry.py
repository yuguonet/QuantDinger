# -*- coding: utf-8 -*-
"""
Skill Registry — 统一 Skill 注册中心。

支持两种注册方式（殊途同归，最终都是 BaseSkill 子类）：

  方式 1: @skill 装饰器（推荐，简洁）
    @skill(name="xxx", description="...", tools=[...], priority=5)
    class MySkill:
        pass

  方式 2: 直接继承 BaseSkill（灵活，可覆盖 build_prompt/analyze）
    class MySkill(BaseSkill):
        name = "xxx"
        description = "..."
        tools = [...]

生命周期：
  1. 模块加载时 @skill 装饰器自动注册，或 BaseSkill 子类被 discover() 发现
  2. skill_registry.discover() 导入 skills/ 包下所有模块
  3. skill_registry.get(name) → BaseSkill 实例
  4. skill_registry.all_skills → 按 priority 排序的 Skill 列表
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Type

from app.agent.skills.base import BaseSkill

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# SkillSpec — Skill 元数据 + 子 agent 配置
# ═══════════════════════════════════════════════════════════════

@dataclass
class SkillSpec:
    """Skill 完整元数据，含子 agent 构造参数。

    Attributes:
        cls: BaseSkill 子类
        name: 技能唯一标识
        description: 技能描述
        tools: 依赖的工具名列表
        instructions: 给 LLM 的指令（注入到 prompt 中）
        priority: 优先级（越高越先执行）
        default_weight: 出厂权重
        max_steps: 子 agent 最大步数（默认 2: execute_skill + final_answer）
        run_summary: 是否返回执行摘要（默认 True）
    """
    cls: Type[BaseSkill]
    name: str
    description: str
    tools: List[str]
    instructions: str
    priority: int
    default_weight: float
    max_steps: int = 2
    run_summary: bool = True


# ═══════════════════════════════════════════════════════════════
# @skill 装饰器
# ═══════════════════════════════════════════════════════════════

def skill(
    name: str,
    description: str = "",
    tools: List[str] = None,
    instructions: str = "",
    priority: int = 0,
    default_weight: float = 1.0,
    max_steps: int = 2,
    run_summary: bool = True,
) -> Callable:
    """装饰器：将普通类转换为 BaseSkill 子类并自动注册。

    使用示例：
        @skill(
            name="technical_agent",
            description="A股技术面+动量分析专家",
            tools=["analyze_trend", "get_indicator_snapshot"],
            priority=9,
            max_steps=3,
        )
        class TechnicalSkill:
            pass

    装饰后：
        - MomentumTrackerSkill 变成 BaseSkill 的子类
        - 自动注册到全局 skill_registry
        - instructions 存储在类属性中，由 BaseSkill.build_prompt() 使用
        - max_steps / run_summary 存储在 SkillSpec 中，用于子 agent 构造

    Args:
        name: 技能唯一标识
        description: 技能描述
        tools: 依赖的工具名列表
        instructions: 给 LLM 的指令（注入到 prompt 中）
        priority: 优先级（越高越先执行）
        default_weight: 出厂权重
        max_steps: 子 agent 最大步数（默认 2: execute_skill + final_answer）
        run_summary: 是否返回执行摘要（默认 True）

    Returns:
        装饰器函数
    """
    tools = tools or []

    # normalize: 如果 instructions 因尾部逗号变成 tuple，还原为 str
    if isinstance(instructions, (list, tuple)):
        instructions = "\n".join(str(x) for x in instructions)

    def decorator(cls) -> Type[BaseSkill]:
        # 从装饰的类中提取 analyze 和 algo_analyze 方法（如果有）
        custom_analyze = cls.__dict__.get("analyze")
        custom_algo_analyze = cls.__dict__.get("algo_analyze")

        # 动态创建 BaseSkill 子类
        skill_cls = type(cls.__name__, (BaseSkill,), {
            "name": name,
            "description": description,
            "tools": list(tools),
            "instructions": instructions,
            "priority": priority,
            "default_weight": default_weight,
            # 保留原始类的模块和限定名
            "__module__": cls.__module__,
            "__qualname__": cls.__qualname__,
        })

        # 如果原始类定义了 analyze 方法，覆盖默认实现
        if custom_analyze is not None:
            skill_cls.analyze = custom_analyze

        # 如果原始类定义了 algo_analyze 方法，覆盖默认实现
        if custom_algo_analyze is not None:
            skill_cls.algo_analyze = custom_algo_analyze

        # 自动注册到全局 registry（传 SkillSpec）
        spec = SkillSpec(
            cls=skill_cls,
            name=name,
            description=description,
            tools=list(tools),
            instructions=instructions,
            priority=priority,
            default_weight=default_weight,
            max_steps=max_steps,
            run_summary=run_summary,
        )
        skill_registry.register(spec)

        return skill_cls

    return decorator


# ═══════════════════════════════════════════════════════════════
# Skill Registry
# ═══════════════════════════════════════════════════════════════

class SkillRegistry:
    """BaseSkill 子类注册中心。

    支持两种来源：
      1. @skill 装饰器自动注册（模块加载时发生）
      2. BaseSkill 子类自动发现（discover() 时扫描）
    """

    def __init__(self):
        self._specs: Dict[str, SkillSpec] = {}      # name → SkillSpec
        self._instances: Dict[str, BaseSkill] = {}  # name → BaseSkill 实例（懒加载）
        self._discovered = False

    def register(self, spec: SkillSpec):
        """注册一个 Skill 的完整元数据。"""
        if not spec.name:
            logger.warning("[SkillRegistry] 跳过无 name 的 spec: %s", spec)
            return
        if spec.name in self._specs:
            logger.debug("[SkillRegistry] 覆盖注册: %s", spec.name)
        self._specs[spec.name] = spec
        # 清除旧实例缓存（如果覆盖注册）
        self._instances.pop(spec.name, None)
        logger.debug("[SkillRegistry] 注册: %s (priority=%s, max_steps=%s)",
                     spec.name, spec.priority, spec.max_steps)

    def discover(self, package: str = "app.agent.skills"):
        """导入包下所有模块，自动发现 BaseSkill 子类并注册。

        @skill 装饰器在模块加载时已自动注册，这里补充发现直接继承 BaseSkill 的类。
        """
        if self._discovered:
            return

        pkg = importlib.import_module(package)
        for importer, mod_name, is_pkg in pkgutil.iter_modules(
            getattr(pkg, "__path__", [])
        ):
            if mod_name.startswith("_"):
                continue
            try:
                mod = importlib.import_module(f"{package}.{mod_name}")
                # 扫描模块中的 BaseSkill 子类（排除 BaseSkill 自身）
                for attr_name in dir(mod):
                    attr = getattr(mod, attr_name)
                    if (
                        isinstance(attr, type)
                        and issubclass(attr, BaseSkill)
                        and attr is not BaseSkill
                        and hasattr(attr, "name")
                        and attr.name
                        and attr.name not in self._specs  # @skill 优先
                    ):
                        spec = SkillSpec(
                            cls=attr,
                            name=attr.name,
                            description=getattr(attr, "description", ""),
                            tools=list(getattr(attr, "tools", [])),
                            instructions=getattr(attr, "instructions", ""),
                            priority=getattr(attr, "priority", 0),
                            default_weight=getattr(attr, "default_weight", 1.0),
                            max_steps=getattr(attr, "max_steps", 2),
                            run_summary=getattr(attr, "run_summary", True),
                        )
                        self.register(spec)
            except Exception as e:
                logger.warning("[SkillRegistry] 导入 %s.%s 失败: %s", package, mod_name, e)

        self._discovered = True
        logger.info("[SkillRegistry] 发现 %d 个 Skill: %s",
                    len(self._specs), list(self._specs.keys()))

    def get(self, name: str) -> Optional[BaseSkill]:
        """获取 Skill 实例（懒加载单例）。"""
        if name in self._instances:
            return self._instances[name]

        spec = self._specs.get(name)
        if spec is None:
            return None

        instance = spec.cls()
        self._instances[name] = instance
        return instance

    def get_class(self, name: str) -> Optional[Type[BaseSkill]]:
        """获取 Skill 类（非实例）。"""
        spec = self._specs.get(name)
        return spec.cls if spec else None

    def get_spec(self, name: str) -> Optional[SkillSpec]:
        """获取 Skill 完整元数据（含子 agent 配置）。"""
        return self._specs.get(name)

    @property
    def all_skills(self) -> List[BaseSkill]:
        """返回所有已注册 Skill 的实例，按 priority 降序排列。"""
        return [
            self.get(name)
            for name in sorted(
                self._specs.keys(),
                key=lambda n: self._specs[n].priority,
                reverse=True,
            )
        ]

    @property
    def all_specs(self) -> List[SkillSpec]:
        """返回所有 SkillSpec，按 priority 降序排列。"""
        return sorted(
            self._specs.values(),
            key=lambda s: s.priority,
            reverse=True,
        )

    @property
    def all_names(self) -> List[str]:
        return list(self._specs.keys())

    def __len__(self):
        return len(self._specs)

    def __contains__(self, name: str):
        return name in self._specs


# ── 全局单例 ──
skill_registry = SkillRegistry()
