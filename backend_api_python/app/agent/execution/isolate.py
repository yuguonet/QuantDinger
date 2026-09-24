# -*- coding: utf-8 -*-
"""app/agent/execution/isolate.py — F5 完整执行隔离（阶段 0.5 终态的第一块）

阶段 0.5 的 deny-list 只是**日志留痕**，不承诺拦截。F5 目标态：
  独立子进程 / 非特权 UID / 文件系统只读+tmp 配额 / 出网白名单 / import 白名单收回。

本模块落地**可复用的子进程隔离执行器骨架**（默认 dry 风格：
  - `run_isolated` 在受限子进程里跑一段 python 源码
  - Windows 降级：无 seccomp/UID 时仍保证**独立进程 + 工作目录隔离 + 超时杀进程**
  - 真正容器/seccomp 留给运维侧；代码侧先把「隔离面」和「审计面」分开写清

⚠️ 不假装安全：本文件 docstring 与 `audit_line()` 明标隔离级别，
避免再造 0.5 那种「有 deny-list 就算防护」的假安全感。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

#: import 白名单（F5：从 ["*"] 收回）。子进程 bootstrap 只允许这些前缀。
IMPORT_ALLOW_PREFIX: Sequence[str] = (
    "math", "json", "re", "sys", "os", "datetime", "typing",
    "collections", "itertools", "functools", "statistics",
)

#: 危险路径 deny（0.5 延续；F5 在子进程侧再执行一次）
DENY_PATH_TOKENS: Sequence[str] = (".env", "id_rsa", "credentials", "secrets", ".git/")


def isolation_level() -> str:
    """当前隔离级别（诚实分级，勿夸大）。"""
    if os.name == "nt":
        return "process+tmpdir+timeout (no uid/seccomp on Windows)"
    return "process+tmpdir+timeout+import_allowlist (uid/seccomp: pending ops)"


def audit_line() -> str:
    return f"[isolate] level={isolation_level()} deny_paths={len(DENY_PATH_TOKENS)} imports={len(IMPORT_ALLOW_PREFIX)}"


_BOOTSTRAP = r'''
import sys, os, json, traceback
ALLOW = set(sys.argv[1].split(","))
src = sys.argv[2]
deny = sys.argv[3].split("|") if len(sys.argv) > 3 else []
# 简易 import 闸（非安全边界，是防误用面）
import ast
tree = ast.parse(src)
for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        for a in node.names:
            root = a.name.split(".")[0]
            if root not in ALLOW and not root.startswith("_"):
                print(json.dumps({"error": "import_denied", "name": a.name}))
                raise SystemExit(3)
    elif isinstance(node, ast.ImportFrom):
        root = (node.module or "").split(".")[0]
        if root and root not in ALLOW and not root.startswith("_"):
            print(json.dumps({"error": "import_denied", "name": node.module}))
            raise SystemExit(3)
# 路径 deny（日志级）
for tok in deny:
    if tok and tok in src:
        print(json.dumps({"warn": "deny_path_token", "token": tok}))
ns = {}
try:
    exec(compile(src, "<isolated>", "exec"), ns, ns)
    out = ns.get("result", ns.get("RESULT", None))
    print(json.dumps({"ok": True, "result": out}, default=str))
except Exception as e:
    print(json.dumps({"error": "exec", "type": type(e).__name__, "msg": str(e)}))
    raise SystemExit(1)
'''


def run_isolated(source: str, *, timeout: float = 8.0,
                 allowed_imports: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """在独立子进程执行 source。返回 {ok, result|error, level}。

    约束：source 内应定义 `result`（或 `RESULT`）。超时杀进程。
    """
    allow = ",".join(allowed_imports or IMPORT_ALLOW_PREFIX)
    deny = "|".join(DENY_PATH_TOKENS)
    with tempfile.TemporaryDirectory(prefix="qd_iso_") as td:
        boot = Path(td) / "_boot.py"
        boot.write_text(_BOOTSTRAP, encoding="utf-8")
        try:
            proc = subprocess.run(
                [sys.executable, str(boot), allow, source, deny],
                cwd=td,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "timeout", "level": isolation_level()}
        lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
        payload: Dict[str, Any] = {}
        for ln in reversed(lines):
            try:
                import json as _json
                payload = _json.loads(ln)
                break
            except Exception:
                continue
        if not payload:
            payload = {"error": "no_output", "stderr": (proc.stderr or "")[-500:]}
        payload.setdefault("returncode", proc.returncode)
        payload["level"] = isolation_level()
        payload["ok"] = bool(payload.get("ok")) and proc.returncode == 0
        return payload
