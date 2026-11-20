"""chain/ingestion_quality.py — 入库质检机械化（方案 §5.4.1 / 验收 §13）。

两项关键判据不接受 LLM 自证：① 引用校验——source_ref + span 可定位且 content_sha
与原文重算一致（复用 experience_capture.content_sha 口径），定位失败/hash 不符/仅
自述引用一律 fail；② 证伪可观察——四段非空且 falsifier 为白名单 (key,op,value)
谓词（复用 condition_eval.structural_check）或可观测事件字段，空话式 fail。
quality_gate 两项过且调用方传入 spot_check=True 才建议 validated，否则维持 candidate
并记录原因；spot_check 缺失不假装通过。纯函数，不碰 DB/LLM、零生产调用方。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from . import condition_eval as _ce

SCHEMA_VERSION = "ingestion-quality-v1"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().with_name("ingestion_quality.json")
CONFIG_PATH_ENV = "QD_INGESTION_QUALITY"
CANDIDATE = "candidate"
VALIDATED = "validated"


def config_path() -> Path:
    raw = os.getenv(CONFIG_PATH_ENV, "").strip()
    return Path(raw) if raw else DEFAULT_CONFIG_PATH


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    p = Path(path) if path else config_path()
    with open(p, encoding="utf-8") as fh:
        cfg = json.load(fh)
    if not isinstance(cfg, dict):
        raise ValueError("ingestion quality config must be an object")
    if cfg.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("schema_version mismatch: %r" % cfg.get("schema_version"))
    for name in ("required_segments", "observable_events", "vague_falsifier_phrases",
                 "citation_min_ref_len"):
        if name not in cfg:
            raise ValueError("ingestion quality config missing %s" % name)
    return copy.deepcopy(cfg)


def _content_sha(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:32]


def verify_citation(item: Dict[str, Any], source_text: Optional[str],
                    cfg: Dict[str, Any]) -> Dict[str, Any]:
    """引用机械化校验：引用可定位 + hash 与原文一致；自述引用不算。"""
    ref = str(item.get("source_ref") or "").strip()
    span = str(item.get("source_span_or_event") or "").strip()
    sha = str(item.get("content_sha") or "").strip()
    min_len = int(cfg.get("citation_min_ref_len") or 0)
    if len(ref) < min_len:
        return _r(False, "ref_not_locatable")
    if not span:
        return _r(False, "span_missing")
    if not sha:
        return _r(False, "hash_missing")
    if source_text is None:
        return _r(False, "source_text_unavailable")
    if _content_sha(source_text) != sha.lower():
        return _r(False, "hash_mismatch")
    if item.get("citation_self_claimed") and not item.get("citation_verified_by_tool"):
        return _r(False, "self_claimed_citation")
    return _r(True, "ok", {"ref": ref, "span": span, "sha": sha})


def _falsifier_observable(falsifier: Any, wl: Dict[str, Any],
                          cfg: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(falsifier, dict):
        stat = _ce.structural_check(falsifier, wl)
        if (stat.get("atoms", 0) > 0 and stat.get("out_of_whitelist", 0) == 0
                and stat.get("invalid", 0) == 0):
            return _r(True, "whitelist_predicate", stat)
        return _r(False, "predicate_not_computable", stat)
    observable = set(cfg.get("observable_events") or ())
    if isinstance(falsifier, str):
        text = falsifier.strip()
        for phrase in cfg.get("vague_falsifier_phrases") or ():
            if phrase and phrase in text:
                return _r(False, "vague_falsifier")
        hit = {name for name in observable
               if re.search(r"\b%s\b" % re.escape(name), text, re.IGNORECASE)}
        if hit:
            return _r(True, "observable_event", {"fields": sorted(hit)})
        return _r(False, "no_observable_event")
    return _r(False, "falsifier_not_structured")


def verify_falsifier(item: Dict[str, Any], wl: Dict[str, Any],
                     cfg: Dict[str, Any]) -> Dict[str, Any]:
    """四段非空 + falsifier 可观察；空话式证伪降级。"""
    segments = cfg.get("required_segments") or []
    content = item.get("normalized_content") or item
    if not isinstance(content, dict):
        return _r(False, "content_not_structured")
    empty = [s for s in segments if not _segment_present(content.get(s), item.get(s))]
    if empty:
        return _r(False, "empty_segments", {"empty": empty})
    return _falsifier_observable(content.get("falsifier", item.get("falsifier")), wl, cfg)


def _segment_present(a: Any, b: Any) -> bool:
    for v in (a, b):
        if v is None:
            continue
        if isinstance(v, str):
            if v.strip():
                return True
        elif isinstance(v, (dict, list)):
            if v:
                return True
        else:
            return True
    return False


def _r(ok: bool, reason: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out = {"ok": ok, "reason": reason}
    if extra:
        out.update(extra)
    return out


def quality_gate(item: Dict[str, Any], *, source_text: Optional[str] = None,
                 spot_check: Optional[bool] = None,
                 cfg: Optional[Dict[str, Any]] = None,
                 whitelist: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """双判据 + 抽检通过才建议 validated，否则 candidate 并记原因。"""
    if cfg is None:
        try:
            cfg = load_config()
        except (OSError, ValueError, json.JSONDecodeError) as e:
            return {"status": CANDIDATE, "reason": "config_unavailable:%s" % type(e).__name__,
                    "citation": {}, "falsifier": {}, "spot_check": None}
    if whitelist is None:
        try:
            whitelist = _ce.load_config()
        except (OSError, ValueError, json.JSONDecodeError):
            return {"status": CANDIDATE, "reason": "whitelist_unavailable",
                    "citation": {}, "falsifier": {}, "spot_check": None}
    if whitelist.get("schema_version") != _ce.SCHEMA_VERSION or not isinstance(
            whitelist.get("keys"), dict):
        return {"status": CANDIDATE, "reason": "whitelist_invalid",
                "citation": {}, "falsifier": {}, "spot_check": None}
    if cfg.get("schema_version") != SCHEMA_VERSION:
        return {"status": CANDIDATE, "reason": "config_schema_mismatch",
                "citation": {}, "falsifier": {}, "spot_check": None}
    citation = verify_citation(item, source_text, cfg)
    falsifier = verify_falsifier(item, whitelist, cfg)
    reasons: Sequence[str] = [x["reason"] for x in (citation, falsifier) if not x["ok"]]
    result = {"status": CANDIDATE, "citation": citation, "falsifier": falsifier,
              "spot_check": spot_check, "reasons": list(reasons)}
    if reasons:
        result["reason"] = "quality_check_failed"
        return result
    if spot_check is None:
        result["reason"] = "spot_check_missing"
        return result
    if spot_check is not True:
        result["reason"] = "spot_check_failed"
        return result
    result["status"] = VALIDATED
    result["reason"] = "ok"
    return result
