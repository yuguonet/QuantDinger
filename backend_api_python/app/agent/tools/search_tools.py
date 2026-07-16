# -*- coding: utf-8 -*-
"""
search_tools — 工具搜索

按关键词搜索可用工具，基于 list_tools 的扫描能力。
"""
from tools.list_tools import _scan_tools


def search_tools(query: str, domain: str = "") -> str:
    """
    按关键词搜索可用工具。

    Args:
        query: 搜索关键词（如 '资金流', 'K线', '选股'）。
        domain: 领域名称。
                - 空/不指定：只搜索 tools/ 根目录（通用工具）
                - 指定领域：搜索 tools/{domain}/ + tools/ 根目录
                - "all"：搜索所有领域所有工具

    Returns:
        匹配的工具列表（格式化字符串）

    示例：
        search_tools("资金")              # 在通用工具中搜索
        search_tools("K线", "finance")    # 在 finance 领域 + 通用工具中搜索
        search_tools("", "all")           # 空关键词，等同于 list_tools("all")
    """
    if not query:
        return "请提供搜索关键词。"

    query_lower = query.lower()
    all_tools = _scan_tools(domain)

    matched = [t for t in all_tools
               if query_lower in t["name"].lower() or query_lower in t["desc"].lower()]

    if not matched:
        return f"未找到匹配 '{query}' 的工具。请使用 mcp(action='list') 查看所有可用工具。"

    title = f"找到 {len(matched)} 个工具："

    lines = [title]
    for t in matched:
        sig = t.get("sig", "")
        if sig:
            lines.append(f"  - {t['name']}({sig}) — {t['desc']}")
        else:
            lines.append(f"  - {t['name']}() — {t['desc']}")
    return "\n".join(lines)


# 兼容：从 list_tools 导入 list_tools，让 search_tools 模块也能直接调用
from tools.list_tools import list_tools  # noqa: E402, F401
