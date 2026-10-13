# -*- coding: utf-8 -*-
"""knowledge 域分类学（DomainSpec，占位最小集）。

知识域无交易日口径、无数据域词典；有分类学需求时在此扩展。

★ 2026-10-14：`primary=False` 随 DomainSpec 的 `primary` 字段一并删除（该字段只被
  零消费方的 `fallback_domain()` 读）。扩展时请只填**仍有消费方**的字段：
  entity_types / trading_calendar / verb_nouns / word_pattern
  （见 `app/agent/domain_registry.py::DomainSpec` 头注）。
"""
from __future__ import annotations

from ...domain_registry import DomainSpec

SPEC = DomainSpec(
    name="knowledge",
)
