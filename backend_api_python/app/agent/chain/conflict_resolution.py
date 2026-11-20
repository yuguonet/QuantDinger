"""冲突消解与组合合成：多条经验如何在一次决策里共存（§8.4③）。

适用条件重叠且结论相反的经验按共享来源归组联合评估，不当独立投票；
无法单独归因标 unattributable（对齐 outcome link 字段语义）且不摊分权重；
组合级结论先于单项。冲突未决时显式呈现分歧与不确定性并 abstain，
禁止按最新版本静默覆盖，禁止文字权重改写量化概率。

纯函数层：不碰 DB/LLM，来源/link 由调用方传入，零生产调用方。
枚举与排除原因走版本化配置，未预注册/配置损坏 fail-closed。
"""

import copy
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

SCHEMA_VERSION = "conflict-resolution-config-v1"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().with_name("conflict_resolution_config.json")
CONFIG_PATH_ENV = "QD_CONFLICT_RESOLUTION_CONFIG"

UNATTRIBUTABLE = "unattributable"
ATTRIBUTABLE = "attributable"
REQUIRED_KEYS = ("opposing_pairs", "unattributable_exclusion_reason",
                 "abstain_marker")


def config_path() -> Path:
    raw = os.getenv(CONFIG_PATH_ENV, "").strip()
    return Path(raw) if raw else DEFAULT_CONFIG_PATH


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    p = Path(path) if path else config_path()
    with open(p, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    if not isinstance(cfg, dict) or cfg.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("schema_mismatch")
    for name in REQUIRED_KEYS:
        if name not in cfg:
            raise ValueError("conflict config missing %s" % name)
    return cfg


def _complete(cfg: Dict[str, Any]) -> bool:
    return isinstance(cfg, dict) and all(k in cfg for k in REQUIRED_KEYS)


def _atoms(cond: Any) -> List[Dict[str, Any]]:
    if not isinstance(cond, dict):
        return []
    out: List[Dict[str, Any]] = []
    for logical in ("and", "or"):
        if logical in cond and isinstance(cond[logical], list):
            for x in cond[logical]:
                out.extend(_atoms(x))
            return out
    if "key" in cond:
        return [cond]
    return out


def conditions_overlap(cond_a: Any, cond_b: Any) -> bool:
    """结构重叠判断：空条件=通用(潜在重叠)；共享 key 值域相交则重叠。"""
    aa, bb = _atoms(cond_a), _atoms(cond_b)
    if not aa or not bb:
        return True
    ka = {a.get("key") for a in aa}
    kb = {b.get("key") for b in bb}
    shared = ka & kb
    if not shared:
        return False
    for key in shared:
        for a in aa:
            if a.get("key") != key:
                continue
            for b in bb:
                if b.get("key") != key:
                    continue
                if _value_ranges_intersect(a, b):
                    return True
    return False


def _value_ranges_intersect(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    opa, opb = a.get("op"), b.get("op")
    va, vb = a.get("value"), b.get("value")
    sets_a = set(va) if isinstance(va, (list, tuple, set)) else {va}
    sets_b = set(vb) if isinstance(vb, (list, tuple, set)) else {vb}
    if opa == "eq" and opb == "eq":
        return bool(sets_a & sets_b)
    if "eq" in (opa, opb) and "in" in (opa, opb):
        return bool(sets_a & sets_b)
    if opa == "in" and opb == "in":
        return bool(sets_a & sets_b)
    if {opa, opb} <= {"gt", "lt"}:
        return True
    if opa == opb:
        return True
    return bool(sets_a & sets_b)


def _opposite(c1: Any, c2: Any, cfg: Dict[str, Any]) -> bool:
    def _norm(x: Any) -> str:
        for v in (x, (x or {}).get("value") if isinstance(x, dict) else None):
            if isinstance(v, str):
                return v.strip().lower()
        return ""

    a, b = _norm(c1), _norm(c2)
    if not a or not b:
        return False
    for left, right in cfg.get("opposing_pairs", []):
        if (a == left and b == right) or (a == right and b == left):
            return True
    return False


def group_conflicts(items: Sequence[Dict[str, Any]],
                    cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """按条件重叠+结论相反归冲突组，按共享来源归共线组，其余独立。"""
    try:
        c = cfg if cfg is not None else load_config()
    except (OSError, ValueError, json.JSONDecodeError):
        return {"ok": False, "reason": "config_unavailable", "conflicts": [],
                "collinear": [], "independent": []}
    if c.get("schema_version") != SCHEMA_VERSION:
        return {"ok": False, "reason": "schema_mismatch", "conflicts": [],
                "collinear": [], "independent": []}
    if not _complete(c):
        return {"ok": False, "reason": "config_incomplete", "conflicts": [],
                "collinear": [], "independent": []}

    conflicts: List[Dict[str, Any]] = []
    collinear: List[Dict[str, Any]] = []
    independent: List[Dict[str, Any]] = []
    used: Set[int] = set()

    for i, a in enumerate(items):
        if i in used:
            continue
        group = [a]
        group_idx = [i]
        reason = "independent"
        for j in range(i + 1, len(items)):
            if j in used:
                continue
            b = items[j]
            overlap = conditions_overlap(a.get("condition"), b.get("condition"))
            if not overlap:
                continue
            shared_lineage = _shared_lineage(a, b)
            opposite = _opposite(a.get("conclusion"), b.get("conclusion"), c)
            if opposite:
                group.append(b)
                group_idx.append(j)
                reason = "conflict"
            elif shared_lineage:
                group.append(b)
                group_idx.append(j)
                if reason != "conflict":
                    reason = "collinear"
        used.update(group_idx)
        rec = {"member_ids": [x.get("experience_id") for x in group],
               "members": copy.deepcopy(group), "reason": reason}
        if reason == "conflict":
            conflicts.append(rec)
        elif reason == "collinear":
            collinear.append(rec)
        else:
            independent.append(rec)

    return {"ok": True, "reason": "grouped", "conflicts": conflicts,
            "collinear": collinear, "independent": independent,
            "conflict_group_count": len(conflicts)}


def _shared_lineage(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    la, lb = a.get("source_lineage"), b.get("source_lineage")
    if not la or not lb:
        return False
    if isinstance(la, (list, tuple, set)) and isinstance(lb, (list, tuple, set)):
        return bool(set(la) & set(lb))
    return la == lb


def attribute_group(group: Dict[str, Any], links: Sequence[Dict[str, Any]],
                    cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """组级联合归因：无法单独归因标 unattributable，不摊分权重，组合级先于单项。"""
    try:
        c = cfg if cfg is not None else load_config()
    except (OSError, ValueError, json.JSONDecodeError):
        return {"ok": False, "reason": "config_unavailable", "attribution": UNATTRIBUTABLE}
    if c.get("schema_version") != SCHEMA_VERSION:
        return {"ok": False, "reason": "schema_mismatch", "attribution": UNATTRIBUTABLE}
    if not _complete(c):
        return {"ok": False, "reason": "config_incomplete", "attribution": UNATTRIBUTABLE}

    member_ids = set(group.get("member_ids") or [])
    rel = [l for l in links if l.get("experience_id") in member_ids]
    ineligible = any(l.get("eligible_for_attribution") is False for l in rel)
    link_ids = [l.get("link_id") or l.get("event_id") for l in rel]
    shared_link = len([x for x in link_ids if x is not None]) != len(set(
        x for x in link_ids if x is not None))
    collinear = group.get("reason") == "collinear"

    if ineligible or shared_link or collinear:
        return {"ok": True, "attribution": UNATTRIBUTABLE,
                "exclusion_reason": c["unattributable_exclusion_reason"],
                "weight_split": False, "group_conclusion_first": True,
                "reasons": _attr_reasons(ineligible, shared_link, collinear)}
    return {"ok": True, "attribution": ATTRIBUTABLE, "exclusion_reason": None,
            "weight_split": False, "group_conclusion_first": True, "reasons": []}


def _attr_reasons(ineligible: bool, shared_link: bool, collinear: bool) -> List[str]:
    out = []
    if ineligible:
        out.append("ineligible_for_attribution")
    if shared_link:
        out.append("shared_outcome_link")
    if collinear:
        out.append("collinear_source")
    return out


def resolve_presentation(groups: Sequence[Dict[str, Any]],
                         attrs: Sequence[Dict[str, Any]],
                         cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """冲突未决→显式分歧+不确定性+abstain；不按版本覆盖、不改概率。"""
    try:
        c = cfg if cfg is not None else load_config()
    except (OSError, ValueError, json.JSONDecodeError):
        return {"ok": False, "reason": "config_unavailable", "abstain": True}
    if c.get("schema_version") != SCHEMA_VERSION:
        return {"ok": False, "reason": "schema_mismatch", "abstain": True}
    if not _complete(c):
        return {"ok": False, "reason": "config_incomplete", "abstain": True}

    unresolved = [g for g in groups if g.get("reason") == "conflict"]
    unattributed = [a for a in attrs if a.get("attribution") == UNATTRIBUTABLE]
    abstain = bool(unresolved)
    presentation = {
        "ok": True,
        "abstain": abstain,
        "abstain_marker": c["abstain_marker"] if abstain else None,
        "conflict_group_count": len(unresolved),
        "unattributable_count": len(unattributed),
        "explicit_disagreement": [
            {"member_ids": g["member_ids"],
             "conclusions": [m.get("conclusion") for m in g["members"]],
             "uncertainty": "elevated"}
            for g in unresolved],
        "overwrite_by_version": False,
        "probability_rewritten": False,
    }
    return presentation
