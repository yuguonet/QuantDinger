# -*- coding: utf-8 -*-
"""
Code Workspace Tools — file system + command execution, sandboxed to workspace.

Improvements over basic version:
1. Data source injection — registered data_tools functions available in scripts
2. Streaming output — real-time stdout/stderr via progress_callback
3. Script versioning — auto-version on save, diff support
4. Error recovery — actionable suggestions on failures
5. Background execution — run scripts in background + poll results

Env configuration:
    AGENT_WORKSPACE_ROOT       — root directory for all workspaces
    AGENT_SHELL_TIMEOUT        — max shell command timeout in seconds (default: 300)
    AGENT_SHELL_ALLOWED_CMDS   — comma-separated allowed command prefixes (empty = allow all)
    AGENT_SHELL_BLOCKED_CMDS   — comma-separated blocked commands
    AGENT_SHELL_MAX_OUTPUT     — max output bytes (default: 50000)
    AGENT_BG_MAX_CONCURRENT    — max concurrent background tasks (default: 3)
"""
from __future__ import annotations

import json
import logging
import multiprocessing
import os
import queue
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── LLM code normalization ─────────────────────────────────────

def _normalize_llm_code(code: str) -> str:
    """Normalize code from LLM tool-call arguments.

    Some LLMs serialize newlines as literal '\\n' in function-call args,
    causing compile() failures. Also strips markdown code fences.
    """
    if not code:
        return code
    code = code.strip()
    if code.startswith("```"):
        first_nl = code.find("\n")
        if first_nl != -1:
            code = code[first_nl + 1:]
        if code.rstrip().endswith("```"):
            code = code.rstrip()[:-3].rstrip()
    if "\n" not in code and "\\n" in code:
        code = code.replace("\\n", "\n").replace("\\t", "\t")
    return code


# ── Env-configurable limits ────────────────────────────────────
SHELL_TIMEOUT = int(os.getenv("AGENT_SHELL_TIMEOUT", "300"))
SHELL_BLOCKED = [
    c.strip() for c in os.getenv(
        "AGENT_SHELL_BLOCKED_CMDS",
        "rm -rf /,rm -rf ~,mkfs,dd if=,wipefs,shred,chmod 777 /,chown root"
    ).split(",") if c.strip()
]
SHELL_ALLOWED = [
    c.strip() for c in os.getenv("AGENT_SHELL_ALLOWED_CMDS", "").split(",") if c.strip()
]
MAX_OUTPUT_BYTES = int(os.getenv("AGENT_SHELL_MAX_OUTPUT", "50000"))
BG_MAX_CONCURRENT = int(os.getenv("AGENT_BG_MAX_CONCURRENT", "3"))

# ── Background task registry ───────────────────────────────────
_bg_tasks: Dict[str, Dict[str, Any]] = {}
_bg_lock = threading.Lock()


# ── Safety helpers ─────────────────────────────────────────────

def _check_shell_safety(cmd: str) -> dict:
    """Check if a shell command is safe."""
    cmd_stripped = cmd.strip()
    for blocked in SHELL_BLOCKED:
        if blocked and blocked in cmd_stripped:
            return {"error": f"命令被安全策略阻止: 包含 '{blocked}'", "blocked": True}
    if SHELL_ALLOWED:
        if not any(cmd_stripped.startswith(a) for a in SHELL_ALLOWED):
            return {
                "error": f"命令不在允许列表中。允许的命令前缀: {', '.join(SHELL_ALLOWED)}",
                "blocked": True,
            }
    return {}


def _recovery_suggestion(tool: str, error: str) -> str:
    """Generate actionable recovery suggestions based on error context."""
    err_lower = (error or "").lower()

    suggestions = []

    if "timeout" in err_lower or "超时" in err_lower:
        suggestions.append("尝试减少数据量或增加 timeout 参数")
        if tool == "exec_script":
            suggestions.append("长时间任务可用 run_background 后台执行")

    if "import" in err_lower or "module" in err_lower:
        # Extract module name
        import re
        m = re.search(r"No module named '(\w+)'", error or "")
        mod = m.group(1) if m else ""
        if mod:
            suggestions.append(f"安装依赖: shell_exec('pip install {mod}')")
            suggestions.append("如需换源: shell_exec('pip install {mod} -i https://pypi.tuna.tsinghua.edu.cn/simple')")

    if "filenotfound" in err_lower or "no such file" in err_lower or "不存在" in err_lower:
        suggestions.append("检查文件路径是否正确（相对于工作区根目录）")
        suggestions.append("用 list_workspace 查看现有文件")

    if "permission" in err_lower or "权限" in err_lower:
        suggestions.append("检查文件权限: shell_exec('ls -la <path>')")

    if "memory" in err_lower or "内存" in err_lower:
        suggestions.append("减少数据量或分批处理")

    if "keyerror" in err_lower or "column" in err_lower:
        suggestions.append("检查数据列名: read_file 查看CSV文件头")

    if not suggestions:
        suggestions.append("检查错误详情，修正代码后重试")

    return "💡 建议: " + "; ".join(suggestions[:3])


# ── Streaming execution helpers ────────────────────────────────

def _stream_exec(cmd: str, cwd: str, timeout: int,
                 emit: Optional[Callable] = None,
                 tool_name: str = "shell_exec") -> Dict[str, Any]:
    """Execute a command with streaming stdout/stderr output.

    Args:
        cmd: Command to execute
        cwd: Working directory
        timeout: Timeout in seconds
        emit: Progress callback for streaming output
        tool_name: Tool name for progress events

    Returns:
        {"output": str, "exit_code": int, "duration": float, "error": str|None}
    """
    t0 = time.time()
    output_lines = []

    try:
        proc = subprocess.Popen(
            cmd,
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,  # Line buffered
            env={**os.environ, "WORKSPACE": cwd, "WORKSPACE_DIR": cwd},
        )

        # Read output line by line with timeout
        import select

        deadline = t0 + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                proc.terminate()
                time.sleep(0.5)
                if proc.poll() is None:
                    proc.kill()
                output_lines.append("\n[超时终止]")
                break

            # Check if there's data to read
            try:
                ready, _, _ = select.select([proc.stdout], [], [], min(remaining, 0.5))
            except (ValueError, OSError):
                break

            if ready:
                line = proc.stdout.readline()
                if line:
                    output_lines.append(line)
                    # Emit streaming progress (throttled to every 3 lines)
                    if emit and len(output_lines) % 3 == 0:
                        emit({
                            "type": "tool_stream",
                            "tool": tool_name,
                            "output": "".join(output_lines[-10:]),
                            "lines_total": len(output_lines),
                        })
                elif proc.poll() is not None:
                    break
            elif proc.poll() is not None:
                # Process ended, drain remaining
                remaining_out = proc.stdout.read()
                if remaining_out:
                    output_lines.append(remaining_out)
                break

        proc.wait(timeout=5)
        exit_code = proc.returncode

    except Exception as e:
        return {
            "output": "".join(output_lines)[-MAX_OUTPUT_BYTES:],
            "exit_code": -1,
            "duration": round(time.time() - t0, 2),
            "error": str(e),
        }

    full_output = "".join(output_lines)[-MAX_OUTPUT_BYTES:]
    error = None if exit_code == 0 else f"Exit code: {exit_code}"

    result = {
        "output": full_output,
        "exit_code": exit_code,
        "duration": round(time.time() - t0, 2),
        "error": error,
    }

    # Add recovery suggestion on failure
    if error:
        result["recovery"] = _recovery_suggestion(tool_name, full_output)

    return result


# ── Data source injection ──────────────────────────────────────

def _build_data_source_code() -> str:
    """Generate Python code that injects registered tool functions into exec context.

    Instead of reimplementing data-fetching logic, this directly imports and wraps
    the same functions used by the registered OpenAI tools (data_tools.py).
    This ensures exec_script code uses the same reliable接口 as the agent's tools,
    with consistent signatures and market detection.
    """
    return '''
# === Data Source Auto-Injection (via registered tools) ===
# Provides: get_kline, get_ticker, get_stock_info, resolve_stock_name
# These wrap the same functions registered as agent tools in data_tools.py,
# ensuring consistent behavior and signatures.
try:
    from app.agent.tools.data import (
        get_daily_history as _get_daily_history,
        get_realtime_quote as _get_realtime_quote,
        get_stock_info as _get_stock_info,
        resolve_stock_name as _resolve_stock_name,
    )
    import pandas as _pd

    def get_kline(code, period="1D", count=120):
        """获取K线数据，返回DataFrame。

        Args:
            code: 股票代码 (如 '000001', '600519') 或交易对 (如 'BTC/USDT')
            period: K线周期，默认 '1D' (日线)
            count: 数据条数，默认 120

        Returns:
            pandas DataFrame with columns: time, open, high, low, close, volume
            or None if no data
        """
        # period 转 days 映射 (get_daily_history 用 days 参数)
        _PERIOD_DAYS = {"1D": 1, "1W": 7, "1M": 30}
        days = count  # 默认 count 直接作为天数
        # 如果传了 period 且不是 1D，调整天数
        if period and period != "1D":
            days = count * _PERIOD_DAYS.get(period, 1)

        data = _get_daily_history(code, days=days)
        if isinstance(data, list) and data:
            return _pd.DataFrame(data)
        if isinstance(data, dict) and data.get("error"):
            print(f"[get_kline] 错误: {data['error']}")
        return None

    def get_ticker(code):
        """获取实时行情 (同 get_realtime_quote)"""
        return _get_realtime_quote(code)

    def get_stock_info(code):
        """获取股票基本面信息"""
        return _get_stock_info(code)

    def resolve_name(code):
        """根据股票代码获取中文名称"""
        return _resolve_stock_name(code)

    _DATASOURCE_AVAILABLE = True
except Exception as _ds_err:
    _DATASOURCE_AVAILABLE = False
    import warnings
    warnings.warn(f"Data source injection failed: {_ds_err}")
    def get_kline(*a, **kw): return None
    def get_ticker(*a, **kw): return None
    def get_stock_info(*a, **kw): return None
    def resolve_name(*a, **kw): return None
'''


# ── Tool implementations ───────────────────────────────────────

def shell_exec(
    command: str,
    timeout: int = SHELL_TIMEOUT,
) -> Dict[str, Any]:
    """Execute a shell command in the session's workspace directory.

    Streaming: real-time output is pushed via progress events.

    Args:
        command: Shell command to execute
        timeout: Timeout in seconds (default from AGENT_SHELL_TIMEOUT env, max 600)

    Returns:
        {"output": str, "exit_code": int, "duration": float, "error": str|None}
    """
    from app.agent.core.workspace import get_workspace
    from app.agent.core.tool_context import get_session_id, emit_progress

    timeout = min(max(timeout, 1), 600)

    safety = _check_shell_safety(command)
    if safety:
        return {"output": "", "exit_code": -1, "duration": 0, **safety}

    ws = get_workspace(get_session_id() or "default")
    emit = lambda ev: emit_progress(ev)

    return _stream_exec(command, str(ws.session_dir), timeout, emit, "shell_exec")


def workspace_save_script(
    name: str,
    code: str,
    description: str = "",
) -> Dict[str, Any]:
    """Save a Python script with automatic versioning.

    Each save increments the version number. Previous versions are preserved
    and can be loaded or diffed.

    Args:
        name: Script filename (e.g., "backtest_ma.py")
        code: Python source code
        description: What this script does

    Returns:
        {"name": str, "path": str, "size": int, "lines": int, "version": int}
    """
    from app.agent.core.workspace import get_workspace
    from app.agent.core.tool_context import get_session_id
    ws = get_workspace(get_session_id() or "default")
    return ws.save_script(name, code, description)


def workspace_load_script(
    name: str,
    version: int = 0,
) -> Dict[str, Any]:
    """Load a script from the workspace.

    Args:
        name: Script filename
        version: Specific version (0 = latest)

    Returns:
        {"name": str, "code": str, "lines": int, "version": int}
    """
    from app.agent.core.workspace import get_workspace
    from app.agent.core.tool_context import get_session_id
    ws = get_workspace(get_session_id() or "default")
    return ws.load_script(name, version)


def workspace_list_versions(
    name: str,
) -> Dict[str, Any]:
    """List all versions of a script.

    Args:
        name: Script filename

    Returns:
        {"name": str, "versions": [{"version": int, "size": int, "saved_at": str}]}
    """
    from app.agent.core.workspace import get_workspace
    from app.agent.core.tool_context import get_session_id
    ws = get_workspace(get_session_id() or "default")
    versions = ws.list_script_versions(name)
    return {"name": name, "versions": versions}


def workspace_diff_versions(
    name: str,
    v1: int,
    v2: int,
) -> Dict[str, Any]:
    """Get a diff between two script versions.

    Args:
        name: Script filename
        v1: First version
        v2: Second version

    Returns:
        {"diff": str, "name": str, "v1": int, "v2": int}
    """
    from app.agent.core.workspace import get_workspace
    from app.agent.core.tool_context import get_session_id
    ws = get_workspace(get_session_id() or "default")
    return ws.diff_versions(name, v1, v2)


def workspace_list() -> Dict[str, Any]:
    """List all files in the workspace (scripts, data, outputs).

    Returns:
        {"scripts": [...], "data_files": [...], "output_files": [...]}
    """
    from app.agent.core.workspace import get_workspace
    from app.agent.core.tool_context import get_session_id
    ws = get_workspace(get_session_id() or "default")
    return ws.info()


def workspace_write_file(
    path: str,
    content: str,
) -> Dict[str, Any]:
    """Write content to a file in the workspace.

    Args:
        path: Relative path (e.g., "data/prices.csv")
        content: File content

    Returns:
        {"path": str, "size": int}
    """
    from app.agent.core.workspace import get_workspace
    from app.agent.core.tool_context import get_session_id
    from pathlib import Path

    ws = get_workspace(get_session_id() or "default")
    safe = path.lstrip("/").replace("..", "")
    full_path = ws.session_dir / safe

    try:
        full_path.resolve().relative_to(ws.session_dir.resolve())
    except ValueError:
        return {"error": f"路径越界: {path}（必须在工作区内）"}

    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content, encoding="utf-8")
    return {"path": str(full_path), "name": safe, "size": len(content)}


def workspace_read_file(
    path: str,
    max_chars: int = 100000,
) -> Dict[str, Any]:
    """Read a file from the workspace.

    Args:
        path: Relative path (e.g., "output/result.csv")
        max_chars: Maximum characters to return

    Returns:
        {"path": str, "content": str, "size": int}
    """
    from app.agent.core.workspace import get_workspace
    from app.agent.core.tool_context import get_session_id
    from pathlib import Path

    ws = get_workspace(get_session_id() or "default")
    safe = path.lstrip("/").replace("..", "")
    full_path = ws.session_dir / safe

    try:
        full_path.resolve().relative_to(ws.session_dir.resolve())
    except ValueError:
        return {"error": f"路径越界: {path}"}

    if not full_path.exists():
        return {"error": f"文件不存在: {path}", "recovery": _recovery_suggestion("read_file", "文件不存在")}

    content = full_path.read_text(encoding="utf-8")
    if len(content) > max_chars:
        content = content[:max_chars] + f"\n... (截断, 共 {len(content)} 字符)"

    return {"path": str(full_path), "content": content, "size": len(content)}


def workspace_exec_script(
    name: str = "",
    code: str = "",
    timeout: int = 120,
    save_as: str = "",
) -> Dict[str, Any]:
    """Execute a Python script in the workspace with full filesystem access + data source.

    Enhanced over python_exec:
    - cwd = workspace (read/write files freely)
    - Auto-injected: WORKSPACE, SCRIPTS_DIR, DATA_DIR, OUTPUT_DIR, Path, pd, np
    - Auto-injected: get_kline(), get_ticker(), get_stock_info(), resolve_name() via registered data_tools
    - Streaming output via progress events
    - Up to 600s timeout
    - Auto-save with versioning

    Args:
        name: Script name to load (from workspace). If empty, uses `code`.
        code: Python code to execute
        timeout: Timeout in seconds (max 600)
        save_as: Auto-save code before execution with versioning

    Returns:
        {"output": str, "result": any, "error": str|None, "variables": list, "duration": float}
    """
    from app.agent.core.workspace import get_workspace
    from app.agent.core.tool_context import get_session_id, emit_progress

    timeout = min(max(timeout, 5), 600)
    ws = get_workspace(get_session_id() or "default")

    # Load script if name given
    if name and not code:
        loaded = ws.load_script(name)
        if loaded.get("error"):
            return {"output": "", "result": None, "error": loaded["error"],
                    "recovery": _recovery_suggestion("exec_script", loaded["error"])}
        code = loaded["code"]

    # Normalize LLM-generated code (fix literal \\n, strip code fences, etc.)
    code = _normalize_llm_code(code)

    if not code:
        return {"output": "", "result": None, "error": "没有可执行的代码"}

    # Auto-save with versioning if requested
    save_info = None
    if save_as:
        save_info = ws.save_script(save_as, code)
        emit_progress({"type": "tool_info", "tool": "exec_script",
                        "message": f"脚本已保存: {save_as} v{save_info.get('version', '?')}"})

    # Inject data source code at the top
    full_code = _build_data_source_code() + "\n" + code

    # Execute with streaming
    t0 = time.time()
    try:
        result = _exec_in_workspace_streaming(full_code, ws, timeout, emit_progress)
        result["duration"] = round(time.time() - t0, 2)
        if save_info:
            result["saved_as"] = save_info
        if result.get("error"):
            result["recovery"] = _recovery_suggestion("exec_script", result["error"])
        return result
    except Exception as e:
        return {
            "output": "",
            "result": None,
            "error": str(e),
            "duration": round(time.time() - t0, 2),
            "recovery": _recovery_suggestion("exec_script", str(e)),
        }


def run_background(
    code: str = "",
    name: str = "",
    timeout: int = 600,
) -> Dict[str, Any]:
    """Execute a script in the background. Returns immediately with a task_id.

    Use poll_task(task_id) to check status and get results.

    Args:
        code: Python code to execute
        name: Script name to load from workspace
        timeout: Max execution time in seconds

    Returns:
        {"task_id": str, "status": "running"}
    """
    from app.agent.core.workspace import get_workspace
    from app.agent.core.tool_context import get_session_id

    with _bg_lock:
        running = sum(1 for t in _bg_tasks.values() if t["status"] == "running")
        if running >= BG_MAX_CONCURRENT:
            return {"error": f"并发后台任务已达上限 ({BG_MAX_CONCURRENT})，请先等待任务完成"}

    ws = get_workspace(get_session_id() or "default")

    if name and not code:
        loaded = ws.load_script(name)
        if loaded.get("error"):
            return {"error": loaded["error"]}
        code = loaded["code"]

    if not code:
        return {"error": "没有可执行的代码"}

    import uuid
    task_id = str(uuid.uuid4())[:8]
    full_code = _build_data_source_code() + "\n" + code

    _bg_tasks[task_id] = {
        "task_id": task_id,
        "status": "running",
        "started_at": time.time(),
        "result": None,
    }

    def _run():
        try:
            result = _exec_in_workspace_streaming(full_code, ws, timeout, None)
            result["duration"] = round(time.time() - _bg_tasks[task_id]["started_at"], 2)
            _bg_tasks[task_id]["status"] = "completed"
            _bg_tasks[task_id]["result"] = result
        except Exception as e:
            _bg_tasks[task_id]["status"] = "failed"
            _bg_tasks[task_id]["result"] = {"error": str(e)}

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    return {"task_id": task_id, "status": "running", "timeout": timeout}


def poll_task(
    task_id: str,
) -> Dict[str, Any]:
    """Poll the status of a background task.

    Args:
        task_id: Task ID from run_background

    Returns:
        {"task_id": str, "status": str, "result": dict|None}
    """
    task = _bg_tasks.get(task_id)
    if not task:
        return {"error": f"Task {task_id} not found"}
    return {
        "task_id": task_id,
        "status": task["status"],
        "started_at": task.get("started_at"),
        "duration": round(time.time() - task["started_at"], 1) if task["status"] == "running" else None,
        "result": task.get("result"),
    }


def apply_template(
    template: str,
) -> Dict[str, Any]:
    """Apply a project template to the workspace.

    Templates: multi_factor, backtest_pipeline, data_pipeline

    Args:
        template: Template name

    Returns:
        {"template": str, "created_scripts": [...]}
    """
    from app.agent.core.workspace import apply_template as _apply
    from app.agent.core.tool_context import get_session_id
    return _apply(get_session_id() or "default", template)


def list_templates() -> Dict[str, Any]:
    """List available project templates."""
    from app.agent.core.workspace import list_templates as _list
    templates = _list()
    return {"templates": templates}


# ── Internal execution ─────────────────────────────────────────

def _exec_in_workspace_streaming(
    code: str,
    ws,
    timeout: int,
    emit: Optional[Callable],
) -> Dict[str, Any]:
    """Execute code in a subprocess with workspace as cwd, streaming output via queue."""
    result_queue = multiprocessing.Queue()
    output_queue = multiprocessing.Queue() if emit else None

    proc = multiprocessing.Process(
        target=_workspace_exec_worker,
        args=(code, str(ws.session_dir), str(ws.scripts_dir),
              str(ws.data_dir), str(ws.output_dir), result_queue, output_queue),
        daemon=True,
    )
    proc.start()

    # Wait with real-time output streaming
    start = time.time()
    last_emit = 0
    accumulated_output = []

    while proc.is_alive():
        elapsed = time.time() - start
        if elapsed > timeout:
            proc.terminate()
            time.sleep(0.5)
            if proc.is_alive():
                proc.kill()
            proc.join(timeout=2)
            return {
                "output": "".join(accumulated_output)[-5000:],
                "result": None,
                "error": f"代码执行超时（{timeout}秒）",
                "recovery": _recovery_suggestion("exec_script", "timeout"),
            }

        # Drain output queue
        if output_queue:
            while not output_queue.empty():
                try:
                    line = output_queue.get_nowait()
                    accumulated_output.append(line)
                except Exception:
                    break

            # Throttled emit (every 0.5s)
            now = time.time()
            if accumulated_output and now - last_emit > 0.5:
                emit({"type": "tool_stream", "tool": "exec_script",
                       "output": "".join(accumulated_output[-20:]),
                       "lines_total": len(accumulated_output)})
                last_emit = now

        time.sleep(0.2)

    # Drain remaining output
    if output_queue:
        while not output_queue.empty():
            try:
                accumulated_output.append(output_queue.get_nowait())
            except Exception:
                break

    if not result_queue.empty():
        result = result_queue.get()
        # Include streaming output if available
        if accumulated_output and not result.get("output"):
            result["output"] = "".join(accumulated_output)[-5000:]
        return result

    return {
        "output": "".join(accumulated_output)[-5000:],
        "result": None,
        "error": "执行异常：未产生结果",
    }


def _workspace_exec_worker(
    code: str,
    workspace_dir: str,
    scripts_dir: str,
    data_dir: str,
    output_dir: str,
    result_queue: multiprocessing.Queue,
    output_queue: Optional[multiprocessing.Queue] = None,
):
    """Worker process for workspace-aware code execution."""
    import io
    import sys
    from pathlib import Path

    # Worker needs os for chdir, but we'll block it from user code
    import os as _os
    _os.chdir(workspace_dir)

    # Now remove os from sys.modules so user code can't import it
    if "os" in sys.modules:
        del sys.modules["os"]
    if "os.path" in sys.modules:
        del sys.modules["os.path"]

    try:
        import pandas as pd
    except ImportError:
        pd = None
    try:
        import numpy as np
    except ImportError:
        np = None

    # Build safe builtins — block dangerous introspection
    import builtins as _b
    safe_builtins = {}
    blocked = {
        "exec", "eval", "compile", "__import__",
        "breakpoint", "input", "exit", "quit",
        "globals", "locals", "vars", "dir",
        "memoryview", "bytearray", "super",
        "getattr", "setattr", "delattr",  # 防止属性绕过
    }
    for name in dir(_b):
        if name.startswith("_") or name in blocked:
            continue
        safe_builtins[name] = getattr(_b, name)
    safe_builtins["__import__"] = _safe_import_for_workspace

    global_ns = {"__builtins__": safe_builtins}
    local_ns: Dict[str, Any] = {}

    if pd is not None:
        local_ns["pd"] = pd
    if np is not None:
        local_ns["np"] = np

    # Inject workspace paths (as Path objects, no os dependency)
    local_ns["WORKSPACE"] = workspace_dir
    local_ns["SCRIPTS_DIR"] = scripts_dir
    local_ns["DATA_DIR"] = data_dir
    local_ns["OUTPUT_DIR"] = output_dir
    local_ns["Path"] = Path

    old_stdout = sys.stdout
    old_stderr = sys.stderr
    captured_out = io.StringIO()
    captured_err = io.StringIO()

    # Streaming tee: write to both capture buffer and output_queue
    class _TeeWriter:
        def __init__(self, original, capture, q):
            self._original = original
            self._capture = capture
            self._queue = q

        def write(self, text):
            self._capture.write(text)
            if self._queue and text.strip():
                try:
                    self._queue.put_nowait(text)
                except Exception:
                    pass

        def flush(self):
            pass

        def __getattr__(self, name):
            return getattr(self._original, name)

    try:
        os.chdir(workspace_dir)
        tee_out = _TeeWriter(old_stdout, captured_out, output_queue)
        tee_err = _TeeWriter(old_stderr, captured_err, output_queue)
        sys.stdout = tee_out
        sys.stderr = tee_err

        compiled = compile(code, "<workspace_script>", "exec")
        exec(compiled, global_ns, local_ns)

        result = local_ns.get("result")
        user_vars = [
            k for k in local_ns
            if not k.startswith("_") and k not in (
                "pd", "np", "data", "__builtins__",
                "WORKSPACE", "SCRIPTS_DIR", "DATA_DIR", "OUTPUT_DIR", "Path",
                "get_kline", "get_ticker", "get_stock_info", "resolve_name",
            )
        ]

        result_queue.put({
            "output": captured_out.getvalue()[:5000],
            "result": _serialize_for_workspace(result),
            "error": None,
            "variables": user_vars[:30],
        })

    except ImportError as e:
        result_queue.put({
            "output": captured_out.getvalue()[:2000],
            "result": None,
            "error": f"Import error: {e}",
        })
    except Exception:
        import traceback
        result_queue.put({
            "output": captured_out.getvalue()[:2000],
            "result": None,
            "error": traceback.format_exc()[:3000],
        })
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr


def _safe_import_for_workspace(name: str, *args, **kwargs):
    """Restricted import for workspace execution.

    Strategy: whitelist safe modules instead of blacklist dangerous ones.
    'os' is NOT allowed (too many dangerous functions like os.system, os.popen).
    Use pathlib.Path for all file operations.
    """
    import importlib

    # Explicitly blocked (even if somehow in whitelist)
    BLOCKED = {
        "os", "sys", "subprocess", "shutil", "multiprocessing",
        "socket", "http", "urllib", "requests",
        "ctypes", "signal", "threading", "importlib",
        "code", "codeop", "compileall",
        "tempfile", "shelve", "dbm", "sqlite3", "pickle",
    }

    # Whitelist: allowed modules for workspace scripts
    ALLOWED = {
        # 数据处理
        "pandas", "numpy", "scipy", "statistics", "math", "decimal", "fractions",
        # 技术分析
        "ta", "talib",
        # 机器学习
        "sklearn", "sklearn.linear_model", "sklearn.ensemble", "sklearn.preprocessing",
        "sklearn.metrics", "sklearn.model_selection", "sklearn.cluster",
        # 可视化
        "matplotlib", "matplotlib.pyplot", "seaborn",
        # 文本/数据
        "json", "csv", "re", "collections", "itertools", "functools", "operator",
        "string", "textwrap", "unicodedata",
        # 日期时间
        "datetime", "time", "calendar",
        # 文件（安全子集）
        "pathlib", "glob", "fnmatch", "io",
        # 其他
        "copy", "pprint", "enum", "dataclasses", "typing",
        "hashlib", "base64", "uuid", "traceback",
    }

    root = name.split(".")[0]
    if root in BLOCKED:
        raise ImportError(f"Import of '{name}' is blocked for security reasons.")

    # Check whitelist: root module must be allowed
    for i in range(len(name.split(".")), 0, -1):
        prefix = ".".join(name.split(".")[:i])
        if prefix in ALLOWED:
            return importlib.import_module(name, *args, **kwargs)

    raise ImportError(
        f"Import of '{name}' is not allowed in workspace scripts. "
        f"Use pathlib.Path for file operations instead of os."
    )


def _serialize_for_workspace(obj: Any, _depth: int = 0) -> Any:
    """Serialize result for JSON transport."""
    if _depth > 10:
        return str(obj)
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_serialize_for_workspace(x, _depth + 1) for x in obj[:500]]
    if isinstance(obj, dict):
        return {str(k): _serialize_for_workspace(v, _depth + 1) for k, v in list(obj.items())[:200]}
    try:
        import pandas as pd
        if isinstance(obj, pd.DataFrame):
            if len(obj) > 200:
                return {"_type": "DataFrame", "shape": list(obj.shape),
                        "columns": list(obj.columns),
                        "head": _serialize_for_workspace(obj.head(20).to_dict(orient="records")),
                        "tail": _serialize_for_workspace(obj.tail(5).to_dict(orient="records"))}
            return {"_type": "DataFrame", "data": obj.to_dict(orient="records")}
    except ImportError:
        pass
    try:
        import numpy as np
        if isinstance(obj, np.ndarray):
            if obj.size > 500:
                return {"_type": "ndarray", "shape": list(obj.shape),
                        "sample": _serialize_for_workspace(obj.flat[:20].tolist())}
            return {"_type": "ndarray", "data": obj.tolist()}
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
    except ImportError:
        pass
    return str(obj)[:2000]


# ── Tool specs for registry ────────────────────────────────────

TOOL_SPEC = [
    {
        "fn": shell_exec,
        "name": "shell_exec",
        "description": (
            "在工作区目录中执行 shell 命令（支持流式输出）。"
            "命令的 cwd 自动设为当前会话的工作区。"
            "可用环境变量 WORKSPACE 获取工作区绝对路径。"
            "\n\n适用场景：pip install、python、文件操作、数据处理、调用外部工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的 shell 命令"},
                "timeout": {
                    "type": "integer",
                    "description": f"超时秒数（默认 {SHELL_TIMEOUT}，最大 600）",
                    "default": SHELL_TIMEOUT,
                },
            },
            "required": ["command"],
        },
    },
    {
        "fn": workspace_save_script,
        "name": "save_script",
        "description": (
            "保存 Python 脚本到工作区（自动版本管理）。"
            "每次保存自动递增版本号（v1, v2, ...），历史版本可查可回溯。"
            "支持迭代开发：保存(v1) → 执行 → 修改 → 保存(v2) → ..."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "脚本文件名"},
                "code": {"type": "string", "description": "Python 源代码"},
                "description": {"type": "string", "description": "脚本用途说明"},
            },
            "required": ["name", "code"],
        },
    },
    {
        "fn": workspace_load_script,
        "name": "load_script",
        "description": "加载工作区中的脚本。可指定版本号（默认最新版本）。",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "脚本文件名"},
                "version": {"type": "integer", "description": "版本号（0=最新）", "default": 0},
            },
            "required": ["name"],
        },
    },
    {
        "fn": workspace_list_versions,
        "name": "list_versions",
        "description": "列出脚本的所有版本历史。",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "脚本文件名"},
            },
            "required": ["name"],
        },
    },
    {
        "fn": workspace_diff_versions,
        "name": "diff_versions",
        "description": "对比脚本两个版本的差异。",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "脚本文件名"},
                "v1": {"type": "integer", "description": "旧版本号"},
                "v2": {"type": "integer", "description": "新版本号"},
            },
            "required": ["name", "v1", "v2"],
        },
    },
    {
        "fn": workspace_list,
        "name": "list_workspace",
        "description": "列出工作区中所有文件。",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "fn": workspace_write_file,
        "name": "write_file",
        "description": "向工作区写入文件（CSV、JSON、TXT等）。路径必须相对，自动创建父目录。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对路径，如 data/prices.csv"},
                "content": {"type": "string", "description": "文件内容"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "fn": workspace_read_file,
        "name": "read_file",
        "description": "读取工作区中的文件。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对路径"},
                "max_chars": {"type": "integer", "description": "最大返回字符数", "default": 100000},
            },
            "required": ["path"],
        },
    },
    {
        "fn": workspace_exec_script,
        "name": "exec_script",
        "description": (
            "在工作区执行 Python 脚本（增强版）。"
            "\n\n与 python_exec 的区别："
            "\n- 工作目录=工作区，可自由读写文件"
            "\n- 自动注入 get_kline/get_ticker/get_stock_info/resolve_name（委托 data_tools，共用已注册工具实现）"
            "\n- 自动注入 WORKSPACE/SCRIPTS_DIR/DATA_DIR/OUTPUT_DIR/Path/pd/np"
            "\n- 流式输出（实时查看执行进度）"
            "\n- 超时上限 600 秒"
            "\n\n适用：复杂分析、多步数据管道、需要文件I/O的场景。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "已保存脚本名（与 code 二选一）"},
                "code": {"type": "string", "description": "直接执行的代码"},
                "timeout": {"type": "integer", "description": "超时秒数（默认120，最大600）", "default": 120},
                "save_as": {"type": "string", "description": "执行前自动保存为该文件名（可选，带版本号）"},
            },
            "required": [],
        },
    },
    {
        "fn": run_background,
        "name": "run_background",
        "description": (
            "后台执行脚本，立即返回 task_id。用 poll_task 查询结果。"
            "适合长时间任务（大数据处理、全市场扫描等）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python 代码"},
                "name": {"type": "string", "description": "已保存脚本名"},
                "timeout": {"type": "integer", "description": "最大执行时间", "default": 600},
            },
            "required": [],
        },
    },
    {
        "fn": poll_task,
        "name": "poll_task",
        "description": "查询后台任务状态和结果。",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务ID（来自 run_background）"},
            },
            "required": ["task_id"],
        },
    },
    {
        "fn": apply_template,
        "name": "apply_template",
        "description": (
            "应用项目模板到工作区，一键生成分析脚手架。"
            "可用模板：multi_factor(多因子选股)、backtest_pipeline(回测管道)、data_pipeline(数据清洗)"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "template": {"type": "string", "description": "模板名称"},
            },
            "required": ["template"],
        },
    },
    {
        "fn": list_templates,
        "name": "list_templates",
        "description": "列出可用的项目模板。",
        "parameters": {"type": "object", "properties": {}},
    },
]
