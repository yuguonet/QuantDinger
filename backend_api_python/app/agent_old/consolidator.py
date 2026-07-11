# -*- coding: utf-8 -*-
"""
Consolidator — 异步上下文压缩器（Agent-Template 模式）。

职责：
  - 当消息 token 数超过阈值时，异步压缩旧消息为摘要
  - 压缩后从 Session 中移除原始消息，只保留最近 N 条 + 摘要
  - 写入 session_store 的 context_summaries（按域分轮）

与 finalize_node 的同步压缩不同：
  - Consolidator 是独立模块，可被 cron/heartbeat 触发
  - token 级阈值，不是消息条数
  - 压缩结果持久化到 session_store，不依赖 LangGraph state

用法：
  from app.agent.consolidator import consolidate_session
  result = consolidate_session(session_id)
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from app.agent.log import logger
from app.agent.utils import estimate_tokens


# ── 配置 ──────────────────────────────────────────────────────
# token 阈值：超过此数量触发压缩
COMPRESS_TOKEN_THRESHOLD = int(
    __import__("os").getenv("CONSOLIDATE_TOKEN_THRESHOLD", "12000")
)
# 压缩后保留的最近消息条数
KEEP_RECENT = int(__import__("os").getenv("CONSOLIDATE_KEEP_RECENT", "6"))
# 摘要最大长度（字符）
SUMMARY_MAX_CHARS = 500


def _estimate_messages_tokens(messages: list) -> int:
    """估算消息列表的总 token 数。"""
    total = 0
    for m in messages:
        content = ""
        if isinstance(m, dict):
            content = m.get("content", "")
        elif hasattr(m, "content"):
            content = m.content
        total += estimate_tokens(str(content))
    return total


def _messages_to_dicts(messages: list) -> list:
    """统一转为 OpenAI API 格式。"""
    _type_to_role = {"human": "user", "ai": "assistant", "system": "system", "tool": "tool"}
    dicts = []
    for m in messages:
        if isinstance(m, dict):
            role = m.get("role") or _type_to_role.get(m.get("type", "")) or "user"
            content = m.get("content", "")
            dicts.append({"role": role, "content": content if content else ""})
        elif hasattr(m, "model_dump"):
            d = m.model_dump()
            role = d.get("role") or _type_to_role.get(d.get("type", "")) or "user"
            content = d.get("content", "")
            dicts.append({"role": role, "content": content if content else ""})
        elif hasattr(m, "role") and hasattr(m, "content"):
            dicts.append({"role": m.role, "content": m.content})
        else:
            dicts.append({"role": "user", "content": str(m)})
    return dicts


def _extract_dialogue(messages: list) -> str:
    """提取消息中的纯文本对话（跳过 system/tool）。"""
    parts = []
    for m in messages:
        role = m.get("role", "") if isinstance(m, dict) else getattr(m, "role", "")
        if role not in ("user", "assistant"):
            continue
        content = m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
        if isinstance(content, str) and content:
            parts.append(f"{role}: {content[:300]}")
    return "\n".join(parts[-30:])  # 最多取最近 30 条


def _compress_with_llm(messages: list, query: str, domain: str) -> str:
    """用 LLM 压缩消息为摘要。"""
    dialogue = _extract_dialogue(messages)
    if not dialogue:
        return ""

    prompt = f"""请将以下对话历史压缩为一段简洁的上下文摘要（150字以内）。
保留关键信息：用户关注的股票、分析结论、重要数据点、用户偏好。
丢弃：寒暄、重复、工具调用细节、中间推理过程。

领域: {domain}
当前问题: {query}

对话历史:
{dialogue}

要求：直接输出摘要，不要解释。"""

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
                "max_tokens": 200,
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        summary = resp.json()["choices"][0]["message"]["content"].strip()
        return summary[:SUMMARY_MAX_CHARS]
    except Exception as e:
        logger.warning("[Consolidator] LLM 压缩失败: %s", e)
        return ""


def consolidate_session(
    session_id: str,
    state_messages: list = None,
    domain: str = "",
    query: str = "",
    force: bool = False,
) -> Dict[str, Any]:
    """压缩指定会话的上下文。

    Args:
        session_id: 会话 ID
        state_messages: 当前 state 中的消息列表（如果有的话）
        domain: 当前领域
        query: 当前用户问题
        force: 强制压缩（忽略 token 阈值）

    Returns:
        {
            "compressed": bool,     # 是否执行了压缩
            "summary": str,         # 生成的摘要
            "before_tokens": int,   # 压缩前 token 数
            "after_tokens": int,    # 压缩后 token 数
            "messages_before": int, # 压缩前消息条数
            "messages_after": int,  # 压缩后消息条数
        }
    """
    result = {
        "compressed": False,
        "summary": "",
        "before_tokens": 0,
        "after_tokens": 0,
        "messages_before": 0,
        "messages_after": 0,
    }

    if not state_messages:
        return result

    dicts = _messages_to_dicts(state_messages)
    total_tokens = _estimate_messages_tokens(dicts)
    result["before_tokens"] = total_tokens
    result["messages_before"] = len(dicts)

    # 检查是否需要压缩
    if not force and total_tokens < COMPRESS_TOKEN_THRESHOLD:
        logger.debug(
            "[Consolidator] %s: %d tokens < %d 阈值，跳过",
            session_id, total_tokens, COMPRESS_TOKEN_THRESHOLD,
        )
        return result

    # 取旧消息（排除最近 KEEP_RECENT 条）做压缩
    old_messages = dicts[:-KEEP_RECENT] if len(dicts) > KEEP_RECENT else dicts[:-2]
    recent_messages = dicts[-KEEP_RECENT:] if len(dicts) > KEEP_RECENT else dicts[-2:]

    if not old_messages:
        return result

    # LLM 压缩
    summary = _compress_with_llm(old_messages, query, domain)
    if not summary:
        logger.warning("[Consolidator] %s: 压缩失败，跳过", session_id)
        return result

    # 写入 session_store
    try:
        from app.agent.session_store import get_session_store
        store = get_session_store()
        store.save_context_summary(session_id, summary, domain=domain)
    except Exception as e:
        logger.warning("[Consolidator] 写入 session_store 失败: %s", e)

    result["compressed"] = True
    result["summary"] = summary
    result["after_tokens"] = _estimate_messages_tokens(recent_messages)
    result["messages_after"] = len(recent_messages)

    logger.info(
        "[Consolidator] %s: %d 条 (%d tokens) → %d 条 (%d tokens) + 摘要 (%d 字)",
        session_id,
        result["messages_before"], result["before_tokens"],
        result["messages_after"], result["after_tokens"],
        len(summary),
    )

    return result


def should_consolidate(state_messages: list) -> bool:
    """检查是否需要压缩（不执行压缩）。"""
    if not state_messages:
        return False
    dicts = _messages_to_dicts(state_messages)
    return _estimate_messages_tokens(dicts) >= COMPRESS_TOKEN_THRESHOLD
