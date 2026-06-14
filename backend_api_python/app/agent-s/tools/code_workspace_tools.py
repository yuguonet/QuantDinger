# -*- coding: utf-8 -*-
"""
Code Workspace Tools — file system + command execution, sandboxed to workspace.

Improvements over basic version:
1. Data source injection — DataSourceFactory functions available in scripts
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
from app.agent.tools.registry import tool

logger = logging.getLogger(__name__)

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
    """Generate Python code that auto-discovers and injects all data source APIs.

    Uses introspection to discover capabilities from:
    - DataSourceFactory + BaseDataSource (per-market data: kline, ticker, etc.)
    - index module (index realtime, kline, northbound, etc.)
    - china_market module (fear_greed, hot_sectors, sector_trend, sector_cycle, etc.)
    - StockBasicDB (stock_basic_info table: search, filter, concepts, industries, etc.)

    No hardcoding — new methods added to any of these are automatically exposed.
    """
    return '''
# === Data Source Auto-Injection (Introspection-based) ===
import inspect as _inspect

def _discover_methods(obj, prefix="", exclude=None):
    """Discover all public methods of an object, return {name: callable}."""
    exclude = set(exclude or [])
    methods = {}
    for name in dir(obj):
        if name.startswith("_") or name in exclude:
            continue
        attr = getattr(obj, name, None)
        if attr is not None and callable(attr):
            methods[f"{prefix}{name}" if prefix else name] = attr
    return methods

# ── 1. DataSourceFactory: per-market data sources ──
try:
    from app.data_sources.factory import DataSourceFactory as _DSF
    from app.data_sources.market_detector import detect_market as _detect_market

    def get_ds(market=None, code=None):
        if code and not market:
            market = _detect_market(code)
        return _DSF.get_source(market or "CNStock")

    # Discover all methods on BaseDataSource (kline, ticker, get_stock_info, etc.)
    _ds_instance = _DSF.get_source("CNStock")
    _ds_methods = _discover_methods(_ds_instance)

    # Wrap each method: auto-inject ds instance, first arg = code if needed
    def _make_ds_wrapper(method_name, method):
        def wrapper(*args, market=None, code=None, **kwargs):
            ds = get_ds(market=market, code=code) if market or code else _ds_instance
            fn = getattr(ds, method_name)
            return fn(*args, **kwargs)
        wrapper.__name__ = method_name
        wrapper.__doc__ = method.__doc__ or f"DataSource.{method_name}"
        return wrapper

    for _name, _method in _ds_methods.items():
        globals()[_name] = _make_ds_wrapper(_name, _method)

    def list_markets():
        return ["CNStock", "HKStock", "USStock", "Crypto", "Forex", "Futures", "MOEX"]

    _DS_AVAILABLE = True
except Exception as _ds_err:
    _DS_AVAILABLE = False
    def list_markets(): return ["CNStock", "HKStock", "USStock", "Crypto", "Forex", "Futures", "MOEX"]

# ── 2. index: market index data (realtime, kline, northbound) ──
try:
    from app.market_cn.index import (
        get_index_realtime as _get_index_realtime,
        get_index_daily_kline as _get_index_daily_kline,
        get_northbound_realtime as _get_northbound_realtime,
        get_northbound_daily as _get_northbound_daily,
    )

    def get_index_realtime(codes=None):
        """获取指数实时行情"""
        return _get_index_realtime(codes)

    def get_index_daily_kline(code="000001", days=200):
        """获取指数日K线"""
        return _get_index_daily_kline(code, days)

    def get_northbound_realtime():
        """获取北向资金实时流向"""
        return _get_northbound_realtime()

    def get_northbound_daily(days=120):
        """获取北向资金日级数据"""
        return _get_northbound_daily(days)

    _INDEX_AVAILABLE = True
except Exception as _index_err:
    _INDEX_AVAILABLE = False

# ── 3. china_market: cached market analysis (fear_greed, sectors, macro, etc.) ──
try:
    from app.market_cn import china_market as _cm
    _cm_funcs = {n: f for n, f in _inspect.getmembers(_cm, _inspect.isfunction)
                  if not n.startswith("_") and n not in ("cache_get", "cache_put", "cache_is_stale")}

    for _name, _func in _cm_funcs.items():
        globals()[_name] = _func

    _CM_AVAILABLE = True
except Exception as _cm_err:
    _CM_AVAILABLE = False

# ── 4. StockBasicDB: stock_basic_info table (search, filter, concepts, etc.) ──
# Lazy init: defer DB connection to first call
try:
    from app.utils.basicinfo_db import get_stock_basic_db as _get_basic_db
    _basic_db_ref = [None]
    _EXCLUDE_BASIC = {"close", "ensure_table", "_get_mgr", "_get_pool"}

    def _get_basic():
        if _basic_db_ref[0] is None:
            _basic_db_ref[0] = _get_basic_db()
        return _basic_db_ref[0]

    # Discover from class, not instance
    from app.utils.basicinfo_db import StockBasicDB as _StockBasicDB
    _basic_names = [n for n in dir(_StockBasicDB)
                    if not n.startswith("_") and n not in _EXCLUDE_BASIC
                    and callable(getattr(_StockBasicDB, n, None))]

    def _make_basic_wrapper(name):
        def wrapper(*args, **kwargs):
            return getattr(_get_basic(), name)(*args, **kwargs)
        wrapper.__name__ = name
        wrapper.__doc__ = getattr(_StockBasicDB, name).__doc__ or f"StockBasicDB.{name}"
        return wrapper

    for _name in _basic_names:
        globals()[_name] = _make_basic_wrapper(_name)

    _BASIC_DB_AVAILABLE = True
except Exception as _db_err:
    _BASIC_DB_AVAILABLE = False

# ── 5. app/ — scan entire backend, blacklist sensitive directories ──
# Prefix by package path to avoid name collisions: e.g. util_xxx, svc_xxx, route_xxx
_PKG_BLACKLIST = {
    # Sensitive: auth, credentials, payment, security
    "auth", "credential_crypto", "security_service",
    "billing_service", "email_service", "oauth_service",
    "usdt_payment_service", "user_service",
    # Trading execution: too dangerous for sandbox scripts
    "exchange_execution", "trading_executor", "pending_order_worker",
    "ibkr_trading", "mt5_trading", "live_trading",
    # Internal infra: logger, config, raw DB drivers
    "logger", "config_loader", "db_postgres", "db", "safe_exec",
    "cache",
    # Agent internals: avoid self-reference loops
    "agent",
}
try:
    import app as _app_pkg
    import pkgutil as _pkgutil
    _app_discovered = 0
    _app_mod_count = 0

    def _scan_app_modules(pkg, path_prefix="", depth=0):
        """Recursively scan app/, yielding (dotted_path, module) for all leaf modules."""
        global _app_mod_count
        if depth > 4:
            return
        for _importer, _mod_name, _is_pkg in _pkgutil.iter_modules(pkg.__path__):
            if _mod_name.startswith("_"):
                continue
            full_path = f"{path_prefix}.{_mod_name}" if path_prefix else _mod_name
            # Blacklist check: skip entire directory trees
            if _mod_name in _PKG_BLACKLIST:
                continue
            try:
                _mod = _importer.find_module(_mod_name).load_module(_mod_name)
            except Exception:
                continue
            _app_mod_count += 1
            yield full_path, _mod
            if _is_pkg and hasattr(_mod, "__path__"):
                yield from _scan_app_modules(_mod, path_prefix=full_path, depth=depth+1)

    # Function/class name blacklist — sensitive operations within otherwise safe modules
    _FN_BLACKLIST_PATTERNS = (
        "password", "passwd", "secret", "token", "credential", "api_key",
        "encrypt", "decrypt", "hash", "sign", "verify", "auth",
        "login", "logout", "send_email", "send_sms", "send_notification",
        "execute_trade", "place_order", "cancel_order", "withdraw",
        "delete_user", "drop_table", "truncate", "grant", "revoke",
        "shell", "exec_cmd", "run_cmd", "popen", "system_call",
    )

    def _is_fn_safe(name: str) -> bool:
        """Check if a function/class name is safe to expose."""
        lower = name.lower()
        return not any(p in lower for p in _FN_BLACKLIST_PATTERNS)

    for _full_path, _mod in _scan_app_modules(_app_pkg):
        # Derive prefix from path: e.g. "utils.cn_stock_info" → "util_", "services.fast_analysis" → "svc_"
        _parts = _full_path.split(".")
        if len(_parts) >= 2:
            _top = _parts[0]
            _prefix_map = {"utils": "util", "services": "svc", "routes": "route",
                           "data_sources": "ds", "market_cn": "mkt", "backtest": "bt",
                           "interfaces": "iface"}
            _prefix = _prefix_map.get(_top, _top) + "_"
        else:
            _prefix = ""

        for _name, _obj in _inspect.getmembers(_mod, _inspect.isfunction):
            if _name.startswith("_") or not _is_fn_safe(_name):
                continue
            globals()[f"{_prefix}{_name}"] = _obj
            _app_discovered += 1
        for _name, _obj in _inspect.getmembers(_mod, _inspect.isclass):
            if _name.startswith("_") or _obj.__module__ != _mod.__name__ or not _is_fn_safe(_name):
                continue
            globals()[f"{_prefix}{_name}"] = _obj
            _app_discovered += 1

    _APP_SCAN_AVAILABLE = True
    _APP_SCAN_COUNT = _app_discovered
    _APP_MOD_COUNT = _app_mod_count
except Exception as _app_err:
    _APP_SCAN_AVAILABLE = False
    _APP_SCAN_COUNT = 0
    _APP_MOD_COUNT = 0

# ── 7. market_cn cards: dashboard cards (overview, dragon_tiger, hot_list, etc.) ──
try:
    from app.market_cn.cards import _base as _cards_base
    _registered_cards = _cards_base.get_all()
    for _card_name, (_meta, _fetch_fn) in _registered_cards.items():
        _safe_name = "card_" + _card_name.replace("/", "_").replace("-", "_")
        globals()[_safe_name] = _fetch_fn
    _CARDS_AVAILABLE = True
except Exception as _card_err:
    _CARDS_AVAILABLE = False

# ── Summary ──
_DATA_INJECTION_STATUS = {
    "DataSourceFactory": _DS_AVAILABLE if "_DS_AVAILABLE" in dir() else False,
    "Index": _INDEX_AVAILABLE if "_INDEX_AVAILABLE" in dir() else False,
    "china_market": _CM_AVAILABLE if "_CM_AVAILABLE" in dir() else False,
    "StockBasicDB": _BASIC_DB_AVAILABLE if "_BASIC_DB_AVAILABLE" in dir() else False,
    "app/全量扫描": _APP_SCAN_AVAILABLE if "_APP_SCAN_AVAILABLE" in dir() else False,
    "Cards": _CARDS_AVAILABLE if "_CARDS_AVAILABLE" in dir() else False,
}
# Print summary
_mod_cnt = _APP_MOD_COUNT if "_APP_MOD_COUNT" in dir() else 0
_sym_cnt = _APP_SCAN_COUNT if "_APP_SCAN_COUNT" in dir() else 0
print(f"\\n📦 数据注入完成: {_mod_cnt} 个模块, {_sym_cnt} 个符号")
_prefix_labels = {"util_": "utils", "svc_": "services", "route_": "routes",
                   "ds_": "data_sources", "mkt_": "market_cn", "bt_": "backtest",
                   "iface_": "interfaces"}
for _pfx, _label in _prefix_labels.items():
    _cnt = sum(1 for k in globals() if k.startswith(_pfx))
    if _cnt:
        print(f"  {_pfx:<8} → {_label} ({_cnt})")
'''

# ── Tool implementations ───────────────────────────────────────

@tool(
    description="在工作区目录中执行 shell 命令（支持流式输出）。命令的 cwd 自动设为当前会话的工作区。可用环境变量 WORKSPACE 获取工作区绝对路径。适用场景：pip install、python、文件操作、数据处理、调用外部工具。",
    category="工作区",
    layer="支撑层",
    domain=["coding"],
)
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
    from app.agent.workspace import get_workspace
    from app.agent.tool_context import get_session_id, emit_progress

    timeout = min(max(timeout, 1), 600)

    safety = _check_shell_safety(command)
    if safety:
        return {"output": "", "exit_code": -1, "duration": 0, **safety}

    ws = get_workspace(get_session_id() or "default")
    emit = lambda ev: emit_progress(ev)

    return _stream_exec(command, str(ws.session_dir), timeout, emit, "shell_exec")

@tool(
    description="保存 Python 脚本到工作区，支持自动版本管理。",
    category="工作区",
    layer="支撑层",
    domain=["coding"],
)
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
    from app.agent.workspace import get_workspace
    from app.agent.tool_context import get_session_id
    ws = get_workspace(get_session_id() or "default")
    return ws.save_script(name, code, description)

@tool(
    description="从工作区加载 Python 脚本。",
    category="工作区",
    layer="支撑层",
    domain=["coding"],
)
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
    from app.agent.workspace import get_workspace
    from app.agent.tool_context import get_session_id
    ws = get_workspace(get_session_id() or "default")
    return ws.load_script(name, version)

@tool(
    description="列出工作区中的所有脚本和文件。",
    category="工作区",
    layer="支撑层",
    domain=["coding"],
)
def workspace_list() -> Dict[str, Any]:
    """List all files in the workspace (scripts, data, outputs).

    Returns:
        {"scripts": [...], "data_files": [...], "output_files": [...]}
    """
    from app.agent.workspace import get_workspace
    from app.agent.tool_context import get_session_id
    ws = get_workspace(get_session_id() or "default")
    return ws.info()

@tool(
    description="写入文件到工作区。用于创建新文件、保存脚本、写入数据等。修改代码时先用 workspace_read_file 读取，再用此工具写回。",
    category="工作区",
    layer="支撑层",
    domain=["coding"],
)
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
    from app.agent.workspace import get_workspace
    from app.agent.tool_context import get_session_id
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

@tool(
    description="读取工作区中的文件内容。用于查看代码、读取数据、分析文件等。修改代码前先用此工具读取当前内容。",
    category="工作区",
    layer="支撑层",
    domain=["coding"],
)
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
    from app.agent.workspace import get_workspace
    from app.agent.tool_context import get_session_id
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

@tool(
    description="精确编辑工作区文件：支持精确文本替换和正则表达式替换。比全量重写更安全高效。先用 workspace_read_file 读取内容，找到要修改的部分，再用此工具精确替换。",
    category="工作区",
    layer="支撑层",
    domain=["coding"],
)
def workspace_edit_file(
    path: str,
    find: str = "",
    replace: str = "",
    regex: bool = False,
    count: int = 0,
    line_range: str = "",
) -> Dict[str, Any]:
    """Edit a file in the workspace with find/replace or regex replace.

    Supports two modes:
    1. Exact match: find="old text", replace="new text"
    2. Regex: find=r"pattern", replace=r"replacement", regex=True

    Args:
        path: Relative path (e.g., "scripts/strategy.py")
        find: Text or regex pattern to find
        replace: Replacement text
        regex: If True, treat find as regex pattern (supports groups like \\1)
        count: Max replacements (0 = all). Use 1 to replace only the first match.
        line_range: Optional line range "start-end" (1-indexed, e.g., "10-20") to limit search scope.

    Returns:
        {"path": str, "replacements": int, "original_size": int, "new_size": int}
    """
    import re as _re
    from app.agent.workspace import get_workspace
    from app.agent.tool_context import get_session_id
    from pathlib import Path

    ws = get_workspace(get_session_id() or "default")
    safe = path.lstrip("/").replace("..", "")
    full_path = ws.session_dir / safe

    try:
        full_path.resolve().relative_to(ws.session_dir.resolve())
    except ValueError:
        return {"error": f"路径越界: {path}"}

    if not full_path.exists():
        return {"error": f"文件不存在: {path}"}

    content = full_path.read_text(encoding="utf-8")
    original_size = len(content)

    # Line range filtering: only edit within specified lines
    if line_range:
        try:
            parts = line_range.split("-")
            start_line = int(parts[0])
            end_line = int(parts[1]) if len(parts) > 1 else start_line
            lines = content.split("\n")
            if start_line < 1 or end_line > len(lines):
                return {"error": f"行范围越界: 文件共 {len(lines)} 行"}
            # Edit only the specified range
            section = "\n".join(lines[start_line - 1 : end_line])
            if regex:
                new_section, n = _re.subn(find, replace, section, count=count if count else 0)
            else:
                n = section.count(find) if not count else min(section.count(find), count)
                new_section = section.replace(find, replace, count if count else -1)
            if n > 0:
                lines[start_line - 1 : end_line] = new_section.split("\n")
                content = "\n".join(lines)
        except (ValueError, _re.error) as e:
            return {"error": f"行范围或正则表达式错误: {e}"}
    else:
        # Full file edit
        if regex:
            try:
                content, n = _re.subn(find, replace, content, count=count if count else 0)
            except _re.error as e:
                return {"error": f"正则表达式错误: {e}"}
        else:
            n = content.count(find) if not count else min(content.count(find), count)
            content = content.replace(find, replace, count if count else -1)

    if n == 0:
        return {"error": "未找到匹配内容", "find": find[:200], "replacements": 0}

    # Auto-snapshot before edit
    try:
        from app.agent.tools.iteration_tools import auto_snapshot_before_edit
        auto_snapshot_before_edit(f"before edit {safe}")
    except Exception:
        pass

    full_path.write_text(content, encoding="utf-8")
    return {
        "path": str(full_path),
        "replacements": n,
        "original_size": original_size,
        "new_size": len(content),
        "message": f"已完成 {n} 处替换",
    }

@tool(
    description="对工作区中的 Python 代码进行静态审查：语法检查、AST分析、常见问题检测。在执行代码前调用可提前发现错误。",
    category="工作区",
    layer="支撑层",
    domain=["coding"],
)
def workspace_code_review(
    path: str = "",
    code: str = "",
) -> Dict[str, Any]:
    """Static analysis of Python code — syntax check, AST analysis, common pitfalls.

    Use before exec_script to catch errors early. Checks:
    1. Syntax validity (compile)
    2. AST-level issues (unused imports, bare except, mutable defaults)
    3. Common pitfalls (print vs return, == vs is, f-string issues)

    Args:
        path: Relative path to a file in workspace (e.g., "scripts/strategy.py")
        code: Or pass code directly

    Returns:
        {"valid": bool, "errors": [...], "warnings": [...], "info": {...}}
    """
    import ast
    import sys
    from app.agent.workspace import get_workspace
    from app.agent.tool_context import get_session_id
    from pathlib import Path

    # Load code
    if path and not code:
        ws = get_workspace(get_session_id() or "default")
        safe = path.lstrip("/").replace("..", "")
        full_path = ws.session_dir / safe
        if not full_path.exists():
            return {"error": f"文件不存在: {path}"}
        code = full_path.read_text(encoding="utf-8")

    if not code:
        return {"error": "没有可审查的代码"}

    errors = []
    warnings = []
    info = {}

    # 1. Syntax check
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return {
            "valid": False,
            "errors": [{"type": "SyntaxError", "line": e.lineno, "message": str(e.msg), "text": (e.text or "").strip()}],
            "warnings": [],
            "info": {"lines": code.count(chr(10)) + 1},
        }

    # 2. AST analysis
    info["lines"] = code.count(chr(10)) + 1
    info["statements"] = len([n for n in ast.walk(tree) if isinstance(n, ast.stmt)])
    info["functions"] = len([n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))])
    info["classes"] = len([n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)])

    # Collect imports
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)
    info["imports"] = imports

    # 3. Common issue detection
    for node in ast.walk(tree):
        # Bare except
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            warnings.append({
                "type": "bare_except",
                "line": node.lineno,
                "message": "裸 except: 捕获所有异常（包括 KeyboardInterrupt）。建议指定具体异常类型。",
            })

        # Mutable default argument
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for default in node.args.defaults + node.args.kw_defaults:
                if default and isinstance(default, (ast.List, ast.Dict, ast.Set)):
                    warnings.append({
                        "type": "mutable_default",
                        "line": node.lineno,
                        "message": f"函数 '{node.name}' 使用了可变默认参数。建议用 None 代替。",
                    })

        # Comparison with literals using == (should be is for None/True/False)
        if isinstance(node, ast.Compare):
            for op, comparator in zip(node.ops, node.comparators):
                if isinstance(comparator, ast.Constant) and comparator.value is None:
                    if isinstance(op, ast.Eq):
                        warnings.append({
                            "type": "none_comparison",
                            "line": node.lineno,
                            "message": "用 == None 比较。建议用 is None。",
                        })

        # Star import
        if isinstance(node, ast.ImportFrom) and node.names and any(a.name == "*" for a in node.names):
            warnings.append({
                "type": "star_import",
                "line": node.lineno,
                "message": f"from {node.module} import * — 污染命名空间，建议显式导入。",
            })

    # 4. Variable usage analysis (simple unused detection)
    assigned = set()
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned.add((target.id, node.lineno))
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used.add(node.id)

    for name, lineno in assigned:
        if name not in used and not name.startswith("_"):
            warnings.append({
                "type": "unused_variable",
                "line": lineno,
                "message": f"变量 '{name}' 赋值后未使用。",
            })

    # 5. Check for print statements (might want return instead)
    prints = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Name) and n.func.id == "print"]
    if prints:
        info["print_count"] = len(prints)
        if len(prints) > 10:
            warnings.append({
                "type": "excessive_print",
                "line": prints[0].lineno,
                "message": f"代码中有 {len(prints)} 个 print 语句。考虑用 logging 或返回值代替。",
            })

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "info": info,
        "summary": f"✅ 语法正确 | {info['lines']}行 | {info['functions']}个函数 | {info['classes']}个类 | {len(warnings)}个警告",
    }

@tool(
    description="在工作区中执行 Python 脚本，支持数据源注入（get_kline/get_ticker 等自动可用）。可加载已保存脚本（传 name）或直接执行代码（传 code）。支持流式输出和最长 600 秒超时。",
    category="工作区",
    layer="支撑层",
    domain=["coding"],
)
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
    - Auto-injected: get_kline(), get_ticker(), get_stock_info() via DataSourceFactory
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
    from app.agent.workspace import get_workspace
    from app.agent.tool_context import get_session_id, emit_progress

    timeout = min(max(timeout, 5), 600)
    ws = get_workspace(get_session_id() or "default")

    # Load script if name given
    if name and not code:
        loaded = ws.load_script(name)
        if loaded.get("error"):
            return {"output": "", "result": None, "error": loaded["error"],
                    "recovery": _recovery_suggestion("exec_script", loaded["error"])}
        code = loaded["code"]

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

@tool(
    description="后台执行脚本，立即返回 task_id。用 poll_task 查询结果。适合长时间任务（大数据处理、全市场扫描等）。",
    category="工作区",
    layer="支撑层",
    domain=["coding"],
)
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
    from app.agent.workspace import get_workspace
    from app.agent.tool_context import get_session_id

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

@tool(
    description="查询后台任务状态和结果。",
    category="工作区",
    layer="支撑层",
    domain=["coding"],
)
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

    # Worker needs os for chdir — keep it available for user code too
    import os as _os
    _os.chdir(workspace_dir)

    try:
        import pandas as pd
    except ImportError:
        pd = None
    try:
        import numpy as np
    except ImportError:
        np = None

    # Build safe builtins — block dangerous introspection, allow most operations
    import builtins as _b
    safe_builtins = {}
    blocked = {
        "breakpoint",   # 调试器断点
        "exit", "quit", # 退出进程
    }
    for name in dir(_b):
        if name.startswith("_") and name != "__import__":
            continue
        if name in blocked:
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
        # Filter out injected helpers, keep only user-created variables
        _injected = {"pd", "np", "data", "__builtins__",
                     "WORKSPACE", "SCRIPTS_DIR", "DATA_DIR", "OUTPUT_DIR", "Path"}
        user_vars = [
            k for k in local_ns
            if not k.startswith("_") and k not in _injected
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

    Strategy: open sandbox — allow most modules, only block destructive ones.
    Destructive operations (rm, delete, overwrite system files) are blocked at
    the function level, not at import level. This lets agent code use os.path,
    subprocess, requests, etc. freely for read/analysis tasks.
    """
    import importlib

    # Only block modules that are inherently dangerous or irrelevant to analysis
    BLOCKED_MODULES = {
        "ctypes",          # 直接内存操作
        "signal",          # 进程信号操控
        "importlib",       # 动态加载绕过
        "code", "codeop", "compileall",  # 交互式解释器
        "shelve", "dbm", "sqlite3",      # 本地数据库写入（读可以用）
        "pickle",          # 反序列化攻击
        "marshal",         # 同上
        "winreg",          # Windows 注册表
        "msvcrt",          # Windows 运行时
        "termios", "tty",  # 终端控制
        "readline",        # 终端输入操控
    }

    # Destructive function patterns — if imported via "from X import Y", block Y
    BLOCKED_FUNCTIONS = {
        "os": {"remove", "unlink", "rmdir", "removedirs", "rename", "renames",
               "replace", "chmod", "chown", "chroot", "system", "popen",
               "exec", "execve", "execvp", "execvpe", "spawn", "kill",
               "killpg", "_exit"},
        "shutil": {"rmtree", "move", "copytree"},  # copy/copy2 are OK
        "subprocess": {"Popen"},  # run/call/check_output are OK (controlled by shell_exec anyway)
    }

    root = name.split(".")[0]
    if root in BLOCKED_MODULES:
        raise ImportError(f"Import of '{name}' is blocked for security reasons.")

    # Check for "from os import remove" style dangerous imports
    if root in BLOCKED_FUNCTIONS and args and isinstance(args[0], (list, tuple)):
        blocked = BLOCKED_FUNCTIONS[root]
        for fn in args[0]:
            if fn in blocked:
                raise ImportError(
                    f"Import of '{root}.{fn}' is blocked (destructive operation). "
                    f"Use pathlib.Path for safe file operations."
                )

    return importlib.import_module(name, *args, **kwargs)

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

# Legacy list — kept for backward compat during migration; safe to remove later.