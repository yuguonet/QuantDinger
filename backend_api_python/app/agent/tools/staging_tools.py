# -*- coding: utf-8 -*-
"""暂存区工具注入（2026-09-12）：把 stage_write/read/list 注册为 executor 可见函数。

设计：跟随 tools/ 的既有注册方式（公开函数 + docstring 首行 = 描述），
domain="common"。scope 由任务书下发（_run_phase_step 生成 run scope 并写进任务书）。
"""
from __future__ import annotations

from tools.staging import stage_list, stage_read, stage_write

__all__ = ["stage_write", "stage_read", "stage_list"]
