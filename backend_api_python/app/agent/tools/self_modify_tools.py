# -*- coding: utf-8 -*-
"""
Self-Modify Tools — Agent 对指定目录的自修改、自升级、自扩充能力。

安全约束：
1. 只能修改 AGENT_SELF_MODIFY_PATHS 中列出的目录
2. 每次修改前自动备份原文件到 .backups/
3. 修改后需重新 import 才生效（需要 agent 重建）
4. 有修改日志记录（.modify_log.jsonl）

配置项：
    AGENT_TOOLS_SELF_MODIFY=true/false        是否启用（默认 false）
    AGENT_SELF_MODIFY_PATHS=...               允许修改的目录（逗号分隔，相对于项目根）
                                              默认: backend_api_python/app/agent/tools
                                              示例: backend_api_python/app/agent/tools,backend_api_python/app/services,scripts
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent  # QuantDinger root
BACKUP_DIR = PROJECT_ROOT / ".agent_backups"

MODIFY_PATHS_DEFAULT = ["backend_api_python/app/agent/tools"]


def is_self_modify_enabled() -> bool:
    return os.getenv("AGENT_TOOLS_SELF_MODIFY", "false").lower() == "true"


def get_modify_paths() -> List[Path]:
    """从配置读取可修改目录列表。"""
    raw = os.getenv("AGENT_SELF_MODIFY_PATHS", "")
    if raw:
        paths = [PROJECT_ROOT / p.strip() for p in raw.split(",") if p.strip()]
    else:
        paths = [PROJECT_ROOT / p for p in MODIFY_PATHS_DEFAULT]
    # 安全检查 + 不存在的目录自动创建
    safe = []
    for p in paths:
        try:
            p.resolve().relative_to(PROJECT_ROOT.resolve())
            p.mkdir(parents=True, exist_ok=True)
            safe.append(p)
        except ValueError:
            logger.warning("[SelfModify] 路径越界，跳过: %s", p)
    return safe


def _ensure_backup_dir():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)


def _validate_path(filepath: str) -> Any:
    """验证文件路径安全性。返回 Path 或 error dict。"""
    # 允许子目录路径（如 subdir/file.py），但不允许 ..
    if ".." in filepath:
        return {"error": f"不安全的路径: {filepath}（不允许 ..）"}

    # 在所有允许的目录中查找匹配
    target = (PROJECT_ROOT / filepath).resolve()
    try:
        target.relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        return {"error": f"路径越界: {filepath}"}

    # 检查是否在某个允许修改的目录下
    modify_roots = get_modify_paths()
    in_scope = any(
        str(target).startswith(str(sr.resolve()) + os.sep) or str(target) == str(sr.resolve())
        for sr in modify_roots
    )
    if not in_scope:
        allowed = ", ".join(str(r.relative_to(PROJECT_ROOT)) for r in modify_roots)
        return {"error": f"路径不在允许修改的目录范围内: {filepath}\n允许的目录: {allowed}"}

    return target


def _validate_relative_path(filepath: str) -> Any:
    """验证路径并返回相对于项目根的路径字符串。"""
    result = _validate_path(filepath)
    if isinstance(result, dict) and "error" in result:
        return result
    # result 是绝对 Path，返回相对路径
    return str(result.relative_to(PROJECT_ROOT))


def _append_modify_log(entry: dict):
    """追加修改日志。"""
    log_path = BACKUP_DIR / ".modify_log.jsonl"
    _ensure_backup_dir()
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ═══════════════════════════════════════════════════════════════
# Tool implementations
# ═══════════════════════════════════════════════════════════════

def self_modify_list_dirs() -> Dict[str, Any]:
    """列出允许修改的目录及其文件。"""
    if not is_self_modify_enabled():
        return {"error": "自修改未启用，请设置 AGENT_TOOLS_SELF_MODIFY=true"}

    result = {}
    for scan_root in get_modify_paths():
        rel = str(scan_root.relative_to(PROJECT_ROOT))
        files = []
        for p in sorted(scan_root.rglob("*")):
            if p.is_file() and not p.name.startswith("."):
                files.append({
                    "path": str(p.relative_to(PROJECT_ROOT)),
                    "size": p.stat().st_size,
                    "modified": time.ctime(p.stat().st_mtime),
                })
        result[rel] = {"count": len(files), "files": files}
    return {"allowed_dirs": result}


def self_modify_read(filepath: str) -> Dict[str, Any]:
    """读取指定文件的源码。

    Args:
        filepath: 相对于项目根目录的路径（如 backend_api_python/app/agent/tools/data_tools.py）
    """
    if not is_self_modify_enabled():
        return {"error": "自修改未启用，请设置 AGENT_TOOLS_SELF_MODIFY=true"}

    validated = _validate_path(filepath)
    if isinstance(validated, dict) and "error" in validated:
        return validated

    target = validated
    if not target.exists():
        return {"error": f"文件不存在: {filepath}"}
    if not target.is_file():
        return {"error": f"不是文件: {filepath}"}

    size = target.stat().st_size
    if size > 500_000:
        return {"error": f"文件过大 ({size} bytes)，请指定具体范围"}

    content = target.read_text(encoding="utf-8", errors="replace")
    return {
        "path": filepath,
        "content": content,
        "size": size,
        "lines": content.count("\n") + 1,
    }


def self_modify_write(filepath: str, content: str, reason: str = "") -> Dict[str, Any]:
    """写入/修改文件（自动备份原文件）。

    修改后需重启 Agent 生效。

    Args:
        filepath: 相对于项目根目录的路径
        content: 完整的文件内容
        reason: 修改原因（记录日志用）
    """
    if not is_self_modify_enabled():
        return {"error": "自修改未启用，请设置 AGENT_TOOLS_SELF_MODIFY=true"}

    validated = _validate_path(filepath)
    if isinstance(validated, dict) and "error" in validated:
        return validated

    target = validated
    _ensure_backup_dir()

    # 备份现有文件
    backup_info = None
    if target.exists():
        rel = str(target.relative_to(PROJECT_ROOT)).replace("/", "_").replace("\\", "_")
        backup_name = f"{rel}_{int(time.time())}.bak"
        backup_path = BACKUP_DIR / backup_name
        shutil.copy2(target, backup_path)
        backup_info = str(backup_path.relative_to(PROJECT_ROOT))
        logger.info("[SelfModify] Backed up %s → %s", filepath, backup_info)

    # 写入
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    log_entry = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "action": "write",
        "file": filepath,
        "reason": reason,
        "backup": backup_info,
        "size": len(content),
    }
    _append_modify_log(log_entry)

    logger.info("[SelfModify] Wrote %s (%d bytes) reason=%s", filepath, len(content), reason)

    return {
        "success": True,
        "path": filepath,
        "size": len(content),
        "backup": backup_info,
        "message": f"文件 {filepath} 已写入。可能需要重启 Agent 才能生效。",
    }


def self_modify_create(filepath: str, content: str, description: str = "") -> Dict[str, Any]:
    """创建新文件。

    Args:
        filepath: 相对于项目根目录的路径
        content: 文件内容
        description: 用途描述
    """
    if not is_self_modify_enabled():
        return {"error": "自修改未启用，请设置 AGENT_TOOLS_SELF_MODIFY=true"}

    validated = _validate_path(filepath)
    if isinstance(validated, dict) and "error" in validated:
        return validated

    target = validated
    if target.exists():
        return {"error": f"文件已存在，请使用 self_modify_write 修改: {filepath}"}

    _ensure_backup_dir()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")

    log_entry = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "action": "create",
        "file": filepath,
        "reason": f"CREATE: {description}",
        "size": len(content),
    }
    _append_modify_log(log_entry)

    logger.info("[SelfModify] Created %s (%d bytes)", filepath, len(content))

    return {
        "success": True,
        "path": filepath,
        "size": len(content),
        "message": f"文件 {filepath} 已创建。",
    }


def self_modify_diff(filepath: str) -> Dict[str, Any]:
    """查看文件与最近备份的差异。"""
    if not is_self_modify_enabled():
        return {"error": "自修改未启用，请设置 AGENT_TOOLS_SELF_MODIFY=true"}

    validated = _validate_path(filepath)
    if isinstance(validated, dict) and "error" in validated:
        return validated

    target = validated
    if not target.exists():
        return {"error": f"文件不存在: {filepath}"}

    _ensure_backup_dir()
    rel = str(target.relative_to(PROJECT_ROOT)).replace("/", "_").replace("\\", "_")
    backups = sorted(BACKUP_DIR.glob(f"{rel}_*.bak"), reverse=True)
    if not backups:
        return {"error": "没有备份文件可供对比"}

    latest_backup = backups[0]
    current = target.read_text().splitlines(keepends=True)
    old = latest_backup.read_text().splitlines(keepends=True)
    diff = difflib.unified_diff(old, current, fromfile=f"backup/{latest_backup.name}", tofile=filepath)
    return {
        "path": filepath,
        "backup": latest_backup.name,
        "diff": "".join(diff)[:5000],
    }


def self_modify_rollback(filepath: str) -> Dict[str, Any]:
    """回滚文件到最近的备份版本。"""
    if not is_self_modify_enabled():
        return {"error": "自修改未启用，请设置 AGENT_TOOLS_SELF_MODIFY=true"}

    validated = _validate_path(filepath)
    if isinstance(validated, dict) and "error" in validated:
        return validated

    target = validated
    if not target.exists():
        return {"error": f"文件不存在: {filepath}"}

    _ensure_backup_dir()
    rel = str(target.relative_to(PROJECT_ROOT)).replace("/", "_").replace("\\", "_")
    backups = sorted(BACKUP_DIR.glob(f"{rel}_*.bak"), reverse=True)
    if not backups:
        return {"error": "没有备份文件可供回滚"}

    latest = backups[0]
    # 先备份当前版本
    current_backup = BACKUP_DIR / f"{rel}_{int(time.time())}_pre_rollback.bak"
    shutil.copy2(target, current_backup)
    # 回滚
    shutil.copy2(latest, target)

    log_entry = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "action": "rollback",
        "file": filepath,
        "reason": f"Rollback to {latest.name}",
        "backup": str(current_backup.relative_to(PROJECT_ROOT)),
    }
    _append_modify_log(log_entry)

    return {
        "success": True,
        "path": filepath,
        "restored_from": latest.name,
        "message": f"已回滚 {filepath} 到 {latest.name}。当前版本已备份。",
    }


def self_modify_log(last_n: int = 20) -> Dict[str, Any]:
    """查看修改历史日志。"""
    if not is_self_modify_enabled():
        return {"error": "自修改未启用，请设置 AGENT_TOOLS_SELF_MODIFY=true"}

    log_path = BACKUP_DIR / ".modify_log.jsonl"
    if not log_path.exists():
        return {"entries": [], "message": "暂无修改记录"}

    lines = log_path.read_text().strip().splitlines()
    entries = []
    for line in lines[-last_n:]:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return {"entries": entries}


def self_modify_diff_head(filepath: str, lines: int = 80) -> Dict[str, Any]:
    """读取文件头部（快速预览结构，不读全文）。

    Args:
        filepath: 相对于项目根目录的路径
        lines: 读取前 N 行（默认 80）
    """
    if not is_self_modify_enabled():
        return {"error": "自修改未启用，请设置 AGENT_TOOLS_SELF_MODIFY=true"}

    validated = _validate_path(filepath)
    if isinstance(validated, dict) and "error" in validated:
        return validated

    target = validated
    if not target.exists():
        return {"error": f"文件不存在: {filepath}"}

    all_lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    head = all_lines[:lines]
    return {
        "path": filepath,
        "total_lines": len(all_lines),
        "showing": len(head),
        "content": "\n".join(head),
    }


# ═══════════════════════════════════════════════════════════════
# Tool specs for registry
# ═══════════════════════════════════════════════════════════════

SELF_MODIFY_TOOLS: list = []


def _register_self_modify_tools():
    if not is_self_modify_enabled():
        return

    SELF_MODIFY_TOOLS.extend([
        {
            "fn": self_modify_list_dirs,
            "name": "self_modify_list_dirs",
            "description": (
                "列出所有允许修改的目录及其文件。"
                "用于了解当前可修改的范围。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "fn": self_modify_read,
            "name": "self_modify_read",
            "description": (
                "读取指定文件的完整源码。"
                "路径相对于项目根目录（如 backend_api_python/app/agent/tools/data_tools.py）。"
                "用于理解现有代码，再决定如何修改。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "文件路径（相对于项目根目录）",
                    },
                },
                "required": ["filepath"],
            },
        },
        {
            "fn": self_modify_diff_head,
            "name": "self_modify_diff_head",
            "description": (
                "读取文件头部 N 行（快速预览结构，不读全文）。"
                "适合大文件的快速了解。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "文件路径（相对于项目根目录）",
                    },
                    "lines": {
                        "type": "integer",
                        "description": "读取前 N 行（默认 80）",
                        "default": 80,
                    },
                },
                "required": ["filepath"],
            },
        },
        {
            "fn": self_modify_write,
            "name": "self_modify_write",
            "description": (
                "修改现有文件（自动备份原文件）。"
                "修改后可能需重启 Agent 生效。"
                "建议先用 self_modify_read 理解现有代码，再做最小改动。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "文件路径（相对于项目根目录）",
                    },
                    "content": {
                        "type": "string",
                        "description": "完整的文件内容",
                    },
                    "reason": {
                        "type": "string",
                        "description": "修改原因（记录日志）",
                    },
                },
                "required": ["filepath", "content"],
            },
        },
        {
            "fn": self_modify_create,
            "name": "self_modify_create",
            "description": "创建新文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "文件路径（相对于项目根目录）",
                    },
                    "content": {
                        "type": "string",
                        "description": "文件内容",
                    },
                    "description": {
                        "type": "string",
                        "description": "用途描述",
                    },
                },
                "required": ["filepath", "content"],
            },
        },
        {
            "fn": self_modify_diff,
            "name": "self_modify_diff",
            "description": "查看文件与最近备份的差异。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "文件路径（相对于项目根目录）",
                    },
                },
                "required": ["filepath"],
            },
        },
        {
            "fn": self_modify_rollback,
            "name": "self_modify_rollback",
            "description": "回滚文件到最近的备份版本。当前版本会自动备份。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "文件路径（相对于项目根目录）",
                    },
                },
                "required": ["filepath"],
            },
        },
        {
            "fn": self_modify_log,
            "name": "self_modify_log",
            "description": "查看修改历史日志。",
            "parameters": {
                "type": "object",
                "properties": {
                    "last_n": {
                        "type": "integer",
                        "description": "显示最近 N 条记录（默认 20）",
                        "default": 20,
                    },
                },
            },
        },
    ])
    logger.info("[SelfModifyTools] Registered %d self-modify tools (dirs: %s)",
                len(SELF_MODIFY_TOOLS),
                [str(p.relative_to(PROJECT_ROOT)) for p in get_modify_paths()])


_register_self_modify_tools()
