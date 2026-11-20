"""chain/experiment_config.py — P1 版本化实验判定策略（方案 §9.3/§9.4）。

纯函数层：只读一份版本化 JSON 配置，按预注册阈值判定经验消费档位升降。
不碰 DB、不调 LLM、不跑实验、不写库；统计量由调用方算好传入。

硬纪律：
- 代码内不填任何 δ / mde / n_min 数值（禁硬编码），全部来自配置文件；
- 配置未预注册（关键阈值为 null）或 schema 版本不符 ⇒ undecidable，
  维持当前档位、记录原因，绝不默认放行（fail-closed）；
- 一次只升一档 observe→shadow→active，不跨级；人保留否决与撤回权；
- 端点不匹配、look 超上限、簇数封顶均返回 inconclusive 而非放行。

档位与证据成熟度正交，本模块只决定 consumption_mode，不改 evidence_status。
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

SCHEMA_VERSION = "experiment-config-v1"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().with_name("experiment_config.json")
CONFIG_PATH_ENV = "QD_EXPERIMENT_CONFIG"

EXPERIMENT_KEYS = ("feedback_iteration", "experience_injection")
THRESHOLD_KEYS = ("delta_pred", "delta_brier", "delta_return",
                  "delta_drawdown", "mde", "power", "n_min", "n_cap",
                  "max_looks")
PRIMARY_THRESHOLDS = {
    "direction_success_rate": ("delta_pred",),
    "brier": ("delta_brier",),
    "net_excess_return": ("delta_return", "delta_drawdown"),
}
ENDPOINTS = tuple(PRIMARY_THRESHOLDS)
MODES = ("observe", "shadow", "active")


def config_path() -> Path:
    """配置路径：env QD_EXPERIMENT_CONFIG 可覆盖，便于用外部定档文件。"""
    raw = os.getenv(CONFIG_PATH_ENV, "").strip()
    return Path(raw) if raw else DEFAULT_CONFIG_PATH


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """加载配置并做结构校验；返回深拷贝。结构非法抛 ValueError。"""
    p = Path(path) if path else config_path()
    with open(p, encoding="utf-8") as fh:
        cfg = json.load(fh)
    if not isinstance(cfg, dict):
        raise ValueError("experiment config must be a JSON object")
    if cfg.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("schema_version mismatch: %r != %r" % (
            cfg.get("schema_version"), SCHEMA_VERSION))
    exps = cfg.get("experiments")
    if not isinstance(exps, dict):
        raise ValueError("experiments must be an object")
    for key in EXPERIMENT_KEYS:
        if key not in exps:
            raise ValueError("missing experiment: %s" % key)
        exps[key]["thresholds"] = _check_thresholds(exps[key].get("thresholds"), key)
    return copy.deepcopy(cfg)


def _numeric(value: Any, *, integer: bool = False) -> bool:
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    if integer and not isinstance(value, int):
        return False
    return True


def _check_thresholds(raw: Any, key: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("%s.thresholds must be an object" % key)
    for name in THRESHOLD_KEYS:
        if name not in raw:
            raise ValueError("%s.thresholds missing %s" % (key, name))
        value = raw[name]
        if value is not None and not _numeric(
                value, integer=name in ("n_min", "n_cap", "max_looks")):
            raise ValueError("%s.thresholds.%s must be numeric" % (key, name))
    return dict(raw)


def registration_status(cfg: Dict[str, Any], experiment: str) -> Dict[str, Any]:
    """报告某实验是否完成预注册：缺哪个字段就列哪个，不擅自补值。"""
    exp = cfg.get("experiments", {}).get(experiment, {})
    endpoint = exp.get("primary_endpoint")
    thr = exp.get("thresholds", {})
    missing = []
    if endpoint not in ENDPOINTS:
        missing.append("primary_endpoint")
    for name in PRIMARY_THRESHOLDS.get(endpoint, ()):
        if thr.get(name) is None:
            missing.append(name)
    for name in ("mde", "n_min"):
        if thr.get(name) is None:
            missing.append(name)
    return {"experiment": experiment, "registered": not missing,
            "endpoint": endpoint, "missing": missing}


def _decision(mode: str, verdict: str, reason: str,
              evidence: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"consumption_mode": mode, "verdict": verdict,
            "reason": reason, "evidence": evidence or {}}


def decide_mode(cfg: Dict[str, Any], experiment: str, current_mode: str = "observe",
                *, endpoint: Optional[str] = None,
                ci_low: Optional[float] = None, ci_high: Optional[float] = None,
                n_clusters: Optional[int] = None, looks: Optional[int] = None,
                drawdown_ci_high: Optional[float] = None) -> Dict[str, Any]:
    """按预注册阈值判定档位升降；证据不足一律维持 current_mode。

    ci_low/ci_high 是主端点 A−B 的单侧 95% CI；方向成功率看 ci_low>δ_pred，
    Brier 看 ci_high<−δ_brier，净超额收益还要求回撤 ci 上界<δ_drawdown。
    """
    if cfg.get("schema_version") != SCHEMA_VERSION:
        return _decision(current_mode, "undecidable", "schema_mismatch")
    if experiment not in cfg.get("experiments", {}):
        return _decision(current_mode, "undecidable", "unknown_experiment")
    if current_mode not in MODES:
        return _decision(current_mode, "undecidable", "invalid_current_mode")
    if current_mode == "active":
        return _decision(current_mode, "no_change", "already_active")

    status = registration_status(cfg, experiment)
    if not status["registered"]:
        return _decision(current_mode, "undecidable",
                         "not_preregistered", {"missing": status["missing"]})

    exp = cfg["experiments"][experiment]
    reg_endpoint = exp["primary_endpoint"]
    if endpoint != reg_endpoint:
        return _decision(current_mode, "undecidable", "endpoint_mismatch",
                         {"registered": reg_endpoint, "given": endpoint})

    thr = exp["thresholds"]
    if any(not _numeric(thr.get(name))
           for name in PRIMARY_THRESHOLDS.get(reg_endpoint, ())):
        return _decision(current_mode, "undecidable", "invalid_threshold_type")
    if any(thr.get(name) is not None and not _numeric(thr[name], integer=True)
           for name in ("n_min", "n_cap", "max_looks")):
        return _decision(current_mode, "undecidable", "invalid_threshold_type")
    if any(v is None for v in (ci_low, ci_high, n_clusters, looks)):
        return _decision(current_mode, "undecidable", "evidence_incomplete")
    if not (_numeric(ci_low) and _numeric(ci_high) and
            _numeric(n_clusters, integer=True) and _numeric(looks, integer=True)):
        return _decision(current_mode, "undecidable", "invalid_evidence_type")
    if drawdown_ci_high is not None and not _numeric(drawdown_ci_high):
        return _decision(current_mode, "undecidable", "invalid_evidence_type")
    n_min, n_cap, max_looks = (thr["n_min"], thr["n_cap"], thr["max_looks"])
    if any(v is None for v in (n_min, n_cap, max_looks)):
        return _decision(current_mode, "undecidable", "threshold_incomplete")

    evidence = {"n_clusters": n_clusters, "ci_low": ci_low,
                "ci_high": ci_high, "n_min": n_min}
    if n_clusters < n_min:
        return _decision(current_mode, "inconclusive", "clusters_below_n_min",
                         evidence)
    if (n_cap is not None and n_clusters > n_cap) or (
            max_looks is not None and looks > max_looks):
        return _decision(current_mode, "inconclusive", "capped", evidence)

    passed = False
    if endpoint == "direction_success_rate":
        passed = ci_low > thr["delta_pred"]
    elif endpoint == "brier":
        passed = ci_high < -thr["delta_brier"]
    elif endpoint == "net_excess_return":
        passed = (ci_low > -thr["delta_return"] and
                  drawdown_ci_high is not None and
                  drawdown_ci_high < thr["delta_drawdown"])
    if not passed:
        return _decision(current_mode, "no_gain", "threshold_not_met", evidence)

    target = MODES[MODES.index(current_mode) + 1]
    return _decision(target, "promote", "threshold_met", evidence)
