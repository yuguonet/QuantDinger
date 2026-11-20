"""元学习：把"哪类经验在哪类情境有效"本身登记为经验（§8.4⑤）。

meta_experience 仅靠 experience_type 分型，身份/版本/evidence_status/
consumption_mode 完全复用普通经验口径，默认 candidate/observe，不绑定
任何 skill/工具。其 outcome 是"按元规则选包 vs 无差别选包"的组合级
描述性差值：只作诊断与候选生成，不构成因果结论、不得单独驱动升档。
升 shadow/active 必须有 meta 专属预注册对照并通过 §9.3 decide_mode 门槛。

纯函数层：不碰 DB/LLM，统计量由调用方传入，零生产调用方。
所有阈值/分型值走版本化配置，未预注册一律 fail-closed。
"""

import copy
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import condition_eval as _ce
from . import experiment_config as _ec

SCHEMA_VERSION = "meta-learning-config-v1"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().with_name("meta_learning_config.json")
CONFIG_PATH_ENV = "QD_META_LEARNING_CONFIG"

DIAGNOSTIC_ONLY = "diagnostic_only"
META_TYPE = "meta_experience"


def config_path() -> Path:
    raw = os.getenv(CONFIG_PATH_ENV, "").strip()
    return Path(raw) if raw else DEFAULT_CONFIG_PATH


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    p = Path(path) if path else config_path()
    with open(p, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    if not isinstance(cfg, dict) or cfg.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("schema_mismatch")
    return cfg


def _mean(xs: Sequence[float]) -> Optional[float]:
    nums = [float(x) for x in xs]
    return sum(nums) / len(nums) if nums else None


def build_meta_candidate(rule: Dict[str, Any], *, experience_id: str,
                         experience_version: str = "v1",
                         cfg: Optional[Dict[str, Any]] = None,
                         whitelist: Optional[Dict[str, Any]] = None
                         ) -> Dict[str, Any]:
    """把元规则包装成 meta_experience 草稿；selection_condition 须过白名单校验。"""
    try:
        c = cfg if cfg is not None else load_config()
    except (OSError, ValueError, json.JSONDecodeError):
        return {"ok": False, "reason": "config_unavailable", "candidate": None}
    if c.get("schema_version") != SCHEMA_VERSION:
        return {"ok": False, "reason": "schema_mismatch", "candidate": None}
    if not experience_id:
        return {"ok": False, "reason": "missing_experience_id", "candidate": None}
    if not isinstance(rule, dict):
        return {"ok": False, "reason": "invalid_rule", "candidate": None}

    sel = rule.get("selection_condition")
    if whitelist is None:
        try:
            whitelist = _ce.load_config()
        except (OSError, ValueError, json.JSONDecodeError):
            return {"ok": False, "reason": "whitelist_unavailable", "candidate": None}
    if not isinstance(whitelist, dict) or "keys" not in whitelist:
        return {"ok": False, "reason": "whitelist_invalid", "candidate": None}
    stat = _ce.structural_check(sel, whitelist)
    if stat.get("atoms", 0) == 0 or stat.get("out_of_whitelist", 0) > 0 \
            or stat.get("invalid", 0) > 0:
        return {"ok": False, "reason": "selection_condition_not_computable",
                "stat": stat, "candidate": None}

    draft = {
        "experience_id": experience_id,
        "experience_version": experience_version,
        "experience_type": c["meta_experience_type"],
        "evidence_status": c["default_evidence_status"],
        "consumption_mode": c["default_consumption_mode"],
        "meta_rule": copy.deepcopy(rule),
        "disclosure": {
            "delta_semantics": c["delta_semantics"],
            "causal": False,
            "drives_promotion": False,
            "bound_to_skill": False,
        },
    }
    return {"ok": True, "reason": "built", "candidate": draft, "stat": stat}


def descriptive_delta(picked: Sequence[float], baseline: Sequence[float], *,
                      cfg: Optional[Dict[str, Any]] = None,
                      ci_low: Optional[float] = None,
                      ci_high: Optional[float] = None,
                      n_clusters: Optional[int] = None) -> Dict[str, Any]:
    """按元规则选包 vs 无差别选包的描述性差值；只作诊断，不构成因果结论。"""
    try:
        c = cfg if cfg is not None else load_config()
    except (OSError, ValueError, json.JSONDecodeError):
        return {"ok": False, "reason": "config_unavailable"}
    if c.get("schema_version") != SCHEMA_VERSION:
        return {"ok": False, "reason": "schema_mismatch"}

    stat = _usable(c, len(picked), len(baseline), n_clusters)
    if not stat["usable"]:
        return {"ok": False, "reason": stat["reason"], "stat": stat}

    mp, mb = _mean(picked), _mean(baseline)
    delta = None if mp is None or mb is None else mp - mb
    return {
        "ok": True,
        "reason": "computed",
        "semantics": DIAGNOSTIC_ONLY,
        "causal": False,
        "drives_promotion": False,
        "picked_mean": mp,
        "baseline_mean": mb,
        "delta": delta,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "n_clusters": n_clusters,
        "stat": stat,
    }


def _usable(cfg: Dict[str, Any], n_picked: int, n_baseline: int,
            n_clusters: Optional[int]) -> Dict[str, Any]:
    min_g = cfg.get("min_group_size")
    min_c = cfg.get("min_clusters_for_descriptive")
    stat = {"min_group_size": min_g, "min_clusters": min_c,
            "n_picked": n_picked, "n_baseline": n_baseline,
            "n_clusters": n_clusters, "usable": False, "reason": ""}
    if min_g is None or min_c is None:
        stat["reason"] = "descriptive_threshold_unregistered"
        return stat
    if n_picked < min_g or n_baseline < min_g:
        stat["reason"] = "group_below_min_size"
        return stat
    if n_clusters is None or n_clusters < min_c:
        stat["reason"] = "clusters_below_min"
        return stat
    stat["usable"] = True
    stat["reason"] = "usable"
    return stat


def promotion_eligible(meta: Dict[str, Any], descriptive: Dict[str, Any], *,
                       cfg: Optional[Dict[str, Any]] = None,
                       experiment_cfg: Optional[Dict[str, Any]] = None,
                       preregistered_experiment: Optional[str] = None,
                       current_mode: str = "observe",
                       endpoint: Optional[str] = None,
                       ci_low: Optional[float] = None,
                       ci_high: Optional[float] = None,
                       n_clusters: Optional[int] = None,
                       looks: Optional[int] = None,
                       drawdown_ci_high: Optional[float] = None
                       ) -> Dict[str, Any]:
    """元经验升档：描述性差值绝不算数，须 meta 专属预注册对照 + §9.3 门槛。"""
    def _block(mode: str, reason: str, **extra: Any) -> Dict[str, Any]:
        out = {"eligible": False, "consumption_mode": mode,
               "reason": reason, "decision": None}
        out.update(extra)
        return out

    try:
        c = cfg if cfg is not None else load_config()
    except (OSError, ValueError, json.JSONDecodeError):
        return _block(current_mode, "config_unavailable")
    if c.get("schema_version") != SCHEMA_VERSION:
        return _block(current_mode, "schema_mismatch")
    if not isinstance(meta, dict) or meta.get("experience_type") != c["meta_experience_type"]:
        return _block(current_mode, "not_meta_experience")
    if meta.get("consumption_mode") != current_mode:
        return _block(current_mode, "mode_mismatch")

    if isinstance(descriptive, dict) and descriptive.get("ok"):
        return _block(current_mode, "descriptive_delta_cannot_promote")

    target_exp = c.get("promotion_experiment")
    if not target_exp:
        return _block(current_mode, "promotion_experiment_not_preregistered")
    if not preregistered_experiment or preregistered_experiment != target_exp:
        return _block(current_mode, "experiment_mismatch",
                      required=target_exp, given=preregistered_experiment)

    if experiment_cfg is None:
        try:
            experiment_cfg = _ec.load_config()
        except (OSError, ValueError, json.JSONDecodeError):
            return _block(current_mode, "experiment_config_unavailable")

    decision = _ec.decide_mode(
        experiment_cfg, target_exp, current_mode=current_mode,
        endpoint=endpoint, ci_low=ci_low, ci_high=ci_high,
        n_clusters=n_clusters, looks=looks,
        drawdown_ci_high=drawdown_ci_high)
    eligible = decision.get("verdict") == "promote"
    return {"eligible": eligible, "consumption_mode": decision["consumption_mode"],
            "reason": decision["reason"], "decision": decision}
