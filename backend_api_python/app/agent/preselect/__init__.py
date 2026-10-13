# -*- coding: utf-8 -*-
"""工具预选子系统 —— 决定"这一轮模型看到哪些按需工具"。

## 为什么它**不在** `tools/` 目录下

`tools/` 是**工具自发现目录**：`ToolProvider` 会扫它（含子目录）把"公开 +
有 docstring"的函数注册成模型可调工具。而本包里的东西**恰恰不是工具**——
它们是决定工具面的**管线**：词典、倒排索引、目录生成、plan 解析、确定性 lint、
共现权重、观测计数器。

此前它们住在 `tools/` 下，靠模块级 `_NOT_TOOLS` 名单"声明自己不该被注册"。
那是把结构问题当成配置问题治：名单要手工同步，忘了就**静默**多一份必注入
schema（token 回升），且模型真可能去调它——`preselect_stats.reset` 就漏在
名单外，等于把"清空观测计数器"开放给了模型（2026-10-13 P9 实测）。

⇒ 移出扫描目录后，名单机制对这三者整体作废：**它们根本不会被扫到**。

## 包内分工

    tool_preselect.py  管线主体：域词典 / 倒排索引 / 目录 / 提示词 / 解析 / lint
    preselect_cooc.py  共现权重（周级离线攒"任务→实际调用工具"，只改目录呈现顺序）
    preselect_stats.py 观测计数器（空工具面占比，用数据决定要不要救，不猜）

## 调用面

历史调用点是 `from .preselect import lint_selection` 这种**模块路径**
导入。本包 `__init__` 把三者公开面**整体再导出**，调用方写成
`from .preselect import lint_selection` 即可，不再依赖内部文件划分。
"""
from __future__ import annotations

from .tool_preselect import (
    CAP_SECTION_NOTE, DATA_DOMAINS, DATA_DOMAIN_TOOLS, MIN_FACE,
    PRESELECT_PROMPT, PRESELECT_SYSTEM,
    LintReport, apply_lint, build_catalog, build_catalog_grouped,
    build_preselect_messages, build_tool_index, capability_domain,
    check_domain_dict, detect_domains, fallback_domain_tools,
    get_tool_index, has_domain_hint, index_candidates, lint_selection,
    names_in_text, parse_plan, parse_selection,
)
from .preselect_cooc import (
    build_weights, get_weights, heldout_recall, load_pairs,
    load_pairs_from_db, load_weights, rank_candidates,
    record_pairs_path, save_weights, weights_path,
)
from .preselect_stats import (
    empty_rate, pairs_path, record_empty_face, record_selected,
    record_task_calls, report, reset,
)

__all__ = [
    # 管线主体
    "CAP_SECTION_NOTE", "DATA_DOMAINS", "DATA_DOMAIN_TOOLS", "MIN_FACE",
    "PRESELECT_PROMPT", "PRESELECT_SYSTEM",
    "LintReport", "apply_lint", "build_catalog", "build_catalog_grouped",
    "build_preselect_messages", "build_tool_index", "capability_domain",
    "check_domain_dict", "detect_domains", "fallback_domain_tools",
    "get_tool_index", "has_domain_hint", "index_candidates", "lint_selection",
    "names_in_text", "parse_plan", "parse_selection",
    # 共现权重
    "build_weights", "get_weights", "heldout_recall", "load_pairs",
    "load_pairs_from_db", "load_weights", "rank_candidates",
    "record_pairs_path", "save_weights", "weights_path",
    # 观测计数器
    "empty_rate", "pairs_path", "record_empty_face", "record_selected",
    "record_task_calls", "report", "reset",
]
