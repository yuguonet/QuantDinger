# -*- coding: utf-8 -*-
"""
formatters — 结果格式化/汇总模块

职责：将 CodeAgent 的原始输出汇总为结构化报告。

设计：
  - selected_skill 有值时跳过（SKILL.md 已定义输出规范）
  - 无 skill 时，根据 entity_type 选择对应 formatter
  - 无匹配 formatter 时，使用 default formatter

目录：
  base.py     — BaseFormatter 基类
  default.py  — 通用兜底（纯 LLM 自适应）
  finance.py  — 金融领域模板
"""

from .base import BaseFormatter, get_formatter, register_formatter

# 导入领域 formatter 以触发注册
# from . import finance  # noqa: F401

__all__ = ["BaseFormatter", "get_formatter", "register_formatter"]
