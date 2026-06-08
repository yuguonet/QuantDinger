# -*- coding: utf-8 -*-
"""
Skill Registry — BaseSkill 子类自动发现 + 注册。

生命周期：
  1. 各 skill 模块定义 BaseSkill 子类（technical.py, momentum.py 等）
  2. registry.discover() 导入 skills/ 包下所有模块
  3. registry.get(name) → BaseSkill 实例
  4. registry.all_skills → 按 priority 排序的 Skill 列表

与旧版区别：
  - 旧版：@skill 装饰器注册 → build_managed_agents() 构建 smolagents ManagedAgent
  - 新版：BaseSkill 子类自动发现 → 直接实例化，不依赖 smolagents
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import Any, Dict, List, Optional, Type

from app.agent.skills.base import BaseSkill

logger = logging.getLogger(__name__)


class SkillRegistry:
    """BaseSkill 子类注册中心。"""

    def __init__(self):
        self._skills: Dict[str, Type[BaseSkill]] = {}
        self._instances: Dict[str, BaseSkill] = {}
        self._discovered = False

    def register(self, cls: Type[BaseSkill]):
        """注册一个 BaseSkill 子类。"""
        if not hasattr(cls, "name") or not cls.name:
            logger.warning("[SkillRegistry] 跳过无 name 的类: %s", cls)
            return
        self._skills[cls.name] = cls
        logger.debug("[SkillRegistry] 注册: %s", cls.name)

    def discover(self, package: str = "app.agent.skills"):
        """导入包下所有模块，自动发现 BaseSkill 子类并注册。"""
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
                # 扫描模块中的 BaseSkill 子类
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

    def __len__(self):
        return len(self._skills)

    def __contains__(self, name: str):
        return name in self._skills


# ── 全局单例 ──
skill_registry = SkillRegistry()
