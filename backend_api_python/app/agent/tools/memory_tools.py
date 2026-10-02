# -*- coding: utf-8 -*-
"""remember / recall —— 长期记忆工具（RAG/记忆「工具化」补充，mimoagent 无内置记忆）。

配合 qd_service 的自动预取（每轮把记忆近史注入上下文）：
  注入保下限，工具保上限——模型觉得上下文里的记忆不够可主动 recall。
存储后端复用 memory/ 包（LocalMemory / PostgresMemory，由 agent.py 按 .env 选型），
挂在固定命名空间 session_id="long_term_memory"。
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict

_MEMORY_NS = "long_term_memory"


def _get_memory():
    from agent import memory as _m
    return _m


def remember(text: str) -> Dict[str, Any]:
    """把重要事实/结论存入长期记忆（用户偏好、决策背景、研究结论等）。

    Args:
        text: 要记住的内容（一句话或一段）

    Returns:
        {"status": "ok"|"error", ...}
    """
    if not text or not text.strip():
        return {"error": "text 为空"}
    try:
        ok = asyncio.run(_get_memory().add(_MEMORY_NS, "user", text.strip()))
        return {"status": "ok" if ok else "error", "stored": text.strip()[:200]}
    except Exception as e:
        return {"error": str(e)}


def recall(query: str = "", count: int = 10) -> Dict[str, Any]:
    """召回长期记忆。query 非空时按关键词过滤，空则返回最近 count 条。

    Args:
        query: 关键词（可选）
        count: 最多返回条数

    Returns:
        {"count": N, "memories": [{role, content}, ...]}
    """
    try:
        hist = asyncio.run(_get_memory().get_history(_MEMORY_NS, limit=max(count * 3, count)))
    except Exception as e:
        return {"error": str(e)}
    items = [{"role": getattr(m, "role", "user"), "content": str(getattr(m, "content", ""))}
             for m in (hist or [])]
    if query:
        keys = [k for k in query.lower().split() if k]
        items = [it for it in items if any(k in it["content"].lower() for k in keys)]
    items = items[-count:]
    return {"count": len(items), "memories": items}
