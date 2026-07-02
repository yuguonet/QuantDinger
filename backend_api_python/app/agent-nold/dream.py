# -*- coding: utf-8 -*-
"""
Dream — 长期知识提取器（Agent-Template Dream 模式）。

职责：
  - 定期分析历史对话，提取长期知识
  - 用户偏好、分析模式、历史教训、关注的股票
  - 写入 memory.md（通过 MemoryStore）

触发方式：
  - cron 定时调用 dream_all_users()
  - heartbeat 中调用 dream_user(user_id)
  - 手动调用 dream_user(user_id)

设计：
  - Phase 1：LLM 分析历史，生成结构化建议（单次调用，快）
  - Phase 2：执行写入 memory.md（纯文件操作，无 LLM）

用法：
  from app.agent.dream import dream_user, dream_all_users
  result = dream_user("1")
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List

from app.agent.log import logger


# ── 配置 ──────────────────────────────────────────────────────
# Dream 分析的历史窗口（最近 N 个会话）
DREAM_HISTORY_WINDOW = int(os.getenv("DREAM_HISTORY_WINDOW", "10"))
# Dream 最小间隔（秒），防止频繁触发
DREAM_MIN_INTERVAL = int(os.getenv("DREAM_MIN_INTERVAL", "3600"))


def _get_recent_contexts(user_id: str) -> List[Dict[str, Any]]:
    """获取用户最近的会话上下文。"""
    contexts = []

    # 从 session_store 读取
    try:
        from app.agent.session_store import get_session_store
        store = get_session_store()
        sessions = store.list_sessions(limit=DREAM_HISTORY_WINDOW)
        for sess in sessions:
            sid = sess.get("session_id", "")
            if not sid:
                continue
            ctx_summaries = store.get_context_summary(sid, current_domain="")
            tool_results = store.get_tool_results(sid)
            if ctx_summaries or tool_results:
                contexts.append({
                    "session_id": sid,
                    "domain": sess.get("domain", ""),
                    "stock_code": sess.get("stock_code", ""),
                    "summary": ctx_summaries if isinstance(ctx_summaries, str) else str(ctx_summaries),
                    "tool_keys": list(tool_results.keys()) if tool_results else [],
                    "updated_at": sess.get("updated_at", ""),
                })
    except Exception as e:
        logger.debug("[Dream] session_store 读取失败: %s", e)

    # 从 checkpointer 读取
    try:
        from app.agent.graph import list_checkpointer_sessions, get_previous_state
        sessions = list_checkpointer_sessions(limit=DREAM_HISTORY_WINDOW)
        for sess in sessions:
            sid = sess.get("session_id", "")
            if not sid:
                continue
            state = get_previous_state(sid)
            if not state or not isinstance(state, dict):
                continue
            contexts.append({
                "session_id": sid,
                "query": state.get("query", "")[:200],
                "domain": state.get("domain", ""),
                "stock_code": state.get("stock_code", ""),
                "stock_name": state.get("stock_name", ""),
                "verb": state.get("intent_verb", ""),
                "noun": state.get("intent_noun", ""),
                "context_summary": state.get("context_summary", ""),
                "step_count": len(state.get("step_records", [])),
            })
    except Exception as e:
        logger.debug("[Dream] checkpointer 读取失败: %s", e)

    return contexts


def _analyze_with_llm(contexts: List[Dict], existing_memory: str) -> Dict[str, Any]:
    """Phase 1: LLM 分析历史，生成结构化建议。"""
    if not contexts:
        return {"skip": True, "reason": "no history"}

    # 构建历史摘要
    history_parts = []
    for ctx in contexts[-DREAM_HISTORY_WINDOW:]:
        query = ctx.get("query", "")
        domain = ctx.get("domain", "")
        stock = ctx.get("stock_name", "") or ctx.get("stock_code", "")
        summary = ctx.get("context_summary", "")
        verb = ctx.get("verb", "")
        noun = ctx.get("noun", "")

        parts = []
        if query:
            parts.append(f"问题: {query[:100]}")
        if verb or noun:
            parts.append(f"意图: {verb} {noun}")
        if stock:
            parts.append(f"标的: {stock}")
        if domain:
            parts.append(f"领域: {domain}")
        if summary:
            parts.append(f"摘要: {summary[:100]}")

        if parts:
            history_parts.append(" | ".join(parts))

    history_text = "\n".join(history_parts[-15:])  # 最多 15 条
    if not history_text:
        return {"skip": True, "reason": "empty history"}

    prompt = f"""你是一个知识提取器。分析以下用户的历史对话记录，提取值得长期记住的知识。

## 已有记忆（避免重复）
{existing_memory or "（无）"}

## 历史对话记录
{history_text}

## 输出格式（只输出 JSON）
```json
{{
  "preferences": ["用户偏好1", "用户偏好2"],
  "lessons": ["历史教训1"],
  "patterns": ["分析模式1"],
  "stocks": ["关注的股票信息1"]
}}
```

## 提取规则
- preferences: 用户的沟通偏好、分析习惯、常用标的类型
- lessons: 从历史中发现的规律（如"用户常问资金流向"、"用户偏好技术面分析"）
- patterns: 用户反复使用的分析模式（如"先看K线再看资金"）
- stocks: 用户反复关注的股票或板块

## 注意
- 只提取有充分证据的结论（至少出现2次以上）
- 不要提取一次性事件
- 每个类别最多5条
- 如果没有值得提取的内容，返回空数组"""

    try:
        from app.services.llm import LLMService
        import requests

        svc = LLMService()
        api_key = svc.get_api_key()
        base_url = svc.get_base_url()
        model_id = svc.get_default_model()

        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model_id,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 500,
            },
            timeout=60.0,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()

        # 解析 JSON
        import re
        raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            data = json.loads(m.group())
            if isinstance(data, dict):
                return data

        logger.warning("[Dream] LLM 输出解析失败: %s", raw[:200])
        return {"skip": True, "reason": "parse error"}

    except Exception as e:
        logger.warning("[Dream] LLM 调用失败: %s", e)
        return {"skip": True, "reason": str(e)}


def dream_user(user_id: str = "1", force: bool = False) -> Dict[str, Any]:
    """对指定用户执行 Dream：分析历史 → 提取知识 → 写入 memory.md。

    Args:
        user_id: 用户 ID
        force: 强制执行（忽略最小间隔）

    Returns:
        执行结果
    """
    from app.agent.memory_store import get_memory
    memory = get_memory(user_id)

    # 检查间隔
    if not force:
        memory_path = memory.path
        if memory_path.exists():
            mtime = memory_path.stat().st_mtime
            if time.time() - mtime < DREAM_MIN_INTERVAL:
                return {"skipped": True, "reason": "too frequent"}

    # 获取历史
    contexts = _get_recent_contexts(user_id)
    if not contexts:
        return {"skipped": True, "reason": "no history"}

    # Phase 1: LLM 分析
    existing_memory = memory.get_content()
    suggestions = _analyze_with_llm(contexts, existing_memory)

    if suggestions.get("skip"):
        return {"skipped": True, "reason": suggestions.get("reason")}

    # Phase 2: 写入 memory.md
    added = {"preferences": 0, "lessons": 0, "patterns": 0, "stocks": 0}

    for pref in suggestions.get("preferences", []):
        if memory.add_preference(pref):
            added["preferences"] += 1

    for lesson in suggestions.get("lessons", []):
        if memory.add_lesson(lesson):
            added["lessons"] += 1

    for pattern in suggestions.get("patterns", []):
        if memory.add_pattern(pattern):
            added["patterns"] += 1

    for stock in suggestions.get("stocks", []):
        if memory.add_stock(stock):
            added["stocks"] += 1

    total = sum(added.values())
    logger.info("[Dream] %s: 提取 %d 条知识 %s", user_id, total, added)

    return {"success": True, "added": added, "total": total}


def dream_all_users() -> Dict[str, Any]:
    """对所有用户执行 Dream。"""
    from app.agent.memory_store import MemoryStore

    results = {}
    memory_root = MemoryStore._MEMORY_ROOT if hasattr(MemoryStore, '_MEMORY_ROOT') else \
        os.path.join(os.path.dirname(__file__), "..", "..", "workspaces", "memory")

    try:
        root = os.path.join(os.path.dirname(__file__), "..", "..", "workspaces", "memory")
        if os.path.exists(root):
            for entry in os.listdir(root):
                user_dir = os.path.join(root, entry)
                if os.path.isdir(user_dir):
                    result = dream_user(entry)
                    results[entry] = result
    except Exception as e:
        logger.warning("[Dream] dream_all_users 失败: %s", e)

    return results
