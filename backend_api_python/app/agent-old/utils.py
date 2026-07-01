# -*- coding: utf-8 -*-
"""
Agent Utils — 共享工具函数。

公开接口：
  detect_market(stock_code) → str（CNStock/HKStock/Forex/Crypto）
  trim_messages(messages, max_tokens, keep_recent) → list  （token 级消息裁剪）
  estimate_tokens(text) → int  （粗估 token 数）
"""
from __future__ import annotations

import logging
from typing import Dict, List

logger = logging.getLogger(__name__)

# ── Token 估算 ──────────────────────────────────────────────
# 中文约 1.5 token/字，英文约 0.75 token/word，取混合均值
_CHARS_PER_TOKEN = 2.0  # 粗估：2 字符 ≈ 1 token（中英混合场景）


def estimate_tokens(text: str) -> int:
    """粗估文本 token 数（无需 tiktoken 依赖）。"""
    if not text:
        return 0
    return max(1, int(len(text) / _CHARS_PER_TOKEN))


def _message_tokens(msg: dict) -> int:
    """估算单条消息的 token 数（含 role 开销）。"""
    content = msg.get("content", "")
    if isinstance(content, list):
        # structured content blocks
        content = " ".join(
            b.get("text", "") if isinstance(b, dict) else str(b)
            for b in content
        )
    return estimate_tokens(str(content)) + 4  # role/name overhead


def trim_messages(
    messages: list,
    max_tokens: int = 8000,
    keep_recent: int = 4,
) -> list:
    """Token 级消息裁剪，保留最近 keep_recent 条，其余按 token 预算裁剪。

    策略（与 Agent-Template 的 trim_messages 对齐）：
      1. 最近 keep_recent 条消息始终保留（保证上下文连贯）
      2. 更早的消息从最旧的开始保留，直到 token 预算耗尽
      3. 如果整体不超限，原样返回

    Args:
        messages: [{role, content}, ...] 或 BaseMessage list
        max_tokens: token 预算上限
        keep_recent: 最少保留的最近消息数

    Returns:
        裁剪后的消息列表
    """
    if not messages:
        return []

    # 转为 dict 格式统一处理
    dicts = []
    for m in messages:
        if isinstance(m, dict):
            dicts.append(m)
        elif hasattr(m, "model_dump"):
            dicts.append(m.model_dump())
        elif hasattr(m, "role") and hasattr(m, "content"):
            dicts.append({"role": m.role, "content": m.content})
        else:
            dicts.append({"role": "user", "content": str(m)})

    # 不超限直接返回
    total = sum(_message_tokens(m) for m in dicts)
    if total <= max_tokens:
        return dicts

    # 分离：旧消息 + 最近 keep_recent 条
    recent = dicts[-keep_recent:]
    older = dicts[:-keep_recent]

    recent_tokens = sum(_message_tokens(m) for m in recent)
    budget = max_tokens - recent_tokens

    if budget <= 0:
        # 连最近的消息都超预算，只保留最后 2 条
        return dicts[-2:]

    # 从旧消息中按时间顺序保留（最旧的优先丢弃 → 反转后从最新旧消息开始保留）
    kept_older: list = []
    used = 0
    for msg in reversed(older):
        cost = _message_tokens(msg)
        if used + cost > budget:
            break
        kept_older.insert(0, msg)
        used += cost

    result = kept_older + recent
    if len(result) < len(dicts):
        logger.debug(
            "[TrimMessages] %d → %d messages (%d → ~%d tokens)",
            len(dicts), len(result), total, sum(_message_tokens(m) for m in result),
        )
    return result


# ── Market detection ────────────────────────────────────────

def detect_market(stock_code: str) -> str:
    """Detect market type from stock code.

    Returns one of: CNStock, HKStock, Forex, Crypto
    """
    code = (stock_code or "").strip().upper()
    if not code:
        return "CNStock"

    if code.startswith(("SH", "SZ", "BJ")):
        return "CNStock"
    if code.startswith("HK"):
        return "HKStock"

    if len(code) == 6 and code.isdigit():
        return "CNStock"

    _CRYPTO_PREFIXES = (
        "BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "DOT",
        "AVAX", "MATIC", "LINK", "UNI", "LTC", "ATOM", "FIL",
        "ARB", "OP", "APT", "SUI", "PEPE", "SHIB", "TRX",
    )
    _CRYPTO_SUFFIXES = ("USDT", "USDC", "BUSD", "BTC", "ETH")
    if any(code.startswith(p) for p in _CRYPTO_PREFIXES):
        return "Crypto"
    if any(code.endswith(s) for s in _CRYPTO_SUFFIXES) and not code.isalpha():
        return "Crypto"

    if len(code) == 6 and code.isalpha():
        return "Forex"

    return "CNStock"
