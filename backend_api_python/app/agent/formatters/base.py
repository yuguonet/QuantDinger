# -*- coding: utf-8 -*-
"""
formatters/base.py — 格式化基类 + 注册表

设计模式：和 resolvers/ 一样的注册表模式。
  - BaseFormatter：抽象基类，定义 format() 接口
  - _REGISTRY：全局注册表，key=entity_type, value=formatter_class
  - register_formatter()：装饰器，注册 formatter
  - get_formatter()：根据 entity_type 查找 formatter，找不到返回 default
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Dict, Optional, Type

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
#  注册表
# ═══════════════════════════════════════════════════════════════

_REGISTRY: Dict[str, Type["BaseFormatter"]] = {}


def register_formatter(entity_type: str):
    """装饰器：注册 formatter 到全局注册表。

    用法：
        @register_formatter("finance")
        class FinanceFormatter(BaseFormatter):
            ...
    """
    def decorator(cls: Type[BaseFormatter]):
        _REGISTRY[entity_type] = cls
        logger.debug("[Formatter] 注册: entity_type=%s → %s", entity_type, cls.__name__)
        return cls
    return decorator


def get_formatter(entity_type: str) -> "BaseFormatter":
    """根据 entity_type 查找 formatter。

    优先精确匹配，找不到返回 default formatter。
    """
    if entity_type in _REGISTRY:
        cls = _REGISTRY[entity_type]
        logger.debug("[Formatter] 匹配: entity_type=%s → %s", entity_type, cls.__name__)
        return cls()

    # 兜底：default
    from .default import DefaultFormatter
    logger.debug("[Formatter] 兜底: entity_type=%s → DefaultFormatter", entity_type)
    return DefaultFormatter()


# ═══════════════════════════════════════════════════════════════
#  基类
# ═══════════════════════════════════════════════════════════════

class BaseFormatter(ABC):
    """结果格式化基类。

    子类实现 format() 方法，将 CodeAgent 的原始输出汇总为结构化报告。
    """

    @abstractmethod
    async def format(self, raw_result: str, context: dict) -> str:
        """格式化/汇总结果。

        Args:
            raw_result: CodeAgent 的原始输出（final_answer 的内容）
            context: 上下文信息，包含：
                - entity_type: 实体类型（stock/commodity/crypto/...）
                - entity_code: 实体代码
                - entity_name: 实体名称
                - task: 任务描述
                - user_input: 用户原始输入
                - selected_skill: 选中的技能名（应为空，有值时不会调用 formatter）
                - skill_body: 技能正文

        Returns:
            格式化后的报告字符串
        """
        pass

    def _build_prompt(self, raw_result: str, context: dict, system_prompt: str) -> tuple[str, str]:
        """构建 LLM 消息（system + user）。

        子类可复用此方法构建 prompt。

        Returns:
            (system_content, user_content) 元组
        """
        user_parts = []

        # 任务信息
        task = context.get("task", "")
        if task:
            user_parts.append(f"【任务】\n{task}")

        # 实体信息
        entity_type = context.get("entity_type", "")
        entity_code = context.get("entity_code", "")
        entity_name = context.get("entity_name", "")
        if entity_code:
            entity_desc = f"{entity_name}({entity_code})" if entity_name else entity_code
            user_parts.append(f"【实体】{entity_desc} [{entity_type}]")

        # 原始数据
        user_parts.append(f"【原始数据】\n{raw_result}")

        user_content = "\n\n".join(user_parts)
        return system_prompt, user_content
