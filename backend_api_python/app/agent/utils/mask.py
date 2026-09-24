# -*- coding: utf-8 -*-
"""app/agent/utils/mask.py — MASK 脱敏（方案常设 6：覆盖 JSONL 与 log，不止 DB）。

规则（白名单式，保守）：
  - API Key / token 形态（sk-…、长 hex、Bearer …）
  - 手机号 / 身份证粗匹配
  - 密码= / SECRET= / api_key= 等键值对的值

用于 tracing JSONL、log.py 输出、eval dump。**不改变** DB 既有列语义。
"""
from __future__ import annotations

import re
from typing import Any

_PATTERNS = (
    re.compile(r"(?i)\b(sk-[A-Za-z0-9_\-]{8,})"),
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9\-\._~\+\/]+=*"),
    re.compile(r"(?i)\b((?:api[_-]?key|secret|password|token|access[_-]?key)\s*[=:]\s*)([^\s,;\"']{6,})"),
    re.compile(r"\b(1[3-9]\d{9})\b"),
    re.compile(r"\b(\d{17}[\dXx])\b"),
)


def mask_text(text: str) -> str:
    if not text or not isinstance(text, str):
        return text
    out = text
    out = _PATTERNS[0].sub("sk-***", out)
    out = _PATTERNS[1].sub(lambda m: m.group(1) + "***", out)
    out = _PATTERNS[2].sub(lambda m: m.group(1) + "***", out)
    out = _PATTERNS[3].sub(lambda m: m.group(1)[:3] + "****" + m.group(1)[-2:], out)
    out = _PATTERNS[4].sub("***ID***", out)
    return out


def mask_obj(obj: Any) -> Any:
    """递归脱敏 dict/list/str。"""
    if isinstance(obj, str):
        return mask_text(obj)
    if isinstance(obj, dict):
        return {k: mask_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        t = [mask_obj(v) for v in obj]
        return type(obj)(t) if isinstance(obj, tuple) else t
    return obj
