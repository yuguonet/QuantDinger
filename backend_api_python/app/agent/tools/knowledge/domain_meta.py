# -*- coding: utf-8 -*-
"""knowledge 域分类学（DomainSpec，占位最小集）。

知识域无交易日口径、无数据域词典；有分类学需求时在此扩展。
"""
from __future__ import annotations

from domain_registry import DomainSpec

SPEC = DomainSpec(
    name="knowledge",
    primary=False,
)
