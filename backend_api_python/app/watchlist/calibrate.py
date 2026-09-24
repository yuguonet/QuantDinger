# -*- coding: utf-8 -*-
"""app/watchlist/calibrate.py — 预测分拟合 / 衰减检测 / 质量门 / 落盘

产品口径（用户裁定 2026-09-25）：
  **只保当前与未来的预测力，不管历史分数是否跨版本可比。**
  换系数 = 全量重刷当前自选分；旧分不必强行对齐。

职责：
  1. `build_samples`   — as-of 无未来函数的 (X, y, r_{t+1})
  2. `fit_logit`       — sklearn L2（lazy import，运行时无 sklearn 也能跑打分）
  3. `evaluate`        — AUC / 五分位（检测衰减、拦烂模型）
  4. `detect_decay`    — 冻结权重在近窗上的 AUC
  5. `recalibrate_if_needed` — 衰减则重标；质量门不过则**不**落盘
  6. `apply_weights`   — 写 `pred_weights.json` 并 `score_version += 1`

纪律
  - 本文件**不 import** `app.agent.*` / `app.market_cn.auto.*`
  - sklearn / 重标路径与打分路径隔离：`model.py` 只读 JSON，不依赖本模块
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.utils.logger import get_logger
from app.watchlist import model

logger = get_logger(__name__)

WEIGHTS_PATH = Path(__file__).with_name("pred_weights.json")

#: 近窗 AUC 低于此值 ⇒ 判定衰减，触发重标
AUC_DECAY_THRESHOLD = 0.52
#: 新模型上线门（近窗 AUC 须达到）
AUC_LIVE_FLOOR = 0.55
#: 重标训练：每票 K 线根数 / 采样步长
FIT_LIMIT = 300
FIT_STEP = 3
#: 衰减检测只用最近多少根（避免用太久远的分布）
EVAL_LIMIT = 120
EVAL_STEP = 2
#: 质量门：五分位收益极差（bp）
QUINTILE_SPREAD_FLOOR = 10.0


def _sigmoid(z: float) -> float:
    if z >= 30:
        return 1.0
    if z <= -30:
        return 0.0
    return 1.0 / (1.0 + math.exp(-z))


def load_weights_file() -> Optional[Dict[str, Any]]:
    try:
        return json.loads(WEIGHTS_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[label.cal] 权重文件读取失败: %s", e)
        return None


def apply_weights(payload: Dict[str, Any], *, reason: str = "") -> Dict[str, Any]:
    """落盘 + 热更新 model 常量 + score_version 自增。只影响当前/未来打分。"""
    cur = model.SCORE_VERSION
    payload = dict(payload)
    payload["score_version"] = cur + 1
    payload["applied_at"] = datetime.now().isoformat(sep=" ")
    payload["apply_reason"] = reason or payload.get("apply_reason") or ""
    WEIGHTS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    model.load_pred_weights(payload)
    logger.info("[label.cal] 已上线新系数 score_version %s→%s reason=%s",
                cur, payload["score_version"], reason or "-")
    return payload


def _features_row(sfeats, mfeats, br, zh) -> Optional[Dict[str, float]]:
    x: Dict[str, float] = {}
    for k, v in (mfeats or {}).items():
        if v is not None and k in model.MARKET_LOGIT_W:
            x[f"M:{k}"] = float(v)
    for k, v in (sfeats or {}).items():
        if v is not None and k in model.STOCK_LOGIT_W:
            x[f"S:{k}"] = float(v)
    regime = (br or {}).get("regime") or "unknown"
    x["R:inv"] = 1.0 if regime == "inverse" else 0.0
    x["R:indep"] = 1.0 if regime == "independent" else 0.0
    if (zh or {}).get("score") is not None:
        x["Z:score"] = float(zh["score"]) / 100.0
    return x or None


def _asof_market(market_klines: Sequence[dict], t_time: int) -> List[dict]:
    out = []
    for k in market_klines:
        try:
            if int(k["time"]) <= int(t_time):
                out.append(k)
        except Exception:
            continue
    return out


def build_samples(klines_list: List[List[dict]], market_klines: List[dict],
                  *, limit: int = FIT_LIMIT, step: int = FIT_STEP
                  ) -> Tuple[List[Dict[str, float]], List[int], List[float]]:
    from app.watchlist.predict import (
        MIN_BARS, market_raw_features, stock_raw_features, beta_regime, zhuang_signal,
    )

    xs: List[Dict[str, float]] = []
    ys: List[int] = []
    fr: List[float] = []
    for ks in klines_list:
        if not ks:
            continue
        if len(ks) > limit:
            ks = ks[-limit:]
        if len(ks) < MIN_BARS + 2:
            continue
        for t in range(MIN_BARS, len(ks) - 1, max(1, step)):
            hist = ks[: t + 1]
            nxt = ks[t + 1]["close"] / ks[t]["close"] - 1 if ks[t]["close"] else 0.0
            m_hist = _asof_market(market_klines, hist[-1]["time"]) if market_klines else []
            sfeats = stock_raw_features(hist, None)
            mfeats = market_raw_features(m_hist) if m_hist else {}
            br = beta_regime(hist, m_hist)
            zh = zhuang_signal(hist, None, m_hist)
            row = _features_row(sfeats, mfeats, br, zh)
            if not row:
                continue
            xs.append(row)
            ys.append(1 if nxt > 0 else 0)
            fr.append(nxt * 1e4)
    return xs, ys, fr


def standardize(X: List[Dict[str, float]]) -> Tuple[List[Dict[str, float]], Dict[str, Tuple[float, float]]]:
    keys = sorted({k for row in X for k in row})
    stats: Dict[str, Tuple[float, float]] = {}
    for k in keys:
        vals = [row[k] for row in X if k in row]
        m = sum(vals) / len(vals)
        s = math.sqrt(sum((v - m) ** 2 for v in vals) / max(1, len(vals) - 1)) or 1.0
        stats[k] = (m, s)
    Xz = [{k: (v - stats[k][0]) / stats[k][1] for k, v in row.items()} for row in X]
    return Xz, stats


def evaluate(p: List[float], y: List[int], fr: List[float]) -> Dict[str, Any]:
    pairs = sorted(zip(p, y, fr), key=lambda t: t[0])
    n = len(pairs)
    if n < 30:
        return {"n": n, "auc": None, "quintiles": [], "spread_bp": None}
    pos, neg = sum(y), n - sum(y)
    auc = None
    if pos and neg:
        rank_sum = 0.0
        for i, (_, yi, _) in enumerate(pairs):
            if yi == 1:
                rank_sum += i + 1
        auc = (rank_sum - pos * (pos + 1) / 2) / (pos * neg)
    quint = []
    for q in range(5):
        seg = pairs[q * n // 5: (q + 1) * n // 5]
        if not seg:
            continue
        win = sum(1 for _, yi, _ in seg if yi == 1) / len(seg)
        ret = sum(r for _, _, r in seg) / len(seg)
        quint.append({"q": q + 1, "win": round(win, 4), "avg_ret_bp": round(ret, 2), "n": len(seg)})
    spread = None
    if len(quint) >= 2:
        spread = quint[-1]["avg_ret_bp"] - quint[0]["avg_ret_bp"]
    return {"n": n, "auc": round(auc, 4) if auc is not None else None,
            "quintiles": quint, "spread_bp": round(spread, 2) if spread is not None else None}


def score_with_current(X_raw: List[Dict[str, float]]) -> List[float]:
    """用**当前** model 线性式给原始特征打 p_up（与线上一致）。"""
    ps = []
    for row in X_raw:
        z = model.PRED_BIAS
        for k, w in model.STOCK_LOGIT_W.items():
            x = row.get(f"S:{k}")
            if x is not None:
                z += w * x
        for k, w in model.MARKET_LOGIT_W.items():
            x = row.get(f"M:{k}")
            if x is not None:
                z += w * x
        z += model.PRED_EXTRA_W["inv"] * row.get("R:inv", 0.0)
        z += model.PRED_EXTRA_W["indep"] * row.get("R:indep", 0.0)
        z += model.PRED_EXTRA_W["zhuang"] * row.get("Z:score", 0.0)
        ps.append(_sigmoid(z))
    return ps


def fit_logit(X: List[Dict[str, float]], y: List[int], l2: float = 1.0) -> Dict[str, float]:
    import numpy as np
    from sklearn.linear_model import LogisticRegression

    keys = sorted({k for row in X for k in row})
    idx = {k: i for i, k in enumerate(keys)}
    Xm = np.zeros((len(X), len(keys)))
    for r, row in enumerate(X):
        for k, v in row.items():
            if k in idx:
                Xm[r, idx[k]] = v
    clf = LogisticRegression(C=1.0 / max(l2, 1e-3), solver="lbfgs", max_iter=2000, random_state=42)
    clf.fit(Xm, np.asarray(y, dtype=int))
    out = {"bias": float(clf.intercept_[0])}
    for k, j in idx.items():
        out[k] = float(clf.coef_[0, j])
    return out


def _map_to_raw(w_z: Dict[str, float], stats: Dict[str, Tuple[float, float]]) -> Dict[str, float]:
    bias = w_z.get("bias", 0.0)
    out: Dict[str, float] = {}
    for k, (m, s) in stats.items():
        wz = w_z.get(k, 0.0)
        out[k] = wz / s
        bias -= wz * m / s
    out["bias"] = bias
    return out


def _payload_from_raw(w_raw: Dict[str, float], report: Dict[str, Any]) -> Dict[str, Any]:
    mkt, stk, extra = {}, {}, {"inv": 0.0, "indep": 0.0, "zhuang": 0.0}
    for k, v in w_raw.items():
        if k == "bias":
            continue
        if k.startswith("M:"):
            mkt[k[2:]] = round(v, 6)
        elif k.startswith("S:"):
            stk[k[2:]] = round(v, 6)
        elif k == "R:inv":
            extra["inv"] = round(v, 6)
        elif k == "R:indep":
            extra["indep"] = round(v, 6)
        elif k == "Z:score":
            extra["zhuang"] = round(v, 6)
    return {
        "fitted_at": datetime.now().isoformat(sep=" "),
        "pred_bias": round(w_raw.get("bias", 0.0), 6),
        "market_logit_w": mkt,
        "stock_logit_w": stk,
        "pred_extra_w": extra,
        "report": report,
    }


def _load_universe_klines(market_klines_out: List[dict], *, limit: int, max_symbols: int = 40
                          ) -> List[List[dict]]:
    from app.services.kline import KlineService
    from app.watchlist.predict import load_market_klines
    from app.watchlist import store

    mk = load_market_klines(max(limit, 250))
    market_klines_out[:] = mk or []

    ks = KlineService()
    pairs = store.list_watchlist_symbols()
    if not pairs:
        pairs = [
            ("CNStock", s) for s in (
                "600519", "601318", "600036", "000858", "601166", "600276", "000333",
                "600030", "601398", "600900", "000651", "601888", "600887", "002415",
                "601012", "600309", "000001", "002594", "601668", "600585",
            )
        ]
    out: List[List[dict]] = []
    for market, symbol in pairs[:max_symbols]:
        try:
            k = ks.get_kline(market=market, symbol=symbol, timeframe="1D", limit=limit)
            if k and len(k) >= 62:
                out.append(list(k))
        except Exception as e:
            logger.debug("[label.cal] kline %s:%s skip: %s", market, symbol, e)
    return out


def detect_decay(*, limit: int = EVAL_LIMIT, step: int = EVAL_STEP) -> Dict[str, Any]:
    """当前权重在近窗上的 AUC/分位。`should_refit=True` ⇒ 判定衰减。"""
    mk: List[dict] = []
    klines_list = _load_universe_klines(mk, limit=limit)
    X, y, fr = build_samples(klines_list, mk, limit=limit, step=step)
    if not X:
        return {"ok": False, "n": 0, "auc": None, "should_refit": False, "reason": "no_samples"}
    p = score_with_current(X)
    rep = evaluate(p, y, fr)
    auc = rep.get("auc")
    spread = rep.get("spread_bp")
    should = (
        auc is not None and auc < AUC_DECAY_THRESHOLD
    ) or (
        spread is not None and spread < QUINTILE_SPREAD_FLOOR
    ) or (auc is None and rep.get("n", 0) >= 50)
    rep.update({
        "ok": True,
        "score_version": model.SCORE_VERSION,
        "auc_floor": AUC_DECAY_THRESHOLD,
        "should_refit": bool(should),
        "reason": "auc_decay" if (auc is not None and auc < AUC_DECAY_THRESHOLD) else (
            "spread_flat" if should else "ok"),
    })
    return rep


def recalibrate(*, limit: int = FIT_LIMIT, step: int = FIT_STEP) -> Dict[str, Any]:
    """重标 + 质量门。门不过返回 applied=False，**不**覆盖线上权重。"""
    mk: List[dict] = []
    klines_list = _load_universe_klines(mk, limit=limit)
    X_raw, y, fr = build_samples(klines_list, mk, limit=limit, step=step)
    if len(X_raw) < 200:
        return {"ok": False, "applied": False, "reason": f"insufficient_samples:{len(X_raw)}"}

    # 质量门用同一份样本评（近窗检测已用 EVAL_LIMIT；这里看全窗拟合后是否明显可用）
    X, stats = standardize(X_raw)
    w_z = fit_logit(X, y, l2=1.0)
    p_new = [_sigmoid(w_z.get("bias", 0.0) + sum(w_z.get(k, 0.0) * v for k, v in row.items()))
             for row in X]
    rep_new = evaluate(p_new, y, fr)
    p_old = score_with_current(X_raw)
    rep_old = evaluate(p_old, y, fr)

    auc_new = rep_new.get("auc") or 0.0
    spread_new = rep_new.get("spread_bp") or 0.0
    gate_ok = auc_new >= AUC_LIVE_FLOOR and spread_new >= QUINTILE_SPREAD_FLOOR
    if not gate_ok:
        logger.warning("[label.cal] 质量门未过，不应用: auc=%.4f spread=%.2f (floor %.2f/%.1f)",
                       auc_new, spread_new, AUC_LIVE_FLOOR, QUINTILE_SPREAD_FLOOR)
        return {"ok": True, "applied": False, "report_new": rep_new, "report_old": rep_old,
                "reason": "quality_gate"}

    payload = _payload_from_raw(_map_to_raw(w_z, stats), rep_new)
    payload["report_old"] = rep_old
    applied = apply_weights(payload, reason=f"auto_recal auc={auc_new:.4f}")
    return {"ok": True, "applied": True, "score_version": applied.get("score_version"),
            "report_new": rep_new, "payload": applied}


def run_auto_calibrate(*, force: bool = False) -> Dict[str, Any]:
    """后台入口：检测衰减 → 必要时重标上线。调用方随后 `write_system_facts()` 全量刷分。

    节流：默认每周最多巡检一次（`force=True` 忽略）。重标本身也只在衰减时触发。
    """
    from datetime import date

    meta = load_weights_file() or {}
    today = date.today().isoformat()
    last = meta.get("last_detect_date")
    if not force and last and last == today:
        return {"detect": {"ok": True, "skipped": "already_today", "should_refit": False}, "recal": None}
    # 周级节流：与上次巡检间隔 < 5 个日历日则跳过（盘后每天会调 run_daily）
    if not force and last:
        try:
            if (date.today() - date.fromisoformat(str(last)[:10])).days < 5:
                return {"detect": {"ok": True, "skipped": "weekly_throttle",
                                   "last_detect_date": last, "should_refit": False}, "recal": None}
        except Exception:
            pass

    det = detect_decay()
    result: Dict[str, Any] = {"detect": det, "recal": None}
    if not det.get("ok"):
        return result

    # 记录巡检日（即使不重标）
    try:
        meta["last_detect_date"] = today
        WEIGHTS_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    if force or det.get("should_refit"):
        logger.info("[label.cal] 触发重标 force=%s reason=%s auc=%s",
                    force, det.get("reason"), det.get("auc"))
        result["recal"] = recalibrate()
    else:
        logger.info("[label.cal] 近窗 AUC=%s 未衰减，跳过重标", det.get("auc"))
    return result
