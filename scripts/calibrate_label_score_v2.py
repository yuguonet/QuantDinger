# -*- coding: utf-8 -*-
"""标定 label 评分 v2：用历史日 K 拟合「次日上涨概率」逻辑回归系数。

目标（与 app/watchlist 一致）：
    y = 1{ close[t+1] > close[t] }     # 次日上涨
    z = X[t] @ w                        # 特征截至 t 收盘
    p = σ(z)                            # 主指标 = P(y=1)
    辅助: exp_ret = E[r_{t+1}]，与 p 单调

用法（在 backend 能 import app 的环境）:
    python scripts/calibrate_label_score_v2.py --universe data/hs300_symbols.txt --out tmp/pred_v2_weights.json

产出:
    - tmp/pred_v2_weights.json  冻结系数
    - 分位单调性 / AUC / 胜率报告（stdout）
    - 将系数**手工**粘贴进 app/watchlist/model.py 后 SCORE_VERSION += 1

纪律: 不自动改 model.py（口径变更必须人工确认 + 版本递增）。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend_api_python"))

from app.watchlist.model import (  # noqa: E402
    MARKET_LOGIT_W, STOCK_LOGIT_W, MIN_W_EFF, SCORE_VERSION,
)
from app.watchlist.predict import (  # noqa: E402
    MIN_BARS, market_raw_features, stock_raw_features, beta_regime, zhuang_signal,
    load_market_klines,
)


def _sigmoid(z: float) -> float:
    if z >= 30:
        return 1.0
    if z <= -30:
        return 0.0
    return 1.0 / (1.0 + math.exp(-z))


def _features_row(sfeats, mfeats, br, zh) -> Optional[Dict[str, float]]:
    """拼逻辑回归设计矩阵一行（仅有效特征）。"""
    x: Dict[str, float] = {}
    for k, v in (mfeats or {}).items():
        if v is not None and k in MARKET_LOGIT_W:
            x[f"M:{k}"] = float(v)
    for k, v in (sfeats or {}).items():
        if v is not None and k in STOCK_LOGIT_W:
            x[f"S:{k}"] = float(v)
    # regime / zhuang 注入
    regime = (br or {}).get("regime") or "unknown"
    x["R:inv"] = 1.0 if regime == "inverse" else 0.0
    x["R:indep"] = 1.0 if regime == "independent" else 0.0
    if (zh or {}).get("score") is not None:
        x["Z:score"] = float(zh["score"]) / 100.0
    return x or None


def _asof_market(market_klines: List[dict], t_time: int) -> List[dict]:
    """截断大盘 K 至不晚于个股 hist 末根 —— **禁止未来函数**。"""
    out = []
    for k in market_klines:
        try:
            if int(k["time"]) <= int(t_time):
                out.append(k)
        except Exception:
            continue
    return out


def collect_xy(klines_list: List[List[dict]], market_klines: List[dict],
               step: int = 3, use_chip: bool = False):
    """样本: (X[t], y=1{r_{t+1}>0}, r_{t+1})。市场/个股特征均严格截至 t 收盘。"""
    xs: List[Dict[str, float]] = []
    ys: List[int] = []
    fr: List[float] = []
    for ks in klines_list:
        if not ks or len(ks) < MIN_BARS + 2:
            continue
        for t in range(MIN_BARS, len(ks) - 1, max(1, step)):
            hist = ks[: t + 1]
            nxt = ks[t + 1]["close"] / ks[t]["close"] - 1 if ks[t]["close"] else 0.0
            m_hist = _asof_market(market_klines, hist[-1]["time"]) if market_klines else []
            chip = None
            if use_chip:
                try:
                    from app.services.chip_service import compute_chip_core
                    c = compute_chip_core(hist, lookback_days=120)
                    chip = None if "error" in c else c
                except Exception:
                    chip = None
            sfeats = stock_raw_features(hist, chip)
            mfeats = market_raw_features(m_hist) if m_hist else {}
            br = beta_regime(hist, m_hist)
            zh = zhuang_signal(hist, chip, m_hist)
            row = _features_row(sfeats, mfeats, br, zh)
            if not row:
                continue
            xs.append(row)
            ys.append(1 if nxt > 0 else 0)
            fr.append(nxt * 1e4)  # bp
    return xs, ys, fr


def standardize(X: List[Dict[str, float]]) -> Tuple[List[Dict[str, float]], Dict[str, Tuple[float, float]]]:
    """z-score（防 IRLS 因量纲跑飞）。返回 (Xz, {k:(mean,std)}）。"""
    keys = sorted({k for row in X for k in row})
    stats: Dict[str, Tuple[float, float]] = {}
    for k in keys:
        vals = [row[k] for row in X if k in row]
        m = sum(vals) / len(vals)
        s = math.sqrt(sum((v - m) ** 2 for v in vals) / max(1, len(vals) - 1)) or 1.0
        stats[k] = (m, s)
    Xz = []
    for row in X:
        Xz.append({k: (v - stats[k][0]) / stats[k][1] for k, v in row.items()})
    return Xz, stats


def fit_logit_irls(X: List[Dict[str, float]], y: List[int], l2: float = 2.0) -> Dict[str, float]:
    """逻辑回归（sklearn L2）。X 需已标准化。手工 IRLS 已弃用（大样本易发散）。"""
    import numpy as np
    from sklearn.linear_model import LogisticRegression

    keys = sorted({k for row in X for k in row})
    idx = {k: i for i, k in enumerate(keys)}
    Xm = np.zeros((len(X), len(keys)))
    for r, row in enumerate(X):
        for k, v in row.items():
            if k in idx:
                Xm[r, idx[k]] = v
    yv = np.asarray(y, dtype=int)
    # C 越小正则越强；l2≈2 → C=0.5
    clf = LogisticRegression(
        C=1.0 / max(l2, 1e-3), penalty="l2", solver="lbfgs",
        max_iter=2000, random_state=42,
    )
    clf.fit(Xm, yv)
    out = {"bias": float(clf.intercept_[0])}
    for k, j in idx.items():
        out[k] = float(clf.coef_[0, j])
    return out


def evaluate(p: List[float], y: List[int], fr: List[float]) -> Dict[str, Any]:
    """AUC / 五分位胜率与平均次日收益（bp）。"""
    pairs = sorted(zip(p, y, fr), key=lambda t: t[0])
    n = len(pairs)
    pos = sum(y)
    neg = n - pos
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
    hit = sum(1 for pi, yi, _ in pairs if (pi >= 0.5) == (yi == 1)) / n if n else None
    return {"n": n, "base_rate": round(pos / n, 4) if n else None,
            "auc": round(auc, 4) if auc is not None else None,
            "hit_rate@0.5": round(hit, 4) if hit is not None else None,
            "quintiles": quint}


def _map_weights_to_raw(w_z: Dict[str, float], stats: Dict[str, Tuple[float, float]]) -> Dict[str, float]:
    """标准化权重 → 原始量纲：w_raw = w_z / std, bias_raw = bias - Σ w_z*mean/std。"""
    bias = w_z.get("bias", 0.0)
    out = {}
    for k, (m, s) in stats.items():
        wz = w_z.get(k, 0.0)
        out[k] = wz / s
        bias -= wz * m / s
    out["bias"] = bias
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols-file", required=True, help="每行 market:symbol，如 CNStock:600519")
    ap.add_argument("--out", default="tmp/pred_v2_weights.json")
    ap.add_argument("--limit", type=int, default=400, help="每票K线根数")
    ap.add_argument("--step", type=int, default=3, help="训练窗采样步长（隔天）")
    ap.add_argument("--use-chip", action="store_true", help="逐日重算筹码（慢，仅终标用）")
    ap.add_argument("--max-symbols", type=int, default=0, help=">0 时只取前 N 只")
    args = ap.parse_args()

    from app.services.kline import KlineService
    ks = KlineService()
    market_klines = load_market_klines(400)
    klines_list = []
    count = 0
    for line in Path(args.symbols_file).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if args.max_symbols and count >= args.max_symbols:
            break
        m, s = line.split(":", 1)
        try:
            k = ks.get_kline(market=m, symbol=s, timeframe="1D", limit=args.limit)
            if k and len(k) >= MIN_BARS + 2:
                klines_list.append(k)
                count += 1
                print(f"loaded {count}: {line} bars={len(k)}")
            else:
                print(f"skip {line}: bars={len(k) if k else 0}")
        except Exception as e:
            print(f"skip {line}: {e}")

    X_raw, y, fr = collect_xy(klines_list, market_klines, step=args.step, use_chip=args.use_chip)
    print(f"samples={len(X_raw)} pos_rate={sum(y)/len(y) if y else 0:.3f}")
    X, stats = standardize(X_raw)
    w_z = fit_logit_irls(X, y, l2=1.0)
    w_raw = _map_weights_to_raw(w_z, stats)

    # 打成 model.py 权重格式
    mkt, stk, extra = {}, {}, {}
    for k, v in w_raw.items():
        if k == "bias":
            mkt["bias"] = round(v, 6)
        elif k.startswith("M:"):
            mkt[k[2:]] = round(v, 6)
        elif k.startswith("S:"):
            stk[k[2:]] = round(v, 6)
        else:
            extra[k] = round(v, 6)

    # 用**标准化空间**打分再评估（与训练一致）
    p = []
    for row in X:
        z = w_z.get("bias", 0.0) + sum(w_z.get(k, 0.0) * v for k, v in row.items())
        p.append(_sigmoid(z))
    report = evaluate(p, y, fr)
    out = {
        "score_version_next": SCORE_VERSION + 1,
        "fitted_at": datetime.now().isoformat(sep=" "),
        "market_logit_w": mkt,
        "stock_logit_w": stk,
        "extra_w": extra,
        "feature_stats": {k: {"mean": round(m, 8), "std": round(s, 8)} for k, (m, s) in stats.items()},
        "w_standardized": {k: round(v, 6) for k, v in w_z.items()},
        "report": report,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("top |w| (standardized):")
    for k, v in sorted(w_z.items(), key=lambda kv: -abs(kv[1]))[:15]:
        print(f"  {k:20s} {v:+.4f}")
    print(f"weights → {args.out}")


if __name__ == "__main__":
    main()
