"""chain/power_analysis.py — §9.4 功效审计：TOST 等价检验 / 多次 look 的 α 消耗 / n_min 公式。

纯函数层：实现方案 §9.4 的三项判定——
① TOST 双单侧检验：估计值的 (1-α) CI 完全落在 ±margin 内即「无实质增益」(equivalent)，
   达标可判 no_gain 而非让「证不出有效」永久 pending；
② 多次 look 的族错误率控制：O'Brien-Fleming α spending 与 Holm 步降两种预注册方法，
   禁止看到结果后追加 look；
③ n_min ∝ σ²/mde² 样本量公式：mde 必须预注册、不得用实测效应反推（winner's curse）。

硬纪律：所有 α/power/mde/σ/等效边界/design/method 均来自版本化配置或显式参数，
代码内不填任何业务阈值；配置未预注册（关键值为 null）、schema 不符、参数非法
一律 fail-closed（ok=False + undecidable + reason，不崩溃、不放行）；纯函数，
不碰 DB/LLM、不写库、不接生产调用方、不绑定任何 skill/领域。
"""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = "power-analysis-v1"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().with_name("power_analysis.json")
CONFIG_PATH_ENV = "QD_POWER_ANALYSIS"

ALPHA_SPENDING_METHODS = ("obrien_fleming", "holm")
N_DESIGNS = ("one_sample", "two_sample")


def config_path() -> Path:
    raw = os.getenv(CONFIG_PATH_ENV, "").strip()
    return Path(raw) if raw else DEFAULT_CONFIG_PATH


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    p = Path(path) if path else config_path()
    with open(p, encoding="utf-8") as fh:
        cfg = json.load(fh)
    if not isinstance(cfg, dict) or cfg.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("schema_version mismatch")
    exps = cfg.get("experiments")
    if not isinstance(exps, dict):
        raise ValueError("experiments must be an object")
    return copy.deepcopy(cfg)


def _finite(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def _res(ok: bool, verdict: str, reason: str, **kw: Any) -> Dict[str, Any]:
    return {"ok": ok, "verdict": verdict, "reason": reason, **kw}


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(q: float) -> float:
    lo, hi = -40.0, 40.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if _norm_cdf(mid) < q:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def tost_equivalent(estimate: Any, se: Any, *, margin: Any,
                    alpha: Any) -> Dict[str, Any]:
    if not (_finite(estimate) and _finite(se) and _finite(margin) and _finite(alpha)):
        return _res(False, "undecidable", "invalid_param_type")
    est, se_f, margin_f, alpha_f = (float(estimate), float(se),
                                    float(margin), float(alpha))
    if se_f <= 0 or margin_f <= 0 or not (0.0 < alpha_f < 1.0):
        return _res(False, "undecidable", "invalid_param_range")
    z = _norm_ppf(1.0 - alpha_f)
    lo, hi = est - z * se_f, est + z * se_f
    equivalent = (lo > -margin_f) and (hi < margin_f)
    return _res(True, "equivalent" if equivalent else "not_equivalent", "ok",
                estimate=est, se=se_f, margin=margin_f,
                ci_low=lo, ci_high=hi, z_crit=round(z, 6))


def obrien_fleming_alpha(look: Any, total_looks: Any, *,
                         alpha: Any) -> Dict[str, Any]:
    if not (_finite(look) and _finite(total_looks) and _finite(alpha)):
        return _res(False, "undecidable", "invalid_param_type")
    look_i, total_i, alpha_f = int(look), int(total_looks), float(alpha)
    if (look_i != look or total_i != total_looks or look_i < 1 or total_i < 1
            or look_i > total_i or not (0.0 < alpha_f < 1.0)):
        return _res(False, "undecidable", "invalid_param_range")
    z_half = _norm_ppf(1.0 - alpha_f / 2.0)
    z_boundary = z_half * math.sqrt(total_i / look_i)
    spent = 2.0 * (1.0 - _norm_cdf(z_boundary))
    return _res(True, "ok", "ok", look=look_i, total_looks=total_i,
                alpha_spent=spent, z_boundary=round(z_boundary, 6))


def holm_stepdown(p_values: Any, *, alpha: Any) -> Dict[str, Any]:
    if not isinstance(p_values, (list, tuple)) or not _finite(alpha):
        return _res(False, "undecidable", "invalid_param_type")
    alpha_f = float(alpha)
    if not (0.0 < alpha_f < 1.0):
        return _res(False, "undecidable", "invalid_param_range")
    if not all(_finite(p) and 0.0 <= float(p) <= 1.0 for p in p_values):
        return _res(False, "undecidable", "invalid_p_value")
    size = len(p_values)
    if size == 0:
        return _res(True, "ok", "empty", rejections=[], thresholds=[])
    order = sorted(range(size), key=lambda i: float(p_values[i]))
    rejected = [False] * size
    thresholds: List[Optional[float]] = [None] * size
    stopped = False
    for rank, idx in enumerate(order):
        thr = alpha_f / (size - rank)
        thresholds[idx] = round(thr, 8)
        if not stopped and float(p_values[idx]) <= thr:
            rejected[idx] = True
        else:
            stopped = True
    return _res(True, "ok", "ok", rejections=rejected, thresholds=thresholds,
                n_rejected=sum(rejected))


def required_n(sigma: Any, mde: Any, *, alpha: Any, power: Any,
               design: str = "two_sample") -> Dict[str, Any]:
    if not (_finite(sigma) and _finite(mde) and _finite(alpha) and _finite(power)):
        return _res(False, "undecidable", "invalid_param_type")
    sigma_f, mde_f = float(sigma), float(mde)
    alpha_f, power_f = float(alpha), float(power)
    if (sigma_f <= 0 or mde_f <= 0 or not (0.0 < alpha_f < 1.0)
            or not (0.0 < power_f < 1.0)):
        return _res(False, "undecidable", "invalid_param_range")
    if design not in N_DESIGNS:
        return _res(False, "undecidable", "invalid_design")
    z_alpha = _norm_ppf(1.0 - alpha_f / 2.0)
    z_power = _norm_ppf(power_f)
    factor = 2.0 if design == "two_sample" else 1.0
    raw = factor * (z_alpha + z_power) ** 2 * (sigma_f / mde_f) ** 2
    return _res(True, "ok", "ok", n_required=int(math.ceil(raw)), raw=raw,
                factor=factor, z_alpha=round(z_alpha, 6),
                z_power=round(z_power, 6), design=design)


def audit_from_config(experiment: str, *, path: Optional[str] = None,
                      estimate: Any = None, se: Any = None,
                      look: Any = None, total_looks: Any = None,
                      p_values: Any = None, n_min: Any = None,
                      alpha: Any = None, power: Any = None, mde: Any = None,
                      sigma: Any = None, method: Any = None,
                      design: Any = None) -> Dict[str, Any]:
    try:
        cfg = load_config(path)
    except (OSError, ValueError) as e:
        return _res(False, "undecidable", "config_invalid", detail=str(e)[:120])
    exps = cfg.get("experiments", {})
    if experiment not in exps:
        return _res(False, "undecidable", "unknown_experiment")
    entry = exps[experiment]
    a = alpha if alpha is not None else entry.get("alpha")
    pw = power if power is not None else entry.get("power")
    md = mde if mde is not None else entry.get("mde")
    sg = sigma if sigma is not None else entry.get("sigma")
    meth = method if method is not None else entry.get("alpha_spending_method")
    des = design if design is not None else entry.get("n_design")
    margin = entry.get("equivalence_margin")
    if any(v is None for v in (a, pw, md, sg, meth, des, margin)):
        return _res(False, "undecidable", "not_preregistered")
    if meth not in ALPHA_SPENDING_METHODS:
        return _res(False, "undecidable", "unknown_method")
    out: Dict[str, Any] = {"ok": True, "experiment": experiment,
                           "alpha": a, "mde": md, "method": meth}
    if estimate is not None and se is not None:
        out["tost"] = tost_equivalent(estimate, se, margin=margin, alpha=a)
    if meth == "obrien_fleming":
        if look is None or total_looks is None:
            return _res(False, "undecidable", "look_missing")
        out["alpha_spending"] = obrien_fleming_alpha(look, total_looks, alpha=a)
    else:
        if p_values is None:
            return _res(False, "undecidable", "p_values_missing")
        out["holm"] = holm_stepdown(p_values, alpha=a)
    if n_min is not None:
        rn = required_n(sg, md, alpha=a, power=pw, design=des)
        out["required_n"] = rn
        if rn.get("ok"):
            out["n_min_adequate"] = int(n_min) >= rn["n_required"]
    return out
