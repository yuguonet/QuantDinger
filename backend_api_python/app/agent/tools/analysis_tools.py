# -*- coding: utf-8 -*-
"""
Analysis tools — comprehensive technical analysis for agent.

v2: MACD / RSI / BOLL / KDJ / 多周期均线 / 改进K线形态识别
Pure-Python calculations on K-line data, no external API calls.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

def _get_ds(market: str = "CNStock"):
    from app.data_sources.factory import DataSourceFactory
    return DataSourceFactory.get_source(market)

# ── Re-exported from shared utils (kept for backward compat) ──
from app.agent.utils import detect_market as _detect_market

# ═══════════════════════════════════════════════════════════════
# 数据获取辅助
# ═══════════════════════════════════════════════════════════════

def _fetch_klines(stock_code: str, days: int = 120) -> List[Dict[str, Any]]:
    """获取原始K线数据（含 OHLCV）。"""
    market = _detect_market(stock_code)
    ds = _get_ds(market)
    return ds.get_kline(stock_code, "1D", days) or []

def _fetch_closes(stock_code: str, days: int = 120) -> List[float]:
    """Fetch close prices from data source."""
    klines = _fetch_klines(stock_code, days)
    return [float(k.get("close", 0)) for k in klines if k.get("close")]

def _fetch_ohlcv(stock_code: str, days: int = 120) -> Dict[str, List[float]]:
    """获取 OHLCV 五组数据序列。"""
    klines = _fetch_klines(stock_code, days)
    o, h, l, c, v = [], [], [], [], []
    for k in klines:
        o.append(float(k.get("open", 0)))
        h.append(float(k.get("high", 0)))
        l.append(float(k.get("low", 0)))
        c.append(float(k.get("close", 0)))
        v.append(float(k.get("volume", 0)))
    return {"open": o, "high": h, "low": l, "close": c, "volume": v}

def _safe_round(v: float, n: int = 4) -> float:
    if v is None or math.isnan(v) or math.isinf(v):
        return 0.0
    return round(v, n)

# ═══════════════════════════════════════════════════════════════
# 指标计算核心（纯 Python，无外部依赖）
# ═══════════════════════════════════════════════════════════════

def _ema(data: List[float], period: int) -> List[float]:
    """指数移动平均"""
    if not data:
        return []
    result = [data[0]]
    k = 2.0 / (period + 1)
    for i in range(1, len(data)):
        result.append(data[i] * k + result[-1] * (1 - k))
    return result

def _sma(data: List[float], period: int) -> List[float]:
    """简单移动平均（返回与 data 等长序列，前 period-1 个用已有数据均值填充）"""
    if not data:
        return []
    result = []
    for i in range(len(data)):
        start = max(0, i - period + 1)
        window = data[start:i + 1]
        result.append(sum(window) / len(window))
    return result

def _calc_macd(closes: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Dict[str, Any]:
    """MACD 指标计算，返回最新值 + 趋势判断。"""
    if len(closes) < slow + signal:
        return {"error": f"数据不足，MACD需要至少{slow + signal}根K线"}

    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    dif = [ema_fast[i] - ema_slow[i] for i in range(len(closes))]
    dea = _ema(dif, signal)
    macd_bar = [(dif[i] - dea[i]) * 2 for i in range(len(closes))]

    # 最新值
    latest_dif = _safe_round(dif[-1])
    latest_dea = _safe_round(dea[-1])
    latest_macd = _safe_round(macd_bar[-1])
    prev_macd = _safe_round(macd_bar[-2]) if len(macd_bar) >= 2 else 0

    # 趋势判断
    signals = []
    if dif[-1] > dea[-1] and dif[-2] <= dea[-2]:
        signals.append("MACD金叉（看多）")
    elif dif[-1] < dea[-1] and dif[-2] >= dea[-2]:
        signals.append("MACD死叉（看空）")

    if latest_macd > 0 and latest_macd > prev_macd:
        bar_trend = "红柱放大"
    elif latest_macd > 0:
        bar_trend = "红柱缩小"
    elif latest_macd < 0 and latest_macd < prev_macd:
        bar_trend = "绿柱放大"
    elif latest_macd < 0:
        bar_trend = "绿柱缩小"
    else:
        bar_trend = "零轴附近"

    # 零轴上下
    position = "零轴之上（多头区域）" if latest_dif > 0 else "零轴之下（空头区域）"

    return {
        "dif": latest_dif,
        "dea": latest_dea,
        "macd": latest_macd,
        "bar_trend": bar_trend,
        "position": position,
        "signals": signals,
    }

def _calc_rsi(closes: List[float], periods: List[int] = None) -> Dict[str, Any]:
    """RSI 指标计算（Wilder 平滑法）。"""
    if periods is None:
        periods = [6, 12, 24]

    max_p = max(periods)
    if len(closes) < max_p + 1:
        return {"error": f"数据不足，RSI需要至少{max_p + 1}根K线"}

    result = {}
    for p in periods:
        gains, losses = [], []
        for i in range(1, len(closes)):
            delta = closes[i] - closes[i - 1]
            gains.append(max(delta, 0))
            losses.append(max(-delta, 0))

        # Wilder smoothing
        avg_gain = sum(gains[:p]) / p
        avg_loss = sum(losses[:p]) / p
        for i in range(p, len(gains)):
            avg_gain = (avg_gain * (p - 1) + gains[i]) / p
            avg_loss = (avg_loss * (p - 1) + losses[i]) / p

        if avg_loss == 0:
            rsi_val = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi_val = 100 - 100 / (1 + rs)

        rsi_val = _safe_round(rsi_val, 2)

        # 区间判断
        if rsi_val >= 80:
            zone = "超买"
        elif rsi_val >= 70:
            zone = "偏强"
        elif rsi_val >= 50:
            zone = "中性偏多"
        elif rsi_val >= 30:
            zone = "中性偏空"
        elif rsi_val >= 20:
            zone = "偏弱"
        else:
            zone = "超卖"

        result[f"rsi{p}"] = rsi_val
        result[f"rsi{p}_zone"] = zone

    # 综合信号
    rsi6 = result.get("rsi6", 50)
    signals = []
    if rsi6 >= 80:
        signals.append("RSI超买，短期回调风险大")
    elif rsi6 <= 20:
        signals.append("RSI超卖，可能存在反弹机会")
    elif rsi6 >= 70:
        signals.append("RSI偏强，注意追高风险")
    elif rsi6 <= 30:
        signals.append("RSI偏弱，空头占优")

    result["signals"] = signals
    return result

def _calc_boll(closes: List[float], period: int = 20, std_dev: float = 2.0) -> Dict[str, Any]:
    """布林带计算。"""
    if len(closes) < period:
        return {"error": f"数据不足，BOLL需要至少{period}根K线"}

    # 中轨
    mid = sum(closes[-period:]) / period

    # 标准差
    variance = sum((c - mid) ** 2 for c in closes[-period:]) / period
    std = math.sqrt(variance)

    upper = mid + std_dev * std
    lower = mid - std_dev * std
    latest = closes[-1]

    # 带宽
    bandwidth = _safe_round((upper - lower) / mid * 100, 2) if mid else 0

    # 位置判断
    if upper != lower:
        position_pct = _safe_round((latest - lower) / (upper - lower) * 100, 2)
    else:
        position_pct = 50

    signals = []
    if latest >= upper:
        signals.append("触及上轨，短期超买或突破信号")
    elif latest <= lower:
        signals.append("触及下轨，短期超卖或破位信号")
    elif position_pct >= 80:
        signals.append("接近上轨，注意压力")
    elif position_pct <= 20:
        signals.append("接近下轨，关注支撑")

    # 带宽收窄 → 变盘信号
    if bandwidth < 5:
        signals.append("布林带极度收窄，即将变盘")

    return {
        "upper": _safe_round(upper),
        "mid": _safe_round(mid),
        "lower": _safe_round(lower),
        "bandwidth": bandwidth,
        "position_pct": position_pct,
        "latest_price": _safe_round(latest),
        "signals": signals,
    }

def _calc_kdj(highs: List[float], lows: List[float], closes: List[float],
              n: int = 9, m1: int = 3, m2: int = 3) -> Dict[str, Any]:
    """KDJ 随机指标计算。"""
    length = len(closes)
    if length < n:
        return {"error": f"数据不足，KDJ需要至少{n}根K线"}

    k_values = [50.0]
    d_values = [50.0]

    for i in range(n - 1, length):
        window_high = max(highs[i - n + 1:i + 1])
        window_low = min(lows[i - n + 1:i + 1])
        if window_high == window_low:
            rsv = 50.0
        else:
            rsv = (closes[i] - window_low) / (window_high - window_low) * 100

        k = (m1 - 1) / m1 * k_values[-1] + 1 / m1 * rsv
        d = (m2 - 1) / m2 * d_values[-1] + 1 / m2 * k
        k_values.append(k)
        d_values.append(d)

    j_values = [3 * k_values[i] - 2 * d_values[i] for i in range(len(k_values))]

    latest_k = _safe_round(k_values[-1], 2)
    latest_d = _safe_round(d_values[-1], 2)
    latest_j = _safe_round(j_values[-1], 2)
    prev_k = _safe_round(k_values[-2], 2) if len(k_values) >= 2 else 50
    prev_d = _safe_round(d_values[-2], 2) if len(d_values) >= 2 else 50

    signals = []
    # 金叉/死叉
    if latest_k > latest_d and prev_k <= prev_d:
        signals.append("KDJ金叉（看多）")
    elif latest_k < latest_d and prev_k >= prev_d:
        signals.append("KDJ死叉（看空）")

    # 超买超卖
    if latest_j >= 100:
        signals.append("J值超买（>100），短期回调风险")
    elif latest_j <= 0:
        signals.append("J值超卖（<0），可能存在反弹")

    if latest_k >= 80 and latest_d >= 80:
        signals.append("KD高位钝化，强势但注意风险")
    elif latest_k <= 20 and latest_d <= 20:
        signals.append("KD低位钝化，弱势但可能筑底")

    return {
        "k": latest_k,
        "d": latest_d,
        "j": latest_j,
        "signals": signals,
    }

# ═══════════════════════════════════════════════════════════════
# Tool 函数（注册给 Agent 调用）
# ═══════════════════════════════════════════════════════════════

def analyze_trend(stock_code: str) -> Dict[str, Any]:
    """获取股票的综合技术趋势分析，包括均线排列、MACD、RSI、BOLL和KDJ等指标。

    Args:
        stock_code: 股票代码，如 "600519"
    """
    try:
        data = _fetch_ohlcv(stock_code, 120)
        closes = data["close"]
        if len(closes) < 5:
            return {"error": "K线数据不足（至少需要5根）", "retriable": True}

        latest = closes[-1]
        prev = closes[-2] if len(closes) >= 2 else latest

        # ── 均线 ──
        ma5 = _safe_round(sum(closes[-5:]) / 5)
        ma10 = _safe_round(sum(closes[-10:]) / 10) if len(closes) >= 10 else ma5
        ma20 = _safe_round(sum(closes[-20:]) / 20) if len(closes) >= 20 else ma10
        ma60 = _safe_round(sum(closes[-60:]) / 60) if len(closes) >= 60 else _safe_round(sum(closes) / len(closes))
        ma120 = _safe_round(sum(closes[-120:]) / 120) if len(closes) >= 120 else ma60

        # 均线排列评分（0-100）
        ma_score = 50
        if ma5 > ma10 > ma20 > ma60:
            ma_desc = "强多头排列"
            ma_score = 90
        elif ma5 > ma10 > ma20:
            ma_desc = "多头排列"
            ma_score = 75
        elif ma5 > ma20:
            ma_desc = "弱势多头"
            ma_score = 60
        elif ma5 < ma10 < ma20 < ma60:
            ma_desc = "强空头排列"
            ma_score = 10
        elif ma5 < ma10 < ma20:
            ma_desc = "空头排列"
            ma_score = 25
        elif ma5 < ma20:
            ma_desc = "弱势空头"
            ma_score = 40
        else:
            ma_desc = "均线缠绕/震荡"
            ma_score = 50

        # 乖离率
        bias_ma5 = _safe_round((latest - ma5) / ma5 * 100, 2) if ma5 else 0
        bias_ma20 = _safe_round((latest - ma20) / ma20 * 100, 2) if ma20 else 0

        # ── MACD ──
        macd_result = _calc_macd(closes)
        macd_score = 50
        if isinstance(macd_result, dict) and "error" not in macd_result:
            if macd_result.get("dif", 0) > macd_result.get("dea", 0):
                macd_score = 70 if macd_result.get("macd", 0) > 0 else 60
            else:
                macd_score = 30 if macd_result.get("macd", 0) < 0 else 40
            if "金叉" in str(macd_result.get("signals", [])):
                macd_score += 15
            elif "死叉" in str(macd_result.get("signals", [])):
                macd_score -= 15

        # ── RSI ──
        rsi_result = _calc_rsi(closes, [6, 12, 24])
        rsi_score = 50
        if isinstance(rsi_result, dict) and "error" not in rsi_result:
            rsi6 = rsi_result.get("rsi6", 50)
            if rsi6 >= 70:
                rsi_score = 25  # 超买 = 看空
            elif rsi6 <= 30:
                rsi_score = 75  # 超卖 = 看多
            else:
                rsi_score = int(rsi6)

        # ── 布林带 ──
        boll_result = _calc_boll(closes)
        boll_score = 50
        if isinstance(boll_result, dict) and "error" not in boll_result:
            pos = boll_result.get("position_pct", 50)
            if pos >= 80:
                boll_score = 30  # 接近上轨 = 风险
            elif pos <= 20:
                boll_score = 70  # 接近下轨 = 机会
            else:
                boll_score = int(50 + (50 - pos) * 0.4)

        # ── KDJ ──
        kdj_result = _calc_kdj(data["high"], data["low"], closes)
        kdj_score = 50
        if isinstance(kdj_result, dict) and "error" not in kdj_result:
            j = kdj_result.get("j", 50)
            if j >= 100:
                kdj_score = 25
            elif j <= 0:
                kdj_score = 75
            elif "金叉" in str(kdj_result.get("signals", [])):
                kdj_score = 70
            elif "死叉" in str(kdj_result.get("signals", [])):
                kdj_score = 30

        # ── 综合评分 ──
        # 权重：均线30% + MACD25% + RSI20% + BOLL15% + KDJ10%
        total_score = int(
            ma_score * 0.30 +
            macd_score * 0.25 +
            rsi_score * 0.20 +
            boll_score * 0.15 +
            kdj_score * 0.10
        )
        total_score = max(0, min(100, total_score))

        # 综合信号
        if total_score >= 75:
            overall = "看多"
            buy_signal = "多指标共振看多"
            signal_score = total_score
        elif total_score >= 60:
            overall = "偏多"
            buy_signal = "偏多但需确认"
            signal_score = total_score
        elif total_score <= 25:
            overall = "看空"
            buy_signal = "多指标共振看空，建议回避"
            signal_score = total_score
        elif total_score <= 40:
            overall = "偏空"
            buy_signal = "偏空，观望为主"
            signal_score = total_score
        else:
            overall = "震荡"
            buy_signal = "多空分歧，观望"
            signal_score = total_score

        # 汇总所有子信号
        all_signals = []
        if isinstance(macd_result, dict):
            all_signals.extend(macd_result.get("signals", []))
        if isinstance(rsi_result, dict):
            all_signals.extend(rsi_result.get("signals", []))
        if isinstance(boll_result, dict):
            all_signals.extend(boll_result.get("signals", []))
        if isinstance(kdj_result, dict):
            all_signals.extend(kdj_result.get("signals", []))

        change_pct = _safe_round((latest - prev) / prev * 100, 2) if prev else 0

        return {
            "stock_code": stock_code,
            "latest_close": _safe_round(latest),
            "change_pct": change_pct,
            # 均线
            "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60, "ma120": ma120,
            "ma_alignment": ma_desc,
            "bias_ma5": bias_ma5, "bias_ma20": bias_ma20,
            # MACD
            "macd": macd_result,
            # RSI
            "rsi": rsi_result,
            # 布林带
            "boll": boll_result,
            # KDJ
            "kdj": kdj_result,
            # 综合
            "trend_score": total_score,
            "trend": overall,
            "buy_signal": buy_signal,
            "signal_score": signal_score,
            "all_signals": all_signals,
            "data_points": len(closes),
        }
    except Exception as e:
        logger.error("analyze_trend(%s) failed: %s", stock_code, e)
        return {"error": str(e)}

def calculate_ma(stock_code: str, periods: str = "5,10,20,60,120") -> Dict[str, Any]:
    """计算均线指标。

    Args:
        stock_code: 股票代码，如 "600519"
        periods: 均线周期列表，默认 [5,10,20,60,120,250]
    """
    try:
        period_list = sorted(set(int(p.strip()) for p in periods.split(",") if p.strip().isdigit()))
        if not period_list:
            return {"error": "无效的周期参数"}

        max_period = max(period_list)
        closes = _fetch_closes(stock_code, max_period + 10)
        if len(closes) < max_period:
            return {"error": f"数据不足，需要至少{max_period}根K线"}

        result = {"stock_code": stock_code, "latest_close": _safe_round(closes[-1])}
        for p in period_list:
            if len(closes) < p:
                continue
            avg = _safe_round(sum(closes[-p:]) / p)
            result[f"ma{p}"] = avg
            # 斜率：与前一天对比
            if len(closes) >= p + 1:
                prev_avg = _safe_round(sum(closes[-p - 1:-1]) / p)
                slope_pct = _safe_round((avg - prev_avg) / prev_avg * 100, 2) if prev_avg else 0
                result[f"ma{p}_slope"] = slope_pct
                result[f"ma{p}_trend"] = "上行" if slope_pct > 0.05 else ("下行" if slope_pct < -0.05 else "走平")
        return result
    except Exception as e:
        logger.error("calculate_ma(%s) failed: %s", stock_code, e)
        return {"error": str(e)}

def get_volume_analysis(stock_code: str) -> Dict[str, Any]:
    """量能分析：量比、换手率、成交量趋势。

    Args:
        stock_code: 股票代码，如 "600519"
    """
    try:
        data = _fetch_ohlcv(stock_code, 30)
        volumes = data["volume"]
        closes = data["close"]
        if len(volumes) < 5:
            return {"error": "K线数据不足"}

        latest_vol = volumes[-1]
        avg_vol_5 = sum(volumes[-5:]) / 5
        avg_vol_20 = sum(volumes) / len(volumes)

        volume_ratio = _safe_round(latest_vol / avg_vol_5, 2) if avg_vol_5 else 0

        if volume_ratio > 2.0:
            status = "显著放量"
            meaning = "放量，需结合价格方向判断"
        elif volume_ratio > 1.5:
            status = "温和放量"
            meaning = "成交活跃度提升"
        elif volume_ratio < 0.5:
            status = "明显缩量"
            meaning = "缩量，抛压减轻或交投清淡"
        elif volume_ratio < 0.8:
            status = "温和缩量"
            meaning = "成交活跃度下降"
        else:
            status = "平量"
            meaning = "成交量维持常态"

        # 成交量趋势
        vol_trend = "数据不足"
        if len(volumes) >= 6:
            recent_avg = sum(volumes[-3:]) / 3
            earlier_avg = sum(volumes[-6:-3]) / 3
            vol_trend = "上升" if recent_avg > earlier_avg * 1.1 else ("下降" if recent_avg < earlier_avg * 0.9 else "平稳")

        # 量价关系
        vol_price_relation = "数据不足"
        if len(closes) >= 2 and latest_vol > 0:
            price_change = closes[-1] - closes[-2]
            if price_change > 0 and volume_ratio > 1.3:
                vol_price_relation = "量价齐升（健康上涨）"
            elif price_change > 0 and volume_ratio < 0.7:
                vol_price_relation = "缩量上涨（上涨乏力，需警惕）"
            elif price_change < 0 and volume_ratio > 1.3:
                vol_price_relation = "放量下跌（恐慌抛售）"
            elif price_change < 0 and volume_ratio < 0.7:
                vol_price_relation = "缩量下跌（下跌动能减弱）"
            else:
                vol_price_relation = "量价配合一般"

        return {
            "stock_code": stock_code,
            "latest_volume": _safe_round(latest_vol, 0),
            "avg_volume_5d": _safe_round(avg_vol_5, 0),
            "avg_volume_20d": _safe_round(avg_vol_20, 0),
            "volume_ratio": volume_ratio,
            "volume_status": status,
            "volume_meaning": meaning,
            "volume_trend": vol_trend,
            "vol_price_relation": vol_price_relation,
        }
    except Exception as e:
        logger.error("get_volume_analysis(%s) failed: %s", stock_code, e)
        return {"error": str(e)}

def analyze_pattern(stock_code: str) -> Dict[str, Any]:
    """识别K线形态（增强版）：锤子线、十字星、吞没、早晨/晚星、三连阳/阴、长上影/下影、缺口等。

    Args:
        stock_code: 股票代码，如 "600519"
    """
    try:
        data = _fetch_ohlcv(stock_code, 10)
        if len(data["close"]) < 3:
            return {"error": "K线数据不足"}

        patterns = []
        o, h, l, c = data["open"], data["high"], data["low"], data["close"]
        n = len(c)

        # 最近一根
        lo, lh, ll, lc = o[-1], h[-1], l[-1], c[-1]
        body = abs(lc - lo)
        candle_range = lh - ll if lh > ll else 0.001
        upper_shadow = lh - max(lo, lc)
        lower_shadow = min(lo, lc) - ll

        # ── 单根形态 ──

        # 锤子线 / 倒锤子
        if body > 0 and candle_range > 0:
            if lower_shadow >= 2 * body and upper_shadow <= body * 0.3:
                patterns.append("锤子线（底部反转信号）")
            elif upper_shadow >= 2 * body and lower_shadow <= body * 0.3:
                patterns.append("倒锤子线（顶部反转信号）")

        # 十字星
        if body <= candle_range * 0.1:
            if upper_shadow > candle_range * 0.3 and lower_shadow > candle_range * 0.3:
                patterns.append("长腿十字星（强烈犹豫信号）")
            elif upper_shadow > candle_range * 0.4 and lower_shadow < candle_range * 0.1:
                patterns.append("墓碑线（顶部反转）")
            elif lower_shadow > candle_range * 0.4 and upper_shadow < candle_range * 0.1:
                patterns.append("蜻蜓线（底部反转）")
            else:
                patterns.append("十字星（犹豫信号）")

        # 长上影线
        if upper_shadow > body * 2 and upper_shadow > lower_shadow * 2:
            patterns.append("长上影线（上方压力大）")

        # 长下影线
        if lower_shadow > body * 2 and lower_shadow > upper_shadow * 2:
            patterns.append("长下影线（下方支撑强）")

        # 光头光脚大阳线/大阴线
        if body > candle_range * 0.85:
            if lc > lo:
                patterns.append("光头光脚大阳线（强势）")
            else:
                patterns.append("光头光脚大阴线（弱势）")

        # ── 两根形态 ──
        if n >= 2:
            po, pc = o[-2], c[-2]
            prev_body = abs(pc - po)

            # 吞没形态
            if prev_body > 0 and body > prev_body:
                if pc < po and lc > lo and lo <= pc and lc >= po:
                    patterns.append("看涨吞没（底部反转）")
                elif pc > po and lc < lo and lo >= pc and lc <= po:
                    patterns.append("看跌吞没（顶部反转）")

        # ── 三根形态 ──
        if n >= 3:
            o1, c1 = o[-3], c[-3]
            o2, c2 = o[-2], c[-2]
            o3, c3 = o[-1], c[-1]
            body1 = abs(c1 - o1)
            body2 = abs(c2 - o2)
            body3 = abs(c3 - o3)

            # 早晨之星
            if (c1 < o1 and body1 > 0 and
                    body2 < body1 * 0.3 and
                    c3 > o3 and body3 > 0 and
                    c3 > (o1 + c1) / 2):
                patterns.append("早晨之星（底部反转，看多）")

            # 黄昏之星
            if (c1 > o1 and body1 > 0 and
                    body2 < body1 * 0.3 and
                    c3 < o3 and body3 > 0 and
                    c3 < (o1 + c1) / 2):
                patterns.append("黄昏之星（顶部反转，看空）")

            # 三连阳
            if c1 > o1 and c2 > o2 and c3 > o3:
                if body3 > body2 > body1:
                    patterns.append("三连阳递增（强势看多）")
                else:
                    patterns.append("三连阳（多头排列）")

            # 三连阴
            if c1 < o1 and c2 < o2 and c3 < o3:
                if body3 > body2 > body1:
                    patterns.append("三连阴递增（强势看空）")
                else:
                    patterns.append("三连阴（空头排列）")

            # 红三兵
            if (c1 > o1 and c2 > o2 and c3 > o3 and
                    c2 > c1 and c3 > c2 and
                    o2 > o1 and o3 > o2):
                patterns.append("红三兵（强烈看多）")

            # 黑三鸦
            if (c1 < o1 and c2 < o2 and c3 < o3 and
                    c2 < c1 and c3 < c2 and
                    o2 < o1 and o3 < o2):
                patterns.append("黑三鸦（强烈看空）")

        # ── 缺口检测 ──
        if n >= 2:
            prev_high = h[-2]
            prev_low = l[-2]
            if l[-1] > prev_high:
                gap_size = _safe_round((l[-1] - prev_high) / prev_high * 100, 2)
                patterns.append(f"向上跳空缺口（幅度{gap_size}%）")
            elif h[-1] < prev_low:
                gap_size = _safe_round((prev_low - h[-1]) / prev_low * 100, 2)
                patterns.append(f"向下跳空缺口（幅度{gap_size}%）")

        if not patterns:
            patterns.append("无明显特殊形态")

        return {
            "stock_code": stock_code,
            "latest_candle": {"open": _safe_round(lo), "high": _safe_round(lh),
                              "low": _safe_round(ll), "close": _safe_round(lc)},
            "body_size": _safe_round(body),
            "upper_shadow": _safe_round(upper_shadow),
            "lower_shadow": _safe_round(lower_shadow),
            "patterns": patterns,
            "pattern_count": len([p for p in patterns if "无明显" not in p]),
        }
    except Exception as e:
        logger.error("analyze_pattern(%s) failed: %s", stock_code, e)
        return {"error": str(e)}

def get_chip_distribution(stock_code: str, lookback_days: int = 120) -> Dict[str, Any]:
    """筹码分布分析（衰减成本分布模型）。

    从日K线计算筹码分布，不依赖数据源原生接口。
    算法：按日K线的 high/low 区间分配成交量到价格档位，
    用指数衰减加权（近期筹码权重更高），汇总计算各维度指标。

    Args:
        stock_code: 股票代码（str 或 search_stock 返回的 dict）
        lookback_days: 回看天数，默认120天
    """
    # 兼容 search_stock 返回的 dict: {'results': [{'code': '600593', ...}], ...}
    if isinstance(stock_code, dict):
        results = stock_code.get("results", [])
        if results:
            stock_code = results[0].get("code", "")
        else:
            return {"error": "stock_code dict 中无 results", "retriable": False}
    stock_code = str(stock_code).strip()
    if not stock_code:
        return {"error": "stock_code 为空", "retriable": False}
    market = _detect_market(stock_code)
    if market != "CNStock":
        return {"error": f"筹码分布分析仅支持A股，当前市场: {market}", "retriable": False}

    try:
        klines = _fetch_klines(stock_code, lookback_days)
        from app.agent.tools.chip_distribution import calc_chip_distribution
        return calc_chip_distribution(klines, stock_code=stock_code, lookback_days=lookback_days)
    except Exception as e:
        logger.error("get_chip_distribution(%s) failed: %s", stock_code, e, exc_info=True)
        return {"error": str(e)}

def get_indicator_snapshot(stock_code: str) -> Dict[str, Any]:
    """单次获取多个技术指标快照（MACD、RSI、BOLL、KDJ等）。

    Args:
        stock_code: 股票代码，如 "600519"
    """
    try:
        data = _fetch_ohlcv(stock_code, 120)
        closes = data["close"]
        if len(closes) < 10:
            return {"error": "K线数据不足（至少需要10根）", "retriable": True}

        result = {"stock_code": stock_code, "latest_close": _safe_round(closes[-1])}

        # 均线
        for p in [5, 10, 20, 60, 120]:
            if len(closes) >= p:
                result[f"ma{p}"] = _safe_round(sum(closes[-p:]) / p)

        # MACD
        macd = _calc_macd(closes)
        if "error" not in macd:
            result["macd"] = macd

        # RSI
        rsi = _calc_rsi(closes, [6, 12, 24])
        if "error" not in rsi:
            result["rsi"] = rsi

        # BOLL
        boll = _calc_boll(closes)
        if "error" not in boll:
            result["boll"] = boll

        # KDJ
        kdj = _calc_kdj(data["high"], data["low"], closes)
        if "error" not in kdj:
            result["kdj"] = kdj

        # 量能
        if len(data["volume"]) >= 5:
            vol = data["volume"][-1]
            avg5 = sum(data["volume"][-5:]) / 5
            result["volume_ratio"] = _safe_round(vol / avg5, 2) if avg5 else 0

        return result
    except Exception as e:
        logger.error("get_indicator_snapshot(%s) failed: %s", stock_code, e)
        return {"error": str(e)}

# ── OpenAI tool declarations ─────────────────────────────────

