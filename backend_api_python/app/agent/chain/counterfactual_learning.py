"""chain/counterfactual_learning.py — §8.4④ 反事实学习（纯函数层）。

失败经验先按类型归因再对症改写，不直接淘汰：无区分力→叠加前提、时间外不重现→
精简条件、归因不清→双轨变体。改写产新版本草稿（新 experience_id/version +
parent_experience_id/split_rule 引用旧版），回到候选→独立回评，不复活旧结论、
不覆盖旧证据、不建谱系树。防自欺：确认评估数据须与探索数据零交集，且改写候选在
确认开始前登记。阈值/代数/候选数走版本化配置、null=未预注册则 undecidable 不硬猜。
纯函数，不碰 DB/LLM、零生产调用方；condition 须过白名单校验。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import condition_eval as _ce

SCHEMA_VERSION = "counterfactual-config-v1"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().with_name("counterfactual_config.json")
CONFIG_PATH_ENV = "QD_COUNTERFACTUAL_CONFIG"

NO_DISCRIM = "no_discrimination"
OUT_OF_TIME = "out_of_time"
UNCLEAR = "attribution_unclear"
UNDECIDABLE = "undecidable"


def config_path() -> Path:
    raw = os.getenv(CONFIG_PATH_ENV, "").strip()
    return Path(raw) if raw else DEFAULT_CONFIG_PATH


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    p = Path(path) if path else config_path()
    with open(p, encoding="utf-8") as fh:
        cfg = json.load(fh)
    if not isinstance(cfg, dict):
        raise ValueError("counterfactual config must be an object")
    if cfg.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("schema_version mismatch: %r" % cfg.get("schema_version"))
    for name in ("failure_types", "rewrite_actions", "neutral_band", "overlap_hit_min",
                 "max_generation", "max_candidates_per_exp"):
        if name not in cfg:
            raise ValueError("counterfactual config missing %s" % name)
    if not isinstance(cfg["failure_types"], list) or not isinstance(
            cfg["rewrite_actions"], dict):
        raise ValueError("failure_types/rewrite_actions must be list/object")
    for kind in cfg["failure_types"]:
        if kind not in cfg["rewrite_actions"]:
            raise ValueError("rewrite action missing for %s" % kind)
    return copy.deepcopy(cfg)


def _res(ok: bool, reason: str, **extra: Any) -> Dict[str, Any]:
    out = {"ok": ok, "reason": reason}
    out.update(extra)
    return out


def classify_failure(evidence: Dict[str, Any],
                     cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """按样本内/时间外效应与重叠命中数归类；阈值未预注册返回 undecidable。"""
    if cfg is None:
        try:
            cfg = load_config()
        except (OSError, ValueError, json.JSONDecodeError) as e:
            return _res(False, "config_unavailable:%s" % type(e).__name__, failure_type=UNDECIDABLE)
    if cfg.get("schema_version") != SCHEMA_VERSION:
        return _res(False, "schema_mismatch", failure_type=UNDECIDABLE)
    band = cfg.get("neutral_band")
    overlap_min = cfg.get("overlap_hit_min")
    if band is None or overlap_min is None:
        return _res(False, "not_preregistered", failure_type=UNDECIDABLE)
    try:
        in_s = float(evidence["in_sample_effect"])
        overlap = int(evidence.get("overlap_hits") or 0)
    except (KeyError, TypeError, ValueError):
        return _res(False, "evidence_incomplete", failure_type=UNDECIDABLE)
    out_s = evidence.get("out_of_time_effect")
    if out_s is None:
        return _res(False, "out_of_time_unmeasured", failure_type=UNDECIDABLE)
    try:
        out_s = float(out_s)
    except (TypeError, ValueError):
        return _res(False, "invalid_effect", failure_type=UNDECIDABLE)

    overlap_hits = overlap >= overlap_min
    if abs(in_s) <= band:
        kind = NO_DISCRIM
    elif abs(out_s) <= band or (in_s > 0) != (out_s > 0):
        kind = OUT_OF_TIME
    elif overlap_hits:
        kind = UNCLEAR
    else:
        return _res(False, "no_failure_signal", failure_type=UNDECIDABLE)
    return _res(True, "ok", failure_type=kind,
                evidence={"in_sample": in_s, "out_of_time": out_s,
                          "overlap_hits": overlap, "neutral_band": band})


def _condition_ok(cond: Any, wl: Dict[str, Any]) -> bool:
    st = _ce.structural_check(cond, wl)
    return st.get("atoms", 0) > 0 and st.get("out_of_whitelist", 0) == 0 \
        and st.get("invalid", 0) == 0


def _new_version(old: Dict[str, Any], action: str, rule: str) -> Dict[str, Any]:
    base = str(old.get("experience_id") or "exp")
    ver = str(old.get("experience_version") or "v1")
    seed = "%s|%s|%s|%s" % (base, ver, action, rule)
    sha = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    match = re.match(r"^v(\d+)", ver)
    next_ver = "v%d" % ((int(match.group(1)) if match else 0) + 1)
    return {"experience_id": "%s-rw%s" % (base, sha),
            "experience_version": "%s-r%s" % (next_ver, sha[:6]),
            "parent_experience_id": base, "split_rule": rule,
            "source_kind": "failure_rewrite", "generation": int(old.get("generation") or 0) + 1,
            "evidence_status": "candidate", "consumption_mode": "observe"}


def rewrite(failure_type: str, old: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None,
            *, wl: Optional[Dict[str, Any]] = None,
            add_condition: Optional[Dict[str, Any]] = None,
            drop_keys: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """对症改写产新版本草稿；叠加/精简后的 condition 须过白名单校验。"""
    if cfg is None:
        try:
            cfg = load_config()
        except (OSError, ValueError, json.JSONDecodeError) as e:
            return _res(False, "config_unavailable:%s" % type(e).__name__)
    if cfg.get("schema_version") != SCHEMA_VERSION:
        return _res(False, "schema_mismatch")
    if failure_type not in cfg.get("failure_types", []):
        return _res(False, "unknown_failure_type")
    if wl is None:
        try:
            wl = _ce.load_config()
        except (OSError, ValueError, json.JSONDecodeError):
            return _res(False, "whitelist_unavailable")
    max_gen, max_cands = cfg.get("max_generation"), cfg.get("max_candidates_per_exp")
    if max_gen is None or max_cands is None:
        return _res(False, "not_preregistered")
    gen = int(old.get("generation") or 0)
    if gen >= max_gen:
        return _res(False, "generation_cap", generation=gen)
    existing = int(old.get("rewrite_candidate_count") or 0)
    action = (cfg.get("rewrite_actions") or {}).get(failure_type)
    if action is None:
        return _res(False, "action_unregistered")
    produces = 2 if action == "dual_track" else 1
    if existing + produces > max_cands:
        return _res(False, "candidate_cap", existing=existing, produces=produces)

    orig_cond = old.get("condition")
    drafts: List[Dict[str, Any]] = []
    if action == "augment":
        if not isinstance(add_condition, dict):
            return _res(False, "add_condition_missing")
        merged = _merge_condition(orig_cond, add_condition)
        if not _condition_ok(merged, wl):
            return _res(False, "condition_not_allowed")
        d = _new_version(old, action, "augment:%s" % _cond_key(add_condition))
        d["condition"] = merged
        drafts.append(d)
    elif action == "simplify":
        keys = set(drop_keys or ())
        trimmed = _drop_condition(orig_cond, keys)
        if not _condition_ok(trimmed, wl):
            return _res(False, "condition_not_allowed")
        d = _new_version(old, action, "simplify:%s" % ",".join(sorted(keys)))
        d["condition"] = trimmed
        drafts.append(d)
    else:
        for tag in ("A", "B"):
            d = _new_version(old, action, "dual_track:%s" % tag)
            d["condition"] = orig_cond
            d["variant"] = tag
            drafts.append(d)
    return _res(True, "ok", action=action, drafts=drafts,
                rewrites_old=bool(old.get("evidence_status") == "retired"))


def _cond_key(cond: Dict[str, Any]) -> str:
    return "%s_%s" % (cond.get("key"), cond.get("value"))


def _merge_condition(orig: Any, add: Dict[str, Any]) -> Any:
    if orig is None:
        return add
    return {"and": [orig, add]}


def _drop_condition(cond: Any, keys: Sequence[str]) -> Any:
    if not keys:
        return cond
    if isinstance(cond, dict) and "and" in cond:
        kept = [_drop_condition(x, keys) for x in cond["and"]]
        kept = [x for x in kept if x is not None]
        if not kept:
            return None
        return kept[0] if len(kept) == 1 else {"and": kept}
    if isinstance(cond, dict) and str(cond.get("key")) in keys:
        return None
    return cond


def _parse_day(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", text)
    if match:
        text = "%s-%02d-%02d" % (match.group(1), int(match.group(2)),
                                int(match.group(3)))
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def confirmation_eligible(exploration_ids: Sequence[str],
                          confirmation_ids: Sequence[str],
                          registered_at: Optional[str],
                          confirm_start: Optional[str],
                          cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """防自欺：确认数据与探索数据零交集 + 改写候选在确认前登记，才允许确认评估。"""
    if cfg is None:
        try:
            cfg = load_config()
        except (OSError, ValueError, json.JSONDecodeError) as e:
            return _res(False, "config_unavailable:%s" % type(e).__name__)
    if cfg.get("schema_version") != SCHEMA_VERSION:
        return _res(False, "schema_mismatch")
    overlap = set(map(str, exploration_ids)) & set(map(str, confirmation_ids))
    must_disjoint = cfg.get("confirm_data_must_be_disjoint")
    must_register = cfg.get("must_register_before_confirm")
    if must_disjoint and overlap:
        return _res(False, "data_overlap", overlap=sorted(overlap)[:20])
    if must_register:
        if not registered_at or not confirm_start:
            return _res(False, "time_basis_unknown")
        reg_day, confirm_day = _parse_day(registered_at), _parse_day(confirm_start)
        if reg_day is None or confirm_day is None:
            return _res(False, "time_basis_incomparable",
                        registered_at=registered_at, confirm_start=confirm_start)
        if reg_day >= confirm_day:
            return _res(False, "registered_after_confirm_start",
                        registered_at=registered_at, confirm_start=confirm_start)
    return _res(True, "ok", disjoint=True, registered_before_confirm=True)
