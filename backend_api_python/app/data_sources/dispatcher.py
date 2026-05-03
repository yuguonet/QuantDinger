# -*- coding: utf-8 -*-
"""
调度器 — 向后兼容层（已迁移）

本模块是历史遗留的兼容入口，核心逻辑已迁移至 factory.py：
  - sequential_fallback → factory.sequential_fallback
  - race                → factory.race
  - InflightDedup       → factory.InflightDedup

保留此文件的目的是：
  1. 外部代码（如 QuantDinger）如果通过旧路径导入，不会报 ImportError
  2. 渐进式迁移：先迁移内部，再迁移外部调用方

迁移状态: 核心逻辑已完全迁移，本文件仅保留 re-export。
"""

from app.data_sources.factory import (  # noqa: F401
    InflightDedup,
    sequential_fallback,
    race,
)
