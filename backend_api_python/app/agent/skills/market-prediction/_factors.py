# -*- coding: utf-8 -*-
"""
market-prediction/_factors.py — 四维因子纯计算层

设计原则：
1. 纯函数，无 I/O，无副作用，可单测
2. 只依赖 app.market_cn.* 本地模块
3. 输出标准化因子字典，供 _model.py 和 _report.py 使用
4. 所有分数归一到 0-100

因子维度：
- technical (30%)：均线、MACD、布林带、趋势、关键位
- fund_flow (25%)：北向、融资、成交量、资金连续性
- sentiment (20%)：涨跌停、贪恐、炸板、新高
- valuation (25%)：PE/PB分位、股债利差、历史百分位

2026-11-27 v1.0: 初始版本
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ================================================================
# 技术面因子
# ================================================================

def compute_technical_factors() -> Dict[str, Any]:
    """
    计算技术面因子（0-100）。
    
    子因子：
    - ma_trend: 均线多头排列度
    - macd_signal: MACD 金叉/死叉强度
    - boll_position: 布林带位置
    - trend_strength: 趋势强度（ADX 简化版）
    - key_level_break: 关键位突破情况
    
    Returns:
        dict: {score: 0-100, details: {...}}
    """
    try:
        from app.market_cn.index import get_index_daily_kline
        
        # 获取上证指数日线（120天）
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
        
        # 1. 均线系统
        ma5 = sum(closes[-5:]) / 5
        ma10 = sum(closes[-10:]) / 10
        ma20 = sum(closes[-20:]) / 20
        ma60 = sum(closes[-60:]) / 60
        
        # 均线多头排列度 (0-100)
        ma_score = 0
        if c > ma5 > ma10 > ma20 > ma60:
            ma_score = 90  # 完美多头
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
        
        # MA20 斜率（趋势方向）
        ma20_prev = sum(closes[-21:-1]) / 20
        ma20_slope = (ma20 - ma20_prev) / ma20_prev * 100 if ma20_prev > 0 else 0
        slope_score = clamp(50 + ma20_slope * 10, 0, 100)
        
        # 2. MACD
        macd_score = _compute_macd_score(closes)
        
        # 3. 布林带位置
        boll_score = _compute_bollinger_score(closes, highs, lows)
        
        # 4. 趋势强度（简化 ADX）
        trend_score = _compute_trend_strength(highs, lows, closes)
        
        # 5. 关键位突破（前高/前低/整数关口）
        key_level_score = _compute_key_level_break(closes, highs, lows)
        
        # 加权合成
        weights = {
            "ma_trend": 0.30,
            "ma_slope": 0.15,
            "macd": 0.20,
            "bollinger": 0.15,
            "trend_strength": 0.10,
            "key_level": 0.10,
        }
        
        composite = (
            ma_score * weights["ma_trend"] +
            slope_score * weights["ma_slope"] +
            macd_score * weights["macd"] +
            boll_score * weights["bollinger"] +
            trend_score * weights["trend_strength"] +
            key_level_score * weights["key_level"]
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
            }
        }
        
    except Exception as e:
        logger.warning("[market-prediction] technical factors failed: %s", e)
        return {"score": 50, "details": {"error": str(e)}}


def _compute_macd_score(closes: List[float]) -> float:
    """MACD 评分 (0-100)"""
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
    
    # 计算 DEA (DIF 的 9 日 EMA)
    dif_series = []
    for i in range(len(closes)):
        if i >= 11:
            ema12_i = _ema(closes[:i+1], 12)
            ema26_i = _ema(closes[:i+1], 26)
            dif_series.append(ema12_i - ema26_i)
    
    if len(dif_series) < 9:
        dea = dif_series[-1] if dif_series else 0
    else:
        dea = _ema(dif_series, 9)
    
    hist = dif - dea
    prev_hist = dif_series[-2] - (_ema(dif_series[:-1], 9) if len(dif_series) > 9 else dif_series[-2]) if len(dif_series) >= 2 else 0
    
    # 评分逻辑
    if hist > 0 and hist > prev_hist:
        return 85  # 金叉向上
    elif hist > 0:
        return 70  # DIF>DEA 但柱状收缩
    elif hist < 0 and hist < prev_hist:
        return 25  # 死叉向下
    else:
        return 40  # DIF<DEA 但柱状收缩


def _compute_bollinger_score(closes: List[float], highs: List[float], lows: List[float]) -> float:
    """布林带位置评分 (0-100)"""
    if len(closes) < 20:
        return 50
    
    recent_closes = closes[-20:]
    recent_highs = highs[-20:]
    recent_lows = lows[-20:]
    
    mid = sum(recent_closes) / 20
    std = (sum((x - mid) ** 2 for x in recent_closes) / 19) ** 0.5
    upper = mid + 2 * std
    lower = mid - 2 * std
    
    c = closes[-1]
    
    if upper == lower:
        return 50
    
    # 位置百分比 (0=下轨, 100=上轨)
    pos_pct = (c - lower) / (upper - lower) * 100
    
    # 评分：中轨附近偏高分，极端位置偏低
    if 40 <= pos_pct <= 60:
        return 75  # 中轨附近，均衡
    elif 30 <= pos_pct < 40 or 60 < pos_pct <= 70:
        return 65
    elif 20 <= pos_pct < 30 or 70 < pos_pct <= 80:
        return 50
    elif 10 <= pos_pct < 20 or 80 < pos_pct <= 90:
        return 35
    else:
        return 20  # 超买/超卖极端


def _compute_trend_strength(highs: List[float], lows: List[float], closes: List[float]) -> float:
    """简化趋势强度评分 (0-100)，参考 ADX 思想"""
    if len(closes) < 20:
        return 50
    
    # 计算 +DM, -DM, TR
    plus_dm = []
    minus_dm = []
    tr = []
    
    for i in range(1, min(20, len(closes))):
        h_diff = highs[-i] - highs[-i-1]
        l_diff = lows[-i-1] - lows[-i]
        
        if h_diff > l_diff and h_diff > 0:
            plus_dm.append(h_diff)
        else:
            plus_dm.append(0)
            
        if l_diff > h_diff and l_diff > 0:
            minus_dm.append(l_diff)
        else:
            minus_dm.append(0)
        
        tr.append(max(
            highs[-i] - lows[-i],
            abs(highs[-i] - closes[-i-1]),
            abs(lows[-i] - closes[-i-1])
        ))
    
    if not tr:
        return 50
    
    atr = sum(tr) / len(tr)
    if atr == 0:
        return 50
    
    plus_di = (sum(plus_dm) / len(plus_dm)) / atr * 100
    minus_di = (sum(minus_dm) / len(minus_dm)) / atr * 100
    
    dx = abs(plus_di - minus_di) / (plus_di + minus_di) * 100 if (plus_di + minus_di) > 0 else 0
    
    # ADX 简化：DX 本身作为趋势强度
    # DX > 25 强趋势，> 40 极强
    if dx > 40:
        return 90
    elif dx > 25:
        return 75
    elif dx > 20:
        return 60
    else:
        return 40


def _compute_key_level_break(closes: List[float], highs: List[float], lows: List[float]) -> float:
    """关键位突破评分 (0-100)"""
    if len(closes) < 60:
        return 50
    
    c = closes[-1]
    
    # 近 60 日高低点
    high_60 = max(highs[-60:])
    low_60 = min(lows[-60:])
    
    # 近 20 日高低点
    high_20 = max(highs[-20:])
    low_20 = min(lows[-20:])
    
    # 整数关口（3000, 3200, 3400, 3500...）
    int_levels = [i * 100 for i in range(25, 45)]
    nearest_above = min([l for l in int_levels if l > c], default=c + 100)
    nearest_below = max([l for l in int_levels if l < c], default=c - 100)
    
    score = 50
    
    # 突破 60 日新高
    if c >= high_60 * 0.995:
        score += 20
    # 站上 20 日高点
    if c >= high_20 * 0.995:
        score += 10
    # 跌破 60 日新低
    if c <= low_60 * 1.005:
        score -= 20
    # 跌破 20 日低点
    if c <= low_20 * 1.005:
        score -= 10
    # 接近上方整数关口（压力）
    if (nearest_above - c) / c < 0.01:
        score -= 5
    # 刚站上下方整数关口（支撑）
    if (c - nearest_below) / c < 0.01:
        score += 5
    
    return clamp(score, 0, 100)


# ================================================================
# 资金面因子
# ================================================================

def compute_fund_flow_factors() -> Dict[str, Any]:
    """
    计算资金面因子（0-100）。
    
    子因子：
    - north_flow: 北向资金连续性
    - margin_flow: 融资余额变化
    - volume_trend: 成交量趋势
    - flow_persistence: 资金流向持续性
    
    Returns:
        dict: {score: 0-100, details: {...}}
    """
    try:
        from app.market_cn.fund_flow_local import get_fund_flow_summary
        from app.market_cn.index import get_index_daily_kline
        
        # 获取资金流汇总（近 20 日）
        flow_data = get_fund_flow_summary(days=20) or {}
        
        # 获取指数成交量
        klines = get_index_daily_kline("000001", 60) or []
        volumes = [float(k.get("volume", 0)) for k in klines if k.get("volume")]
        
        # 1. 北向资金 (权重 35%)
        north_score = _score_north_flow(flow_data)
        
        # 2. 融资余额 (权重 25%)
        margin_score = _score_margin_flow(flow_data)
        
        # 3. 成交量趋势 (权重 20%)
        volume_score = _score_volume_trend(volumes)
        
        # 4. 资金持续性 (权重 20%)
        persistence_score = _score_flow_persistence(flow_data)
        
        composite = (
            north_score * 0.35 +
            margin_score * 0.25 +
            volume_score * 0.20 +
            persistence_score * 0.20
        )
        
        return {
            "score": round(composite, 1),
            "details": {
                "north_flow": round(north_score, 1),
                "margin_flow": round(margin_score, 1),
                "volume_trend": round(volume_score, 1),
                "persistence": round(persistence_score, 1),
                "raw": flow_data,
            }
        }
        
    except Exception as e:
        logger.warning("[market-prediction] fund_flow factors failed: %s", e)
        return {"score": 50, "details": {"error": str(e)}}


def _score_north_flow(flow_data: Dict) -> float:
    """北向资金评分"""
    north = flow_data.get("north_flow", {})
    if not north:
        return 50
    
    # 近 5 日净流入
    net_5d = north.get("net_5d_wan", 0)
    # 近 10 日净流入
    net_10d = north.get("net_10d_wan", 0)
    # 连续流入天数
    streak = north.get("inflow_streak", 0)
    
    score = 50
    if net_5d > 100000:  # 10亿以上
        score += 30
    elif net_5d > 50000:
        score += 20
    elif net_5d > 0:
        score += 10
    elif net_5d < -50000:
        score -= 20
    elif net_5d < -100000:
        score -= 30
    
    if net_10d > 200000:
        score += 15
    elif net_10d < -100000:
        score -= 15
    
    if streak >= 5:
        score += 10
    elif streak >= 3:
        score += 5
    elif streak <= -3:
        score -= 10
    
    return clamp(score, 0, 100)


def _score_margin_flow(flow_data: Dict) -> float:
    """融资余额评分"""
    margin = flow_data.get("margin", {})
    if not margin:
        return 50
    
    change_5d = margin.get("change_5d_pct", 0)
    change_10d = margin.get("change_10d_pct", 0)
    
    score = 50
    if change_5d > 3:
        score += 25
    elif change_5d > 1:
        score += 15
    elif change_5d > 0:
        score += 5
    elif change_5d < -3:
        score -= 25
    elif change_5d < -1:
        score -= 15
    elif change_5d < 0:
        score -= 5
    
    if change_10d > 5:
        score += 10
    elif change_10d < -5:
        score -= 10
    
    return clamp(score, 0, 100)


def _score_volume_trend(volumes: List[float]) -> float:
    """成交量趋势评分"""
    if len(volumes) < 20:
        return 50
    
    vol_5 = sum(volumes[-5:]) / 5
    vol_20 = sum(volumes[-20:]) / 20
    vol_60 = sum(volumes[-60:]) / 60 if len(volumes) >= 60 else vol_20
    
    # 量比
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


def _score_flow_persistence(flow_data: Dict) -> float:
    """资金流向持续性评分"""
    daily = flow_data.get("daily_net_wan", [])
    if not daily or len(daily) < 10:
        return 50
    
    # 连续同向天数
    pos_streak = 0
    neg_streak = 0
    max_pos = 0
    max_neg = 0
    
    for v in daily[-20:]:
        if v > 0:
            pos_streak += 1
            neg_streak = 0
            max_pos = max(max_pos, pos_streak)
        elif v < 0:
            neg_streak += 1
            pos_streak = 0
            max_neg = max(max_neg, neg_streak)
        else:
            pos_streak = 0
            neg_streak = 0
    
    score = 50
    if max_pos >= 5:
        score += 20
    elif max_pos >= 3:
        score += 10
    if max_neg >= 5:
        score -= 20
    elif max_neg >= 3:
        score -= 10
    
    # 净流入占比
    pos_days = sum(1 for v in daily[-20:] if v > 0)
    pos_ratio = pos_days / min(20, len(daily))
    if pos_ratio > 0.7:
        score += 15
    elif pos_ratio > 0.6:
        score += 5
    elif pos_ratio < 0.3:
        score -= 15
    elif pos_ratio < 0.4:
        score -= 5
    
    return clamp(score, 0, 100)


# ================================================================
# 情绪面因子
# ================================================================

def compute_sentiment_factors() -> Dict[str, Any]:
    """
    计算情绪面因子（0-100）。
    
    子因子：
    - limit_up_down: 涨跌停家数/比率
    - broken_rate: 炸板率
    - fear_greed: 贪恐指数
    - new_highs: 新高/新低家数
    
    Returns:
        dict: {score: 0-100, details: {...}}
    """
    try:
        from app.market_cn.emotion import get_emotion_snapshot
        from app.market_cn.fear_greed_index import fear_greed_index
        
        emotion = get_emotion_snapshot() or {}
        fg = fear_greed_index() or {}
        
        # 1. 涨跌停情绪 (40%)
        ltd_score = _score_limit_up_down(emotion)
        
        # 2. 炸板率 (20%)
        broken_score = _score_broken_rate(emotion)
        
        # 3. 贪恐指数 (25%)
        fg_score = _score_fear_greed(fg)
        
        # 4. 新高新低 (15%)
        nh_score = _score_new_highs_lows(emotion)
        
        composite = (
            ltd_score * 0.40 +
            broken_score * 0.20 +
            fg_score * 0.25 +
            nh_score * 0.15
        )
        
        return {
            "score": round(composite, 1),
            "details": {
                "limit_up_down": round(ltd_score, 1),
                "broken_rate": round(broken_score, 1),
                "fear_greed": round(fg_score, 1),
                "new_highs_lows": round(nh_score, 1),
                "raw_emotion": emotion,
                "raw_fg": fg,
            }
        }
        
    except Exception as e:
        logger.warning("[market-prediction] sentiment factors failed: %s", e)
        return {"score": 50, "details": {"error": str(e)}}


def _score_limit_up_down(emotion: Dict) -> float:
    """涨跌停评分"""
    up = emotion.get("limit_up_count", 0)
    down = emotion.get("limit_down_count", 0)
    up_ratio = emotion.get("limit_up_ratio", 0)  # 涨停/跌停比
    
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
    
    if up_ratio > 5:
        score += 15
    elif up_ratio > 3:
        score += 10
    elif up_ratio > 1:
        score += 5
    elif up_ratio < 0.5:
        score -= 15
    elif up_ratio < 0.2:
        score -= 25
    
    return clamp(score, 0, 100)


def _score_broken_rate(emotion: Dict) -> float:
    """炸板率评分（炸板率越低越好）"""
    broken = emotion.get("broken_count", 0)
    total_zt = emotion.get("limit_up_count", 0) + broken
    
    if total_zt == 0:
        return 50
    
    broken_rate = broken / total_zt
    
    # 炸板率 < 20% 优秀，> 50% 极差
    if broken_rate < 0.15:
        return 90
    elif broken_rate < 0.25:
        return 75
    elif broken_rate < 0.35:
        return 60
    elif broken_rate < 0.50:
        return 40
    elif broken_rate < 0.65:
        return 25
    else:
        return 10


def _score_fear_greed(fg: Dict) -> float:
    """贪恐指数评分"""
    data = fg.get("data") or {}
    score = data.get("composite_score", 50)
    label = data.get("label", "中性")
    
    # composite_score 已经是 0-100，直接映射
    # 但极端贪婪(>80)反而要小心，极端恐惧(<20)可能是机会
    if score > 80:
        return 60  # 极度贪婪，回调风险
    elif score > 65:
        return 75  # 贪婪，趋势向上
    elif score > 55:
        return 65  # 偏贪婪
    elif score > 45:
        return 55  # 中性偏暖
    elif score > 35:
        return 50  # 中性
    elif score > 25:
        return 55  # 偏恐惧，可能超跌
    elif score > 15:
        return 65  # 恐惧，反弹机会
    else:
        return 70  # 极度恐惧，大底机会


def _score_new_highs_lows(emotion: Dict) -> float:
    """新高新低评分"""
    new_high = emotion.get("new_high_count", 0)
    new_low = emotion.get("new_low_count", 0)
    
    score = 50
    if new_high > 100:
        score += 25
    elif new_high > 50:
        score += 15
    elif new_high > 20:
        score += 5
    
    if new_low > 50:
        score -= 20
    elif new_low > 20:
        score -= 10
    elif new_low > 10:
        score -= 5
    
    return clamp(score, 0, 100)


# ================================================================
# 估值面因子
# ================================================================

def compute_valuation_factors() -> Dict[str, Any]:
    """
    计算估值面因子（0-100）。
    
    子因子：
    - pe_percentile: PE 历史分位（越低越好）
    - pb_percentile: PB 历史分位
    - yield_gap: 股债利差（越大越好）
    - valuation_ma: 估值均值回归位置
    
    Returns:
        dict: {score: 0-100, details: {...}}
    """
    try:
        from app.market_cn.index import get_index_valuation_history
        
        # 获取上证指数估值历史（3年以上）
        val_data = get_index_valuation_history("000001", years=3) or {}
        
        if not val_data:
            return {"score": 50, "details": {"error": "估值数据不可用"}}
        
        # 1. PE 分位 (35%) —— 分位越低评分越高
        pe_score = _score_pe_percentile(val_data)
        
        # 2. PB 分位 (25%)
        pb_score = _score_pb_percentile(val_data)
        
        # 3. 股债利差 (25%)
        yg_score = _score_yield_gap(val_data)
        
        # 4. 估值均值回归 (15%)
        ma_score = _score_valuation_mean_reversion(val_data)
        
        composite = (
            pe_score * 0.35 +
            pb_score * 0.25 +
            yg_score * 0.25 +
            ma_score * 0.15
        )
        
        return {
            "score": round(composite, 1),
            "details": {
                "pe_percentile": round(pe_score, 1),
                "pb_percentile": round(pb_score, 1),
                "yield_gap": round(yg_score, 1),
                "mean_reversion": round(ma_score, 1),
                "raw": val_data,
            }
        }
        
    except Exception as e:
        logger.warning("[market-prediction] valuation factors failed: %s", e)
        return {"score": 50, "details": {"error": str(e)}}


def _score_pe_percentile(val_data: Dict) -> float:
    """PE 分位评分（低估值高分）"""
    current_pe = val_data.get("current_pe", 0)
    pe_history = val_data.get("pe_history", [])
    
    if current_pe <= 0 or not pe_history:
        return 50
    
    # 计算当前 PE 在历史中的百分位
    from .common import percentile_rank
    pct = percentile_rank(current_pe, pe_history)
    
    # 百分位越低（越便宜）评分越高
    if pct < 10:
        return 95
    elif pct < 20:
        return 85
    elif pct < 30:
        return 75
    elif pct < 40:
        return 65
    elif pct < 50:
        return 55
    elif pct < 60:
        return 50
    elif pct < 70:
        return 40
    elif pct < 80:
        return 30
    elif pct < 90:
        return 20
    else:
        return 10


def _score_pb_percentile(val_data: Dict) -> float:
    """PB 分位评分"""
    current_pb = val_data.get("current_pb", 0)
    pb_history = val_data.get("pb_history", [])
    
    if current_pb <= 0 or not pb_history:
        return 50
    
    from .common import percentile_rank
    pct = percentile_rank(current_pb, pb_history)
    
    if pct < 10:
        return 95
    elif pct < 20:
        return 85
    elif pct < 30:
        return 75
    elif pct < 40:
        return 65
    elif pct < 50:
        return 55
    elif pct < 60:
        return 50
    elif pct < 70:
        return 40
    elif pct < 80:
        return 30
    elif pct < 90:
        return 20
    else:
        return 10


def _score_yield_gap(val_data: Dict) -> float:
    """股债利差评分（差值越大越好）"""
    yield_gap = val_data.get("yield_gap", 0)  # 股息率 - 10年国债收益率
    
    # 股债利差 > 2% 很安全，< 0% 危险
    if yield_gap > 2.5:
        return 95
    elif yield_gap > 2.0:
        return 85
    elif yield_gap > 1.5:
        return 75
    elif yield_gap > 1.0:
        return 65
    elif yield_gap > 0.5:
        return 55
    elif yield_gap > 0:
        return 50
    elif yield_gap > -0.5:
        return 40
    elif yield_gap > -1.0:
        return 30
    elif yield_gap > -1.5:
        return 20
    else:
        return 10


def _score_valuation_mean_reversion(val_data: Dict) -> float:
    """估值均值回归位置评分"""
    pe_ma = val_data.get("pe_ma_250", 0)
    current_pe = val_data.get("current_pe", 0)
    pb_ma = val_data.get("pb_ma_250", 0)
    current_pb = val_data.get("current_pb", 0)
    
    if pe_ma <= 0 or current_pe <= 0:
        return 50
    
    pe_ratio = current_pe / pe_ma
    pb_ratio = current_pb / pb_ma if pb_ma > 0 else 1
    
    # 综合比率
    avg_ratio = (pe_ratio + pb_ratio) / 2
    
    # 低于均值 0.8 倍很便宜，高于 1.2 倍很贵
    if avg_ratio < 0.75:
        return 90
    elif avg_ratio < 0.85:
        return 80
    elif avg_ratio < 0.95:
        return 65
    elif avg_ratio < 1.05:
        return 55
    elif avg_ratio < 1.15:
        return 45
    elif avg_ratio < 1.25:
        return 35
    else:
        return 20


# ================================================================
# 统一入口
# ================================================================

def compute_all_factors() -> Dict[str, Any]:
    """
    计算所有四维因子，返回统一格式。
    
    Returns:
        dict: {
            "technical": {"score": ..., "details": ..., "weight": 0.30},
            "fund_flow": {"score": ..., "details": ..., "weight": 0.25},
            "sentiment": {"score": ..., "details": ..., "weight": 0.20},
            "valuation": {"score": ..., "details": ..., "weight": 0.25},
            "composite_score": ...,  # 加权总分
            "timestamp": ...
        }
    """
    tech = compute_technical_factors()
    fund = compute_fund_flow_factors()
    sent = compute_sentiment_factors()
    val = compute_valuation_factors()
    
    weights = {
        "technical": 0.30,
        "fund_flow": 0.25,
        "sentiment": 0.20,
        "valuation": 0.25,
    }
    
    composite = (
        tech["score"] * weights["technical"] +
        fund["score"] * weights["fund_flow"] +
        sent["score"] * weights["sentiment"] +
        val["score"] * weights["valuation"]
    )
    
    return {
        "technical": {"score": tech["score"], "details": tech["details"], "weight": weights["technical"]},
        "fund_flow": {"score": fund["score"], "details": fund["details"], "weight": weights["fund_flow"]},
        "sentiment": {"score": sent["score"], "details": sent["details"], "weight": weights["sentiment"]},
        "valuation": {"score": val["score"], "details": val["details"], "weight": weights["valuation"]},
        "composite_score": round(composite, 1),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))