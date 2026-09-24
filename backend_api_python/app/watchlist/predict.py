# -*- coding: utf-8 -*-
"""app/watchlist/predict.py — **次日方向预测**（评分 v2 口径的核心）

设计依据（用户裁定 2026-09）：
  1. **语义**：`score` = P(下一交易日上涨)×100（主），预期收益作辅助（extras）。
     用户见分即知今日操作：高=偏多可持/可买，低=偏空宜减/回避。
     **不再**是「过去形态健康度」——那是 v1，对行动无意义。
  2. **两层结构**：
        大盘基础分  →  个股独立分  →  按 beta/相关性合成  →  叠加「庄/筹码异动」
     大盘涨跌带动个股；反向票（beta<0）自动**反转**大盘贡献；
     独立/有庄票削弱大盘权重、放大个股自身信号。
  3. **可回测**：全部特征可从日 K 复现；系数冻结在 `model.PRED_*`，
     标定脚本重拟合后必须 `SCORE_VERSION += 1`。

链路：
    services/kline + market_cn.index.get_index_daily_kline
      → extract_* 纯特征
      → model.predict_from_features  （logistic，可手算）
      → {p_up, exp_ret_bp, market, beta, regime, zhuang, ...}

纪律
  - 本文件**不 import** `app.agent.*` / `app.market_cn.auto.*`
  - 预测目标是 **T+1**（用截至 T 收盘的数据），不是回看 T 日涨跌
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.utils.logger import get_logger
from app.watchlist import model

logger = get_logger(__name__)

#: 大盘/个股对齐用的收益率窗口
BETA_WINDOW = 60
#: 「庄」代理：近 N 根看量能异常
ZHUANG_VOL_WINDOW = 20
ZHUANG_BASE_VOL_WINDOW = 60
#: 最少样本：特征窗 + 1 根次日标签（训练）/ 对齐
MIN_BARS = 60


def _closes(klines: Sequence[Dict[str, Any]]) -> List[float]:
    return [float(k["close"]) for k in klines]


def _returns(closes: Sequence[float]) -> List[float]:
    out: List[float] = []
    for i in range(1, len(closes)):
        p = closes[i - 1]
        out.append((closes[i] - p) / p if p > 0 else 0.0)
    return out


def _mean(xs: Sequence[float]) -> Optional[float]:
    return (sum(xs) / len(xs)) if xs else None


def _std(xs: Sequence[float]) -> Optional[float]:
    n = len(xs)
    if n < 2:
        return None
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return math.sqrt(var)


def _corr(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    n = min(len(a), len(b))
    if n < 10:
        return None
    a, b = list(a[-n:]), list(b[-n:])
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    if da <= 0 or db <= 0:
        return None
    return num / (da * db)


def _beta(stock_ret: Sequence[float], mkt_ret: Sequence[float]) -> Optional[float]:
    n = min(len(stock_ret), len(mkt_ret))
    if n < 20:
        return None
    s, m = list(stock_ret[-n:]), list(mkt_ret[-n:])
    ms, mm = sum(s) / n, sum(m) / n
    num = sum((x - ms) * (y - mm) for x, y in zip(s, m))
    den = sum((y - mm) ** 2 for y in m)
    if den <= 0:
        return None
    return num / den


# ═══════════════════════════════════════════════════════════════════
# 1. 大盘基础分（T+1 次日上涨倾向）
# ═══════════════════════════════════════════════════════════════════

def market_raw_features(index_klines: Sequence[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    """指数侧原料（已归一到 ~N(0,1) 量纲的 z 或有界分）。

    与 optimizer/market_sentiment 同源思想，但输出改为 **可进 logistic 的特征**，
    而不是 0~100 情绪展示分。
    """
    closes = _closes(index_klines)
    n = len(closes)
    if n < MIN_BARS:
        return {"m_trend": None, "m_mom5": None, "m_mom20": None, "m_vol": None, "m_rev": None}

    rets = _returns(closes)
    c = closes[-1]
    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / 60 if n >= 60 else ma20

    # 趋势：价相对均线
    m_trend = (c / ma20 - 1) + 0.5 * (c / ma60 - 1) if ma20 > 0 and ma60 > 0 else None
    # 动量
    m_mom5 = (c / closes[-6] - 1) if n >= 6 else None
    m_mom20 = (c / closes[-21] - 1) if n >= 21 else None
    # 波动（近 20 日收益 std）——高波动压低可预测性/抬高风险
    m_vol = _std(rets[-20:]) if len(rets) >= 20 else None
    # 短期反转：3 日连涨后的均值回归压力（负向）
    r3 = (c / closes[-4] - 1) if n >= 4 else None
    m_rev = r3

    return {
        "m_trend": m_trend,
        "m_mom5": m_mom5,
        "m_mom20": m_mom20,
        "m_vol": m_vol,
        "m_rev": m_rev,
    }


def market_score_from_features(mfeats: Dict[str, Optional[float]]) -> Dict[str, Any]:
    """大盘 → 次日 P(涨)。经 `model.market_p_up`。"""
    return model.market_p_up(mfeats)


def load_market_klines(days: int = 120) -> List[Dict[str, Any]]:
    """上证指数日 K（大盘代理）。失败返回 []（个股降级为仅独立分）。"""
    try:
        from app.market_cn.index import get_index_daily_kline
        raw = get_index_daily_kline("000001", days) or []
        # 统一成 label 侧 kline 形状: time/close/...
        out = []
        for r in raw:
            if not isinstance(r, dict):
                continue
            day = r.get("day") or r.get("date") or r.get("time")
            close = r.get("close")
            if day is None or close is None:
                continue
            try:
                if isinstance(day, (int, float)):
                    ts = int(day)
                else:
                    ts = int(datetime.strptime(str(day)[:10], "%Y-%m-%d").timestamp())
                out.append({
                    "time": ts,
                    "open": float(r.get("open") or close),
                    "high": float(r.get("high") or close),
                    "low": float(r.get("low") or close),
                    "close": float(close),
                    "volume": float(r.get("volume") or 0),
                })
            except Exception:
                continue
        return out
    except Exception as e:
        logger.warning("[label.v2] 大盘指数加载失败: %s", e)
        return []


# ═══════════════════════════════════════════════════════════════════
# 2. 个股特征 + beta/反向 + 庄代理
# ═══════════════════════════════════════════════════════════════════

def stock_raw_features(klines: Sequence[Dict[str, Any]],
                       chip_core: Optional[Dict[str, Any]] = None) -> Dict[str, Optional[float]]:
    """个股原料（无未来函数：只用截至末根 bar 的数据）。"""
    closes = _closes(klines)
    highs = [float(k["high"]) for k in klines]
    lows = [float(k["low"]) for k in klines]
    vols = [float(k.get("volume") or 0) for k in klines]
    n = len(closes)
    if n < MIN_BARS:
        return {k: None for k in (
            "s_trend", "s_mom5", "s_mom20", "s_rev", "s_vol",
            "s_vr", "s_profit", "s_amp", "s_macd",
        )}

    c = closes[-1]
    rets = _returns(closes)
    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / 60
    ma5 = sum(closes[-5:]) / 5

    s_trend = (c / ma20 - 1) + 0.5 * (ma5 / ma20 - 1) if ma20 > 0 else None
    s_mom5 = (c / closes[-6] - 1) if n >= 6 else None
    s_mom20 = (c / closes[-21] - 1) if n >= 21 else None
    s_rev = (c / closes[-4] - 1) if n >= 4 else None  # 3 日过涨 → 反转压力
    s_vol = _std(rets[-20:]) if len(rets) >= 20 else None

    # 量能比 5/20
    s_vr = None
    if n >= 20:
        mv20 = _mean(vols[-20:]) or 0
        mv5 = _mean(vols[-5:])
        if mv20 > 0 and mv5 is not None:
            s_vr = mv5 / mv20

    s_profit = None
    if chip_core and "error" not in chip_core:
        s_profit = chip_core.get("profit_ratio_raw")

    # 20 日振幅
    if n >= 20 and c > 0:
        w_hi = max(highs[-20:])
        w_lo = min(lows[-20:])
        s_amp = (w_hi - w_lo) / c
    else:
        s_amp = None

    # MACD hist 方向（简化）
    s_macd = None
    if n >= 26:
        def _ema(xs, p):
            k = 2 / (p + 1)
            e = xs[0]
            for x in xs[1:]:
                e = x * k + e * (1 - k)
            return e
        ema12 = _ema(closes, 12)
        ema26 = _ema(closes, 26)
        dif = ema12 - ema26
        # 近似 hist：dif 与 9 日 dif 均值差
        # （完整 MACD 在 utils.indicators；此处避免重复依赖细节，用 dif 动量）
        s_macd = dif / c if c > 0 else None

    return {
        "s_trend": s_trend, "s_mom5": s_mom5, "s_mom20": s_mom20, "s_rev": s_rev,
        "s_vol": s_vol, "s_vr": s_vr, "s_profit": s_profit, "s_amp": s_amp,
        "s_macd": s_macd,
    }


def beta_regime(stock_klines: Sequence[Dict[str, Any]],
                market_klines: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """个股 vs 大盘：beta、相关性、regime（follow / inverse / independent）。

    - beta 显著为负 ⇒ **反向票**：大盘贡献取反号
    - |corr| 低 且 |beta| 低 ⇒ **独立票**（或可能有庄主导）：压低大盘权重
    """
    if not market_klines or len(stock_klines) < 30 or len(market_klines) < 30:
        return {"beta": None, "corr": None, "regime": "unknown", "mkt_weight": 0.5}

    # 按日期对齐（取 stock 末 N 根，在 market 中找同日）
    def _by_day(ks):
        d = {}
        for k in ks:
            day = datetime.fromtimestamp(int(k["time"])).strftime("%Y-%m-%d")
            d[day] = float(k["close"])
        return d

    sd, md = _by_day(stock_klines), _by_day(market_klines)
    days = [d for d in sorted(sd.keys()) if d in md]
    days = days[-BETA_WINDOW:]
    if len(days) < 20:
        return {"beta": None, "corr": None, "regime": "unknown", "mkt_weight": 0.5}

    s_c = [sd[d] for d in days]
    m_c = [md[d] for d in days]
    s_r, m_r = _returns(s_c), _returns(m_c)
    beta = _beta(s_r, m_r)
    corr = _corr(s_r, m_r)

    if beta is None or corr is None:
        regime, mkt_w = "unknown", 0.5
    elif beta < -0.15 or corr < -0.2:
        regime, mkt_w = "inverse", 0.55
    elif corr < 0.15 or (beta is not None and abs(beta) < 0.3):
        regime, mkt_w = "independent", 0.2
    else:
        regime, mkt_w = "follow", 0.6

    return {
        "beta": round(beta, 4) if beta is not None else None,
        "corr": round(corr, 4) if corr is not None else None,
        "regime": regime,
        "mkt_weight": mkt_w,
    }


def zhuang_signal(stock_klines: Sequence[Dict[str, Any]],
                  chip_core: Optional[Dict[str, Any]],
                  market_klines: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """「有庄 / 筹码异动」**代理指标**（不是穿透主力账户，是量价+筹码统计证据）。

    证据（可回测、可解释）：
      1. `vol_bulge`   — 近 20 日均量 / 前 60 日均量，放量而不跌
      2. `hold_vs_mkt` — 大盘近 5 日收益为负时个股仍能收涨/横盘的次数占比（抗跌/逆市）
      3. `chip_focus`  — 筹码峰强度集中度（top2 peak strength / total）
      4. `profit_jump` — 获利盘快速抬升（吸筹迹象）

    返回 `score` 0~100（越高越像有庄在场），以及分项（进 extras）。
    """
    closes = _closes(stock_klines)
    vols = [float(k.get("volume") or 0) for k in stock_klines]
    n = len(closes)
    ev = {"vol_bulge": None, "hold_vs_mkt": None, "chip_focus": None, "profit_jump": None}

    if n < MIN_BARS:
        return {**ev, "score": None, "flags": []}

    # 1) 量能凸起
    if n >= ZHUANG_BASE_VOL_WINDOW:
        base = _mean(vols[-ZHUANG_BASE_VOL_WINDOW:-ZHUANG_VOL_WINDOW]) or 0
        recent = _mean(vols[-ZHUANG_VOL_WINDOW:]) or 0
        ev["vol_bulge"] = round(recent / base, 4) if base > 0 else None

    # 2) 抗跌/逆市
    if market_klines and len(market_klines) >= 10:
        md = {}
        for k in market_klines:
            day = datetime.fromtimestamp(int(k["time"])).strftime("%Y-%m-%d")
            md[day] = float(k["close"])
        days = []
        for k in stock_klines[-6:]:
            day = datetime.fromtimestamp(int(k["time"])).strftime("%Y-%m-%d")
            if day in md:
                days.append((day, float(k["close"]), md[day]))
        # 用 close 序列估日收益
        holds = total = 0
        for i in range(1, len(days)):
            s_ret = days[i][1] / days[i - 1][1] - 1 if days[i - 1][1] else 0
            m_ret = days[i][2] / days[i - 1][2] - 1 if days[i - 1][2] else 0
            if m_ret < -0.002:
                total += 1
                if s_ret >= -0.001:
                    holds += 1
        if total > 0:
            ev["hold_vs_mkt"] = round(holds / total, 4)

    # 3) 筹码集中
    if chip_core and "error" not in chip_core:
        peaks = chip_core.get("peaks") or []
        total_d = sum(p.get("strength") or 0 for p in peaks)
        if total_d > 0 and peaks:
            top2 = sorted((p.get("strength") or 0 for p in peaks), reverse=True)[:2]
            ev["chip_focus"] = round(sum(top2) / total_d, 4)

    # 4) 获利盘跳升（近 5 日 vs 前值——有 chip 时用当前 profit 与中位代理）
    #    无历史 chip 时用「收盘快速站上筹码均价」近似
    if chip_core and "error" not in chip_core:
        avg = chip_core.get("avg_cost_raw")
        cur = chip_core.get("current_price")
        if avg and cur and avg > 0:
            ev["profit_jump"] = round((cur / avg - 1), 4)

    # 加权成 0~100（先验系数，标定脚本可替换）
    z = model.zhuang_score(ev)
    return {**ev, **z}


# ═══════════════════════════════════════════════════════════════════
# 3. 合成：T+1 P(涨) + 预期收益
# ═══════════════════════════════════════════════════════════════════

def predict_next_day(stock_klines: Sequence[Dict[str, Any]],
                     market_klines: Sequence[Dict[str, Any]],
                     chip_core: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """完整 v2 预测：大盘基础分 + 个股独立分 + beta/反向 + 庄代理 → P(T+1 涨)。

    Returns:
        None ⇒ 数据不足，不产出（= 空白）。
        dict ⇒
          p_up, exp_ret_bp, score (=p_up*100),
          market: {p_up, features...},
          beta: {beta, corr, regime, mkt_weight},
          zhuang: {...},
          features: {进模型的全部有效特征},
          breakdown: {因子→贡献 logit}
    """
    if not stock_klines or len(stock_klines) < MIN_BARS:
        return None

    m_klines = list(market_klines or [])
    mfeats = market_raw_features(m_klines) if m_klines else {}
    mres = market_score_from_features(mfeats) if mfeats else {"p_up": 0.5, "logit": 0.0, "breakdown": {}}

    sfeats = stock_raw_features(stock_klines, chip_core)
    br = beta_regime(stock_klines, m_klines)
    zh = zhuang_signal(stock_klines, chip_core, m_klines)

    pred = model.predict_from_features(
        stock_feats=sfeats,
        market_feats=mfeats,
        regime=br.get("regime") or "unknown",
        zhuang_score=zh.get("score"),
        beta=br.get("beta"),
    )
    if pred is None:
        return None

    return {
        "score": pred["score"],
        "p_up": pred["p_up"],
        "exp_ret_bp": pred["exp_ret_bp"],
        "market": {"p_up": mres.get("p_up"), "logit": mres.get("logit"),
                   "breakdown": mres.get("breakdown"), **{k: mfeats.get(k) for k in mfeats}},
        "beta": br,
        "zhuang": zh,
        "features": {**{k: v for k, v in sfeats.items() if v is not None},
                     **{k: v for k, v in (mfeats or {}).items() if v is not None}},
        "breakdown": pred.get("breakdown"),
    }
