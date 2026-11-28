# -*- coding: utf-8 -*-
"""
market-prediction/_factors.py — 四维因子计算层

设计原则：
1. 计算层集中在此文件，数据取数均在做失败降级，绝不伪造"有效数据"
2. technical / fund_flow / sentiment 走 app.market_cn.* 本地协调器
3. valuation 按用户拍板（2026-11-28）在技能内直连 akshare，不动数据层
4. 北向资金已彻底剔除（2024-08-16 起停更 551+ 交易日）；两融余额全市场无源，同样剔除
5. 所有分数归一到 0-100；error 一律显式写入 details，供上游降置信度

维度：
- technical (30%)：均线、MACD、布林带、趋势、关键位
- fund_flow (25%)：主力净流入、资金持续性、成交量趋势
- sentiment (20%)：涨跌停、炸板率、贪恐指数、市场宽度
- valuation (25%)：上证/沪深300 长短周期 PE/PB 分位融合、股债利差、估值中枢

2026-11-27 v1.0 初始版本；2026-11-28 v1.1 修数据源与分位融合。
"""

import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from .common import clamp, percentile_rank

logger = logging.getLogger(__name__)

# 估值接口为网络请求，同一进程内缓存 30 分钟，避免一次预测重复拉取
_CACHE_TTL = 1800
_CACHE: Dict[str, tuple] = {}


def _cached(key: str, loader, ttl: int = _CACHE_TTL):
    """轻量进程内缓存；失败结果不缓存，便于下次重试。"""
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = loader()
    if value and not (isinstance(value, dict) and value.get("_failed")):
        _CACHE[key] = (now, value)
    return value


# ================================================================
# 技术面因子
# ================================================================

def compute_technical_factors() -> Dict[str, Any]:
    """技术面因子（0-100）。"""
    try:
        from app.market_cn.index import get_index_daily_kline

        klines = get_index_daily_kline("000001", 120) or []
        if len(klines) < 60:
            return {"score": 50, "details": {"error": "数据不足"}}

        closes = [float(k.get("close", 0)) for k in klines if k.get("close")]
        highs = [float(k.get("high", 0)) for k in klines if k.get("high")]
        lows = [float(k.get("low", 0)) for k in klines if k.get("low")]
        volumes = [float(k.get("volume", 0)) for k in klines if k.get("volume")]

        if len(closes) < 60:
            return {"score": 50, "details": {"error": "有效数据不足"}}

        c = closes[-1]
        ma5 = sum(closes[-5:]) / 5
        ma10 = sum(closes[-10:]) / 10
        ma20 = sum(closes[-20:]) / 20
        ma60 = sum(closes[-60:]) / 60

        if c > ma5 > ma10 > ma20 > ma60:
            ma_score = 90
        elif c > ma5 > ma10 > ma20:
            ma_score = 75
        elif c > ma5 > ma10:
            ma_score = 60
        elif c > ma20 > ma60:
            ma_score = 55
        elif c > ma20:
            ma_score = 50
        else:
            ma_score = 30

        ma20_prev = sum(closes[-21:-1]) / 20
        ma20_slope = (ma20 - ma20_prev) / ma20_prev * 100 if ma20_prev > 0 else 0
        slope_score = clamp(50 + ma20_slope * 10, 0, 100)

        macd_score = _compute_macd_score(closes)
        boll_score = _compute_bollinger_score(closes, highs, lows)
        trend_score = _compute_trend_strength(highs, lows, closes)
        key_level_score = _compute_key_level_break(closes, highs, lows)

        weights = {
            "ma_trend": 0.30,
            "ma_slope": 0.15,
            "macd": 0.20,
            "bollinger": 0.15,
            "trend_strength": 0.10,
            "key_level": 0.10,
        }
        composite = (
            ma_score * weights["ma_trend"]
            + slope_score * weights["ma_slope"]
            + macd_score * weights["macd"]
            + boll_score * weights["bollinger"]
            + trend_score * weights["trend_strength"]
            + key_level_score * weights["key_level"]
        )
        return {
            "score": round(composite, 1),
            "details": {
                "ma_trend": round(ma_score, 1),
                "ma_slope": round(slope_score, 1),
                "ma20_slope_pct": round(ma20_slope, 2),
                "macd": round(macd_score, 1),
                "bollinger": round(boll_score, 1),
                "trend_strength": round(trend_score, 1),
                "key_level_break": round(key_level_score, 1),
                "current_price": c,
                "ma5": round(ma5, 2),
                "ma20": round(ma20, 2),
                "ma60": round(ma60, 2),
            },
        }
    except Exception as e:
        logger.warning("[market-prediction] technical factors failed: %s", e)
        return {"score": 50, "details": {"error": str(e)}}


def _compute_macd_score(closes: List[float]) -> float:
    if len(closes) < 26:
        return 50

    def _ema(xs, period):
        k = 2 / (period + 1)
        e = xs[0]
        for x in xs[1:]:
            e = x * k + e * (1 - k)
        return e

    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    dif = ema12 - ema26

    dif_series = []
    for i in range(len(closes)):
        if i >= 11:
            ema12_i = _ema(closes[: i + 1], 12)
            ema26_i = _ema(closes[: i + 1], 26)
            dif_series.append(ema12_i - ema26_i)

    if len(dif_series) < 9:
        dea = dif_series[-1] if dif_series else 0
    else:
        dea = _ema(dif_series, 9)

    hist = dif - dea
    prev_hist = (
        dif_series[-2] - (_ema(dif_series[:-1], 9) if len(dif_series) > 9 else dif_series[-2])
        if len(dif_series) >= 2
        else 0
    )
    if hist > 0 and hist > prev_hist:
        return 85
    if hist > 0:
        return 70
    if hist < 0 and hist < prev_hist:
        return 25
    return 40


def _compute_bollinger_score(closes: List[float], highs: List[float], lows: List[float]) -> float:
    if len(closes) < 20:
        return 50
    recent_closes = closes[-20:]
    mid = sum(recent_closes) / 20
    std = (sum((x - mid) ** 2 for x in recent_closes) / 19) ** 0.5
    upper = mid + 2 * std
    lower = mid - 2 * std
    if upper == lower:
        return 50
    c = closes[-1]
    pos_pct = (c - lower) / (upper - lower) * 100
    if 40 <= pos_pct <= 60:
        return 75
    if 30 <= pos_pct < 40 or 60 < pos_pct <= 70:
        return 65
    if 20 <= pos_pct < 30 or 70 < pos_pct <= 80:
        return 50
    if 10 <= pos_pct < 20 or 80 < pos_pct <= 90:
        return 35
    return 20


def _compute_trend_strength(highs: List[float], lows: List[float], closes: List[float]) -> float:
    if len(closes) < 20:
        return 50
    plus_dm, minus_dm, tr = [], [], []
    for i in range(1, min(20, len(closes))):
        h_diff = highs[-i] - highs[-i - 1]
        l_diff = lows[-i - 1] - lows[-i]
        plus_dm.append(h_diff if h_diff > l_diff and h_diff > 0 else 0)
        minus_dm.append(l_diff if l_diff > h_diff and l_diff > 0 else 0)
        tr.append(max(highs[-i] - lows[-i], abs(highs[-i] - closes[-i - 1]), abs(lows[-i] - closes[-i - 1])))
    if not tr:
        return 50
    atr = sum(tr) / len(tr)
    if atr == 0:
        return 50
    plus_di = (sum(plus_dm) / len(plus_dm)) / atr * 100
    minus_di = (sum(minus_dm) / len(minus_dm)) / atr * 100
    dx = abs(plus_di - minus_di) / (plus_di + minus_di) * 100 if (plus_di + minus_di) > 0 else 0
    if dx > 40:
        return 90
    if dx > 25:
        return 75
    if dx > 20:
        return 60
    return 40


def _dynamic_round_levels(current: float) -> List[float]:
    """按当前点位动态生成整数关口，避免写死 2500-4400。"""
    step = 100 if current >= 1000 else (10 if current >= 100 else 1)
    center = int(current / step)
    return [i * step for i in range(max(1, center - 5), center + 6)]


def _compute_key_level_break(closes: List[float], highs: List[float], lows: List[float]) -> float:
    if len(closes) < 60:
        return 50
    c = closes[-1]
    high_60, low_60 = max(highs[-60:]), min(lows[-60:])
    high_20, low_20 = max(highs[-20:]), min(lows[-20:])
    int_levels = _dynamic_round_levels(c)
    nearest_above = min([lv for lv in int_levels if lv > c], default=c + 100)
    nearest_below = max([lv for lv in int_levels if lv < c], default=c - 100)
    score = 50
    if c >= high_60 * 0.995:
        score += 20
    if c >= high_20 * 0.995:
        score += 10
    if c <= low_60 * 1.005:
        score -= 20
    if c <= low_20 * 1.005:
        score -= 10
    if (nearest_above - c) / c < 0.01:
        score -= 5
    if (c - nearest_below) / c < 0.01:
        score += 5
    return clamp(score, 0, 100)


# ================================================================
# 资金面因子（北向/两融已彻底剔除）
# ================================================================

def compute_fund_flow_factors() -> Dict[str, Any]:
    """
    资金面因子：全市场主力净流入 + 连续性 + 成交量趋势。
    数据源 app.market_cn.fund_flow_api.market_flow_summary（DB 分钟资金聚合，本地）。
    """
    try:
        from app.market_cn.fund_flow_api import market_flow_summary
        from app.market_cn.index import get_index_daily_kline

        flow = market_flow_summary(days=20) or {}
        klines = get_index_daily_kline("000001", 60) or []
        volumes = [float(k.get("volume", 0)) for k in klines if k.get("volume")]

        net_score = _score_net_flow(flow)
        persistence_score = _score_flow_persistence(flow)
        volume_score = _score_volume_trend(volumes)
        composite = net_score * 0.40 + persistence_score * 0.35 + volume_score * 0.25

        details = {
            "net_flow": round(net_score, 1),
            "persistence": round(persistence_score, 1),
            "volume_trend": round(volume_score, 1),
            "net_yi": flow.get("net_yi"),
            "net_pct": flow.get("net_pct"),
            "direction": flow.get("direction"),
            "direction_seq": flow.get("direction_seq"),
            "trade_date": flow.get("trade_date"),
            "freshness": flow.get("freshness"),
        }
        if flow.get("freshness") == "stale" or flow.get("stale"):
            details.setdefault("warnings", []).append("资金流数据滞后，方向信号仅供参考")
        return {"score": round(composite, 1), "details": details}
    except Exception as e:
        logger.warning("[market-prediction] fund_flow factors failed: %s", e)
        return {"score": 50, "details": {"error": str(e)}}


def _score_net_flow(flow: Dict) -> float:
    """主力净流入强度（net_pct = 净流入/成交额，单位 %）。"""
    net_pct = flow.get("net_pct")
    if net_pct is None:
        return 50
    net_pct = float(net_pct)
    score = 50
    if net_pct >= 10:
        score = 85
    elif net_pct >= 5:
        score = 75
    elif net_pct >= 2:
        score = 65
    elif net_pct >= 0:
        score = 55
    elif net_pct >= -2:
        score = 45
    elif net_pct >= -5:
        score = 35
    else:
        score = 20
    if str(flow.get("direction", "")).lower() == "outflow":
        score -= 5
    return clamp(score, 0, 100)


def _score_flow_persistence(flow: Dict) -> float:
    """基于 direction_seq / history 的连续性，替代旧 north_flow。"""
    seq = flow.get("direction_seq") or []
    if not seq:
        seq = [
            "inflow" if float((h or {}).get("net_yi", 0) or 0) > 0 else "outflow"
            for h in (flow.get("history") or [])
        ]
    if not seq:
        return 50
    max_in = max_out = cur_in = cur_out = 0
    for direction in seq[-20:]:
        if direction == "inflow":
            cur_in += 1
            cur_out = 0
            max_in = max(max_in, cur_in)
        elif direction == "outflow":
            cur_out += 1
            cur_in = 0
            max_out = max(max_out, cur_out)
    score = 50
    if max_in >= 5:
        score += 20
    elif max_in >= 3:
        score += 10
    if max_out >= 5:
        score -= 20
    elif max_out >= 3:
        score -= 10
    in_ratio = sum(1 for d in seq[-20:] if d == "inflow") / len(seq[-20:])
    if in_ratio > 0.7:
        score += 15
    elif in_ratio > 0.6:
        score += 5
    elif in_ratio < 0.3:
        score -= 15
    elif in_ratio < 0.4:
        score -= 5
    return clamp(score, 0, 100)


def _score_volume_trend(volumes: List[float]) -> float:
    if len(volumes) < 20:
        return 50
    vol_5 = sum(volumes[-5:]) / 5
    vol_20 = sum(volumes[-20:]) / 20
    vol_60 = sum(volumes[-60:]) / 60 if len(volumes) >= 60 else vol_20
    vr_5_20 = vol_5 / vol_20 if vol_20 > 0 else 1
    vr_20_60 = vol_20 / vol_60 if vol_60 > 0 else 1
    score = 50
    if vr_5_20 > 1.5:
        score += 25
    elif vr_5_20 > 1.2:
        score += 15
    elif vr_5_20 > 1.0:
        score += 5
    elif vr_5_20 < 0.7:
        score -= 15
    elif vr_5_20 < 0.5:
        score -= 25
    if vr_20_60 > 1.2:
        score += 10
    elif vr_20_60 < 0.8:
        score -= 10
    return clamp(score, 0, 100)


# ================================================================
# 情绪面因子
# ================================================================

def compute_sentiment_factors() -> Dict[str, Any]:
    """情绪面：涨跌停池 + 炸板率 + 贪恐指数 + 上涨家数比例（市场宽度）。"""
    try:
        from app.market_cn.dragon_limit import get_broken_board, get_dt_pool, get_zt_pool
        from app.market_cn.emotion import fetch_emotion_cycle
        from app.market_cn.fear_greed_index import fear_greed_index

        emo = _cached("emotion", lambda: fetch_emotion_cycle() or {}, ttl=600) or {}
        row = (emo.get("data") or {}).get("emotion") or {}
        fg = _cached("fear_greed", lambda: fear_greed_index() or {}, ttl=600) or {}
        pools = _cached(
            "limit_pools",
            lambda: {
                "zt": len(get_zt_pool() or []),
                "dt": len(get_dt_pool() or []),
                "broken": len(get_broken_board() or []),
            },
            ttl=600,
        )
        zt = pools["zt"]
        dt = pools["dt"]
        broken = pools["broken"]
        # 池全空时用情绪快照的语义字段兜底，避免误判"情绪冰点"
        if zt == 0 and dt == 0:
            zt = int(row.get("ztjs", 0) or 0)
            dt = int(row.get("dtjs", 0) or 0)
        if broken == 0:
            broken = int(row.get("zbjs", 0) or 0)
        breadth = float(row.get("szbl", 50) or 50)

        ltd_score = _score_limit_up_down({"limit_up_count": zt, "limit_down_count": dt})
        broken_score = _score_broken_rate({"broken_count": broken, "limit_up_count": zt})
        fg_score = _score_fear_greed(fg)
        breadth_score = _score_breadth(breadth)
        composite = ltd_score * 0.40 + broken_score * 0.20 + fg_score * 0.25 + breadth_score * 0.15

        details = {
            "limit_up_down": round(ltd_score, 1),
            "broken_rate": round(broken_score, 1),
            "fear_greed": round(fg_score, 1),
            "breadth": round(breadth_score, 1),
            "zt_count": zt,
            "dt_count": dt,
            "broken_count": broken,
            "up_ratio_pct": breadth,
            "fg_composite": fg.get("composite_score"),
            "fg_label": fg.get("label"),
            "emotion_date": row.get("date1"),
        }
        if zt == 0 and dt == 0 and not row:
            details.setdefault("warnings", []).append("涨跌停池与情绪快照均无数据")
        return {"score": round(composite, 1), "details": details}
    except Exception as e:
        logger.warning("[market-prediction] sentiment factors failed: %s", e)
        return {"score": 50, "details": {"error": str(e)}}


def _score_limit_up_down(emotion: Dict) -> float:
    up = int(emotion.get("limit_up_count", 0) or 0)
    down = int(emotion.get("limit_down_count", 0) or 0)
    ratio = up / max(down, 1)
    score = 50
    if up > 80:
        score += 30
    elif up > 50:
        score += 20
    elif up > 30:
        score += 10
    elif up < 10:
        score -= 15
    elif up < 5:
        score -= 25
    if down > 30:
        score -= 20
    elif down > 15:
        score -= 10
    if ratio > 5:
        score += 15
    elif ratio > 3:
        score += 10
    elif ratio > 1:
        score += 5
    elif ratio < 0.2:
        score -= 25
    elif ratio < 0.5:
        score -= 15
    return clamp(score, 0, 100)


def _score_broken_rate(emotion: Dict) -> float:
    broken = int(emotion.get("broken_count", 0) or 0)
    total_zt = int(emotion.get("limit_up_count", 0) or 0) + broken
    if total_zt == 0:
        return 50
    rate = broken / total_zt
    if rate < 0.15:
        return 90
    if rate < 0.25:
        return 75
    if rate < 0.35:
        return 60
    if rate < 0.50:
        return 40
    if rate < 0.65:
        return 25
    return 10


def _score_fear_greed(fg: Dict) -> float:
    """真实结构：{timestamp, composite_score, label, indicators}，无 data 包裹。"""
    score = fg.get("composite_score")
    if score is None:
        return 50
    score = float(score)
    if score > 80:
        return 60
    if score > 65:
        return 75
    if score > 55:
        return 65
    if score > 45:
        return 55
    if score > 35:
        return 50
    if score > 25:
        return 55
    if score > 15:
        return 65
    return 70


def _score_breadth(up_ratio_pct: float) -> float:
    """市场宽度：上涨家数占比（情绪快照 szbl，单位 %）。"""
    if up_ratio_pct >= 80:
        return 90
    if up_ratio_pct >= 65:
        return 80
    if up_ratio_pct >= 55:
        return 68
    if up_ratio_pct >= 45:
        return 55
    if up_ratio_pct >= 30:
        return 40
    if up_ratio_pct >= 20:
        return 25
    return 15


# ================================================================
# 估值面因子（技能内直连 akshare：上证短周期 + 沪深300长周期融合）
# ================================================================

def _numeric_series(df, *columns: str) -> List[float]:
    """从 DataFrame 中按候选列名提取有效数值序列。"""
    for col in columns:
        if col in df.columns:
            out = []
            for v in df[col].tolist():
                try:
                    if v is None or (isinstance(v, float) and v != v):
                        continue
                    out.append(float(v))
                except (TypeError, ValueError):
                    continue
            return out
    return []


def _load_valuation_data() -> Dict[str, Any]:
    """直连 akshare 拉取估值原始数据（技能内，用户 2026-11-28 拍板）。"""
    import akshare as ak

    out: Dict[str, Any] = {"source": "akshare", "errors": []}

    def _pull(key: str, fn, symbol: str, *columns: str):
        try:
            df = fn(symbol=symbol)
            values = _numeric_series(df, *columns)
            if not values:
                raise ValueError(f"列 {columns} 无有效数据")
            out[key] = values
            return values
        except Exception as e:
            out["errors"].append(f"{key}: {type(e).__name__}: {e}")
            return []

    sh_pe = _pull("sh_pe_history", ak.stock_market_pe_lg, "上证", "平均市盈率")
    sh_pb = _pull("sh_pb_history", ak.stock_market_pb_lg, "上证", "市净率")
    hs300_pe = _pull("hs300_pe_history", ak.stock_index_pe_lg, "沪深300", "滚动市盈率")
    hs300_pb = _pull("hs300_pb_history", ak.stock_index_pb_lg, "沪深300", "市净率")

    # 股息率（沪深300，近 20 日）与 10 年期国债收益率 → 股债利差
    try:
        df = ak.stock_zh_index_value_csindex(symbol="000300")
        dy = _numeric_series(df, "股息率1", "股息率2")
        out["dividend_yield"] = dy[-1] if dy else None
    except Exception as e:
        out["errors"].append(f"dividend_yield: {type(e).__name__}: {e}")
        out["dividend_yield"] = None
    try:
        bond = ak.bond_zh_us_rate(start_date="20190101")
        cgb = _numeric_series(bond, "中国国债收益率10年")
        out["cgb_10y"] = cgb[-1] if cgb else None
    except Exception as e:
        out["errors"].append(f"cgb_10y: {type(e).__name__}: {e}")
        out["cgb_10y"] = None

    dy, cgb = out.get("dividend_yield"), out.get("cgb_10y")
    out["yield_gap"] = round(dy - cgb, 4) if dy is not None and cgb is not None else None

    if not any(out.get(k) for k in ("sh_pe_history", "hs300_pe_history")):
        out["_failed"] = True
    return out


def _fused_percentile(current: float, short_hist: List[float], long_hist: List[float]) -> Dict[str, float]:
    """长短周期分位融合：长周期定方向 0.6 + 短周期定节奏 0.4。"""
    short_pct = percentile_rank(current, short_hist) if short_hist else 50.0
    long_pct = percentile_rank(current, long_hist) if long_hist else 50.0
    return {
        "short_pct": round(short_pct, 1),
        "long_pct": round(long_pct, 1),
        "fused_pct": round(long_pct * 0.6 + short_pct * 0.4, 1),
    }


def _pct_to_value_score(pct: float) -> float:
    """估值分位 → 得分（分位越低越便宜，得分越高）。"""
    if pct < 10:
        return 95
    if pct < 20:
        return 85
    if pct < 30:
        return 75
    if pct < 40:
        return 65
    if pct < 50:
        return 55
    if pct < 60:
        return 50
    if pct < 70:
        return 40
    if pct < 80:
        return 30
    if pct < 90:
        return 20
    return 10


def _score_pe_pb(val: Dict[str, Any], metric: str) -> tuple:
    short_key = f"sh_{metric}_history"
    long_key = f"hs300_{metric}_history"
    short_hist = val.get(short_key) or []
    long_hist = val.get(long_key) or []
    if not short_hist and not long_hist:
        return 50.0, {}
    # 用长周期最新值作为当前值（沪深300 口径更稳定），短周期缺失时长短同用长周期
    current = (long_hist or short_hist)[-1]
    pct = _fused_percentile(current, short_hist or long_hist, long_hist or short_hist)
    return _pct_to_value_score(pct["fused_pct"]), {
        f"current_{metric}": round(current, 3),
        f"{metric}_short_pct": pct["short_pct"],
        f"{metric}_long_pct": pct["long_pct"],
        f"{metric}_fused_pct": pct["fused_pct"],
    }


def _score_yield_gap(val: Dict[str, Any]) -> float:
    gap = val.get("yield_gap")
    if gap is None:
        return 50
    gap = float(gap)
    if gap > 2.5:
        return 95
    if gap > 2.0:
        return 85
    if gap > 1.5:
        return 75
    if gap > 1.0:
        return 65
    if gap > 0.5:
        return 55
    if gap > 0:
        return 50
    if gap > -0.5:
        return 40
    if gap > -1.0:
        return 30
    if gap > -1.5:
        return 20
    return 10


def _score_valuation_center(val: Dict[str, Any]) -> float:
    """估值中枢偏离：当前值 / 历史中位数（月频序列，不用假 250 日均线）。"""
    ratios = []
    details: Dict[str, Any] = {}
    for metric, key in (("pe", "hs300_pe_history"), ("pb", "hs300_pb_history")):
        hist = val.get(key) or []
        if not hist:
            continue
        current = hist[-1]
        ordered = sorted(hist)
        median = ordered[len(ordered) // 2]
        if median > 0:
            ratios.append(current / median)
        details[f"{metric}_vs_median"] = round(current / median, 3) if median > 0 else None
    if not ratios:
        return 50.0, details
    avg = sum(ratios) / len(ratios)
    if avg < 0.75:
        score = 90
    elif avg < 0.85:
        score = 80
    elif avg < 0.95:
        score = 65
    elif avg < 1.05:
        score = 55
    elif avg < 1.15:
        score = 45
    elif avg < 1.25:
        score = 35
    else:
        score = 20
    return float(score), details


def compute_valuation_factors() -> Dict[str, Any]:
    """估值面：上证（短周期）+ 沪深300（长周期）PE/PB 分位融合 + 股债利差 + 中枢偏离。"""
    try:
        val = _cached("valuation", _load_valuation_data)
        if not val or val.get("_failed"):
            return {"score": 50, "details": {"error": "估值数据不可用"}}
        pe_score, pe_detail = _score_pe_pb(val, "pe")
        pb_score, pb_detail = _score_pe_pb(val, "pb")
        yg_score = _score_yield_gap(val)
        center_score, center_detail = _score_valuation_center(val)

        composite = pe_score * 0.35 + pb_score * 0.25 + yg_score * 0.25 + center_score * 0.15
        details = {
            "pe_percentile": round(pe_score, 1),
            "pb_percentile": round(pb_score, 1),
            "yield_gap": round(yg_score, 1),
            "mean_reversion": round(center_score, 1),
            "yield_gap_pct": val.get("yield_gap"),
            "dividend_yield_pct": val.get("dividend_yield"),
            "cgb_10y_pct": val.get("cgb_10y"),
            "source": val.get("source"),
            "hist_points": {
                "sh_pe": len(val.get("sh_pe_history") or []),
                "sh_pb": len(val.get("sh_pb_history") or []),
                "hs300_pe": len(val.get("hs300_pe_history") or []),
                "hs300_pb": len(val.get("hs300_pb_history") or []),
            },
        }
        details.update(pe_detail)
        details.update(pb_detail)
        details.update(center_detail)
        if val.get("errors"):
            details["warnings"] = val["errors"]
        return {"score": round(composite, 1), "details": details}
    except Exception as e:
        logger.warning("[market-prediction] valuation factors failed: %s", e)
        return {"score": 50, "details": {"error": str(e)}}


# ================================================================
# 统一入口
# ================================================================

def compute_all_factors() -> Dict[str, Any]:
    """四维因子统一入口，附数据完整度供 _model 降置信度。"""
    tech = compute_technical_factors()
    fund = compute_fund_flow_factors()
    sent = compute_sentiment_factors()
    val = compute_valuation_factors()

    weights = {"technical": 0.30, "fund_flow": 0.25, "sentiment": 0.20, "valuation": 0.25}
    composite = (
        tech["score"] * weights["technical"]
        + fund["score"] * weights["fund_flow"]
        + sent["score"] * weights["sentiment"]
        + val["score"] * weights["valuation"]
    )
    dims = {"technical": tech, "fund_flow": fund, "sentiment": sent, "valuation": val}
    available = [k for k, v in dims.items() if "error" not in v.get("details", {})]
    degraded = [k for k in dims if k not in available]
    return {
        **{
            k: {"score": v["score"], "details": v["details"], "weight": weights[k]}
            for k, v in dims.items()
        },
        "composite_score": round(composite, 1),
        "available_dimensions": available,
        "degraded_dimensions": degraded,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
