# -*- coding: utf-8 -*-
"""
Context Builder — 统一上下文组装模块（Agent-Template 模式）。

职责：
  - 按优先级组装 6 层上下文
  - Token 预算管理：总预算内按优先级分配
  - 统一入口：所有需要构建 prompt 的地方都走这里

6 层优先级（从高到低）：
  1. Persona / 角色定义（始终加载）
  2. Rules / 行为约束（始终加载）
  3. Skills / 技能摘要（始终加载摘要，完整内容按需）
  4. Tools / 工具 schema（始终加载）
  5. History / 对话历史（压缩后的 context_summary）
  6. Memory / 长期记忆（memory.md 摘要）

用法：
  from app.agent.context_builder import ContextBuilder
  ctx = ContextBuilder(total_budget=8000)
  ctx.set_persona(...)
  ctx.set_rules(...)
  ctx.set_skills(...)
  ctx.set_tools(...)
  ctx.set_history(...)
  ctx.set_memory(...)
  prompt = ctx.build()
"""
from __future__ import annotations

from app.agent.log import logger
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ── Token 估算 ──────────────────────────────────────────────
def _estimate_tokens(text: str) -> int:
    """粗估 token 数（中英混合，2 字符 ≈ 1 token）。"""
    if not text:
        return 0
    return max(1, int(len(text) / 2))


@dataclass
class ContextLayer:
    """单层上下文。"""
    name: str
    priority: int          # 越小越优先
    content: str = ""
    required: bool = True  # True=必须保留，False=可被裁剪
    tokens: int = 0

    def __post_init__(self):
        if self.content:
            self.tokens = _estimate_tokens(self.content)


class ContextBuilder:
    """统一上下文组装器。

    Args:
        total_budget: 总 token 预算（默认 8000）
    """

    def __init__(self, total_budget: int = 8000):
        self.total_budget = total_budget
        self._layers: Dict[str, ContextLayer] = {}

    def set_layer(self, name: str, priority: int, content: str, required: bool = True):
        """设置一层上下文。"""
        if content:
            self._layers[name] = ContextLayer(
                name=name, priority=priority, content=content, required=required,
            )
        elif name in self._layers:
            del self._layers[name]

    # ── 便捷方法 ─────────────────────────────────────────────

    def set_persona(self, content: str):
        """Layer 1: 角色定义（必须）。"""
        self.set_layer("persona", priority=1, content=content, required=True)

    def set_rules(self, content: str):
        """Layer 2: 行为约束（必须）。"""
        self.set_layer("rules", priority=2, content=content, required=True)

    def set_skills_summary(self, content: str):
        """Layer 3a: 技能摘要（必须）。"""
        self.set_layer("skills_summary", priority=3, content=content, required=True)

    def set_skill_body(self, skill_name: str, content: str):
        """Layer 3b: 单个技能完整内容（按需加载，可裁剪）。"""
        self.set_layer(f"skill:{skill_name}", priority=3, content=content, required=False)

    def set_tools(self, content: str):
        """Layer 4: 工具 schema（必须）。"""
        self.set_layer("tools", priority=4, content=content, required=True)

    def set_history(self, content: str):
        """Layer 5: 对话历史摘要（可裁剪）。"""
        self.set_layer("history", priority=5, content=content, required=False)

    def set_memory(self, content: str):
        """Layer 6: 长期记忆（可裁剪）。"""
        self.set_layer("memory", priority=6, content=content, required=False)

    def set_domain(self, domain: str, instructions: str):
        """领域专属指令（附加到 rules）。"""
        if domain and instructions:
            existing = self._layers.get("rules")
            extra = f"\n\n## 当前领域: {domain}\n\n{instructions}"
            if existing:
                existing.content += extra
                existing.tokens = _estimate_tokens(existing.content)
            else:
                self.set_layer("domain_rules", priority=2, content=extra, required=True)

    # ── 构建 ─────────────────────────────────────────────────

    def build(self) -> str:
        """按优先级组装上下文，管理 token 预算。

        Returns:
            组装好的 prompt 字符串
        """
        if not self._layers:
            return ""

        # 按优先级排序
        sorted_layers = sorted(self._layers.values(), key=lambda x: x.priority)

        # 计算总 token
        total_tokens = sum(l.tokens for l in sorted_layers)

        # 如果不超预算，直接拼接
        if total_tokens <= self.total_budget:
            parts = [l.content for l in sorted_layers if l.content]
            logger.debug(
                "[ContextBuilder] %d 层, %d tokens (预算 %d), 无裁剪",
                len(parts), total_tokens, self.total_budget,
            )
            return "\n\n".join(parts)

        # 超预算：按优先级裁剪
        logger.info(
            "[ContextBuilder] %d 层, %d tokens 超预算 %d，开始裁剪",
            len(sorted_layers), total_tokens, self.total_budget,
        )

        # Step 1: 先丢弃所有 optional 层
        required_layers = [l for l in sorted_layers if l.required]
        required_tokens = sum(l.tokens for l in required_layers)

        if required_tokens <= self.total_budget:
            # required 层不超预算，optional 层按优先级填充剩余空间
            budget_for_optional = self.total_budget - required_tokens
            used_optional = 0
            result_parts = []
            for layer in sorted_layers:
                if layer.required:
                    result_parts.append(layer.content)
                elif used_optional + layer.tokens <= budget_for_optional:
                    result_parts.append(layer.content)
                    used_optional += layer.tokens
                else:
                    remaining = budget_for_optional - used_optional
                    if remaining > 100:
                        truncated = layer.content[:remaining * 2]
                        result_parts.append(truncated)
                        logger.debug("[ContextBuilder] 截断 optional 层 '%s' (%d → ~%d tokens)",
                                     layer.name, layer.tokens, remaining)
                    break
            return "\n\n".join(p for p in result_parts if p)

        # Step 2: required 层也超预算，从最高优先级开始保留，截断最低优先级
        budget_remaining = self.total_budget
        result_parts = []
        for layer in required_layers:  # 按优先级从高到低
            if layer.tokens <= budget_remaining:
                result_parts.append(layer.content)
                budget_remaining -= layer.tokens
            else:
                # 这层放不下，截断
                truncated = layer.content[:budget_remaining * 2]
                result_parts.append(truncated)
                logger.warning("[ContextBuilder] 截断 required 层 '%s' (%d → ~%d tokens)",
                               layer.name, layer.tokens, budget_remaining)
                break  # 后面更低优先级的不再放
        return "\n\n".join(p for p in result_parts if p)

    def get_stats(self) -> Dict[str, int]:
        """返回各层 token 统计。"""
        return {
            name: layer.tokens
            for name, layer in sorted(self._layers.items(), key=lambda x: x[1].priority)
        }
