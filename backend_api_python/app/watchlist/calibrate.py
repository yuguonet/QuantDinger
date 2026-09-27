# -*- coding: utf-8 -*-
"""app/watchlist/calibrate.py — 预测分拟合 / 衰减检测 / 质量门 / 落盘

产品口径（用户裁定 2026-09-25）：
  **只保当前与未来的预测力，不管历史分数是否跨版本可比。**
  换系数 = 全量重刷当前自选分；旧分不必强行对齐。

评估口径（用户裁定 2026-09-26）：
  **实盘是同一天横向挑票 ⇒ 评估必须走「同日截面」，不能走全样本 pooled。**
    · pooled AUC 把「预测哪天」的信息也记成了预测力（日子效应），系统性虚高；
      且不同日的分直接可比这件事本身不成立。
    · 拟合必须在训练窗做，截距与 z-score 标准化统计量**只允许来自训练集**；
    · 质量门只看**验证窗 + 同日截面**上的 AUC / rank-IC；
    · 衰减检测只在**上次拟合窗之后**的日子上做，否则等于在拟合窗里自测。
  `evaluate`（pooled）保留只为对照与旧报告兼容，**不参与任何门的判定**。

职责：
  1. `build_samples`   — as-of 无未来函数的 (X, y, r_{t+1}, day)
  2. `split_by_day`    — 按交易日切训练/验证（天然 embargo，不带交叉泄漏）
  3. `fit_logit`       — sklearn L2（lazy import，运行时无 sklearn 也能跑打分）
  4. `evaluate`        — 全样本 pooled 口径（只作对照）
  5. `evaluate_xs`     — **同日截面**口径：逐日 AUC / Spearman rank-IC → 跨日聚合
  6. `detect_decay`    — 冻结权重在拟合窗之后的日子上的截面表现
  7. `recalibrate`     — 训练窗拟合；质量门（验证窗截面）不过则**不**落盘
  8. `apply_weights`   — 写 `pred_weights.json` 并 `score_version += 1`

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

# ── 评估口径：同日截面（用户裁定 2026-09-26）────────────────────────────
#: 单日截面样本数下限：低于此值该日不计入截面统计（分不出 AUC / 五分位）
XS_MIN_DAY_N = 5
#: 单日算**五分位收益差**的样本数下限：5 只票分 5 档每档 1 只 ⇒ spread 纯噪声，
#: 所以分位和 AUC/IC 用不同的门槛（AUC/IC 在 5 只时仍是有定义的统计量）。
XS_MIN_DAY_N_QUINT = 10
#: 时序切分：前 `TRAIN_DAY_RATIO` 的交易日进训练窗，其余进验证窗
TRAIN_DAY_RATIO = 0.7
#: 衰减判定：验证窗**同日截面**平均 AUC 低于此值 ⇒ 触发重标。
#: ⚠ 该阈值贴着噪声地板（实测现行权重全样本截面 AUC 0.5278，日间波动 std(IC)≈0.26
#:   ⇒ 42 日均值的标准误在 0.03 量级）。所以它会不定期误触发，真正的把关在
#:   `recalibrate` 的验证窗质量门：触发只是"去试着重拟合一次"，不等于能上线。
AUC_DECAY_THRESHOLD_XS = 0.52
#: 上线门：验证窗同日截面平均 AUC / 平均 rank-IC 须**同时**达到。
#: 0.54 的取值：42 日验证窗下 mean-AUC 的噪声标准误约 0.02~0.03，0.54 要求预测力
#: 高于随机约 1.5σ；IC 门槛则取自裸 `s_mom5` 单因子的实测水平 0.0299 —— 花很大力气
#: 堆出来的全套模型，至少要够得着一个裸因子。
AUC_LIVE_FLOOR_XS = 0.54
IC_LIVE_FLOOR = 0.02
#: 不得显著劣化：新模型验证窗截面 AUC 允许低于旧模型的容差。
#: 取值依据：实测日间 rank-IC 标准差 ≈0.26、验证窗 42 个交易日 ⇒ 跨日均值的
#: 不确定度在 0.03 量级；小于此幅度的差距不可辨识，判为噪声而不是"真的变差了"。
XS_REGRESSION_TOL = 0.02

#: 重标训练：每票 K 线根数 / 采样步长 / 训练池标的上限
FIT_LIMIT = 300
FIT_STEP = 3
FIT_MAX_SYMBOLS = 40
#: 衰减检测：只用最近多少根（避免用太久远的分布）
EVAL_LIMIT = 120
EVAL_STEP = 2
EVAL_MAX_SYMBOLS = 40
#: 质量门：验证窗五分位收益极差（bp）
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
                  ) -> Tuple[List[Dict[str, float]], List[int], List[float], List[str]]:
    """as-of 样本。返回 `(X, y, r_{t+1}, days)`。

    `days[i]` = 第 i 个样本的**所属交易日**（决策日）。它是截面评估与训练/验证切分的
    唯一依据：两个样本同日 ⇒ 它们在 metric 里必须被放在一起比，在切分时必须同进同出。
    """
    from datetime import datetime as _dt

    from app.watchlist.predict import (
        MIN_BARS, market_raw_features, stock_raw_features, beta_regime, zhuang_signal,
    )

    xs: List[Dict[str, float]] = []
    ys: List[int] = []
    fr: List[float] = []
    ds: List[str] = []
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
            ds.append(_dt.fromtimestamp(int(ks[t]["time"])).strftime("%Y-%m-%d"))
    return xs, ys, fr, ds


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


def _auc(p: Sequence[float], y: Sequence[int]) -> Optional[float]:
    """AUC（ Mann-Whitney U 形式）。单一类别（全涨/全跌）⇒ None。**不参与质量门**。"""
    pairs = sorted(zip(p, y), key=lambda t: t[0])
    n = len(pairs)
    pos = sum(1 for _, yi in pairs if yi == 1)
    neg = n - pos
    if not pos or not neg:
        return None
    rank_sum = 0.0
    for i, (_, yi) in enumerate(pairs):
        if yi == 1:
            rank_sum += i + 1
    return (rank_sum - pos * (pos + 1) / 2) / (pos * neg)


def _quintiles(p: Sequence[float], y: Sequence[float], fr: Sequence[float]) -> List[Dict[str, Any]]:
    pairs = sorted(zip(p, y, fr), key=lambda t: t[0])
    n = len(pairs)
    out = []
    for q in range(5):
        seg = pairs[q * n // 5: (q + 1) * n // 5]
        if not seg:
            continue
        out.append({
            "q": q + 1,
            "win": round(sum(1 for _, yi, _ in seg if yi == 1) / len(seg), 4),
            "avg_ret_bp": round(sum(r for _, _, r in seg) / len(seg), 2),
            "n": len(seg),
        })
    return out


def evaluate(p: List[float], y: List[int], fr: List[float]) -> Dict[str, Any]:
    """**全样本 pooled** 口径（AUC / 五分位收益）。

    ⚠ 只作历史对照与旧报告兼容，**禁止用于任何门的判定**：
    样本横跨成百个交易日，pooled 会把「预测哪天」也算成预测力。
    质量门一律走 [`evaluate_xs`]。
    """
    n = len(p)
    if n < 30:
        return {"n": n, "auc": None, "quintiles": [], "spread_bp": None}
    auc = _auc(p, y)
    quint = _quintiles(p, y, fr)
    spread = (quint[-1]["avg_ret_bp"] - quint[0]["avg_ret_bp"]) if len(quint) >= 2 else None
    return {"n": n, "auc": round(auc, 4) if auc is not None else None,
            "quintiles": quint, "spread_bp": round(spread, 2) if spread is not None else None}


# ═══════════════════════════════════════════════════════════════════
# 5b. 同日截面评估（质量门唯一依据）
# ═══════════════════════════════════════════════════════════════════

def _ranks(xs: Sequence[float]) -> List[float]:
    """平均秩（并列取均值），供 Spearman 用。"""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def _pearson(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    n = len(a)
    if n < 3:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    if da <= 0 or db <= 0:
        return None
    return num / (da * db)


def spearman_ic(p: Sequence[float], fr: Sequence[float]) -> Optional[float]:
    """单日 Spearman rank-IC：pred 秩 vs 次日收益秩。

    比 AUC 更贴实盘——它直接衡量「当天把自选股按分排序，是否真的对应了次日收益排序」，
    且不要求当日既有涨又有跌。
    """
    if len(p) < 3:
        return None
    return _pearson(_ranks(p), _ranks(fr))


def evaluate_xs(days: Sequence[str], p: Sequence[float], y: Sequence[int],
                fr: Sequence[float], *, min_day_n: int = XS_MIN_DAY_N) -> Dict[str, Any]:
    """**同日截面**口径评估 → 跨日聚合。这是所有质量门的判定依据。

    做法：按 `days` 分组，组内算 截面AUC / rank-IC / 五分位收益差，再对**日**取均值。

    为什么必须这样：
      · 实盘动作是「今天从自选股里挑几只」，比较只发生在同一天内；
      · 跨日直接比分数没有意义（大盘日涨跌对不同日子的分是不可比的）；
      · 某日个股全涨或全跌时截面 AUC 无定义 ⇒ 当日 AUC 跳过，但 IC 仍能算。

    返回字段：
      `auc_xs`          各日截面 AUC 的均值（**主**）
      `ic_mean` / `ic_ir`  rank-IC 均值 / 信息比（均值 ÷ 日间标准差 × √天数）
      `day_auc_win_rate` 截面 AUC > 0.5 的天数占比（稳定性，比均值更难作弊）
      `spread_bp_xs`     各日 Q5−Q1 收益差的均值（bp；只用样本数 ≥ `XS_MIN_DAY_N_QUINT` 的日）
    """
    by_day: Dict[str, List[int]] = {}
    for i, d in enumerate(days):
        by_day.setdefault(d, []).append(i)

    aucs: List[float] = []
    ics: List[float] = []
    spreads: List[float] = []
    day_ns: List[int] = []
    skipped_single_class = 0

    for day in sorted(by_day):
        idx = by_day[day]
        if len(idx) < min_day_n:
            continue
        ps = [p[i] for i in idx]
        ys = [y[i] for i in idx]
        fs = [fr[i] for i in idx]
        day_ns.append(len(idx))

        a = _auc(ps, ys)
        if a is None:
            skipped_single_class += 1
        else:
            aucs.append(a)

        ic = spearman_ic(ps, fs)
        if ic is not None:
            ics.append(ic)

        if len(idx) >= max(min_day_n, XS_MIN_DAY_N_QUINT):
            qs = _quintiles(ps, ys, fs)
            if len(qs) >= 2:
                spreads.append(qs[-1]["avg_ret_bp"] - qs[0]["avg_ret_bp"])

    def _mean(v: Sequence[float]) -> Optional[float]:
        return sum(v) / len(v) if v else None

    def _std(v: Sequence[float]) -> Optional[float]:
        if len(v) < 2:
            return None
        m = sum(v) / len(v)
        return math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1))

    srt = sorted(day_ns)
    ic_mean, ic_std = _mean(ics), _std(ics)
    return {
        "n": len(days),
        "n_days": len(by_day),
        "days_counted": len(day_ns),
        "days_skipped_single_class": skipped_single_class,
        "median_day_n": srt[len(srt) // 2] if srt else None,
        "auc_xs": round(_mean(aucs), 4) if aucs else None,
        "day_auc_win_rate": round(sum(1 for a in aucs if a > 0.5) / len(aucs), 4) if aucs else None,
        "ic_mean": round(ic_mean, 4) if ic_mean is not None else None,
        "ic_std": round(ic_std, 4) if ic_std is not None else None,
        "ic_ir": round(ic_mean / ic_std * math.sqrt(len(ics)), 4)
                 if (ic_mean is not None and ic_std) else None,
        "ic_pos_rate": round(sum(1 for x in ics if x > 0) / len(ics), 4) if ics else None,
        "spread_bp_xs": round(_mean(spreads), 2) if spreads else None,
    }


def split_by_day(days: Sequence[str], ratio: float = TRAIN_DAY_RATIO
                 ) -> Tuple[List[int], List[int]]:
    """按交易日切训练/验证下标。返回 `(train_idx, valid_idx)`。

    按「天」而不是随机/按行的原因：同日的样本互为兄弟（同一天的大盘、同一天的情绪），
    随机切会把同一天的票拆到两边 ⇒ 验证集被训练集的**当日共性**污染。
    按天切等价于实盘的「用历史预测未来一天」，并且天然带 embargo（无跨窗重叠）。
    """
    uniq = sorted(set(days))
    if len(uniq) < 5:
        return list(range(len(days))), []
    cut = uniq[int(len(uniq) * ratio)]
    tr = [i for i, d in enumerate(days) if d <= cut]
    va = [i for i, d in enumerate(days) if d > cut]
    if not va:                                   # 日子太少 ⇒ 至少留最后一天验证
        last_day = uniq[-1]
        tr = [i for i, d in enumerate(days) if d != last_day]
        va = [i for i, d in enumerate(days) if d == last_day]
    return tr, va


def _score_z(X_raw: Sequence[Dict[str, float]], stats: Dict[str, Tuple[float, float]],
             w_z: Dict[str, float]) -> List[float]:
    """用 z 空间系数 + **给定**标准化统计量打分。

    `stats` 必须来自训练集（否则就是泄漏）；训练集里没有的特征键 ⇒ 该项不参与（= z 取 0），
    与 `model.predict_from_features` 的缺项处理一致。
    """
    out: List[float] = []
    for row in X_raw:
        z = w_z.get("bias", 0.0)
        for k, v in row.items():
            st = stats.get(k)
            if st is None:
                continue
            m, s = st
            z += w_z.get(k, 0.0) * (v - m) / s
        out.append(_sigmoid(z))
    return out


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


def _universe_pairs(max_symbols: int) -> List[Tuple[str, str]]:
    """训练池 = 自选股并集；自选池为空则用 `_FALLBACK_SYMBOLS`。"""
    from app.watchlist import store

    pairs = store.list_watchlist_symbols()
    if not pairs:
        pairs = [("CNStock", s) for s in _FALLBACK_SYMBOLS]
    return list(pairs[:max_symbols])


def _load_universe_klines(market_klines_out: List[dict], *, limit: int,
                          max_symbols: int = FIT_MAX_SYMBOLS
                          ) -> Tuple[List[List[dict]], Dict[str, Any]]:
    """训练取数。**本地优先**（`LOCAL_FIRST`），本地不足才走远端。

    Returns:
        `(klines_list, info)`：`info` 记录本次每个市场/指数的数据来源与规模，
        会写进 `pred_weights.json` 的 `fit_window`，保证"这版系数是用什么数据拟合的"可追溯。
    """
    pairs = _universe_pairs(max_symbols)
    by_market: Dict[str, List[str]] = {}
    for market, symbol in pairs:
        by_market.setdefault(market, []).append(symbol)

    info: Dict[str, Any] = {"source": {}, "limit": limit, "n_pairs": len(pairs)}
    out: List[List[dict]] = []

    for market, symbols in by_market.items():
        got: Dict[str, List[dict]] = {}
        if LOCAL_FIRST:
            got = _load_local_stock_bars(market, symbols, limit)
        missing = [s for s in symbols if s not in got]

        if missing:                                     # 本地缺口 ⇒ 远端兜底单个补齐
            got.update(_load_remote_stock_bars(market, missing, limit))
        for sym in symbols:
            bars = got.get(sym)
            if bars and len(bars) >= LOCAL_MIN_BARS:
                out.append(bars)
        info["source"][market] = {
            "n_symbols": len(symbols),
            "n_local": len(symbols) - len(missing),
            "n_remote": len(missing),
            "n_used": sum(1 for s in symbols if got.get(s)),
        }

    # 大盘代理：本地磁盘缓存优先
    mk = _load_local_index_bars("000001", max(limit, 250)) if LOCAL_FIRST else []
    info["source"]["index"] = "local_cache" if mk else "remote"
    if not mk:
        from app.watchlist.predict import load_market_klines
        mk = load_market_klines(max(limit, 250)) or []
    market_klines_out[:] = mk
    info["n_market_bars"] = len(mk)
    return out, info


def _load_remote_stock_bars(market: str, symbols: Sequence[str],
                            limit: int) -> Dict[str, List[dict]]:
    """远端兜底（`KlineService`）。本地够用时不会被调用。"""
    if not symbols:
        return {}
    from app.services.kline import KlineService

    ks = KlineService()
    out: Dict[str, List[dict]] = {}
    for sym in symbols:
        try:
            k = ks.get_kline(market=market, symbol=sym, timeframe="1D", limit=limit)
            if k and len(k) >= LOCAL_MIN_BARS:
                out[sym] = list(k)
        except Exception as e:
            logger.debug("[label.cal] 远端 %s:%s skip: %s", market, sym, e)
    return out


#: 本地数据源 = **默认**（数据端裁定 2026-09-26）：
#:   · 个股日线：`CNStock_db.kline_1D_YYYY` 分区表（经 `app.utils.db_market` 只读，零外网）
#:   · 指数日线：`data/market_cn_cache/index/<code>_1d.json`（与 `hub.index_daily` 同一份磁盘缓存）
#: 远端（KlineService / index 降级链）只在本地不足时兜底 —— 校准是要反复跑的离线作业，
#: 打外网既慢（远端 vs 本地 ~1000x）又不可复现，还会把批次量的请求打到免费接口上。
LOCAL_FIRST = True
#: 本地缓存落后多少个日历日就告警（不阻断：全部特征都是 as-of 因果的，只会少最后几天）
LOCAL_STALE_DAYS = 7
#: 单票本地 K 线少于此根数 ⇒ 视为该票本地缺失，转兜底
LOCAL_MIN_BARS = 62
_INDEX_CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "market_cn_cache" / "index"
#: 自选池为空时的兜底训练池（沪深代表性标的，与 optimizer 侧同款）
_FALLBACK_SYMBOLS = (
    "600519", "601318", "600036", "000858", "601166", "600276", "000333",
    "600030", "601398", "600900", "000651", "601888", "600887", "002415",
    "601012", "600309", "000001", "002594", "601668", "600585",
)


def _load_local_index_bars(code: str = "000001", days: int = 400) -> List[dict]:
    """本地指数日线（磁盘缓存）→ label 侧 kline 形状。失败/缺失返回 []。"""
    p = _INDEX_CACHE_DIR / f"{code}_1d.json"
    if not p.is_file():
        return []
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[label.cal] 指数缓存解析失败 %s: %s", p.name, e)
        return []
    bars: List[dict] = []
    for r in (payload.get("bars") or [])[-days:]:
        d, close = str(r.get("date", ""))[:10], r.get("close")
        if not d or close is None:
            continue
        try:
            ts = int(datetime.strptime(d, "%Y-%m-%d").timestamp())
            bars.append({
                "time": ts,
                "open": float(r.get("open") or close),
                "high": float(r.get("high") or close),
                "low": float(r.get("low") or close),
                "close": float(close),
                "volume": float(r.get("volume") or 0),
            })
        except (TypeError, ValueError):
            continue
    if bars:
        last = datetime.fromtimestamp(bars[-1]["time"])
        lag = (datetime.now() - last).days
        if lag > LOCAL_STALE_DAYS:
            logger.warning("[label.cal] 指数缓存偏旧: %s 末根 %s（落后 %d 天）",
                           code, last.date().isoformat(), lag)
    return bars


def _load_local_stock_bars(market: str, symbols: Sequence[str],
                           limit: int) -> Dict[str, List[dict]]:
    """本地日线（库存出来的那一版，盘后同步）→ {symbol: kline list}。

    不足 `LOCAL_MIN_BARS` 根的票不返回（留给兜底链路去补）。
    """
    if not symbols:
        return {}
    from app.utils.db_market import get_market_kline_writer

    try:
        w = get_market_kline_writer()
    except Exception as e:
        logger.debug("[label.cal] 本地 kline 库不可用: %s", e)
        return {}

    out: Dict[str, List[dict]] = {}
    for sym in symbols:
        try:
            rows = w.query(market, sym, "1D", limit=int(limit))
        except Exception as e:
            logger.debug("[label.cal] 本地读取 %s:%s 失败: %s", market, sym, e)
            continue
        bars: List[dict] = []
        for r in rows or []:
            t = r.get("time")
            if t is None:
                continue
            try:
                ts = int(t.timestamp())          # naive ⇒ 按本机 tz（市场本地）解释
            except Exception:
                continue
            bars.append({
                "time": ts,
                "open": float(r.get("open") or 0),
                "high": float(r.get("high") or 0),
                "low": float(r.get("low") or 0),
                "close": float(r.get("close") or 0),
                "volume": float(r.get("volume") or 0),
            })
        if len(bars) >= LOCAL_MIN_BARS:
            out[sym] = bars
    return out


def detect_decay(*, limit: int = EVAL_LIMIT, step: int = EVAL_STEP,
                 max_symbols: int = EVAL_MAX_SYMBOLS) -> Dict[str, Any]:
    """冻结权重在**上次拟合窗之后**的日子上的同日截面表现。`should_refit=True` ⇒ 判定衰减。

    为什么必须卡在拟合窗之后：拟合样本与检测样本一旦重叠，测的就是自己，
    "未衰减"会永远成立 ⇒ 这个检测等于没有。锚点取 JSON 里落盘的
    `fit_window.day_train_max`；锚点不存在（还没重标过）或锚点后样本不足 60
    ⇒ 退化为整个近窗，并在 `post_fit_only=False` 里**如实标注**，
    不假装拿出了干净结论。
    """
    mk: List[dict] = []
    klines_list, info = _load_universe_klines(mk, limit=limit, max_symbols=max_symbols)
    X, y, fr, days = build_samples(klines_list, mk, limit=limit, step=step)
    if not X:
        return {"ok": False, "n": 0, "auc_xs": None, "should_refit": False,
                "reason": "no_samples", "data_source": info}

    post_only = False
    anchor = ((load_weights_file() or {}).get("fit_window") or {}).get("day_train_max")
    if anchor:
        keep = [i for i, d in enumerate(days) if d > str(anchor)]
        if len(keep) >= 60:
            X = [X[i] for i in keep]
            y = [y[i] for i in keep]
            fr = [fr[i] for i in keep]
            days = [days[i] for i in keep]
            post_only = True

    p = score_with_current(X)
    rep = evaluate(p, y, fr)                    # pooled：只作历史对照
    rep_xs = evaluate_xs(days, p, y, fr)        # 判定依据
    auc_xs = rep_xs.get("auc_xs")
    spread_xs = rep_xs.get("spread_bp_xs")
    should = (
        auc_xs is not None and auc_xs < AUC_DECAY_THRESHOLD_XS
    ) or (
        spread_xs is not None and spread_xs < QUINTILE_SPREAD_FLOOR
    ) or (auc_xs is None and rep.get("n", 0) >= 50)

    return {
        "ok": True,
        "n": rep.get("n"),
        "auc": rep.get("auc"),
        "spread_bp": rep.get("spread_bp"),
        "auc_xs": auc_xs,
        "ic_mean": rep_xs.get("ic_mean"),
        "ic_ir": rep_xs.get("ic_ir"),
        "spread_bp_xs": spread_xs,
        "n_days": rep_xs.get("days_counted"),
        "post_fit_only": post_only,
        "data_source": info,
        "score_version": model.SCORE_VERSION,
        "auc_floor": AUC_DECAY_THRESHOLD_XS,
        "should_refit": bool(should),
        "reason": "auc_xs_decay" if (auc_xs is not None and auc_xs < AUC_DECAY_THRESHOLD_XS) else (
            "spread_flat" if should else "ok"),
    }


def recalibrate(*, limit: int = FIT_LIMIT, step: int = FIT_STEP,
                max_symbols: int = FIT_MAX_SYMBOLS) -> Dict[str, Any]:
    """训练窗拟合 + **验证窗同日截面**质量门。门不过 ⇒ applied=False，**不**覆盖线上权重。

    与旧实现（全样本拟合 + 全样本 pooled 评估）的三处关键差异：
      1. 按天切训练/验证，z-score 标准化统计量**只来自训练窗**；
      2. 判定一律走验证窗的同日截面 AUC / rank-IC，pooled 结果只留作对照；
      3. 与旧模型的对照也在**同一个验证窗**上做（旧实现是拿不同的窗在两个口径下比，
         得到的"新旧差距"里混着样本差异，不可作）。
    """
    mk: List[dict] = []
    klines_list, info = _load_universe_klines(mk, limit=limit, max_symbols=max_symbols)
    X_raw, y, fr, days = build_samples(klines_list, mk, limit=limit, step=step)
    if len(X_raw) < 200:
        return {"ok": False, "applied": False, "reason": f"insufficient_samples:{len(X_raw)}",
                "data_source": info}

    tr_idx, va_idx = split_by_day(days)
    if len(va_idx) < 100:
        return {"ok": False, "applied": False, "reason": f"insufficient_valid:{len(va_idx)}",
                "data_source": info}

    X_tr = [X_raw[i] for i in tr_idx]
    X_va = [X_raw[i] for i in va_idx]
    y_tr, fr_tr, d_tr = [y[i] for i in tr_idx], [fr[i] for i in tr_idx], [days[i] for i in tr_idx]
    y_va, fr_va, d_va = [y[i] for i in va_idx], [fr[i] for i in va_idx], [days[i] for i in va_idx]

    # 统计量只来自训练窗 ⇒ 验证窗是模型真正没见过的数据
    X_tr_z, stats = standardize(X_tr)
    w_z = fit_logit(X_tr_z, y_tr, l2=1.0)

    p_tr = _score_z(X_tr, stats, w_z)
    p_va = _score_z(X_va, stats, w_z)
    rep_tr = evaluate(p_tr, y_tr, fr_tr)
    rep_tr_xs = evaluate_xs(d_tr, p_tr, y_tr, fr_tr)
    rep_va = evaluate(p_va, y_va, fr_va)
    rep_va_xs = evaluate_xs(d_va, p_va, y_va, fr_va)

    # 旧模型在**同一验证窗**上的对照
    p_va_old = score_with_current(X_va)
    rep_va_old = evaluate(p_va_old, y_va, fr_va)
    rep_va_xs_old = evaluate_xs(d_va, p_va_old, y_va, fr_va)

    auc_xs = rep_va_xs.get("auc_xs")
    ic = rep_va_xs.get("ic_mean")
    spread_xs = rep_va_xs.get("spread_bp_xs")
    auc_old = rep_va_xs_old.get("auc_xs")
    floor_ok = (
        auc_xs is not None and auc_xs >= AUC_LIVE_FLOOR_XS
        and ic is not None and ic >= IC_LIVE_FLOOR
        and spread_xs is not None and spread_xs >= QUINTILE_SPREAD_FLOOR
    )
    # 不得显著劣化：见 XS_REGRESSION_TOL 注释（容差内的差异算噪声）
    no_regression = (
        auc_old is None or auc_xs is None or auc_xs >= auc_old - XS_REGRESSION_TOL
    )
    gate_ok = bool(floor_ok and no_regression)
    if not gate_ok:
        logger.warning(
            "[label.cal] 质量门未过，不应用: 验证窗截面 auc_xs=%s ic=%s spread=%s "
            "(floor %.2f/%.3f/%.1f) 旧模型 auc_xs=%s",
            auc_xs, ic, spread_xs, AUC_LIVE_FLOOR_XS, IC_LIVE_FLOOR,
            QUINTILE_SPREAD_FLOOR, auc_old)
        return {
            "ok": True, "applied": False, "reason": "quality_gate",
            "report_valid": rep_va_xs, "report_valid_pooled": rep_va,
            "report_valid_old": rep_va_xs_old, "report_train": rep_tr_xs,
        }

    payload = _payload_from_raw(_map_to_raw(w_z, stats), rep_va_xs)
    payload["report_train"] = rep_tr_xs
    payload["report_train_pooled"] = rep_tr
    payload["report_valid_pooled"] = rep_va
    payload["report_old_valid"] = rep_va_xs_old
    payload["report_old_valid_pooled"] = rep_va_old
    payload["fit_window"] = {
        "day_train_max": max(d_tr),
        "day_valid_max": max(d_va),
        "n_train": len(tr_idx),
        "n_valid": len(va_idx),
        "n_symbols": len(klines_list),
        "data_source": info,               # 可追溯：这版系数到底是用哪些数据拟合的
    }
    applied = apply_weights(payload, reason=f"auto_recal auc_xs={auc_xs:.4f} ic={ic:+.4f}")
    return {"ok": True, "applied": True, "score_version": applied.get("score_version"),
            "report_valid": rep_va_xs, "payload": applied}


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
