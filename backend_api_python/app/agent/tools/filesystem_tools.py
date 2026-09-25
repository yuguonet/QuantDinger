# -*- coding: utf-8 -*-
"""文件系统工具（对齐 MCP filesystem 常用 API）：list_dir / read_text / write_text。

参考社区惯例（MCP `@modelcontextprotocol/server-filesystem` 同形接口）：
  - list_dir(path) → 目录清单
  - read_text(path, max_chars) → 读文本
  - write_text(path, content) → 写文本

安全边界（比 MCP 默认更紧）：
  - **读**：限工作区内 data/ tmp/ docs/ logs/ analysis_output/
  - **写**：**仅** 项目 tmp/（含 backend_api_python/tmp/）——用户裁定
  - 拒绝 .env / 凭据 / .git / 路径穿越（`..`）
  - 失败一律 {error}，不抛异常打断 agent

用途：回读案例/报告/中间产物，支撑「学习-复盘」闭环；写仅用于草稿落地。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

# 工作区根（QuantDinger/）
_ROOT = Path(r"D:\QuantDinger")
# 读允许的子树（相对 _ROOT）
_READ_OK = ("backend_api_python", "data", "tmp", "docs", "logs", "analysis_output", "optimizer")
# 写只允许 tmp 树
_WRITE_OK = ("tmp", "backend_api_python/tmp", "backend_api_python\\tmp")
_DENY_TOKENS = (".env", "id_rsa", "id_ed25519", "credentials", "secrets", ".git/", ".git\\", ".ssh")


def _deny(path: Path) -> bool:
    s = str(path).lower()
    return any(tok.lower() in s for tok in _DENY_TOKENS)


def _resolve(raw: str, *, write: bool = False) -> Optional[Path]:
    if not raw or not str(raw).strip():
        return None
    p = Path(raw)
    if not p.is_absolute():
        p = _ROOT / p
    try:
        p = p.resolve()
    except Exception:
        return None
    # 路径穿越
    try:
        p.relative_to(_ROOT)
    except ValueError:
        return None
    if _deny(p):
        return None
    if write:
        rel = p.relative_to(_ROOT).as_posix()
        if not (rel == "tmp" or rel.startswith("tmp/") or rel.startswith("backend_api_python/tmp")):
            return None
    else:
        rel = p.relative_to(_ROOT).as_posix()
        if not any(rel == r or rel.startswith(r + "/") or rel.startswith(r + "\\") or rel.startswith(r)
                   for r in _READ_OK):
            return None
    return p


def list_dir(path: str = "tmp", *, max_entries: int = 200) -> Dict[str, Any]:
    """列出目录（工作区只读子树）。返回 {path, count, entries:[{name, type, size}]}。"""
    p = _resolve(path, write=False)
    if p is None:
        return {"error": "路径不允许或不存在（仅工作区 data/tmp/docs/logs 等）"}
    if not p.is_dir():
        return {"error": f"不是目录: {p}"}
    entries: List[Dict[str, Any]] = []
    try:
        for i, c in enumerate(sorted(p.iterdir(), key=lambda x: x.name.lower())):
            if i >= max_entries:
                break
            if _deny(c):
                continue
            try:
                st = c.stat()
                entries.append({
                    "name": c.name,
                    "type": "dir" if c.is_dir() else "file",
                    "size": st.st_size if c.is_file() else None,
                })
            except Exception:
                continue
    except Exception as e:
        return {"error": f"列目录失败: {e}"}
    return {"path": str(p), "count": len(entries), "entries": entries}


def read_text(path: str, *, max_chars: int = 8000) -> Dict[str, Any]:
    """读文本文件（工作区只读子树）。超长截断并标注。"""
    p = _resolve(path, write=False)
    if p is None:
        return {"error": "路径不允许（仅工作区 data/tmp/docs/logs 等）"}
    if not p.is_file():
        return {"error": f"不是文件: {p}"}
    if p.stat().st_size > 2_000_000:
        return {"error": "文件过大(>2MB)，请用专用工具"}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"error": f"读取失败: {e}"}
    truncated = len(text) > max_chars
    return {
        "path": str(p),
        "chars": len(text),
        "truncated": truncated,
        "text": text[:max_chars],
    }


def write_text(path: str = "", content: str = "", *, overwrite: bool = True,
               **kwargs: Any) -> Dict[str, Any]:
    """写文本文件。**仅允许 tmp/**（用户安全边界）。

    兼容：模型偶发 `arg0/arg1/arg2` 工具调用形态（OpenAI schema 序列化）。
    Returns: {path, bytes} 或 {error}。
    """
    path = path or kwargs.get("arg0") or kwargs.get("path") or ""
    content = content or kwargs.get("arg1") or kwargs.get("content") or ""
    if "arg2" in kwargs and overwrite is True:
        overwrite = bool(kwargs.get("arg2"))
    p = _resolve(path, write=True)
    if p is None:
        return {"error": "写路径仅允许工作区 tmp/（禁止穿越/凭据路径）"}
    if not str(content):
        return {"error": "content 为空"}
    if len(content) > 200_000:
        return {"error": "content 过大(>200KB)"}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and not overwrite:
            return {"error": "文件已存在（overwrite=False）"}
        p.write_text(content, encoding="utf-8")
    except Exception as e:
        return {"error": f"写入失败: {e}"}
    return {"path": str(p), "bytes": p.stat().st_size}


def run_python_file(path: str = "", *, timeout_s: float = 15.0,
                    **kwargs: Any) -> Dict[str, Any]:
    """运行 tmp/ 下的 Python 脚本（独立子进程），返回 stdout/stderr。

    用途：写完临时代码后真正「跑起来」（如跑马灯、小脚本）。
    安全：仅 tmp/；超时杀进程；UTF-8 解码（Windows GBK 坑）。
    兼容 arg0/arg1 工具调用形态。
    """
    import subprocess
    import sys as _sys
    path = path or kwargs.get("arg0") or kwargs.get("path") or ""
    if "timeout_s" not in kwargs and "arg1" in kwargs:
        try:
            timeout_s = float(kwargs.get("arg1"))
        except Exception:
            pass
    p = _resolve(path, write=True)  # 运行也按写权限：必须在 tmp/
    if p is None:
        return {"error": "只允许运行 tmp/ 下的脚本"}
    if not p.is_file():
        return {"error": f"脚本不存在: {p}"}
    if p.suffix.lower() not in (".py", ".pyw"):
        return {"error": "只支持 .py 脚本"}
    try:
        proc = subprocess.run(
            [_sys.executable, str(p)],
            cwd=str(p.parent),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1.0, float(timeout_s)),
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"},
        )
    except subprocess.TimeoutExpired:
        return {"error": f"运行超时(>{timeout_s}s)", "path": str(p)}
    except Exception as e:
        return {"error": f"运行失败: {e}", "path": str(p)}
    return {
        "path": str(p),
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "")[-4000:],
        "stderr": (proc.stderr or "")[-2000:],
    }
