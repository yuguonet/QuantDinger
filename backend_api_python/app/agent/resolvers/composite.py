# -*- coding: utf-8 -*-
"""实体解析器组合（chat 阶段统一入口）。

为什么需要这一层（2026-09-13 接线修复）：
  NodeContext.entity_resolver 的契约是 EntityResolver（nodes.py 调 `.resolve()`），
  但 task_agent 之前注入的是裸函数 `_combined_resolver` —— 调用处 AttributeError
  被 chat_node 的 `except Exception: logger.debug(...)` 吞掉，导致 **chat 阶段的
  实体解析与澄清反问在线上从未执行过**（"声明了没接线"类，与 L2/L3 同源）。
  现把组合逻辑收敛成本模块的实体，注入即满足契约，且下一处接线错误会以
  warning 暴露而不是静默。

顺序即语义（不可互换）：
  1. **澄清优先**：任一子解析器 needs_clarify → 立即返回，不再继续、不再合并。
     把不确定的输入拼进任务描述会让 LLM 顺着错误假设往下做，整份结论作废。
  2. **上下文累积**：子解析器按序执行，前序结果以 ctx 交给后续子解析器。
     chat 先于 plan、拿不到 selected_domain，只能由已识别实体倒推领域
     （见 domain_registry.entity_to_domain）——这就是"先标的、后时间"的原因。

扩展新领域（如 crypto/futures）：把新解析器（或 `ctx -> EntityResolver` 工厂）
加进 children 即可，调用方无需改动。
"""
from __future__ import annotations

import logging
from typing import Callable, List, Optional, Union

from .base import EntityResolver, ResolveResult

logger = logging.getLogger(__name__)


class CompositeResolver(EntityResolver):
    """按序执行子解析器并合并结果；任一要求澄清即短路返回。

    children 元素可以是：
      - EntityResolver 实例：直接执行；
      - 工厂 ``ctx -> Optional[EntityResolver]``：按前序累积的 ctx 惰性创建。
        ctx = {"entities": [...], "entity_types": [...]}（按序、去重累积）。
        需要"依赖前序结果"的解析器（如时间解析依赖标的反推的领域）用工厂。
    """

    def __init__(self, children: List[Union[EntityResolver, Callable]]):
        self._children = [c for c in (children or []) if c]

    def resolve(self, user_input: str) -> Optional[ResolveResult]:
        results: List[ResolveResult] = []
        ctx = {"entities": [], "entity_types": []}
        for child in self._children:
            try:
                resolver = child(ctx) if callable(child) else child
            except Exception as e:
                logger.warning("[CompositeResolver] 解析器创建失败，跳过: %s", e)
                continue
            if resolver is None:
                continue
            try:
                r = resolver.resolve(user_input)
            except Exception as e:
                logger.warning("[CompositeResolver] %s 解析失败，跳过: %s",
                               type(resolver).__name__, e)
                continue
            if r is None:
                continue
            # 澄清优先：不继续、不合并——拿不到准确信息就不往下走
            if getattr(r, "needs_clarify", False):
                logger.info("[CompositeResolver] %s 要求澄清 → 短路返回",
                            type(resolver).__name__)
                return r
            results.append(r)
            for _e in (r.entities or []):
                if not isinstance(_e, dict):
                    continue
                ctx["entities"].append(_e)
                _t = _e.get("type")
                if _t and _t not in ctx["entity_types"]:
                    ctx["entity_types"].append(_t)
        return self._merge(user_input, results)

    @staticmethod
    def _merge(user_input: str, results: List[ResolveResult]) -> Optional[ResolveResult]:
        """合并结果：首个产出 effective_input 者为主，其余作为增量追加。"""
        if not results:
            return None
        primary = next((r for r in results if r and r.effective_input), None)
        if primary is None:
            return None
        extras = []
        for r in results:
            if r is primary or not r:
                continue
            if r.effective_input and r.effective_input != user_input:
                extras.append(r.effective_input[len(user_input):].lstrip("，, "))
        merged = primary.effective_input
        for extra in extras:
            merged = f"{merged}；{extra}"
        return ResolveResult(
            entities=sum((r.entities for r in results if r), []),
            entity_code=primary.entity_code,
            entity_name=primary.entity_name,
            entity_type=primary.entity_type,
            effective_input=merged,
        )
