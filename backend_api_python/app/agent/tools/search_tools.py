# -*- coding: utf-8 -*-
"""
search_tools — 工具发现函数

支持领域化搜索：
  - tools/根目录：通用工具
  - tools/领域名/：领域专用工具
  - domain="all"：搜索所有领域
"""
import inspect
import importlib
from pathlib import Path


# 跳过的文件（框架文件，非工具）
_SKIP_FILES = {"__init__", "base", "registry", "em_utils", "pagination",
               "screener_config", "mcp_bridge", "cache_tools", "search_tools"}

# tools 目录路径
_TOOLS_DIR = Path(__file__).resolve().parent


def _scan_module(py_file: Path, module_prefix: str = "tools") -> list[dict]:
    """扫描单个 Python 模块，提取公开函数。"""
    module_name = py_file.stem
    results = []
    try:
        mod = importlib.import_module(f"{module_prefix}.{module_name}")
    except Exception:
        return results

    for attr_name in dir(mod):
        if attr_name.startswith("_"):
            continue
        func = getattr(mod, attr_name)
        if not callable(func) or inspect.isclass(func):
            continue
        if getattr(func, "__module__", "") != mod.__name__:
            continue

        doc = (func.__doc__ or "").strip()
        desc = doc.split("\n")[0][:120] if doc else f"{attr_name}()"
        results.append({
            "name": attr_name,
            "module": module_name,
            "desc": desc,
        })
    return results


def _format_tools(tools: list[dict], title: str = "") -> str:
    """格式化工具列表为字符串。"""
    if not tools:
        return ""
    lines = [title] if title else []
    for t in tools:
        lines.append(f"  - {t['name']} ({t['module']}): {t['desc']}")
    return "\n".join(lines)


def search_tools(query: str = "", domain: str = "") -> str:
    """
    按关键词搜索可用工具。支持领域化搜索。

    Args:
        query: 搜索关键词（如 '资金流', 'K线', '选股'）。为空时返回所有工具。
        domain: 领域名称（如 'finance', 'crypto'）。
                - 空/不指定：只搜索 tools/ 根目录（通用工具）
                - 指定领域：搜索 tools/{domain}/ + tools/ 根目录
                - "all"：搜索所有领域所有目录

    Returns:
        匹配的工具列表

    示例：
        search_tools("资金")              # 在通用工具中搜索
        search_tools("K线", "finance")    # 在 finance 领域 + 通用工具中搜索
        search_tools("", "all")           # 列出所有领域所有工具
        search_tools("选股")              # 通用工具中搜索，没找到返回全部通用工具
    """
    query_lower = query.lower() if query else ""
    all_tools = []

    # ── 确定搜索范围 ──
    if domain == "all":
        # 搜索所有目录（根目录 + 所有子目录）
        scan_dirs = []
        for item in sorted(_TOOLS_DIR.iterdir()):
            if item.is_dir() and item.name != "__pycache__" and not item.name.startswith("."):
                scan_dirs.append((item, f"tools.{item.name}"))
        # 根目录也加上
        scan_dirs.insert(0, (_TOOLS_DIR, "tools"))
    elif domain:
        # 指定领域：搜索 tools/{domain}/ + tools/ 根目录
        domain_dir = _TOOLS_DIR / domain
        scan_dirs = [(_TOOLS_DIR, "tools")]
        if domain_dir.is_dir():
            scan_dirs.append((domain_dir, f"tools.{domain}"))
    else:
        # 默认：只搜索 tools/ 根目录
        scan_dirs = [(_TOOLS_DIR, "tools")]

    # ── 扫描并收集工具 ──
    for scan_dir, module_prefix in scan_dirs:
        for py_file in sorted(scan_dir.glob("*.py")):
            if py_file.stem in _SKIP_FILES:
                continue
            tools = _scan_module(py_file, module_prefix)
            # 添加领域信息
            for t in tools:
                if scan_dir != _TOOLS_DIR:
                    t["domain"] = scan_dir.name
                else:
                    t["domain"] = "common"
            all_tools.extend(tools)

    # ── 按关键词过滤 ──
    if query_lower:
        matched = [t for t in all_tools
                   if query_lower in t["name"].lower() or query_lower in t["desc"].lower()]
    else:
        matched = all_tools

    # ── 无匹配时返回全部（兜底） ──
    if not matched:
        if domain == "all":
            fallback = all_tools
            title = f"未找到匹配 '{query}' 的工具，以下是所有领域所有工具："
        elif domain:
            fallback = [t for t in all_tools if t["domain"] != "common"]
            if not fallback:
                fallback = [t for t in all_tools if t["domain"] == "common"]
            title = f"未找到匹配 '{query}' 的工具，以下是 {domain} 领域可用工具："
        else:
            fallback = [t for t in all_tools if t["domain"] == "common"]
            title = f"未找到匹配 '{query}' 的工具，以下是通用工具："
        return _format_tools(fallback, title)

    # ── 有匹配时返回结果 ──
    title = f"找到 {len(matched)} 个匹配工具："
    return _format_tools(matched, title)
