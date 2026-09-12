# -*- coding: utf-8 -*-
"""阶段中转数据暂存区（2026-09-12）。

背景：执行沙箱不支持 import os / 直接写文件，跨阶段重数据只能靠 ≤2000 字摘要，
明细在阶段间丢失（实测：深入分析阶段的全量结果无法完整传给成稿阶段）。
本模块提供受控暂存区：按 run（scope）分区，phase 间中转大块数据。

安全边界：
  - 目录限定 <项目>/tmp/agent_staging/<scope>/（scope 由任务书下发并做白名单清洗）
  - 文件名白名单 [A-Za-z0-9_.-]，≤80 字符；内容 ≤ 2MB；只暴露 write/read/list 三个显式函数
  - 无任意路径访问；scope/name 非法直接拒绝
易错点：
  - scope 必须在一次 run 内保持稳定（chat_node 用 session_id+start_time 派生），
    否则跨阶段读不到
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from app.utils.logger import get_logger

logger = get_logger(__name__)

# <项目>/tmp/agent_staging/（tools/staging.py → parents[3] = backend_api_python）
_ROOT = Path(__file__).resolve().parents[3] / "tmp" / "agent_staging"

_NAME_RE = re.compile(r"[A-Za-z0-9_.\-]{1,80}")
_SCOPE_RE = re.compile(r"[A-Za-z0-9_\-]{1,64}")
_MAX_CHARS = 2 * 1024 * 1024  # 2MB


def _dir(scope: str) -> Path:
    if not _SCOPE_RE.fullmatch(scope or ""):
        raise ValueError(f"scope 非法: {scope!r}")
    return _ROOT / scope


def _safe_name(name: str) -> str:
    if not _NAME_RE.fullmatch(name or ""):
        raise ValueError(f"文件名非法（限字母数字._-，≤80字符）: {name!r}")
    return name


def stage_write(scope: str, name: str, content: str) -> dict:
    """写入阶段中转数据（重数据落盘，供后续阶段 stage_read 读取）。

    Args:
        scope: 暂存区名（任务书中给定的本次运行标识）
        name: 文件名（字母数字._-，建议带 .json/.md 扩展名）
        content: 文本内容（≤2MB，建议 JSON 或 markdown）

    Returns:
        {"ok": True, "name": ..., "chars": N} 或 {"ok": False, "error": "..."}
    """
    # 参数校验在 try 外：非法 scope/name 直接抛 ValueError（安全边界不可被忽略返回值绕过）
    d = _dir(scope)
    fname = _safe_name(name)
    try:
        text = str(content or "")
        if len(text) > _MAX_CHARS:
            return {"ok": False, "error": f"内容超限（{len(text)} > {_MAX_CHARS} 字符），请先提炼"}
        d.mkdir(parents=True, exist_ok=True)
        (d / fname).write_text(text, encoding="utf-8")
        logger.info("[staging] write %s/%s (%d chars)", scope, fname, len(text))
        return {"ok": True, "name": fname, "chars": len(text)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def stage_read(scope: str, name: str) -> str:
    """读取阶段中转数据（返回文本；超过 60000 字符截断并附提示）。

    Args:
        scope: 暂存区名（与写入时一致）
        name: 文件名

    Returns:
        文件文本；不存在/非法时返回 {"ok": False, "error": ...} 样式的 JSON 字符串
    """
    try:
        d = _dir(scope)
        f = d / _safe_name(name)
        if not f.is_file():
            avail = [x.name for x in d.glob("*") if x.is_file()] if d.is_dir() else []
            return json.dumps({"ok": False, "error": f"文件不存在: {name}", "可用": avail},
                              ensure_ascii=False)
        text = f.read_text(encoding="utf-8", errors="replace")
        if len(text) > 60000:
            text = text[:60000] + f"\n…(截断，共 {len(text)} 字符)"
        return text
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)[:200]}, ensure_ascii=False)


def stage_list(scope: str) -> list:
    """列出暂存区现有文件（name/chars/mtime）。"""
    try:
        d = _dir(scope)
        if not d.is_dir():
            return []
        out = []
        for x in sorted(d.glob("*")):
            if x.is_file():
                out.append({"name": x.name, "chars": x.stat().st_size,
                            "mtime": time.strftime("%H:%M:%S", time.localtime(x.stat().st_mtime))})
        return out
    except Exception as e:
        return [{"error": str(e)[:200]}]
