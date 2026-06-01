# -*- coding: utf-8 -*-
"""
Python exec tool — execute arbitrary Python code in a sandboxed environment.

v2: real timeout via multiprocessing, tightened sandbox, no filesystem access.
"""
from __future__ import annotations

import io
import json
import logging
import multiprocessing
import sys
import traceback
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Allowed imports (whitelist) ───────────────────────────────
# Modules the agent is allowed to import. Prevents dangerous operations.
ALLOWED_MODULES = {
    # 数据处理
    "pandas", "numpy", "scipy", "statistics", "math", "decimal", "fractions",
    # 技术分析
    "ta", "talib",
    # 机器学习
    "sklearn", "sklearn.linear_model", "sklearn.ensemble", "sklearn.preprocessing",
    "sklearn.metrics", "sklearn.model_selection", "sklearn.cluster",
    # 可视化
    "matplotlib", "matplotlib.pyplot", "seaborn",
    # 文本/数据（安全子集）
    "json", "csv", "re", "collections", "itertools", "functools", "operator",
    "string", "textwrap", "unicodedata",
    # 日期时间
    "datetime", "time", "calendar",
    # 其他安全模块
    "copy", "pprint", "enum", "dataclasses", "typing",
    "hashlib", "base64", "uuid",
}

# ── Explicitly blocked modules ────────────────────────────────
BLOCKED_MODULES = {
    "os", "sys", "subprocess", "shutil", "socket", "http", "urllib",
    "requests", "ctypes", "signal", "threading", "multiprocessing",
    "importlib", "code", "codeop", "compileall",
    "pathlib", "glob", "fnmatch", "tempfile", "shelve", "dbm",
    "sqlite3", "pickle", "shelve",
}

# ── Blocked builtins ─────────────────────────────────────────
_BLOCKED_BUILTINS = {
    "open", "exec", "eval", "compile", "__import__",
    "breakpoint", "input", "exit", "quit",
    "globals", "locals", "vars", "dir",
    "memoryview", "bytearray", "super",
    "getattr", "setattr", "delattr",  # 防止通过 getattr 绕过
}

# ── Safe builtins ────────────────────────────────────────────

def _build_safe_builtins() -> Dict[str, Any]:
    """Build a restricted set of builtins."""
    import builtins as _b
    safe = {}
    for name in dir(_b):
        if name.startswith("_"):
            continue
        if name in _BLOCKED_BUILTINS:
            continue
        obj = getattr(_b, name, None)
        if obj is not None:
            safe[name] = obj

    # Overrides
    safe["__import__"] = _safe_import
    safe["True"] = True
    safe["False"] = False
    safe["None"] = None
    return safe


def _safe_import(name: str, *args, **kwargs) -> Any:
    """Restricted import that only allows whitelisted modules."""
    # 先检查黑名单
    root_module = name.split(".")[0]
    if root_module in BLOCKED_MODULES:
        raise ImportError(
            f"Import of '{name}' is blocked for security reasons."
        )

    # 允许子模块如果父模块在白名单中
    parts = name.split(".")
    for i in range(len(parts), 0, -1):
        prefix = ".".join(parts[:i])
        if prefix in ALLOWED_MODULES:
            return __import__(name, *args, **kwargs)

    raise ImportError(
        f"Import of '{name}' is not allowed. "
        f"Allowed modules: {', '.join(sorted(ALLOWED_MODULES))}"
    )


# ── Serialization ────────────────────────────────────────────

def _serialize(obj: Any, _depth: int = 0) -> Any:
    """Safely serialize result for JSON transport."""
    if _depth > 10:
        return str(obj)

    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj

    if isinstance(obj, (list, tuple)):
        return [_serialize(x, _depth + 1) for x in obj[:500]]  # cap at 500 items

    if isinstance(obj, dict):
        return {str(k): _serialize(v, _depth + 1) for k, v in list(obj.items())[:200]}

    # pandas DataFrame
    try:
        import pandas as pd
        if isinstance(obj, pd.DataFrame):
            if len(obj) > 200:
                return {
                    "_type": "DataFrame",
                    "shape": list(obj.shape),
                    "columns": list(obj.columns),
                    "head": _serialize(obj.head(20).to_dict(orient="records")),
                    "tail": _serialize(obj.tail(5).to_dict(orient="records")),
                    "dtypes": {col: str(dtype) for col, dtype in obj.dtypes.items()},
                }
            return {"_type": "DataFrame", "data": obj.to_dict(orient="records")}
        if isinstance(obj, pd.Series):
            if len(obj) > 200:
                return {"_type": "Series", "length": len(obj), "head": _serialize(obj.head(20).to_dict())}
            return {"_type": "Series", "data": obj.to_dict()}
    except ImportError:
        pass

    # numpy arrays
    try:
        import numpy as np
        if isinstance(obj, np.ndarray):
            if obj.size > 500:
                return {"_type": "ndarray", "shape": list(obj.shape), "sample": _serialize(obj.flat[:20].tolist())}
            return {"_type": "ndarray", "data": obj.tolist()}
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
    except ImportError:
        pass

    # matplotlib Figure — skip (not serializable)
    try:
        import matplotlib.figure
        if isinstance(obj, matplotlib.figure.Figure):
            return {"_type": "Figure", "info": "Figure created (not serializable, use savefig)"}
    except ImportError:
        pass

    return str(obj)[:2000]


# ── Worker function (runs in subprocess) ─────────────────────

def _exec_worker(code: str, context_data: Any, result_queue: multiprocessing.Queue):
    """Execute code in a subprocess. Puts result dict into result_queue."""
    import io as _io
    import sys as _sys

    try:
        import pandas as pd
    except ImportError:
        pd = None
    try:
        import numpy as np
    except ImportError:
        np = None

    safe_builtins = _build_safe_builtins()
    global_ns = {"__builtins__": safe_builtins}
    local_ns: Dict[str, Any] = {}

    if pd is not None:
        local_ns["pd"] = pd
    if np is not None:
        local_ns["np"] = np
    local_ns["data"] = context_data

    old_stdout = _sys.stdout
    old_stderr = _sys.stderr
    captured_out = _io.StringIO()
    captured_err = _io.StringIO()

    try:
        _sys.stdout = captured_out
        _sys.stderr = captured_err

        compiled = compile(code, "<agent_code>", "exec")
        exec(compiled, global_ns, local_ns)

        result = local_ns.get("result")
        user_vars = [
            k for k in local_ns
            if not k.startswith("_") and k not in ("pd", "np", "data", "__builtins__")
        ]

        result_queue.put({
            "output": captured_out.getvalue()[:5000],
            "result": _serialize(result),
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
        result_queue.put({
            "output": captured_out.getvalue()[:2000],
            "result": None,
            "error": traceback.format_exc()[:3000],
        })
    finally:
        _sys.stdout = old_stdout
        _sys.stderr = old_stderr


# ── Main tool function ───────────────────────────────────────

def python_exec(
    code: str,
    context: str = "",
    timeout: int = 30,
) -> Dict[str, Any]:
    """
    Execute Python code in a sandboxed environment for data analysis.

    Available by default:
        - pd: pandas
        - np: numpy
        - data: parsed JSON context (if provided)

    Any installed Python package can be imported (whitelist-controlled).

    Set `result` variable to return structured data, or use `print()` for text output.

    Args:
        code: Python code to execute
        context: JSON string injected as `data` variable
        timeout: Execution timeout in seconds (default 30, max 60)

    Returns:
        {"output": str, "result": any, "error": str|None, "variables": list}
    """
    timeout = min(max(timeout, 5), 60)  # clamp 5-60s

    # Parse context data
    context_data = None
    if context:
        try:
            context_data = json.loads(context)
        except (json.JSONDecodeError, TypeError):
            context_data = context

    # Run in subprocess with timeout
    result_queue: multiprocessing.Queue = multiprocessing.Queue()
    proc = multiprocessing.Process(
        target=_exec_worker,
        args=(code, context_data, result_queue),
        daemon=True,
    )
    proc.start()
    proc.join(timeout=timeout)

    if proc.is_alive():
        proc.terminate()
        proc.join(timeout=2)
        if proc.is_alive():
            proc.kill()
        return {
            "output": "",
            "result": None,
            "error": f"代码执行超时（{timeout}秒限制）。请优化代码或减少数据量。",
        }

    if not result_queue.empty():
        return result_queue.get()

    return {
        "output": "",
        "result": None,
        "error": "执行异常：未产生结果（可能内存溢出或被系统终止）",
    }


# ── Tool spec for registry ───────────────────────────────────

PYTHON_EXEC_TOOL = {
    "fn": python_exec,
    "name": "python_exec",
    "description": (
        "执行 Python 代码进行自定义数据分析。"
        "可以使用 pandas(pd)、numpy(np) 以及白名单内的 Python 库。"
        "代码中可通过 `data` 变量接收上下文数据。"
        "将最终结果赋值给 `result` 变量返回，或用 `print()` 输出文本。"
        "\n\n"
        "适用场景：自定义回测、因子分析、统计计算、数据可视化、"
        "机器学习建模、任意复杂的数据处理逻辑。"
        "\n\n"
        "限制：执行时间 5-60 秒（默认30秒），禁止文件系统/网络/进程操作。"
        "\n\n"
        "示例：\n"
        "```python\n"
        "import pandas as pd\n"
        "df = pd.DataFrame(data)\n"
        "returns = df['close'].pct_change().dropna()\n"
        "result = {\n"
        "    'sharpe': returns.mean() / returns.std() * 252**0.5,\n"
        "    'max_dd': ((df['close'].cummax() - df['close']) / df['close'].cummax()).max(),\n"
        "}\n"
        "```"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "要执行的 Python 代码",
            },
            "context": {
                "type": "string",
                "description": "传入代码的上下文数据（JSON 字符串），可通过 data 变量访问",
            },
            "timeout": {
                "type": "integer",
                "description": "执行超时秒数，默认30，最大60",
                "default": 30,
            },
        },
        "required": ["code"],
    },
}
