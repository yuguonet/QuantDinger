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

from app.agent.tools.registry import tool

logger = logging.getLogger(__name__)

_ENABLED = os.getenv("AGENT_SCAN_PROJECT_READONLY", "true").lower() == "true"

if _ENABLED:
    from app.agent.project_scanner import list_project_files, read_project_file, grep_project

    @tool(
        name="list_project_files",
        description=(
            "列出项目源码目录结构（只读）。"
            "用于了解项目架构、查找相关代码文件。"
            "默认扫描 backend_api_python/app/agent/tools、backend_api_python/app/services、"
            "QuantDinger-Vue/src/api、QuantDinger-Vue/src/views。"
        ),
        category="项目扫描",
        layer="支撑层",
        domain=["coding"],
    )
    def _list_project_files(max_depth: int = 3):
        return list_project_files(max_depth=max_depth)

    @tool(
        name="read_project_file",
        description=(
            "读取项目源码文件内容（只读，不可修改）。"
            "路径相对于项目根目录（如 backend_api_python/app/services/llm.py）。"
            "最大 500KB。"
        ),
        category="项目扫描",
        layer="支撑层",
        domain=["coding"],
    )
    def _read_project_file(path: str):
        return read_project_file(path=path)

    @tool(
        name="grep_project",
        description=(
            "在项目源码中搜索代码片段（只读，支持正则表达式）。"
            "用于定位函数定义、查找变量引用、分析调用链等。"
        ),
        category="项目扫描",
        layer="支撑层",
        domain=["coding"],
    )
    def _grep_project(pattern: str, max_results: int = 50):
        return grep_project(pattern=pattern, max_results=max_results)

    logger.info("[ScanTools] Registered 3 read-only scan tools via @tool decorator")
