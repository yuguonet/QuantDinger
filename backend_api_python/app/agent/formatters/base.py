# -*- coding: utf-8 -*-
"""
formatters/base.py — 格式化基类 + 注册表

设计模式：和 resolvers/ 一样的注册表模式。
  - BaseFormatter：抽象基类，定义 format() 接口
  - _REGISTRY：全局注册表，key=领域名或实体类型, value=formatter_class
  - register_formatter()：装饰器，注册 formatter
  - get_formatter()：按 domain → entity_type 查找 formatter，找不到返回 default

key 语义（2026-09-13 修复）：注册方用的是【领域名】（如 register_formatter("finance")），
而调用方曾只传 entity_type（"stock"）——key 语义不一致使 finance formatter 永不命中。
现查找顺序为 domain（领域级标准输出，多领域可复用同一模板）→ entity_type
（领域内某实体单独定制）→ default，既可扩展又向后兼容。
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


def get_formatter(entity_type: str = "", domain: str = "") -> "BaseFormatter":
    """查找 formatter：domain（优先）→ entity_type → default。

    Args:
        entity_type: 实体类型（stock/commodity/crypto/...）
        domain: 工具域（selected_domain，如 finance）。领域级标准输出按此命中，
            多领域复用同一套输出规范；entity_type 供"领域内单实体定制"扩展。

    Returns:
        formatter 实例（永不返回 None：无匹配时为 DefaultFormatter）。
    """
    for key, kind in ((domain, "domain"), (entity_type, "entity_type")):
        if key and key in _REGISTRY:
            cls = _REGISTRY[key]
            logger.debug("[Formatter] 匹配: %s=%s → %s", kind, key, cls.__name__)
            return cls()

    # 兜底：default
    from .default import DefaultFormatter
    logger.debug("[Formatter] 兜底: domain=%s entity_type=%s → DefaultFormatter",
                 domain, entity_type)
    return DefaultFormatter()


def list_formatters() -> Dict[str, str]:
    """已注册 formatter 的快照（key → 类名），供启动自检/排查"注册断链"。"""
    return {k: v.__name__ for k, v in _REGISTRY.items()}


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
