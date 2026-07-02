# -*- coding: utf-8 -*-
"""
History Tools — 按需检索历史信息（Agent-Template 模式）。

LLM 自己决定什么时候调用这些工具，不要在 prompt 里塞历史。

工具：
  search_history(query, limit) — 搜索历史对话
  get_context_summary() — 获取当前会话上下文摘要
  get_previous_analysis(stock_code) — 获取某只股票的上轮分析结果
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict

from app.agent.log import logger


def search_history(query: str, limit: int = 5) -> Dict[str, Any]:
    """搜索历史对话记录。当用户提到"上次"、"之前"、"历史"或你想了解之前聊过什么时调用。

    Args:
        query: 搜索关键词（如股票名称、分析主题等）
        limit: 返回条数上限，默认5

    Returns:
        匹配的历史对话列表
    """
    if not query or not query.strip():
        return {"error": "搜索关键词不能为空", "results": []}

    limit = min(max(limit, 1), 20)
    results = []

    try:
        import psycopg2
        database_url = os.getenv("DATABASE_URL", "")
        if not database_url:
            return {"error": "数据库未配置", "results": []}

        conn = psycopg2.connect(database_url)
        try:
            with conn.cursor() as cur:
                # 从 checkpointer 的 checkpoints 表搜索
                # checkpoint_blobs 存了 state 数据
                cur.execute("""
                    SELECT thread_id, checkpoint_id,
                           checkpoint::text
                    FROM checkpoints
                    ORDER BY checkpoint_id DESC
                    LIMIT 200
                """)
                rows = cur.fetchall()

                query_lower = query.strip().lower()
                for thread_id, cid, checkpoint_text in rows:
                    if len(results) >= limit:
                        break
                    try:
                        # checkpoint 是 JSON，搜索包含关键词的
                        if query_lower in (checkpoint_text or "").lower():
                            # 提取关键信息
                            state = json.loads(checkpoint_text) if checkpoint_text else {}
                            channel_values = state.get("channel_values", {})
                            q = channel_values.get("query", "")
                            domain = channel_values.get("domain", "")
                            stock = channel_values.get("stock_name", "")
                            summary = channel_values.get("context_summary", "")

                            if q and query_lower in q.lower():
                                results.append({
                                    "session_id": thread_id,
                                    "time": cid or "",
                                    "query": q[:200],
                                    "domain": domain,
                                    "stock": stock,
                                    "summary": summary[:200] if summary else "",
                                })
                    except (json.JSONDecodeError, TypeError, KeyError):
                        continue
        finally:
            conn.close()
    except Exception as e:
        logger.warning("[History] search_history 失败: %s", e)
        return {"error": str(e), "results": []}

    return {"query": query, "count": len(results), "results": results}


def get_context_summary(session_id: str = "") -> Dict[str, Any]:
    """获取当前会话的上下文摘要。当你需要了解之前的对话脉络时调用。

    Args:
        session_id: 会话 ID（留空则尝试自动获取）

    Returns:
        当前会话的上下文摘要
    """
    if not session_id:
        # 从 tool_context 尝试获取
        try:
            from app.agent.tool_context import get_tool_context
            ctx = get_tool_context()
            session_id = ctx.get("session_id", "")
        except Exception:
            pass

    if not session_id:
        return {"error": "无法获取 session_id", "summary": ""}

    try:
        from app.agent.session_store import get_session_store
        store = get_session_store()
        summary = store.get_context_summary(session_id)
        if summary:
            return {"session_id": session_id, "summary": summary}
    except Exception as e:
        logger.debug("[History] get_context_summary 失败: %s", e)

    return {"session_id": session_id, "summary": "", "note": "暂无上下文摘要"}


def get_previous_analysis(stock_code: str = "") -> Dict[str, Any]:
    """获取某只股票的上轮分析结果。当用户说"上次分析的"、"之前那个"时调用。

    Args:
        stock_code: 股票代码（如 600519）。留空则返回最近一次分析。

    Returns:
        上轮分析的结论、工具调用记录等
    """
    try:
        database_url = os.getenv("DATABASE_URL", "")
        if not database_url:
            return {"error": "数据库未配置", "result": None}

        import psycopg2
        conn = psycopg2.connect(database_url)
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT thread_id, checkpoint_id, checkpoint::text
                    FROM checkpoints
                    ORDER BY checkpoint_id DESC
                    LIMIT 20
                """)
                rows = cur.fetchall()

            for thread_id, cid, checkpoint_text in rows:
                if not checkpoint_text:
                    continue
                try:
                    state = json.loads(checkpoint_text)
                except (json.JSONDecodeError, TypeError):
                    continue

                channel_values = state.get("channel_values", {})
                if not isinstance(channel_values, dict):
                    continue

                prev_stock = channel_values.get("stock_name", "") or ""
                prev_code = channel_values.get("stock_code", "") or ""
                prev_output = channel_values.get("final_output", {}) or {}
                prev_records = channel_values.get("step_records", []) or []
                prev_verb = channel_values.get("intent_verb", "") or ""
                prev_noun = channel_values.get("intent_noun", "") or ""

                if not (prev_stock or prev_code or prev_verb):
                    # 没有分析痕迹，跳过
                    continue

                # 如果指定了 stock_code，过滤匹配的
                if stock_code and stock_code not in (prev_code, ""):
                    continue

                parts = []
                if prev_verb or prev_noun:
                    parts.append(f"意图: {prev_verb} {prev_noun}")
                if prev_stock:
                    parts.append(f"标的: {prev_stock}({prev_code})")
                if prev_records:
                    for r in prev_records:
                        desc = r.get("description", "") or ""
                        content = r.get("step_content", "") or ""
                        if desc or content:
                            parts.append(f"{desc}: {content[:300]}")
                if prev_output:
                    reply = prev_output.get("reply", "") or prev_output.get("analysis", "") or ""
                    if reply:
                        parts.append(f"结论: {reply[:300]}")

                if parts:
                    return {
                        "stock_code": prev_code or stock_code,
                        "stock_name": prev_stock,
                        "session_id": thread_id,
                        "time": cid or "",
                        "analysis": "\n".join(parts),
                    }
        finally:
            conn.close()

        return {"error": f"未找到 {'股票 ' + stock_code if stock_code else ''}的历史分析", "result": None}

    except Exception as e:
        logger.warning("[History] get_previous_analysis 失败: %s", e)
        return {"error": str(e), "result": None}
