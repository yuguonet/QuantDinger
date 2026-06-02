# -*- coding: utf-8 -*-
"""
Project Scanner — 允许 Agent 只读扫描项目源码。

在 Agent 指令中注入"代码阅读"能力，而非"文件写入"能力。
Agent 可以：
  - list 项目目录结构
  - read 任意源码文件
  - grep/search 代码片段

Agent 不可以：
  - write/create/delete 项目文件
  - 修改项目配置
  - 执行项目级的 shell 命令

配置项：
    AGENT_SCAN_PROJECT_READONLY=true/false  是否启用源码扫描
    AGENT_SCAN_PATHS=...                    可扫描路径（逗号分隔）
"""
from __future__ import annotations

import os
import re
import logging
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent  # QuantDinger root

SCAN_PATHS_DEFAULT = [
    "backend_api_python/app/agent/tools",
    "backend_api_python/app/services",
    "QuantDinger-Vue/src/api",
    "QuantDinger-Vue/src/views",
]


def get_scan_paths() -> List[Path]:
    """从配置读取可扫描路径列表。"""
    raw = os.getenv("AGENT_SCAN_PATHS", "")
    if raw:
        paths = [PROJECT_ROOT / p.strip() for p in raw.split(",") if p.strip()]
    else:
        paths = [PROJECT_ROOT / p for p in SCAN_PATHS_DEFAULT]
    # 安全检查：确保路径在项目根目录内
    safe = []
    for p in paths:
        try:
            p.resolve().relative_to(PROJECT_ROOT.resolve())
            if p.exists():
                safe.append(p)
        except ValueError:
            continue
    return safe


def is_scan_enabled() -> bool:
    return os.getenv("AGENT_SCAN_PROJECT_READONLY", "true").lower() == "true"


def list_project_files(max_depth: int = 3) -> Dict[str, Any]:
    """列出可扫描目录下的文件结构。"""
    if not is_scan_enabled():
        return {"error": "项目扫描未启用（AGENT_SCAN_PROJECT_READONLY=false）"}

    result = {}
    for scan_root in get_scan_paths():
        rel = scan_root.relative_to(PROJECT_ROOT)
        tree = {}
        for p in sorted(scan_root.rglob("*")):
            if p.is_file() and not p.name.startswith("."):
                rel_file = p.relative_to(scan_root)
                depth = len(rel_file.parts)
                if depth <= max_depth:
                    tree[str(rel_file)] = {
                        "size": p.stat().st_size,
                        "ext": p.suffix,
                    }
        result[str(rel)] = tree
    return result


def read_project_file(path: str) -> Dict[str, Any]:
    """只读读取项目源码文件。"""
    if not is_scan_enabled():
        return {"error": "项目扫描未启用（AGENT_SCAN_PROJECT_READONLY=false）"}

    target = (PROJECT_ROOT / path).resolve()
    # 安全检查
    try:
        target.relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        return {"error": f"路径越界: {path}"}

    # 检查是否在允许扫描的路径内
    scan_roots = get_scan_paths()
    in_scope = any(
        str(target).startswith(str(sr.resolve()))
        for sr in scan_roots
    )
    if not in_scope:
        return {"error": f"路径不在扫描范围内: {path}"}

    if not target.exists():
        return {"error": f"文件不存在: {path}"}
    if not target.is_file():
        return {"error": f"不是文件: {path}"}

    # 文件大小限制（防止读取超大文件）
    size = target.stat().st_size
    if size > 500_000:  # 500KB
        return {"error": f"文件过大 ({size} bytes)，请指定具体范围"}

    content = target.read_text(encoding="utf-8", errors="replace")
    return {
        "path": path,
        "content": content,
        "size": size,
        "lines": content.count("\n") + 1,
    }


def grep_project(pattern: str, max_results: int = 50) -> Dict[str, Any]:
    """在可扫描范围内搜索代码。"""
    if not is_scan_enabled():
        return {"error": "项目扫描未启用（AGENT_SCAN_PROJECT_READONLY=false）"}

    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return {"error": f"正则表达式错误: {e}"}

    matches = []
    for scan_root in get_scan_paths():
        for p in scan_root.rglob("*"):
            if not p.is_file() or p.stat().st_size > 200_000:
                continue
            if p.suffix not in {".py", ".js", ".vue", ".ts", ".json", ".md", ".yaml", ".yml", ".sh"}:
                continue
            try:
                content = p.read_text(encoding="utf-8", errors="replace")
                for i, line in enumerate(content.splitlines(), 1):
                    if regex.search(line):
                        matches.append({
                            "file": str(p.relative_to(PROJECT_ROOT)),
                            "line": i,
                            "text": line.strip()[:200],
                        })
                        if len(matches) >= max_results:
                            return {"matches": matches, "truncated": True}
            except Exception:
                continue
    return {"matches": matches, "truncated": False}
