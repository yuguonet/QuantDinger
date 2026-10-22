# -*- coding: utf-8 -*-
"""_doc_item — RAG 文档条目统一组装（2026-10-22 冗余收敛）。

收敛前 `agent.py:_load_analysis_memory_docs` 与 `rag/postgres_fts.py::retrieve`
各写一份同型 dict（content 截断 + metadata 信封）——10-19 审计 4 组 curated
重复之一。两处字段语义一致（仅 source 值不同；fts 多带 score/retrieval_route），
故收敛为单一构造器。调用方特有键走 **extra 透传**——不猜调用方要什么，
也不把可选键堆成一张大参数表。
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def doc_item(*, decision: Optional[str], symbol: Optional[str],
             summary: Optional[str], created_at: Any, source: str,
             content_limit: int = 500, **extra: Any) -> Dict[str, Any]:
    """组装一条 RAG 文档：content 截断 + metadata 信封 + extra 透传键。"""
    content = f"{decision or ''} {symbol or ''} {summary or ''}"
    item: Dict[str, Any] = {
        "content": content[:content_limit],
        "metadata": {
            "source": source,
            "symbol": symbol or "",
            "decision": decision or "",
            "date": str(created_at) if created_at else "",
        },
    }
    item.update(extra)
    return item
