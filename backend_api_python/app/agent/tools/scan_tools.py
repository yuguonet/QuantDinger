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
from typing import Any, Dict

logger = logging.getLogger(__name__)

SCAN_TOOLS = []


def _register_scan_tools():
    if os.getenv("AGENT_SCAN_PROJECT_READONLY", "true").lower() != "true":
        return

    from app.agent.project_scanner import list_project_files, read_project_file, grep_project

    SCAN_TOOLS.extend([
        {
            "fn": list_project_files,
            "name": "list_project_files",
            "description": (
                "列出项目源码目录结构（只读）。"
                "用于了解项目架构、查找相关代码文件。"
                "默认扫描 backend_api_python/app/agent/tools、backend_api_python/app/services、"
                "QuantDinger-Vue/src/api、QuantDinger-Vue/src/views。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "max_depth": {
                        "type": "integer",
                        "description": "目录扫描深度（默认 3）",
                        "default": 3,
                    },
                },
            },
        },
        {
            "fn": read_project_file,
            "name": "read_project_file",
            "description": (
                "读取项目源码文件内容（只读，不可修改）。"
                "路径相对于项目根目录（如 backend_api_python/app/services/llm.py）。"
                "最大 500KB。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件路径（相对于项目根目录）",
                    },
                },
                "required": ["path"],
            },
        },
        {
            "fn": grep_project,
            "name": "grep_project",
            "description": (
                "在项目源码中搜索代码片段（只读，支持正则表达式）。"
                "用于定位函数定义、查找变量引用、分析调用链等。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "搜索正则表达式（如 def build_agent|AGENT_TOOLS）",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "最大返回结果数（默认 50）",
                        "default": 50,
                    },
                },
                "required": ["pattern"],
            },
        },
    ])
    logger.info("[ScanTools] Registered %d read-only scan tools", len(SCAN_TOOLS))


_register_scan_tools()
