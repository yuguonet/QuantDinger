# -*- coding: utf-8 -*-
"""
通用格式化工具：dict → markdown / TSV。
"""
from __future__ import annotations
import json
from typing import Any, Dict, List, Optional, Union


def _to_md(data: Any) -> str:
    """dict/list → 简短 markdown 文本。"""
    if isinstance(data, dict):
        return _dict_to_md(data)
    if isinstance(data, list):
        return _list_to_md(data)
    return str(data)


def _dict_to_md(d: dict, depth: int = 0) -> str:
    """dict → markdown 文本。支持嵌套（最多展开2层）。"""
    parts = []
    indent = "  " * depth
    for k, v in d.items():
        if isinstance(v, dict):
            if depth < 2:
                inner = _dict_to_md(v, depth + 1)
                parts.append(f"{indent}- {k}:\n{inner}")
            else:
                parts.append(f"{indent}- {k}: {{...}}")
        elif isinstance(v, list):
            parts.append(f"{indent}- {k}: {_list_to_md(v, depth)}")
        elif isinstance(v, str) and len(v) > 200:
            parts.append(f"{indent}- {k}: {v[:200]}...")
        else:
            parts.append(f"{indent}- {k}: {v}")
    return "\n".join(parts) if parts else str(d)[:200]


def _list_to_md(lst: list, depth: int = 0) -> str:
    """list → markdown 文本。支持嵌套。"""
    if not lst:
        return "(空)"
    indent = "  " * depth
    if isinstance(lst[0], dict):
        parts = []
        limit = 5 if depth == 0 else 3
        for i, item in enumerate(lst[:limit], 1):
            vals = []
            for k, v in list(item.items())[:4]:
                if isinstance(v, (dict, list)):
                    vals.append(f"{k}:{{...}}")
                else:
                    vals.append(f"{k}:{v}")
            parts.append(f"{indent}{i}. {', '.join(vals)}")
        if len(lst) > limit:
            parts.append(f"{indent}...共{len(lst)}项")
        return "\n".join(parts)
    # 简单值列表
    items = [str(x) for x in lst[:10]]
    suffix = f" ...共{len(lst)}项" if len(lst) > 10 else ""
    return ", ".join(items) + suffix


def _to_tsv(rows: List[Dict[str, Any]], columns: Optional[List[str]] = None) -> str:
    """将 dict 列表转为 TSV（tab 分隔）字符串。首行为列名注释行（# 开头）。

    Args:
        rows: dict 列表，每个 dict 代表一行
        columns: 指定列顺序，为 None 时自动从数据中提取

    Returns:
        TSV 格式文本，首行以 # 开头标注列名，方便 Agent 识别字段
    """
    if not rows:
        return "(empty)"
    if columns is None:
        # 保持插入顺序，去重
        columns = list(dict.fromkeys(k for r in rows for k in r.keys()))
    header = "\t".join(columns)
    lines = [f"# {header}"]  # 注释行，帮助 Agent 识别列名
    for r in rows:
        vals = []
        for c in columns:
            v = r.get(c, "")
            if isinstance(v, (list, dict)):
                v = json.dumps(v, ensure_ascii=False)
            vals.append(str(v) if v is not None else "")
        lines.append("\t".join(vals))
    return "\n".join(lines)


def _batch_execute(fn, codes: List[str]) -> Any:
    """多股批量执行模板。单股/多股**统一**返回 {count, data, error}（2026-09-22）。

    历史：单股曾直接返回扁平结果、多股返回 {count, data}——二义性导致模型按
    data['代码'] 取值时单股场景 KeyError（07:22 轮 get_stock_concept_blocks 实测）。
    现统一为与 agent_get_kline 相同的三键结构：无论单多股，取数路径一致。

    Args:
        fn: 单股执行函数 fn(code) -> Any
        codes: 股票代码列表
    """
    def _safe_one(code: str) -> Dict[str, Any]:
        try:
            return fn(code)
        except Exception as e:
            return {"error": str(e)}

    results: Dict[str, Any] = {}
    for code in codes:
        results[code] = _safe_one(code)
    return {"count": len(results), "data": results, "error": None}


