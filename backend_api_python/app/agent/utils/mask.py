# -*- coding: utf-8 -*-
"""app/agent/utils/mask.py — MASK 脱敏（方案常设 6：覆盖 JSONL 与 log，不止 DB）。

规则（白名单式，保守）：
  - API Key / token 形态（sk-…、长 hex、Bearer …）
  - 手机号 / 身份证粗匹配
  - 密码= / SECRET= / api_key= 等键值对的值

用于 tracing JSONL、log.py 输出、eval dump。**不改变** DB 既有列语义。
"""
from __future__ import annotations

import os
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


def user_input_mask_enabled() -> bool:
    """用户输入脱敏口径开关（`QD_TRACE_MASK_USER_INPUT`，默认 **开**）。

    ★ 为什么单列一个开关而不是直接调 `mask_text`：用户输入是**保留面最大**
      的一类数据（traces/*.jsonl + `qd_agent_traces` 表 + 评测侧车），
      脱敏与否是**运维口径** ⇒ 按项目约定走 env，留可回滚口子。
    ⚠ 置 0 只在本地排查时临时用：那会让手机号 / 身份证 / api_key 明文落盘。
    """
    return str(os.getenv("QD_TRACE_MASK_USER_INPUT", "1")).strip().lower() not in (
        "0", "false", "no", "off", "")


def mask_user_text(text: str) -> str:
    """**用户输入**落盘 / 落库前的统一脱敏入口（A-04，2026-10-14）。

    与 `mask_text` 的关系：规则相同，多一层 `QD_TRACE_MASK_USER_INPUT` 口径开关。
    ★ 本函数是「用户输入」的唯一脱敏入口（tracing / preselect 两个 sink 都走它），
      不要在各处自己调 `mask_text` —— 那会让开关对某一路失效（半边脱敏 = 没脱敏）。

    保守白名单式：只抹 API Key / Bearer / 键值对的 secret / 手机号 / 身份证，
    **不动正常问句**（"分析雪天盐业" 原样保留 ⇒ 不损失可排查性、不影响
    preselect 离线共现权重的原料语义）。
    """
    if not user_input_mask_enabled():
        return text
    return mask_text(text)


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
