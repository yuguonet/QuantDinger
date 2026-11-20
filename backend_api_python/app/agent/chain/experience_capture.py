"""chain/experience_capture.py — LLM 输入前端经验捕获（1118 P0-4）。

【为什么在前端接，而不是扫 SKILL.md】
11-17 用户裁定：「经验库要接入的位置应该在 LLM 输入的前端和 LLM 输出端，
也就是捕获全文，分析里面用了哪些经验」。前端 `_prefetch` 的 report 已经记录
**本轮实际注入了什么**（skill / memory / rag / case 四类来源 + 命中名 + 字符数），
是「实际用到」的直接证据；扫 SKILL.md 只能猜「可能用到什么」，还会碰
「经验库不读 SKILL.md」的红线。本模块只消费 report，不读任何技能文件、
不 import learning/、不按技能名硬编码产物字段或中文信号串。

【职责边界】
纯函数层：report → candidate 经验项 dict 列表，不碰 DB、不调 LLM。
写入由 evaluation_store.register_experience 负责（candidate / observe 档）。

【候选 ≠ 有效】
前端捕获只证明「这段内容进了本轮上下文」，不证明它是经验、更不证明它有效。
所有项一律 `evidence_status=candidate` / `consumption_mode=observe`，
必须留来源身份 + 内容 hash 供机械化引用校验；不经回评不升 validated。

【脱敏】
复用 utils.mask.mask_text（API Key / 手机号 / 身份证 / 密钥键值对）。
normalized_excerpt 只留脱敏后前 N 字（默认 240，走 QD_CAPTURE_EXCERPT_CHARS），
不写全文——全文可从 source_ref 定位回原来源，避免把技能正文/会话原文整段复制进目录。
"""
from __future__ import annotations

import hashlib
import os
from typing import Any, Dict, List, Optional, Sequence

from ..utils.mask import mask_text

EXTRACTOR_VER = "frontend-capture-v1"

_SOURCE_KIND = {
    "skill": "skill_content",
    "memory": "session_span",
    "rag": "rag_chunk",
    "case": "historical_case",
}

_CAPTURABLE = frozenset(_SOURCE_KIND)


def _excerpt_limit() -> int:
    try:
        return max(40, int(os.getenv("QD_CAPTURE_EXCERPT_CHARS", "240") or 240))
    except (TypeError, ValueError):
        return 240


def capture_enabled() -> bool:
    return str(os.getenv("QD_EXPERIENCE_CAPTURE_ENABLED", "1")).strip().lower() \
        not in ("0", "false", "no", "off")


def content_sha(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:32]


def _source_ref(block: dict, kind: str) -> str:
    """来源身份：只用 block 已有的通用 key，不按技能名分叉。"""
    for key in ("source_ref", "name", "id", "event_id"):
        v = block.get(key)
        if v:
            return "%s:%s" % (kind, str(v)[:120])
    return "%s:anon" % kind


def capture_from_report(report: Optional[dict],
                        session_id: str = "",
                        decision_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """把 `_prefetch` 的 report 转成候选经验项列表。

    report.capture_items 是 `_prefetch` 在拼装时同步记的来源项
    （kind / text / source_ref / chars）。只处理 injected=True 且 kind 在
    白名单内的项；resolve / prob_contract 属运行时解析与契约注入，不是经验来源。

    任何异常都由调用方 fail-soft 吞掉；本函数自身不碰 IO。
    """
    if not capture_enabled() or not isinstance(report, dict):
        return []
    out: List[Dict[str, Any]] = []
    limit = _excerpt_limit()
    for item in report.get("capture_items") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "")
        if kind not in _CAPTURABLE:
            continue
        text = str(item.get("text") or "")
        if not text.strip():
            continue
        sha = str(item.get("content_sha") or "") or content_sha(text)
        block = {"source_ref": item.get("source_ref")}
        out.append({
            "experience_id": "cap-%s-%s" % (kind, sha[:16]),
            "experience_version": "v1",
            "experience_type": _SOURCE_KIND[kind],
            "normalized_content": {
                "excerpt": mask_text(text)[:limit],
                "chars": int(item.get("chars") or len(text)),
                "session_id": session_id[:80] if session_id else "",
            },
            "content_sha": sha,
            "source_kind": _SOURCE_KIND[kind],
            "source_ref": _source_ref(block, kind),
            "source_version": str(item.get("source_version") or "")[:64],
            "source_span_or_event": str(item.get("span") or "")[:128],
            "extractor_ver": EXTRACTOR_VER,
            "evidence_status": "candidate",
            "consumption_mode": "observe",
        })
    return out
