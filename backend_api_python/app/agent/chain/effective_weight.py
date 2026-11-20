"""chain/effective_weight.py — 经验有效性权重（方案 §5.4.2，纯函数层）。

effective_weight 是经验在检索/排序中的证据强度：由成熟、可归因、目标匹配的
消费请求结果计算，含样本门槛、收缩/不确定性惩罚与范围限制。它不是预测概率、
胜率、仓位或买卖指令，不改 LLM 判断、不进量化概率（验收 §13）。

公式与全部阈值来自版本化配置 effective_weight_config.json，代码内零数值；
关键阈值未预注册(null)或 schema/公式不符 ⇒ undecidable、weight=None；独立簇
不足、来源不明、条件不匹配、不可归因/未成熟 ⇒ neutral、weight=None，绝不
伪造中性评估值（§13「undecidable 不伪装为中性结果」）。统计量由调用方算好
传入，本模块不碰 DB/LLM、不写库、无生产调用方，时间基准由调用方显式给。
"""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = "effective-weight-config-v1"
FORMULA = "beta_shrink_wilson_lower_v1"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().with_name("effective_weight_config.json")
CONFIG_PATH_ENV = "QD_EFFECTIVE_WEIGHT_CONFIG"

REQUIRED_POLICY = ("n_min", "z0", "prior", "confidence_z",
                   "w_min", "w_max", "w_neutral")
OPTIONAL_POLICY = ("min_update_days", "source_whitelist")


def config_path() -> Path:
    raw = os.getenv(CONFIG_PATH_ENV, "").strip()
    return Path(raw) if raw else DEFAULT_CONFIG_PATH


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    p = Path(path) if path else config_path()
    with open(p, encoding="utf-8") as fh:
        cfg = json.load(fh)
    if not isinstance(cfg, dict):
        raise ValueError("effective weight config must be an object")
    if cfg.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("schema_version mismatch: %r != %r" % (
            cfg.get("schema_version"), SCHEMA_VERSION))
    if cfg.get("formula") != FORMULA:
        raise ValueError("formula mismatch: %r != %r" % (cfg.get("formula"), FORMULA))
    pol = cfg.get("policy")
    if not isinstance(pol, dict):
        raise ValueError("policy must be an object")
    for name in REQUIRED_POLICY + OPTIONAL_POLICY:
        if name not in pol:
            raise ValueError("policy missing %s" % name)
    return copy.deepcopy(cfg)


def _result(state: str, reason: str, *, weight: Optional[float] = None,
            components: Optional[Dict[str, Any]] = None,
            version: Optional[str] = None) -> Dict[str, Any]:
    return {"state": state, "reason": reason, "weight": weight,
            "components": components or {}, "weight_policy_version": version}


def _registered(pol: Dict[str, Any]) -> List[str]:
    missing: List[str] = []
    for name in REQUIRED_POLICY:
        if pol.get(name) is None:
            missing.append(name)
    return missing


def _numeric(value: Any, *, integer: bool = False) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if integer and not isinstance(value, int):
        return False
    return True


def _param_ok(pol: Dict[str, Any]) -> Optional[str]:
    if not _numeric(pol.get("n_min"), integer=True) or pol["n_min"] < 1:
        return "invalid_n_min"
    for name in ("z0", "confidence_z"):
        if not _numeric(pol.get(name)) or pol[name] <= 0:
            return "invalid_shrink_or_z"
    prior = pol.get("prior")
    if not _numeric(prior) or not (0.0 <= prior <= 1.0):
        return "invalid_prior"
    wmin, wmax, wn = pol.get("w_min"), pol.get("w_max"), pol.get("w_neutral")
    if not (_numeric(wmin) and _numeric(wmax)) or wmin > wmax:
        return "invalid_range"
    if not _numeric(wn) or not (wmin <= wn <= wmax):
        return "invalid_neutral"
    days = pol.get("min_update_days")
    if days is not None and not (_numeric(days, integer=True) and days >= 0):
        return "invalid_min_update_days"
    return None


def _wilson_lower(p: float, n: int, z: float) -> float:
    denom = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / denom
    margin = (z / denom) * math.sqrt(
        p * (1.0 - p) / n + z * z / (4.0 * n * n))
    return max(0.0, center - margin)


def compute(stats: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None, *,
            source_kind: Optional[str] = None,
            applicability: Optional[str] = None,
            as_of: Optional[str] = None,
            last_matured_at: Optional[str] = None,
            now: Optional[str] = None) -> Dict[str, Any]:
    """由调用方算好的统计量求 effective_weight；任一口径缺失即 fail-closed。

    stats 关键键：success_count / failure_count / partial_count（可计分结果）、
    matured_attributable（成熟可归因数）、n_clusters（独立交易日簇）、
    prediction_eligible（目标/口径匹配数）、attributable（可归因数）。
    """
    if cfg is None:
        try:
            cfg = load_config()
        except (OSError, ValueError, json.JSONDecodeError) as e:
            return _result("undecidable", "config_unavailable:%s" % type(e).__name__)
    if cfg.get("schema_version") != SCHEMA_VERSION:
        return _result("undecidable", "schema_mismatch")
    if cfg.get("formula") != FORMULA:
        return _result("undecidable", "formula_mismatch")
    pol = cfg.get("policy", {})
    missing = _registered(pol)
    if missing:
        return _result("undecidable", "not_preregistered",
                       components={"missing": missing})
    try:
        bad = _param_ok(pol)
    except (TypeError, KeyError, ValueError):
        return _result("undecidable", "invalid_policy")
    if bad:
        return _result("undecidable", bad)

    if source_kind is None:
        return _result("neutral", "source_unknown")
    excluded = set(cfg.get("excluded_sources") or ())
    if source_kind in excluded:
        return _result("neutral", "source_excluded",
                       components={"source_kind": source_kind})
    whitelist = pol.get("source_whitelist")
    if whitelist is not None and source_kind not in whitelist:
        return _result("neutral", "source_not_whitelisted",
                       components={"source_kind": source_kind})

    if applicability != "applicable":
        if applicability in ("not_applicable", "unknown", None):
            return _result("neutral", "condition_not_applicable",
                           components={"applicability": applicability})
        return _result("undecidable", "invalid_applicability",
                       components={"applicability": applicability})

    excluded_states = set(cfg.get("excluded_states") or ())
    state_in = str(stats.get("state") or "")
    if state_in and state_in in excluded_states:
        return _result("neutral", "state_excluded",
                       components={"state": state_in})

    try:
        succ = int(stats.get("success_count") or 0)
        fail = int(stats.get("failure_count") or 0)
        part = int(stats.get("partial_count") or 0)
        clusters = int(stats.get("n_clusters") or 0)
        matured = int(stats.get("matured_attributable") or 0)
        eligible = int(stats.get("prediction_eligible") or 0)
    except (TypeError, ValueError):
        return _result("undecidable", "invalid_stat_type")

    n_eff = min(clusters, matured)
    base = {"n_clusters": clusters, "matured": matured, "n_eff": n_eff,
            "prediction_eligible": eligible, "as_of": as_of}
    if n_eff < pol["n_min"]:
        return _result("neutral", "clusters_below_n_min", components=base)

    min_days = pol.get("min_update_days")
    if min_days is not None:
        if last_matured_at is None or now is None:
            return _result("undecidable", "time_basis_unknown", components=base)
        try:
            from datetime import date as _date
            d0 = _date.fromisoformat(str(last_matured_at)[:10])
            d1 = _date.fromisoformat(str(now)[:10])
            elapsed = (d1 - d0).days
        except (ValueError, TypeError):
            return _result("undecidable", "invalid_time_basis", components=base)
        if elapsed < min_days:
            return _result("neutral", "update_not_due",
                           components=dict(base, elapsed_days=elapsed,
                                           min_update_days=min_days))

    scored = succ + fail + part
    if scored <= 0 or eligible <= 0:
        return _result("neutral", "no_scored_outcome", components=base)
    p_hat = succ / scored
    z0, prior = pol["z0"], pol["prior"]
    p_shrink = (succ + z0 * prior) / (scored + z0)
    lower = _wilson_lower(p_shrink, n_eff, pol["confidence_z"])
    weight = round(min(pol["w_max"], max(pol["w_min"], lower)), 6)
    return _result("computed", "ok", weight=weight,
                   components=dict(base, scored=scored, p_hat=round(p_hat, 6),
                                   p_shrink=round(p_shrink, 6),
                                   wilson_lower=round(lower, 6),
                                   formula=FORMULA),
                   version=SCHEMA_VERSION)


def neutral_placeholder(cfg: Optional[Dict[str, Any]] = None) -> Optional[float]:
    """中性占位值：仅供检索排序的缺省位置，须标注为「非评估值」。"""
    if cfg is None:
        try:
            cfg = load_config()
        except (OSError, ValueError, json.JSONDecodeError):
            return None
    pol = cfg.get("policy", {})
    if _registered(pol):
        return None
    try:
        if _param_ok(pol):
            return None
    except (TypeError, KeyError, ValueError):
        return None
    return pol.get("w_neutral")
