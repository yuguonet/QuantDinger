# -*- coding: utf-8 -*-
"""
Project Scan Tools — Agent 只读扫描项目源码的工具集。

配置：
    AGENT_SCAN_PROJECT_READONLY=true   启用源码扫描（只读）
    AGENT_SCAN_PATHS=...               可扫描路径（逗号分隔）
"""
from __future__ import annotations

import os
import logging


logger = logging.getLogger(__name__)

_ENABLED = os.getenv("AGENT_SCAN_PROJECT_READONLY", "true").lower() == "true"

if _ENABLED:
    from app.agent.project_scanner import list_project_files, read_project_file, grep_project

    def _list_project_files(max_depth: int = 3):
        return list_project_files(max_depth=max_depth)

    def _read_project_file(path: str):
        return read_project_file(path=path)

    def _grep_project(pattern: str, max_results: int = 50):
        return grep_project(pattern=pattern, max_results=max_results)

    logger.info("[ScanTools] Registered 3 read-only scan tools via @tool decorator")
