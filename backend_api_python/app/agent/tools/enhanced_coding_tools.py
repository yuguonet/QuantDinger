# -*- coding: utf-8 -*-
"""
Enhanced Coding Tools — OpenCode-inspired tools for QuantDinger's agent.

Ported design from OpenCode (https://github.com/anomalyco/opencode):
- apply_patch: unified diff multi-file batch editing
- glob_files: recursive file pattern matching
- grep_code: full-project regex code search
- git_snapshot: auto-commit before edits, rollback support
- code_lint: ruff/pylint static analysis
- lsp_diagnostics: pyright type checking

All tools are sandboxed to the session workspace.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.agent.tools.registry import tool

logger = logging.getLogger(__name__)

# ── Helpers ───────────────────────────────────────────────────

def _get_ws():
    """Get current session workspace."""
    from app.agent.workspace import get_workspace
    from app.agent.tool_context import get_session_id
    return get_workspace(get_session_id() or "default")


def _safe_path(ws, rel_path: str) -> Path:
    """Resolve a relative path within workspace, reject traversal."""
    safe = rel_path.lstrip("/").replace("..", "")
    full = ws.session_dir / safe
    try:
        full.resolve().relative_to(ws.session_dir.resolve())
    except ValueError:
        raise ValueError(f"路径越界: {rel_path}")
    return full


def _run_cmd(cmd: list, cwd: str, timeout: int = 30) -> Dict[str, Any]:
    """Run a subprocess command and capture output."""
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True,
            timeout=timeout, env={**os.environ, "WORKSPACE": cwd},
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": f"命令超时 ({timeout}s)", "exit_code": -1}
    except FileNotFoundError:
        return {"stdout": "", "stderr": f"命令未找到: {cmd[0]}", "exit_code": -1}


# ═══════════════════════════════════════════════════════════════
# 1. apply_patch — Unified diff multi-file batch editing
# ═══════════════════════════════════════════════════════════════

def _parse_unified_patch(patch_text: str) -> List[Dict[str, Any]]:
    """Parse a unified diff patch into structured hunks.

    Supports standard unified diff format:
        --- a/path/to/file.py
        +++ b/path/to/file.py
        @@ -10,7 +10,8 @@ some context
         unchanged line
        -removed line
        +added line
         unchanged line

    Also supports simplified format (no --- / +++ headers, just @@ hunks).
    """
    hunks = []
    current_file = None
    current_hunks = []

    lines = patch_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    i = 0
    while i < len(lines):
        line = lines[i]

        # --- a/file.py header
        if line.startswith("--- "):
            # Look for matching +++ line
            if i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
                old_path = line[4:].strip()
                new_path = lines[i + 1][4:].strip()
                # Normalize: strip a/ and b/ prefixes
                for prefix in ("a/", "b/"):
                    if old_path.startswith(prefix):
                        old_path = old_path[2:]
                    if new_path.startswith(prefix):
                        new_path = new_path[2:]
                current_file = new_path or old_path
                current_hunks = []
                i += 2
                continue

        # @@ -old_start,old_count +new_start,new_count @@ context
        if line.startswith("@@ "):
            match = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)", line)
            if match and current_file:
                old_start = int(match.group(1))
                old_count = int(match.group(2) or "1")
                new_start = int(match.group(3))
                new_count = int(match.group(4) or "1")
                context = match.group(5).strip()

                # Collect hunk lines
                hunk_lines = []
                i += 1
                while i < len(lines):
                    hline = lines[i]
                    if hline.startswith("@@ ") or hline.startswith("--- ") or hline.startswith("+++ "):
                        break
                    if hline.startswith("\\"):
                        # "\ No newline at end of file"
                        i += 1
                        continue
                    hunk_lines.append(hline)
                    i += 1

                current_hunks.append({
                    "old_start": old_start,
                    "old_count": old_count,
                    "new_start": new_start,
                    "new_count": new_count,
                    "context": context,
                    "lines": hunk_lines,
                })
                continue

        # If we hit a new file section or end, flush current
        if current_file and (line.startswith("--- ") or i == len(lines) - 1):
            if current_hunks:
                hunks.append({
                    "file": current_file,
                    "hunks": current_hunks,
                })
            current_file = None
            current_hunks = []

        i += 1

    # Flush last file (loop exits when i >= len(lines), missing the flush)
    if current_file and current_hunks:
        hunks.append({
            "file": current_file,
            "hunks": current_hunks,
        })

    return hunks


def _apply_hunk_to_lines(old_lines: List[str], hunk: Dict, offset: int = 0) -> tuple:
    """Apply a single hunk to the file lines.

    Uses fuzzy matching: if the context doesn't match exactly at old_start,
    tries to find the matching region nearby (±10 lines).

    Args:
        old_lines: Current file lines (may have been modified by prior hunks)
        hunk: Hunk dict with old_start, lines, etc.
        offset: Cumulative line offset from prior hunks (adjusts old_start)

    Returns:
        (new_lines, delta) where delta = len(new) - len(old) for this hunk
    """
    old_start = hunk["old_start"] - 1 + offset  # 0-indexed, adjusted for prior changes
    hunk_lines = hunk["lines"]

    # Extract expected old lines from hunk (context + removed lines)
    expected_old = []
    for hl in hunk_lines:
        if hl.startswith(" ") or hl.startswith("-"):
            expected_old.append(hl[1:] if len(hl) > 1 else "")

    # Try exact match at old_start
    match_pos = old_start
    if match_pos < 0 or match_pos + len(expected_old) > len(old_lines):
        # Position out of range, try fuzzy search
        match_pos = _fuzzy_find(old_lines, expected_old, old_start)
        if match_pos is None:
            raise ValueError(
                f"Hunk 无法匹配文件内容 (期望在第 {old_start+1} 行附近找到 {len(expected_old)} 行匹配)\n"
                f"期望的前几行: {expected_old[:3]}"
            )
    else:
        # Verify the expected lines actually match at old_start
        match_ok = True
        for j, exp in enumerate(expected_old):
            if match_pos + j >= len(old_lines) or old_lines[match_pos + j].rstrip() != exp.rstrip():
                match_ok = False
                break
        if not match_ok:
            # Try fuzzy search
            fuzzy_pos = _fuzzy_find(old_lines, expected_old, old_start)
            if fuzzy_pos is not None:
                match_pos = fuzzy_pos
            else:
                raise ValueError(
                    f"Hunk 无法匹配文件内容 (期望在第 {old_start+1} 行附近找到 {len(expected_old)} 行匹配)\n"
                    f"期望的前几行: {expected_old[:3]}"
                )

    # Build new content for this hunk
    new_content = []
    for hl in hunk_lines:
        if hl.startswith("+"):
            new_content.append(hl[1:] if len(hl) > 1 else "")
        elif hl.startswith(" "):
            new_content.append(hl[1:] if len(hl) > 1 else "")
        # "-" lines are removed, skip

    # Reconstruct: before + new_content + after
    result = old_lines[:match_pos] + new_content + old_lines[match_pos + len(expected_old):]

    # Delta: how many lines were added/removed by this hunk
    delta = len(new_content) - len(expected_old)
    return result, delta


def _fuzzy_find(lines: List[str], expected: List[str], hint_pos: int, radius: int = 10) -> Optional[int]:
    """Find where expected lines match in the file, searching near hint_pos."""
    if not expected:
        return hint_pos

    search_start = max(0, hint_pos - radius)
    search_end = min(len(lines), hint_pos + radius + len(expected))

    for pos in range(search_start, search_end):
        if pos + len(expected) > len(lines):
            break
        match = True
        for j, exp in enumerate(expected):
            if lines[pos + j].rstrip() != exp.rstrip():
                match = False
                break
        if match:
            return pos
    return None


@tool(
    description=(
        "应用 unified diff 格式的补丁，支持一次性编辑多个文件。"
        "格式标准 unified diff（带 --- / +++ 头和 @@ 行号标记）。"
        "比 workspace_edit_file 更适合批量修改。"
    ),
    category="代码编辑",
    layer="支撑层",
    domain=["coding"],
)
def apply_patch(
    patch_text: str,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Apply a unified diff patch to workspace files.

    Supports standard unified diff format. Can edit multiple files in one call.
    Each hunk uses fuzzy matching to handle minor context drift.

    Args:
        patch_text: Unified diff text (--- / +++ / @@ format)
        dry_run: If True, only validate without applying

    Returns:
        {"files": [...], "changes": [...], "total_additions": int, "total_deletions": int}
    """
    ws = _get_ws()

    parsed = _parse_unified_patch(patch_text)
    if not parsed:
        return {"error": "无法解析补丁内容。请使用标准 unified diff 格式。"}

    results = []
    total_add = 0
    total_del = 0

    # Auto-snapshot before batch edit
    from app.agent.tools.iteration_tools import auto_snapshot_before_edit
    auto_snapshot_before_edit(f"before apply_patch ({len(parsed)} files)")

    for file_patch in parsed:
        file_path = file_patch["file"]
        full_path = _safe_path(ws, file_path)

        if not full_path.exists():
            return {"error": f"文件不存在: {file_path}。如需创建新文件请使用 workspace_write_file。"}

        old_content = full_path.read_text(encoding="utf-8")
        old_lines = old_content.split("\n")

        new_lines = list(old_lines)
        file_error = None
        line_offset = 0  # Track cumulative line count changes from prior hunks

        for hunk in file_patch["hunks"]:
            try:
                new_lines, delta = _apply_hunk_to_lines(new_lines, hunk, line_offset)
                line_offset += delta
            except ValueError as e:
                file_error = str(e)
                break

        if file_error:
            results.append({"file": file_path, "error": file_error})
            continue

        new_content = "\n".join(new_lines)

        # Count changes
        old_set = old_lines
        additions = max(0, len(new_lines) - len(old_lines))
        deletions = max(0, len(old_lines) - len(new_lines))

        if not dry_run:
            full_path.write_text(new_content, encoding="utf-8")

        results.append({
            "file": file_path,
            "old_lines": len(old_lines),
            "new_lines": len(new_lines),
            "additions": additions,
            "deletions": deletions,
        })
        total_add += additions
        total_del += deletions

    return {
        "files": [r["file"] for r in results],
        "changes": results,
        "total_additions": total_add,
        "total_deletions": total_del,
        "dry_run": dry_run,
    }


# ═══════════════════════════════════════════════════════════════
# 2. glob_files — Recursive file pattern matching
# ═══════════════════════════════════════════════════════════════

@tool(
    description=(
        "按模式匹配搜索工作区文件。支持 glob 模式（如 **/*.py、src/**/*.ts）。"
        "用于快速定位文件、了解项目结构。"
    ),
    category="代码搜索",
    layer="支撑层",
    domain=["coding"],
)
def glob_files(
    pattern: str,
    max_results: int = 100,
) -> Dict[str, Any]:
    """Search workspace files by glob pattern.

    Args:
        pattern: Glob pattern (e.g., "**/*.py", "scripts/*.py", "**/test_*.py")
        max_results: Maximum number of results (default 100)

    Returns:
        {"files": [{"path": str, "size": int, "modified": str}], "total": int}
    """
    ws = _get_ws()

    matches = []
    for p in sorted(ws.session_dir.rglob(pattern)):
        if p.is_file() and not p.name.startswith("."):
            rel = str(p.relative_to(ws.session_dir))
            matches.append({
                "path": rel,
                "size": p.stat().st_size,
                "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime)),
            })
            if len(matches) >= max_results:
                break

    return {
        "files": matches,
        "total": len(matches),
        "truncated": len(matches) >= max_results,
    }


# ═══════════════════════════════════════════════════════════════
# 3. grep_code — Full-project regex code search
# ═══════════════════════════════════════════════════════════════

@tool(
    description=(
        "在工作区中搜索代码（正则表达式）。返回匹配的文件、行号和内容。"
        "用于定位函数定义、查找变量引用、分析调用链。"
    ),
    category="代码搜索",
    layer="支撑层",
    domain=["coding"],
)
def grep_code(
    pattern: str,
    file_glob: str = "",
    max_results: int = 50,
    context_lines: int = 0,
) -> Dict[str, Any]:
    """Search code in workspace with regex pattern.

    Args:
        pattern: Regular expression pattern to search
        file_glob: Optional file filter (e.g., "*.py", "*.js")
        max_results: Maximum matches to return (default 50)
        context_lines: Lines of context before/after each match (default 0)

    Returns:
        {"matches": [{"file": str, "line": int, "text": str, "context_before": [...], "context_after": [...]}], "total": int}
    """
    ws = _get_ws()

    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return {"error": f"正则表达式错误: {e}"}

    matches = []
    searchable_suffixes = {".py", ".js", ".ts", ".vue", ".json", ".md", ".yaml", ".yml", ".sh", ".css", ".html", ".toml", ".cfg", ".ini", ".txt"}

    for p in ws.session_dir.rglob("*"):
        if not p.is_file():
            continue
        if p.stat().st_size > 200_000:
            continue
        if p.suffix not in searchable_suffixes:
            continue
        if file_glob and not fnmatch.fnmatch(p.name, file_glob):
            continue
        if p.name.startswith("."):
            continue

        try:
            content = p.read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines()
            rel = str(p.relative_to(ws.session_dir))

            for i, line in enumerate(lines):
                if regex.search(line):
                    match_info = {
                        "file": rel,
                        "line": i + 1,
                        "text": line.strip()[:300],
                    }
                    if context_lines > 0:
                        start = max(0, i - context_lines)
                        end = min(len(lines), i + context_lines + 1)
                        match_info["context_before"] = [l.strip() for l in lines[start:i]]
                        match_info["context_after"] = [l.strip() for l in lines[i+1:end]]

                    matches.append(match_info)
                    if len(matches) >= max_results:
                        return {"matches": matches, "total": len(matches), "truncated": True}
        except Exception:
            continue

    return {"matches": matches, "total": len(matches), "truncated": False}


# ═══════════════════════════════════════════════════════════════
# 4. git_snapshot — Auto-commit before edits, rollback support
# ═══════════════════════════════════════════════════════════════

def _ensure_git_repo(ws) -> bool:
    """Initialize git repo in workspace if not already one."""
    git_dir = ws.session_dir / ".git"
    if git_dir.exists():
        return True
    result = _run_cmd(["git", "init"], str(ws.session_dir))
    if result["exit_code"] != 0:
        return False
    # Create .gitignore
    gitignore = ws.session_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("__pycache__/\n*.pyc\n.env\n", encoding="utf-8")
    return True


@tool(
    description=(
        "对工作区做 git 快照（自动 commit）。"
        "在大规模修改前调用可保留回滚点。"
        "支持查看历史快照和回滚到指定版本。"
    ),
    category="版本控制",
    layer="支撑层",
    domain=["coding"],
)
def git_snapshot(
    message: str = "",
    action: str = "commit",
    ref: str = "",
) -> Dict[str, Any]:
    """Git snapshot management for workspace.

    Args:
        message: Commit message (for action="commit")
        action: One of "commit", "log", "diff", "rollback"
        ref: Git ref for diff/rollback (commit hash or HEAD~N)

    Returns:
        Action-dependent result dict
    """
    ws = _get_ws()

    if not _ensure_git_repo(ws):
        return {"error": "无法初始化 git 仓库"}

    cwd = str(ws.session_dir)

    if action == "commit":
        # Stage all changes
        _run_cmd(["git", "add", "-A"], cwd)
        # Check if there's anything to commit
        status = _run_cmd(["git", "status", "--porcelain"], cwd)
        if not status["stdout"].strip():
            return {"message": "没有变更需要快照", "status": "clean"}

        msg = message or f"snapshot: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        result = _run_cmd(["git", "commit", "-m", msg], cwd)
        if result["exit_code"] != 0:
            return {"error": f"commit 失败: {result['stderr']}"}

        # Get the commit hash
        log = _run_cmd(["git", "log", "--oneline", "-1"], cwd)
        return {
            "message": f"快照已保存: {msg}",
            "commit": log["stdout"].strip(),
        }

    elif action == "log":
        n = int(ref) if ref.isdigit() else 10
        result = _run_cmd(["git", "log", f"--oneline", f"-{n}"], cwd)
        if result["exit_code"] != 0:
            return {"error": result["stderr"]}
        return {"log": result["stdout"].strip()}

    elif action == "diff":
        if ref:
            result = _run_cmd(["git", "diff", ref], cwd)
        else:
            result = _run_cmd(["git", "diff", "--staged"], cwd)
        return {"diff": result["stdout"][:10000]}

    elif action == "rollback":
        if not ref:
            return {"error": "回滚需要指定 ref（如 HEAD~1 或 commit hash）"}
        # Save current state first
        _run_cmd(["git", "add", "-A"], cwd)
        _run_cmd(["git", "commit", "-m", f"auto-save before rollback to {ref}"], cwd)
        # Do the rollback
        result = _run_cmd(["git", "checkout", ref, "--", "."], cwd)
        if result["exit_code"] != 0:
            return {"error": f"回滚失败: {result['stderr']}"}
        return {"message": f"已回滚到 {ref}", "warning": "当前工作区已恢复，修改已丢失"}

    return {"error": f"未知 action: {action}。可用: commit, log, diff, rollback"}


# ═══════════════════════════════════════════════════════════════
# 5. code_lint — ruff/pylint static analysis
# ═══════════════════════════════════════════════════════════════

@tool(
    description=(
        "对 Python 代码运行 ruff 静态分析（自动安装 ruff 如未安装）。"
        "检查语法错误、风格问题、常见 bug。"
        "比 workspace_code_review 更专业，支持自动修复。"
    ),
    category="代码审查",
    layer="支撑层",
    domain=["coding"],
)
def code_lint(
    path: str = "",
    fix: bool = False,
) -> Dict[str, Any]:
    """Run ruff linter on workspace Python files.

    Args:
        path: Specific file path (relative to workspace). Empty = lint all .py files.
        fix: If True, auto-fix fixable issues

    Returns:
        {"issues": [...], "fixed": int, "total": int, "summary": str}
    """
    ws = _get_ws()

    # Ensure ruff is available
    check_ruff = _run_cmd(["ruff", "--version"], str(ws.session_dir), timeout=5)
    if check_ruff["exit_code"] != 0:
        # Try to install
        install = _run_cmd(["pip", "install", "ruff", "-q"], str(ws.session_dir), timeout=60)
        if install["exit_code"] != 0:
            return {"error": "无法安装 ruff，请手动执行: pip install ruff"}

    target = str(_safe_path(ws, path)) if path else str(ws.session_dir)

    cmd = ["ruff", "check", target]
    if fix:
        cmd.append("--fix")
    cmd.extend(["--output-format", "json", "--no-cache"])

    result = _run_cmd(cmd, str(ws.session_dir), timeout=60)

    if result["exit_code"] == -1:
        return {"error": result["stderr"]}

    issues = []
    try:
        raw_issues = json.loads(result["stdout"]) if result["stdout"].strip() else []
        for issue in raw_issues:
            issues.append({
                "file": issue.get("filename", "").replace(str(ws.session_dir) + "/", ""),
                "line": issue.get("location", {}).get("row", 0),
                "col": issue.get("location", {}).get("column", 0),
                "code": issue.get("code", ""),
                "message": issue.get("message", ""),
                "fixable": issue.get("fix", {}).get("applicability", "") == "safe",
            })
    except json.JSONDecodeError:
        # ruff might output non-JSON on some errors
        return {"raw_output": result["stdout"][:3000], "stderr": result["stderr"][:1000]}

    fixed_count = sum(1 for i in issues if i.get("fixable"))

    return {
        "issues": issues[:50],
        "total": len(issues),
        "fixed": fixed_count if fix else 0,
        "summary": f"{'已修复' if fix else '发现'} {len(issues)} 个问题" + (f"，其中 {fixed_count} 个已自动修复" if fix and fixed_count else ""),
    }


# ═══════════════════════════════════════════════════════════════
# 6. lsp_diagnostics — pyright/pylsp type checking
# ═══════════════════════════════════════════════════════════════

@tool(
    description=(
        "运行 pyright 类型检查（自动安装如未安装）。"
        "检测类型错误、未定义变量、缺失导入等。"
        "在修改代码后调用可即时发现隐藏问题。"
    ),
    category="代码审查",
    layer="支撑层",
    domain=["coding"],
)
def lsp_diagnostics(
    path: str = "",
) -> Dict[str, Any]:
    """Run pyright type checker on workspace Python files.

    Args:
        path: Specific file path (relative to workspace). Empty = check all.

    Returns:
        {"diagnostics": [...], "summary": str}
    """
    ws = _get_ws()

    # Ensure pyright is available
    check = _run_cmd(["pyright", "--version"], str(ws.session_dir), timeout=10)
    if check["exit_code"] != 0:
        install = _run_cmd(["pip", "install", "pyright", "-q"], str(ws.session_dir), timeout=60)
        if install["exit_code"] != 0:
            return {"error": "无法安装 pyright，请手动执行: pip install pyright"}

    target = str(_safe_path(ws, path)) if path else str(ws.session_dir)

    result = _run_cmd(
        ["pyright", "--outputjson", target],
        str(ws.session_dir),
        timeout=120,
    )

    if result["exit_code"] == -1:
        return {"error": result["stderr"]}

    try:
        data = json.loads(result["stdout"]) if result["stdout"].strip() else {}
    except json.JSONDecodeError:
        return {"raw_output": result["stdout"][:3000], "stderr": result["stderr"][:1000]}

    diagnostics = []
    for diag in data.get("generalDiagnostics", []):
        diagnostics.append({
            "file": diag.get("file", "").replace(str(ws.session_dir) + "/", ""),
            "line": diag.get("range", {}).get("start", {}).get("line", 0) + 1,
            "col": diag.get("range", {}).get("start", {}).get("character", 0) + 1,
            "severity": diag.get("severity", ""),
            "message": diag.get("message", ""),
        })

    summary_parts = []
    summary = data.get("summary", {})
    if summary.get("errorCount"):
        summary_parts.append(f"{summary['errorCount']} 个错误")
    if summary.get("warningCount"):
        summary_parts.append(f"{summary['warningCount']} 个警告")
    if summary.get("informationCount"):
        summary_parts.append(f"{summary['informationCount']} 个提示")

    return {
        "diagnostics": diagnostics[:50],
        "total": len(diagnostics),
        "summary": "类型检查: " + (", ".join(summary_parts) if summary_parts else "无问题"),
    }


# ═══════════════════════════════════════════════════════════════
# 7. read_lines — Read specific line ranges from large files
# ═══════════════════════════════════════════════════════════════

@tool(
    description=(
        "读取文件的指定行范围。适合查看大文件的特定部分（如错误所在行附近）。"
        "比 workspace_read_file 更精准，不会返回整个文件。"
    ),
    category="代码阅读",
    layer="支撑层",
    domain=["coding"],
)
def read_lines(
    path: str,
    start_line: int = 1,
    end_line: int = 50,
) -> Dict[str, Any]:
    """Read specific line range from a workspace file.

    Args:
        path: Relative path to file
        start_line: Starting line number (1-indexed, inclusive)
        end_line: Ending line number (1-indexed, inclusive)

    Returns:
        {"path": str, "lines": str, "start": int, "end": int, "total_lines": int}
    """
    ws = _get_ws()
    full_path = _safe_path(ws, path)

    if not full_path.exists():
        return {"error": f"文件不存在: {path}"}

    content = full_path.read_text(encoding="utf-8", errors="replace")
    all_lines = content.splitlines()
    total = len(all_lines)

    start_idx = max(0, start_line - 1)
    end_idx = min(total, end_line)

    selected = all_lines[start_idx:end_idx]
    # Add line numbers
    numbered = [f"{i+1:4d} | {all_lines[i]}" for i in range(start_idx, end_idx)]

    return {
        "path": path,
        "lines": "\n".join(numbered),
        "start": start_idx + 1,
        "end": end_idx,
        "total_lines": total,
    }


# ═══════════════════════════════════════════════════════════════
# 8. test_generator — Auto-generate pytest tests for a function
# ═══════════════════════════════════════════════════════════════

@tool(
    description=(
        "为 Python 函数自动生成 pytest 测试用例模板。"
        "分析函数签名、类型注解和文档字符串，生成基础测试框架。"
    ),
    category="代码生成",
    layer="支撑层",
    domain=["coding"],
)
def test_generator(
    path: str,
    function_name: str = "",
) -> Dict[str, Any]:
    """Generate pytest test template for a function.

    Args:
        path: Path to the Python file
        function_name: Specific function to test (empty = generate for all public functions)

    Returns:
        {"test_code": str, "functions_found": list, "path": str}
    """
    import ast

    ws = _get_ws()
    full_path = _safe_path(ws, path)

    if not full_path.exists():
        return {"error": f"文件不存在: {path}"}

    content = full_path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(content)
    except SyntaxError as e:
        return {"error": f"文件语法错误: {e}"}

    functions = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_") and node.name != "__init__":
                continue
            if function_name and node.name != function_name:
                continue

            # Extract args
            args = []
            for arg in node.args.args:
                if arg.arg == "self":
                    continue
                annotation = ""
                if arg.annotation:
                    try:
                        annotation = ast.unparse(arg.annotation)
                    except Exception:
                        annotation = "Any"
                args.append({"name": arg.arg, "type": annotation or "Any"})

            # Extract return type
            ret_type = ""
            if node.returns:
                try:
                    ret_type = ast.unparse(node.returns)
                except Exception:
                    ret_type = "Any"

            # Extract docstring
            docstring = ast.get_docstring(node) or ""

            functions.append({
                "name": node.name,
                "args": args,
                "return_type": ret_type,
                "docstring": docstring,
                "line": node.lineno,
            })

    if not functions:
        return {"error": f"未找到函数" + (f" '{function_name}'" if function_name else "")}

    # Generate test code
    module_name = full_path.stem
    test_lines = [
        f'"""Auto-generated tests for {module_name}."""',
        "import pytest",
        f"from {module_name} import {', '.join(f['name'] for f in functions)}",
        "",
        "",
    ]

    for func in functions:
        test_lines.append(f"class Test{func['name'].title()}:")
        test_lines.append(f'    """Tests for {func["name"]}."""')
        test_lines.append("")

        # Basic happy path test
        test_lines.append(f"    def test_{func['name']}_basic(self):")
        if func["docstring"]:
            test_lines.append(f'        """{func["docstring"].split(chr(10))[0]}"""')
        # Build args with placeholder values
        arg_strs = []
        for arg in func["args"]:
            t = arg["type"]
            if "str" in t:
                arg_strs.append(f'{arg["name"]}="test"')
            elif "int" in t:
                arg_strs.append(f'{arg["name"]}=1')
            elif "float" in t:
                arg_strs.append(f'{arg["name"]}=1.0')
            elif "bool" in t:
                arg_strs.append(f'{arg["name"]}=True')
            elif "list" in t.lower() or "List" in t:
                arg_strs.append(f'{arg["name"]}=[]')
            elif "dict" in t.lower() or "Dict" in t:
                arg_strs.append(f'{arg["name"]}={{}}')
            else:
                arg_strs.append(f'{arg["name"]}=None')
        args_call = ", ".join(arg_strs)
        test_lines.append(f"        result = {func['name']}({args_call})")
        test_lines.append("        assert result is not None")
        test_lines.append("")

        # Edge case test
        test_lines.append(f"    def test_{func['name']}_edge_cases(self):")
        test_lines.append(f'        """Test edge cases for {func["name"]}."""')
        test_lines.append("        # TODO: Add edge case tests")
        test_lines.append("        pass")
        test_lines.append("")

        # Error case test
        test_lines.append(f"    def test_{func['name']}_error_handling(self):")
        test_lines.append(f'        """Test error handling for {func["name"]}."""')
        test_lines.append("        # TODO: Add error case tests")
        test_lines.append("        pass")
        test_lines.append("")

    test_code = "\n".join(test_lines)

    # Save the test file
    test_filename = f"test_{module_name}.py"
    test_path = ws.scripts_dir / test_filename
    test_path.write_text(test_code, encoding="utf-8")

    return {
        "test_code": test_code,
        "functions_found": [f["name"] for f in functions],
        "saved_to": f"scripts/{test_filename}",
        "path": path,
    }
