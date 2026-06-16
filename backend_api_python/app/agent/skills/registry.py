# -*- coding: utf-8 -*-
"""
Skill Registry — 统一 Skill 注册中心。

支持三种注册方式（殊途同归，最终都是 BaseSkill 子类）：

  方式 1: @skill 装饰器（显式参数）
    @skill(name="xxx", description="...", tools=[...], priority=5)
    class MySkill:
        pass

  方式 2: @skill 装饰器（auto_load，从 SKILL.md 加载）
    @skill("technical_agent", auto_load=True)
    class TechnicalSkill:
        pass

  方式 3: 直接继承 BaseSkill（灵活，可覆盖 build_prompt/analyze）
    class MySkill(BaseSkill):
        name = "xxx"
        description = "..."
        tools = [...]

生命周期：
  1. 模块加载时 @skill 装饰器自动注册，或 BaseSkill 子类被 discover() 发现
  2. skill_registry.discover() 导入 skills/ 包下所有模块
  3. skill_registry.get(name) → BaseSkill 实例
  4. skill_registry.all_skills → 按 priority 排序的 Skill 列表

Phase 2 变更（SEMANTICS_REFACTOR）：
  - auto_load=True 时，从 semantics/skills/{name}/SKILL.md 加载元数据和指令
  - SKILL.md 的 YAML frontmatter → name/description/tags/tools/priority/default_weight
  - SKILL.md 的 Markdown body → instructions（领域特定指令）
  - 单一信源：改描述只改 SKILL.md，不再改 Python 代码
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import Any, Callable, Dict, List, Optional, Type

from app.agent.skills.base import BaseSkill

logger = logging.getLogger(__name__)


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
    tags: List[str] = None,
    auto_load: bool = False,
) -> Callable:
    """装饰器：将普通类转换为 BaseSkill 子类并自动注册。

    两种使用模式：

    模式 A — 显式参数（向后兼容）：
        @skill(
            name="technical_agent",
            description="A股技术面+动量分析专家",
            tools=["analyze_trend", "get_indicator_snapshot"],
            tags=["finance", "technical"],
            priority=9,
            instructions="你是技术分析师...",
        )
        class TechnicalSkill:
            pass

    模式 B — 从 SKILL.md 自动加载（Phase 2 新增）：
        @skill("technical_agent", auto_load=True)
        class TechnicalSkill:
            # 可选: 定义 analyze/algo_analyze 方法覆盖默认实现
            pass

    auto_load=True 时：
        - 从 semantics/skills/{name}/SKILL.md 加载元数据（frontmatter）
        - Markdown body 作为 instructions（领域特定指令）
        - 传入的显式参数会覆盖 SKILL.md 中的值（向后兼容）

    Args:
        name: 技能唯一标识（必填）
        description: 技能描述（auto_load 时从 SKILL.md 加载）
        tools: 依赖的工具名列表（auto_load 时从 SKILL.md 加载）
        instructions: 给 LLM 的指令（auto_load 时从 SKILL.md body 加载）
        priority: 优先级（auto_load 时从 SKILL.md 加载）
        default_weight: 出厂权重（auto_load 时从 SKILL.md 加载）
        tags: 标签列表（auto_load 时从 SKILL.md 加载）
        auto_load: 是否从 SKILL.md 自动加载元数据

    Returns:
        装饰器函数
    """
    # ── auto_load: 从 semantics 加载 SKILL.md ──
    if auto_load:
        from app.agent.semantics import get_skill_meta
        meta = get_skill_meta(name)
        if meta is None:
            logger.warning("[Skill] auto_load 找不到 SKILL.md: %s，使用传入参数", name)
        else:
            # SKILL.md 元数据作为默认值，显式参数可覆盖
            description = description or meta.description
            tools = tools or meta.tools
            instructions = instructions or meta.instructions
            priority = priority or meta.priority
            default_weight = default_weight if default_weight != 1.0 else meta.default_weight
            tags = tags or meta.tags
            logger.debug("[Skill] auto_load %s: %d tools, %s", name, len(tools or []), description[:40])

    tools = tools or []
    tags = tags or []

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
            "tags": list(tags),
            # 保留原始类的模块和限定名
            "__module__": cls.__module__,
            "__qualname__": cls.__qualname__,
        })

        # 继承原始类的自定义类属性 (如 _TOOLS_NEED_STRATEGY)
        for k, v in cls.__dict__.items():
            if k.startswith("_") and not k.startswith("__") and k not in skill_cls.__dict__:
                setattr(skill_cls, k, v)

        # 如果原始类定义了 analyze 方法，覆盖默认实现
        if custom_analyze is not None:
            skill_cls.analyze = custom_analyze

        # 如果原始类定义了 algo_analyze 方法，覆盖默认实现
        if custom_algo_analyze is not None:
            skill_cls.algo_analyze = custom_algo_analyze

        # 自动注册到全局 registry
        skill_registry.register(skill_cls)

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
        self._skills: Dict[str, Type[BaseSkill]] = {}
        self._instances: Dict[str, BaseSkill] = {}
        self._discovered = False

    def register(self, cls: Type[BaseSkill]):
        """注册一个 BaseSkill 子类。"""
        if not hasattr(cls, "name") or not cls.name:
            logger.warning("[SkillRegistry] 跳过无 name 的类: %s", cls)
            return
        if cls.name in self._skills:
            logger.debug("[SkillRegistry] 覆盖注册: %s", cls.name)
        self._skills[cls.name] = cls
        # 清除旧实例缓存（如果覆盖注册）
        self._instances.pop(cls.name, None)
        logger.debug("[SkillRegistry] 注册: %s (priority=%s)", cls.name, getattr(cls, "priority", 0))

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
                    ):
                        self.register(attr)
            except Exception as e:
                logger.warning("[SkillRegistry] 导入 %s.%s 失败: %s", package, mod_name, e)

        self._discovered = True
        logger.info("[SkillRegistry] 发现 %d 个 Skill: %s",
                    len(self._skills), list(self._skills.keys()))

    def get(self, name: str) -> Optional[BaseSkill]:
        """获取 Skill 实例（懒加载单例）。"""
        if name in self._instances:
            return self._instances[name]

        cls = self._skills.get(name)
        if cls is None:
            return None

        instance = cls()
        self._instances[name] = instance
        return instance

    def get_class(self, name: str) -> Optional[Type[BaseSkill]]:
        """获取 Skill 类（非实例）。"""
        return self._skills.get(name)

    @property
    def all_skills(self) -> List[BaseSkill]:
        """返回所有已注册 Skill 的实例，按 priority 降序排列。"""
        return [
            self.get(name)
            for name, cls in sorted(
                self._skills.items(),
                key=lambda x: getattr(x[1], "priority", 0),
                reverse=True,
            )
        ]

    @property
    def all_names(self) -> List[str]:
        return list(self._skills.keys())

    def get_by_tag(self, tag: str) -> List[BaseSkill]:
        """按标签过滤 Skill。"""
        return [
            self.get(name)
            for name, cls in self._skills.items()
            if tag in getattr(cls, "tags", [])
        ]

    def get_by_tags(self, tags: List[str]) -> List[BaseSkill]:
        """按多个标签过滤（OR 语义，命中任一标签即返回）。"""
        tag_set = set(tags)
        return [
            self.get(name)
            for name, cls in self._skills.items()
            if tag_set & set(getattr(cls, "tags", []))
        ]

    def __len__(self):
        return len(self._skills)

    def __contains__(self, name: str):
        return name in self._skills


# ── 全局单例 ──
skill_registry = SkillRegistry()
