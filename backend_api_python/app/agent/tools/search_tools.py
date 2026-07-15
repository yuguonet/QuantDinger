# -*- coding: utf-8 -*-
"""
search_tools — 工具发现与加载

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


def _get_scan_dirs(domain: str) -> list[tuple[Path, str]]:
    """根据 domain 参数确定扫描目录。"""
    if domain == "all":
        dirs = [(_TOOLS_DIR, "tools")]
        for item in sorted(_TOOLS_DIR.iterdir()):
            if item.is_dir() and item.name != "__pycache__" and not item.name.startswith("."):
                dirs.append((item, f"tools.{item.name}"))
        return dirs
    elif domain:
        dirs = [(_TOOLS_DIR, "tools")]
        domain_dir = _TOOLS_DIR / domain
        if domain_dir.is_dir():
            dirs.append((domain_dir, f"tools.{domain}"))
        return dirs
    else:
        return [(_TOOLS_DIR, "tools")]


def search_tools(query: str = "", domain: str = "") -> str:
    """
    搜索/加载可用工具。

    Args:
        query: 搜索关键词（如 '资金流', 'K线', '选股'）。为空时返回所有工具。
        domain: 领域名称（如 'finance', 'crypto'）。
                - 空/不指定：只搜索 tools/ 根目录（通用工具）
                - 指定领域：搜索 tools/{domain}/ + tools/ 根目录
                - "all"：搜索所有领域所有目录

    Returns:
        匹配的工具列表（格式化字符串）

    示例：
        search_tools("资金")              # 在通用工具中搜索
        search_tools("K线", "finance")    # 在 finance 领域 + 通用工具中搜索
        search_tools("", "all")           # 列出所有领域所有工具
        search_tools()                    # 列出所有通用工具
    """
    query_lower = query.lower() if query else ""
    all_tools = []

    for scan_dir, module_prefix in _get_scan_dirs(domain):
        for py_file in sorted(scan_dir.glob("*.py")):
            if py_file.stem in _SKIP_FILES:
                continue
            try:
                mod = importlib.import_module(f"{module_prefix}.{py_file.stem}")
            except Exception:
                continue

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
                domain_name = scan_dir.name if scan_dir != _TOOLS_DIR else "common"
                all_tools.append({
                    "name": attr_name,
                    "module": py_file.stem,
                    "desc": desc,
                    "domain": domain_name,
                })

    # 按关键词过滤
    if query_lower:
        matched = [t for t in all_tools
                   if query_lower in t["name"].lower() or query_lower in t["desc"].lower()]
    else:
        matched = all_tools

    # 无匹配时返回全部（兜底）
    if not matched:
        if domain:
            # 指定领域或 all：返回根目录所有通用工具
            fallback = [t for t in all_tools if t["domain"] == "common"]
            if not fallback:
                fallback = all_tools
            title = f"未找到匹配 '{query}' 的工具，以下是通用工具："
        else:
            fallback = [t for t in all_tools if t["domain"] == "common"]
            title = f"未找到匹配 '{query}' 的工具，以下是通用工具："
        matched = fallback

    # 格式化输出
    title = f"找到 {len(matched)} 个工具：" if query else f"可用工具 ({len(matched)})："
    lines = [title]
    for t in matched:
        lines.append(f"  - {t['name']} ({t['module']}): {t['desc']}")
    return "\n".join(lines)
