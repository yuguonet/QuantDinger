# -*- coding: utf-8 -*-
"""
Analysis tools — comprehensive technical analysis for agent.

v2: MACD / RSI / BOLL / KDJ / 多周期均线 / 改进K线形态识别
Pure-Python calculations on K-line data, no external API calls.
"""
from __future__ import annotations

import json

from app.agent.log import logger
import math
from typing import Any, Dict, List, Optional, Tuple

def _get_ds(market: str = "CNStock"):
    from app.data_sources.factory import DataSourceFactory
    return DataSourceFactory.get_source(market)

# ── Re-exported from shared utils (kept for backward compat) ──
from app.agent.utils import detect_market as _detect_market
from app.agent.utils.md_format import _format_final_md, _format_output, _batch_execute

# ═══════════════════════════════════════════════════════════════
# 数据获取辅助（带请求级缓存，同一次分析内复用）
# ═══════════════════════════════════════════════════════════════
import time as _time

_kline_cache: Dict[str, tuple] = {}  # key=(code,days) → (timestamp, data)
_KLINE_CACHE_TTL = 60  # 60秒内同参数直接返回缓存


def _fetch_klines(stock_code: str, days: int = 120) -> List[Dict[str, Any]]:
    """获取原始K线数据（含 OHLCV）。同参数60秒内返回缓存。"""
    cache_key = f"{stock_code}:{days}"
    now = _time.time()
    if cache_key in _kline_cache:
        ts, data = _kline_cache[cache_key]
        if now - ts < _KLINE_CACHE_TTL:
            return data
    market = _detect_market(stock_code)
    ds = _get_ds(market)
    data = ds.get_kline(stock_code, "1D", days) or []
    _kline_cache[cache_key] = (now, data)
    return data

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

def _calc_obv(closes: List[float], volumes: List[float]) -> Dict[str, Any]:
    """OBV（能量潮）指标计算。"""
    if len(closes) < 3 or len(closes) != len(volumes):
        return {"error": "数据不足"}

    obv = [0.0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            obv.append(obv[-1] + volumes[i])
        elif closes[i] < closes[i - 1]:
            obv.append(obv[-1] - volumes[i])
        else:
            obv.append(obv[-1])

    # OBV 趋势：近 5 日斜率
    window = min(5, len(obv))
    if window >= 2:
        obv_slope = (obv[-1] - obv[-window]) / (window - 1)
    else:
        obv_slope = 0

    # OBV 与价格背离检测
    divergence = "无"
    if len(closes) >= 10:
        price_up = closes[-1] > closes[-10]
        obv_up = obv[-1] > obv[-10]
        if price_up and not obv_up:
            divergence = "顶背离（价格新高但OBV未新高，量价背离看空）"
        elif not price_up and obv_up:
            divergence = "底背离（价格新低但OBV未新低，量价背离看多）"

    # OBV 均线
    obv_ma = sum(obv[-10:]) / min(10, len(obv))
    obv_vs_ma = "OBV在均线上方（多头量能）" if obv[-1] > obv_ma else "OBV在均线下方（空头量能）"

    signals = []
    if divergence != "无":
        signals.append(divergence)
    if obv_slope > 0 and closes[-1] > closes[-2]:
        signals.append("OBV上升+价格上涨，量价配合良好")
    elif obv_slope < 0 and closes[-1] < closes[-2]:
        signals.append("OBV下降+价格下跌，空头量能释放")
    elif obv_slope > 0 and closes[-1] < closes[-2]:
        signals.append("OBV上升但价格下跌，可能有资金吸筹")
    elif obv_slope < 0 and closes[-1] > closes[-2]:
        signals.append("OBV下降但价格上涨，上涨缺乏量能支撑")

    return {
        "obv": _safe_round(obv[-1], 0),
        "obv_prev": _safe_round(obv[-2], 0),
        "obv_slope": _safe_round(obv_slope, 0),
        "obv_trend": "上升" if obv_slope > 0 else ("下降" if obv_slope < 0 else "走平"),
        "divergence": divergence,
        "obv_vs_ma": obv_vs_ma,
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
def _calc_atr(highs: List[float], lows: List[float], closes: List[float],
              period: int = 14) -> Dict[str, Any]:
    """ATR（真实波幅均值）— 波动率指标。"""
    n = len(closes)
    if n < period + 1:
        return {"error": f"数据不足，ATR需要至少{period + 1}根K线"}

    tr_list = []
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        tr_list.append(tr)

    # Wilder 平滑
    atr_val = sum(tr_list[:period]) / period
    for i in range(period, len(tr_list)):
        atr_val = (atr_val * (period - 1) + tr_list[i]) / period

    atr_pct = _safe_round(atr_val / closes[-1] * 100, 2) if closes[-1] else 0

    # ATR 趋势（近 5 日）
    atr_recent = []
    temp_atr = sum(tr_list[:period]) / period
    for i in range(period, len(tr_list)):
        temp_atr = (temp_atr * (period - 1) + tr_list[i]) / period
        if i >= len(tr_list) - 5:
            atr_recent.append(temp_atr)

    if len(atr_recent) >= 2:
        atr_trend = "扩大" if atr_recent[-1] > atr_recent[0] * 1.1 else (
            "收缩" if atr_recent[-1] < atr_recent[0] * 0.9 else "平稳")
    else:
        atr_trend = "数据不足"

    # 波动率等级
    if atr_pct >= 5:
        vol_level = "极高波动（日内振幅大，适合短线）"
    elif atr_pct >= 3:
        vol_level = "高波动"
    elif atr_pct >= 2:
        vol_level = "中等波动"
    elif atr_pct >= 1:
        vol_level = "低波动（横盘整理）"
    else:
        vol_level = "极低波动（变盘前兆）"

    return {
        "atr": _safe_round(atr_val),
        "atr_pct": atr_pct,
        "atr_trend": atr_trend,
        "volatility_level": vol_level,
    }
def _calc_mfi(highs: List[float], lows: List[float], closes: List[float],
              volumes: List[float], period: int = 14) -> Dict[str, Any]:
    """MFI（资金流量指标）— 量价结合的 RSI。"""
    n = len(closes)
    if n < period + 1:
        return {"error": f"数据不足，MFI需要至少{period + 1}根K线"}

    # 典型价格
    tp = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(n)]
    mf = [tp[i] * volumes[i] for i in range(n)]

    pos_mf, neg_mf = [], []
    for i in range(1, n):
        if tp[i] > tp[i - 1]:
            pos_mf.append(mf[i])
            neg_mf.append(0)
        elif tp[i] < tp[i - 1]:
            pos_mf.append(0)
            neg_mf.append(mf[i])
        else:
            pos_mf.append(0)
            neg_mf.append(0)

    # Wilder 平滑
    avg_pos = sum(pos_mf[:period]) / period
    avg_neg = sum(neg_mf[:period]) / period
    for i in range(period, len(pos_mf)):
        avg_pos = (avg_pos * (period - 1) + pos_mf[i]) / period
        avg_neg = (avg_neg * (period - 1) + neg_mf[i]) / period

    if avg_neg == 0:
        mfi_val = 100.0
    else:
        mfr = avg_pos / avg_neg
        mfi_val = 100 - 100 / (1 + mfr)

    mfi_val = _safe_round(mfi_val, 2)

    signals = []
    if mfi_val >= 80:
        signals.append("MFI超买（>80），资金流入过热")
    elif mfi_val <= 20:
        signals.append("MFI超卖（<20），资金流出过度")

    return {
        "mfi": mfi_val,
        "signals": signals,
    }
def _calc_cmf(highs: List[float], lows: List[float], closes: List[float],
              volumes: List[float], period: int = 20) -> Dict[str, Any]:
    """CMF（蔡金资金流量）— 衡量一段时间内资金流入/流出强度。"""
    n = len(closes)
    if n < period:
        return {"error": f"数据不足，CMF需要至少{period}根K线"}

    # CLV = ((close - low) - (high - close)) / (high - low)
    clv = []
    for i in range(n):
        hl_range = highs[i] - lows[i]
        if hl_range == 0:
            clv.append(0)
        else:
            clv.append(((closes[i] - lows[i]) - (highs[i] - closes[i])) / hl_range)

    ad_line = [clv[i] * volumes[i] for i in range(n)]

    # CMF = sum(AD, period) / sum(Volume, period)
    cmf_vals = []
    for i in range(period - 1, n):
        ad_sum = sum(ad_line[i - period + 1:i + 1])
        vol_sum = sum(volumes[i - period + 1:i + 1])
        if vol_sum == 0:
            cmf_vals.append(0)
        else:
            cmf_vals.append(ad_sum / vol_sum)

    cmf_val = _safe_round(cmf_vals[-1], 4) if cmf_vals else 0

    signals = []
    if cmf_val > 0.1:
        signals.append("CMF强正值，持续资金流入")
    elif cmf_val > 0:
        signals.append("CMF正值，资金温和流入")
    elif cmf_val < -0.1:
        signals.append("CMF强负值，持续资金流出")
    elif cmf_val < 0:
        signals.append("CMF负值，资金温和流出")

    return {
        "cmf": cmf_val,
        "signals": signals,
    }
def _calc_hist_volatility(closes: List[float], period: int = 20) -> Dict[str, Any]:
    """历史波动率（基于对数收益率标准差，年化）。"""
    n = len(closes)
    if n < period + 1:
        return {"error": f"数据不足，波动率需要至少{period + 1}根K线"}

    log_returns = []
    for i in range(1, n):
        if closes[i] > 0 and closes[i - 1] > 0:
            log_returns.append(math.log(closes[i] / closes[i - 1]))
        else:
            log_returns.append(0)

    # 滚动计算
    if len(log_returns) < period:
        return {"error": "收益率数据不足"}

    recent = log_returns[-period:]
    mean_r = sum(recent) / period
    variance = sum((r - mean_r) ** 2 for r in recent) / (period - 1)
    daily_vol = math.sqrt(variance)
    annual_vol = daily_vol * math.sqrt(242)  # A股约 242 个交易日

    # 短期波动率（5日）
    short_period = min(5, len(log_returns))
    short_recent = log_returns[-short_period:]
    short_mean = sum(short_recent) / short_period
    short_var = sum((r - short_mean) ** 2 for r in short_recent) / max(short_period - 1, 1)
    short_vol = math.sqrt(short_var) * math.sqrt(242)

    # 波动率变化趋势
    if short_vol > annual_vol * 1.3:
        vol_trend = "短期波动率放大（情绪升温）"
    elif short_vol < annual_vol * 0.7:
        vol_trend = "短期波动率收缩（可能变盘）"
    else:
        vol_trend = "波动率平稳"

    return {
        "daily_volatility": _safe_round(daily_vol * 100, 2),
        "annualized_volatility": _safe_round(annual_vol * 100, 2),
        "short_term_volatility": _safe_round(short_vol * 100, 2),
        "vol_trend": vol_trend,
    }
def _detect_divergence(closes: List[float], indicator: List[float],
                       lookback: int = 20) -> str:
    """检测价格与指标的背离。

    返回: 'bullish_div' / 'bearish_div' / 'none'
    - 底背离(bullish_div): 价格创新低但指标未新低
    - 顶背离(bearish_div): 价格创新高但指标未新高
    """
    if len(closes) < lookback or len(indicator) < lookback:
        return "none"

    price_window = closes[-lookback:]
    ind_window = indicator[-lookback:]

    # 找价格和指标各自的极值
    price_min_idx = price_window.index(min(price_window))
    price_max_idx = price_window.index(max(price_window))
    ind_min_idx = ind_window.index(min(ind_window))
    ind_max_idx = ind_window.index(max(ind_window))

    # 底背离：价格在后半段创新低，但指标低点在前半段
    if price_min_idx >= lookback * 0.6 and ind_min_idx < lookback * 0.4:
        if min(ind_window[-lookback // 3:]) > min(ind_window[:lookback // 3]):
            return "bullish_div"

    # 顶背离：价格在后半段创新高，但指标高点在前半段
    if price_max_idx >= lookback * 0.6 and ind_max_idx < lookback * 0.4:
        if max(ind_window[-lookback // 3:]) < max(ind_window[:lookback // 3]):
            return "bearish_div"

    return "none"
def _calc_ma_convergence(closes: List[float]) -> Dict[str, Any]:
    """均线收敛度检测 — 多条均线粘合程度。"""
    if len(closes) < 60:
        return {"converged": False}

    ma5 = sum(closes[-5:]) / 5
    ma10 = sum(closes[-10:]) / 10
    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / 60

    avg = (ma5 + ma10 + ma20 + ma60) / 4
    if avg == 0:
        return {"converged": False}

    # 各均线偏离平均值的最大幅度
    max_dev = max(
        abs(ma5 - avg) / avg,
        abs(ma10 - avg) / avg,
        abs(ma20 - avg) / avg,
        abs(ma60 - avg) / avg
    ) * 100

    converged = max_dev < 2.0  # 所有均线在 2% 以内 = 粘合

    return {
        "converged": converged,
        "max_deviation_pct": _safe_round(max_dev, 2),
        "description": "均线高度粘合，即将变盘" if converged else "均线发散中",
    }
def _detect_resonance(
    macd_result: Dict[str, Any],
    kdj_result: Dict[str, Any],
    rsi_result: Dict[str, Any],
    boll_result: Dict[str, Any],
    ma_score: int,
    bias_ma20: float,
    ma_convergence: Dict[str, Any],
    macd_div: str,
    rsi_div: str,
) -> Dict[str, Any]:
    """多重共振检测。

    共振 = 多个不同类别的指标同时指向同一方向。
    双重共振 +50% 加成，三重及以上 +100% 加成。
    共振越多，分数越极端（区分度越高）。

    检测维度（6 类）：
    1. 趋势共振：MACD 金叉/死叉
    2. 动量共振：KDJ 金叉/死叉
    3. 超买超卖共振：RSI + KDJ + BOLL
    4. 背离共振：MACD + RSI 背离
    5. 均线粘合共振：MA 收敛 + 动量方向
    6. 乖离率极值共振
    """
    # 每个维度独立计分（不分正负，最后统一方向）
    bullish_scores = []  # [(维度名, 基础分)]
    bearish_scores = []
    signals = []

    # 提取各指标状态
    macd_signals = macd_result.get("signals", []) if isinstance(macd_result, dict) else []
    kdj_signals = kdj_result.get("signals", []) if isinstance(kdj_result, dict) else []
    rsi6 = rsi_result.get("rsi6", 50) if isinstance(rsi_result, dict) else 50
    kdj_j = kdj_result.get("j", 50) if isinstance(kdj_result, dict) else 50
    kdj_k = kdj_result.get("k", 50) if isinstance(kdj_result, dict) else 50
    boll_pos = boll_result.get("position_pct", 50) if isinstance(boll_result, dict) else 50

    macd_golden = any("金叉" in s for s in macd_signals)
    macd_death = any("死叉" in s for s in macd_signals)
    kdj_golden = any("金叉" in s for s in kdj_signals)
    kdj_death = any("死叉" in s for s in kdj_signals)

    # ── 维度 1: 趋势共振（MACD）──
    if macd_golden:
        bullish_scores.append(("趋势", 8))
        signals.append("MACD金叉（趋势看多）")
    elif macd_death:
        bearish_scores.append(("趋势", 8))
        signals.append("MACD死叉（趋势看空）")

    # ── 维度 2: 动量共振（KDJ）──
    if kdj_golden:
        bullish_scores.append(("动量", 8))
        signals.append("KDJ金叉（动量看多）")
    elif kdj_death:
        bearish_scores.append(("动量", 8))
        signals.append("KDJ死叉（动量看空）")

    # ── 维度 3: 超买超卖共振（RSI + KDJ + BOLL）──
    oversold_count = 0
    overbought_count = 0
    if rsi6 <= 30:
        oversold_count += 1
    elif rsi6 >= 70:
        overbought_count += 1
    if kdj_j <= 0 or kdj_k <= 20:
        oversold_count += 1
    elif kdj_j >= 100 or kdj_k >= 80:
        overbought_count += 1
    if boll_pos <= 20:
        oversold_count += 1
    elif boll_pos >= 80:
        overbought_count += 1

    if oversold_count >= 3:
        bullish_scores.append(("超卖", 12))
        signals.append("🔥 RSI+KDJ+BOLL 三重超卖共振")
    elif oversold_count >= 2:
        bullish_scores.append(("超卖", 6))
        signals.append("RSI+KDJ 双重超卖")
    elif overbought_count >= 3:
        bearish_scores.append(("超买", 12))
        signals.append("⚠️ RSI+KDJ+BOLL 三重超买共振")
    elif overbought_count >= 2:
        bearish_scores.append(("超买", 6))
        signals.append("RSI+KDJ 双重超买")

    # ── 维度 4: 背离共振（MACD + RSI）──
    if macd_div == "bullish_div" and rsi_div == "bullish_div":
        bullish_scores.append(("背离", 10))
        signals.append("🔥 MACD+RSI 双底背离共振")
    elif macd_div == "bearish_div" and rsi_div == "bearish_div":
        bearish_scores.append(("背离", 10))
        signals.append("⚠️ MACD+RSI 双顶背离共振")
    elif macd_div == "bullish_div":
        bullish_scores.append(("背离", 4))
        signals.append("MACD底背离")
    elif macd_div == "bearish_div":
        bearish_scores.append(("背离", 4))
        signals.append("MACD顶背离")
    elif rsi_div == "bullish_div":
        bullish_scores.append(("背离", 4))
        signals.append("RSI底背离")
    elif rsi_div == "bearish_div":
        bearish_scores.append(("背离", 4))
        signals.append("RSI顶背离")

    # ── 维度 5: 均线粘合共振 ──
    if ma_convergence.get("converged"):
        if kdj_golden or rsi6 < 40:
            bullish_scores.append(("粘合", 8))
            signals.append("均线粘合+动量偏多，向上变盘概率大")
        elif kdj_death or rsi6 > 60:
            bearish_scores.append(("粘合", 8))
            signals.append("均线粘合+动量偏空，向下变盘概率大")
        else:
            signals.append("均线粘合，方向待定")

    # ── 维度 6: 乖离率极值 ──
    if bias_ma20 > 10:
        bearish_scores.append(("乖离", 8))
        signals.append(f"乖离率MA20={bias_ma20}%，严重超涨")
    elif bias_ma20 > 8:
        bearish_scores.append(("乖离", 4))
        signals.append(f"乖离率MA20={bias_ma20}%，超涨")
    elif bias_ma20 < -10:
        bullish_scores.append(("乖离", 8))
        signals.append(f"乖离率MA20={bias_ma20}%，严重超跌")
    elif bias_ma20 < -8:
        bullish_scores.append(("乖离", 4))
        signals.append(f"乖离率MA20={bias_ma20}%，超跌")

    # ── 多重共振加成计算 ──
    # 统计看多/看空各有多少个维度共振
    bull_dims = len(bullish_scores)
    bear_dims = len(bearish_scores)

    # 加成系数：维度越多加成越大
    if bull_dims >= 4:
        bull_multiplier = 3.0
    elif bull_dims >= 3:
        bull_multiplier = 2.5
    elif bull_dims >= 2:
        bull_multiplier = 2.0
    else:
        bull_multiplier = 1.0

    if bear_dims >= 4:
        bear_multiplier = 3.0
    elif bear_dims >= 3:
        bear_multiplier = 2.5
    elif bear_dims >= 2:
        bear_multiplier = 2.0
    else:
        bear_multiplier = 1.0

    bull_total = int(sum(s for _, s in bullish_scores) * bull_multiplier)
    bear_total = int(sum(s for _, s in bearish_scores) * bear_multiplier)

    score_adj = bull_total - bear_total

    # 确定共振类型
    resonance_type = "无"
    resonance_detail = ""
    if bull_dims >= 3:
        resonance_type = f"🔥 {bull_dims}重看多共振"
        resonance_detail = "+".join(n for n, _ in bullish_scores)
    elif bear_dims >= 3:
        resonance_type = f"⚠️ {bear_dims}重看空共振"
        resonance_detail = "+".join(n for n, _ in bearish_scores)
    elif bull_dims >= 2:
        resonance_type = "双重看多共振"
        resonance_detail = "+".join(n for n, _ in bullish_scores)
    elif bear_dims >= 2:
        resonance_type = "双重看空共振"
        resonance_detail = "+".join(n for n, _ in bearish_scores)

    # 最终修正幅度限制
    score_adj = max(-50, min(50, score_adj))

    return {
        "type": resonance_type,
        "detail": resonance_detail,
        "signals": signals,
        "score_adjustment": score_adj,
        "oversold_count": oversold_count,
        "overbought_count": overbought_count,
        "bull_dims": bull_dims,
        "bear_dims": bear_dims,
    }
# ═══════════════════════════════════════════════════════════════
# Tool 函数（注册给 Agent 调用）
# ═══════════════════════════════════════════════════════════════

def analyze_trend(codes: str, _output: str = "markdown") -> Dict[str, Any]:
    """技术趋势综合分析：趋势类(MA/MACD/BOLL) + 动量类(RSI/KDJ) + 量能类(OBV/MFI/CMF) + 波动率(ATR/HV)。

    包含多指标共振检测、背离检测、均线收敛度、乖离率极值等高级信号。

    Args:
        codes: 多股用逗号分隔
        _output: "markdown"(默认) | "json"
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str) -> Dict[str, Any]:
        try:
            data = _fetch_ohlcv(stock_code, 120)
            closes = data["close"]
            highs = data["high"]
            lows = data["low"]
            volumes = data["volume"]
            if len(closes) < 20:
                return {"error": "K线数据不足（至少需要20根）", "retriable": True}

            latest = closes[-1]
            prev = closes[-2] if len(closes) >= 2 else latest

            # ─────────────────────────────────────────────
            # 1. 均线系统
            # ─────────────────────────────────────────────
            ma5 = _safe_round(sum(closes[-5:]) / 5)
            ma10 = _safe_round(sum(closes[-10:]) / 10)
            ma20 = _safe_round(sum(closes[-20:]) / 20)
            ma60 = _safe_round(sum(closes[-60:]) / 60) if len(closes) >= 60 else _safe_round(sum(closes) / len(closes))
            ma120 = _safe_round(sum(closes[-120:]) / 120) if len(closes) >= 120 else ma60

            ma_score = 50
            if ma5 > ma10 > ma20 > ma60:
                ma_desc, ma_score = "强多头排列", 90
            elif ma5 > ma10 > ma20:
                ma_desc, ma_score = "多头排列", 75
            elif ma5 > ma20:
                ma_desc, ma_score = "弱势多头", 60
            elif ma5 < ma10 < ma20 < ma60:
                ma_desc, ma_score = "强空头排列", 10
            elif ma5 < ma10 < ma20:
                ma_desc, ma_score = "空头排列", 25
            elif ma5 < ma20:
                ma_desc, ma_score = "弱势空头", 40
            else:
                ma_desc, ma_score = "均线缠绕/震荡", 50

            bias_ma5 = _safe_round((latest - ma5) / ma5 * 100, 2) if ma5 else 0
            bias_ma20 = _safe_round((latest - ma20) / ma20 * 100, 2) if ma20 else 0
            ma_convergence = _calc_ma_convergence(closes)

            # ─────────────────────────────────────────────
            # 2. 趋势类：MACD + BOLL
            # ─────────────────────────────────────────────
            macd_result = _calc_macd(closes)
            macd_score = 50
            if isinstance(macd_result, dict) and "error" not in macd_result:
                dif, dea, bar = macd_result.get("dif", 0), macd_result.get("dea", 0), macd_result.get("macd", 0)
                macd_score = 70 if dif > dea else 30
                if bar > 0:
                    macd_score = min(macd_score + 10, 95)
                else:
                    macd_score = max(macd_score - 10, 5)
                if "金叉" in str(macd_result.get("signals", [])):
                    macd_score += 15
                elif "死叉" in str(macd_result.get("signals", [])):
                    macd_score -= 15
            macd_score = max(0, min(100, macd_score))

            boll_result = _calc_boll(closes)
            boll_score = 50
            if isinstance(boll_result, dict) and "error" not in boll_result:
                pos = boll_result.get("position_pct", 50)
                boll_score = int(50 + (50 - pos) * 0.5)  # 下轨偏多，上轨偏空
                boll_score = max(10, min(90, boll_score))

            # ─────────────────────────────────────────────
            # 3. 动量/震荡类：RSI + KDJ
            # ─────────────────────────────────────────────
            rsi_result = _calc_rsi(closes, [6, 12, 24])
            rsi_score = 50
            rsi6_val = 50
            if isinstance(rsi_result, dict) and "error" not in rsi_result:
                rsi6_val = rsi_result.get("rsi6", 50)
                if rsi6_val >= 80:
                    rsi_score = 15
                elif rsi6_val >= 70:
                    rsi_score = 25
                elif rsi6_val <= 20:
                    rsi_score = 85
                elif rsi6_val <= 30:
                    rsi_score = 75
                else:
                    rsi_score = int(100 - rsi6_val)  # RSI低→分数高（超卖看多）

            kdj_result = _calc_kdj(highs, lows, closes)
            kdj_score = 50
            if isinstance(kdj_result, dict) and "error" not in kdj_result:
                j = kdj_result.get("j", 50)
                k, d = kdj_result.get("k", 50), kdj_result.get("d", 50)
                if j >= 100:
                    kdj_score = 15
                elif j <= 0:
                    kdj_score = 85
                elif k >= 80 and d >= 80:
                    kdj_score = 25
                elif k <= 20 and d <= 20:
                    kdj_score = 75
                elif "金叉" in str(kdj_result.get("signals", [])):
                    kdj_score = 70
                elif "死叉" in str(kdj_result.get("signals", [])):
                    kdj_score = 30
                else:
                    kdj_score = int(100 - j) if j != 50 else 50
            kdj_score = max(10, min(90, kdj_score))

            # ─────────────────────────────────────────────
            # 4. 量能类：OBV + MFI + CMF
            # ─────────────────────────────────────────────
            obv_result = _calc_obv(closes, volumes)
            mfi_result = _calc_mfi(highs, lows, closes, volumes)
            cmf_result = _calc_cmf(highs, lows, closes, volumes)

            # 量能综合评分
            volume_score = 50
            if isinstance(obv_result, dict) and "error" not in obv_result:
                if obv_result.get("obv_trend") == "上升":
                    volume_score += 15
                elif obv_result.get("obv_trend") == "下降":
                    volume_score -= 15
                if "底背离" in obv_result.get("divergence", ""):
                    volume_score += 20
                elif "顶背离" in obv_result.get("divergence", ""):
                    volume_score -= 20
            if isinstance(mfi_result, dict) and "error" not in mfi_result:
                mfi_val = mfi_result.get("mfi", 50)
                if mfi_val <= 20:
                    volume_score += 15
                elif mfi_val >= 80:
                    volume_score -= 15
            if isinstance(cmf_result, dict) and "error" not in cmf_result:
                cmf_val = cmf_result.get("cmf", 0)
                if cmf_val > 0.05:
                    volume_score += 10
                elif cmf_val < -0.05:
                    volume_score -= 10
            volume_score = max(10, min(90, volume_score))

            # ─────────────────────────────────────────────
            # 5. 波动率类：ATR + 历史波动率
            # ─────────────────────────────────────────────
            atr_result = _calc_atr(highs, lows, closes)
            hv_result = _calc_hist_volatility(closes)

            # ─────────────────────────────────────────────
            # 6. 背离检测（MACD + RSI）
            # ─────────────────────────────────────────────
            macd_divergence = "none"
            rsi_divergence = "none"
            if len(closes) >= 30:
                # MACD 柱状线序列用于背离检测
                if isinstance(macd_result, dict) and "error" not in macd_result:
                    ema_fast = _ema(closes, 12)
                    ema_slow = _ema(closes, 26)
                    dif_series = [ema_fast[i] - ema_slow[i] for i in range(len(closes))]
                    dea_series = _ema(dif_series, 9)
                    macd_bar_series = [(dif_series[i] - dea_series[i]) * 2 for i in range(len(closes))]
                    macd_divergence = _detect_divergence(closes, macd_bar_series, 30)
                if isinstance(rsi_result, dict) and "error" not in rsi_result:
                    # 用 RSI6 序列
                    rsi_series = []
                    gains, losses = [], []
                    for i in range(1, len(closes)):
                        delta = closes[i] - closes[i - 1]
                        gains.append(max(delta, 0))
                        losses.append(max(-delta, 0))
                    if len(gains) >= 7:
                        avg_g = sum(gains[:6]) / 6
                        avg_l = sum(losses[:6]) / 6
                        for i in range(6, len(gains)):
                            avg_g = (avg_g * 5 + gains[i]) / 6
                            avg_l = (avg_l * 5 + losses[i]) / 6
                            if avg_l == 0:
                                rsi_series.append(100)
                            else:
                                rsi_series.append(100 - 100 / (1 + avg_g / avg_l))
                        if len(rsi_series) >= 20:
                            rsi_divergence = _detect_divergence(closes[-len(rsi_series):], rsi_series, 20)

            # ─────────────────────────────────────────────
            # 7. 多指标共振检测
            # ─────────────────────────────────────────────
            resonance = _detect_resonance(
                macd_result, kdj_result, rsi_result, boll_result,
                ma_score, bias_ma20, ma_convergence,
                macd_divergence, rsi_divergence
            )

            # ─────────────────────────────────────────────
            # 8. 综合评分（加权 + 共振修正）
            # ─────────────────────────────────────────────
            base_score = int(
                ma_score * 0.15 +
                macd_score * 0.15 +
                boll_score * 0.10 +
                rsi_score * 0.15 +
                kdj_score * 0.10 +
                volume_score * 0.20 +
                50 * 0.15  # 波动率中性基准
            )
            # 共振修正：最大 ±15 分
            resonance_adj = resonance.get("score_adjustment", 0)
            total_score = max(0, min(100, base_score + resonance_adj))

            # 背离修正：±10 分
            if macd_divergence == "bullish_div":
                total_score = min(100, total_score + 10)
            elif macd_divergence == "bearish_div":
                total_score = max(0, total_score - 10)
            if rsi_divergence == "bullish_div":
                total_score = min(100, total_score + 8)
            elif rsi_divergence == "bearish_div":
                total_score = max(0, total_score - 8)

            # 乖离率极值修正
            if bias_ma20 > 10:
                total_score = max(0, total_score - 10)  # 严重超涨
            elif bias_ma20 < -10:
                total_score = min(100, total_score + 10)  # 严重超跌
            elif bias_ma20 > 8:
                total_score = max(0, total_score - 5)
            elif bias_ma20 < -8:
                total_score = min(100, total_score + 5)

            # 均线粘合变盘信号
            if ma_convergence.get("converged"):
                # 粘合时加大动量指标权重的修正
                if kdj_score >= 70 or rsi_score >= 70:
                    total_score = min(100, total_score + 8)  # 粘合+动量偏多
                elif kdj_score <= 30 or rsi_score <= 30:
                    total_score = max(0, total_score - 8)  # 粘合+动量偏空

            total_score = max(0, min(100, total_score))

            # ── 多重共振过滤：无共振时强制中性 ──
            # 只有 2 重+共振才给出明确方向，否则判为震荡
            bull_dims = resonance.get("bull_dims", 0)
            bear_dims = resonance.get("bear_dims", 0)
            has_resonance = bull_dims >= 2 or bear_dims >= 2

            # ─────────────────────────────────────────────
            # 9. 趋势判定
            # ─────────────────────────────────────────────
            if not has_resonance:
                # 无多重共振 → 强制震荡，不给方向
                overall, strength = "震荡（无共振）", "弱"
                total_score = 50  # 归中，回测端判为 neutral
            elif total_score >= 80:
                overall, strength = "强烈看多", "强"
            elif total_score >= 65:
                overall, strength = "看多", "中强"
            elif total_score >= 55:
                overall, strength = "偏多", "中"
            elif total_score >= 45:
                overall, strength = "偏多", "中"
            elif total_score >= 35:
                overall, strength = "偏空", "中"
            elif total_score >= 20:
                overall, strength = "看空", "中强"
            else:
                overall, strength = "强烈看空", "强"

            # ─────────────────────────────────────────────
            # 10. 信号汇总
            # ─────────────────────────────────────────────
            all_signals = []
            highlights = []
            warnings = []

            # 共振信号（最高优先级）
            for s in resonance.get("signals", []):
                all_signals.append(s)
                if "看多" in s or "超卖" in s:
                    highlights.append(s)
                elif "看空" in s or "超买" in s:
                    warnings.append(s)

            # 背离信号
            if macd_divergence == "bullish_div":
                all_signals.append("MACD底背离（价格新低但MACD未新低，看多）")
                highlights.append("MACD底背离")
            elif macd_divergence == "bearish_div":
                all_signals.append("MACD顶背离（价格新高但MACD未新高，看空）")
                warnings.append("MACD顶背离")
            if rsi_divergence == "bullish_div":
                all_signals.append("RSI底背离（看多）")
                highlights.append("RSI底背离")
            elif rsi_divergence == "bearish_div":
                all_signals.append("RSI顶背离（看空）")
                warnings.append("RSI顶背离")

            # 各指标原始信号
            for r in [macd_result, rsi_result, boll_result, kdj_result, mfi_result, cmf_result, obv_result]:
                if isinstance(r, dict):
                    all_signals.extend(r.get("signals", []))

            # 均线
            if ma_score >= 75:
                highlights.append(f"均线{ma_desc}")
            elif ma_score <= 25:
                warnings.append(f"均线{ma_desc}")

            # 乖离率
            if bias_ma20 > 8:
                warnings.append(f"乖离率MA20={bias_ma20}%，超涨回调风险")
            elif bias_ma20 < -8:
                highlights.append(f"乖离率MA20={bias_ma20}%，超跌反弹机会")

            # 均线粘合
            if ma_convergence.get("converged"):
                all_signals.append(ma_convergence["description"])
                highlights.append("均线粘合，即将变盘")

            # 量能背离
            if isinstance(obv_result, dict) and "error" not in obv_result:
                div = obv_result.get("divergence", "无")
                if div != "无":
                    all_signals.append(div)
                    if "底" in div:
                        highlights.append(div)
                    else:
                        warnings.append(div)

            # 波动率
            if isinstance(atr_result, dict) and "error" not in atr_result:
                vol_lvl = atr_result.get("volatility_level", "")
                if "极低" in vol_lvl:
                    all_signals.append("ATR极低波动，变盘前兆")
                    highlights.append("波动率极低，即将变盘")
                elif "极高" in vol_lvl:
                    warnings.append(f"ATR波动率极高（{atr_result.get('atr_pct', 0)}%）")

            if isinstance(hv_result, dict) and "error" not in hv_result:
                vol_trend = hv_result.get("vol_trend", "")
                if "收缩" in vol_trend:
                    all_signals.append(vol_trend)

            # ─────────────────────────────────────────────
            # 11. 输出
            # ─────────────────────────────────────────────
            change_pct = _safe_round((latest - prev) / prev * 100, 2) if prev else 0

            evaluation = {
                "score": total_score,
                "scores": {
                    "ma": ma_score, "macd": macd_score, "boll": boll_score,
                    "rsi": rsi_score, "kdj": kdj_score, "volume": volume_score,
                },
                "resonance": resonance.get("type", "无"),
                "resonance_detail": resonance.get("detail", ""),
                "highlights": highlights,
                "warnings": warnings,
            }

            return {
                "stock_code": stock_code,
                "latest_close": _safe_round(latest),
                "change_pct": change_pct,
                # 均线
                "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60, "ma120": ma120,
                "ma_alignment": ma_desc,
                "bias_ma5": bias_ma5, "bias_ma20": bias_ma20,
                "ma_convergence": ma_convergence,
                # 趋势指标
                "macd": macd_result,
                "boll": boll_result,
                # 动量指标
                "rsi": rsi_result,
                "kdj": kdj_result,
                # 量能指标
                "obv": obv_result,
                "mfi": mfi_result,
                "cmf": cmf_result,
                # 波动率
                "atr": atr_result,
                "historical_volatility": hv_result,
                # 背离
                "macd_divergence": macd_divergence,
                "rsi_divergence": rsi_divergence,
                # 共振
                "resonance": resonance,
                # 综合
                "trend_score": total_score,
                "trend": overall,
                "strength": strength,
                "all_signals": all_signals,
                "evaluation": evaluation,
                "data_points": len(closes),
            }
        except Exception as e:
            logger.error("analyze_trend(%s) failed: %s", stock_code, e)
            return {"error": str(e)}

    if len(code_list) == 1:
        _r = _one(code_list[0])
        if _output == "json":
            return json.dumps(_r, ensure_ascii=False)
        if "error" in _r:
            return f"趋势分析失败: {_r['error']}"
        code = _r.get("stock_code", "")
        close = _r.get("latest_close", 0)
        chg = _r.get("change_pct", 0)
        trend = _r.get("trend", "")
        score = _r.get("trend_score", 0)
        strength = _r.get("strength", "")
        ma_align = _r.get("ma_alignment", "")
        macd = _r.get("macd", {})
        rsi = _r.get("rsi", {})
        kdj = _r.get("kdj", {})
        boll = _r.get("boll", {})
        resonance = _r.get("resonance", {})
        eval_data = _r.get("evaluation", {})
        signals = _r.get("all_signals", [])[:3]
        extra = []
        extra.append(f"MACD:{macd.get('bar_trend','')} RSI6:{rsi.get('rsi6',0):.0f} K:{kdj.get('k',0):.0f} BOLL:{boll.get('position_pct',0):.0f}%")
        if resonance.get("type"):
            extra.append(f"共振:{resonance['type']}({resonance.get('detail','')})")
        return _format_final_md(
            title=trend, score=score, direction="bullish" if score >= 55 else "bearish" if score <= 45 else "neutral",
            factors=[{"name": f"{k}", "score": v} for k, v in eval_data.get("scores", {}).items()],
            signals=signals, extra=extra,
            first_line=f"{code} {close} {chg:+.2f}% {trend} {score}分 {strength} {ma_align}",
        )

    return _batch_execute(_one, code_list)
def calculate_ma(codes: str, periods: str = "5,10,20,60,120", _output: str = "markdown") -> str:
    """均线指标：返回指定周期(5/10/20/60/120/250)的MA值、斜率和趋势方向。

    Args:
        codes: 多股用逗号分隔"
        periods: 均线周期列表，默认 [5,10,20,60,120,250]
        _output: "markdown"(默认) | "json"
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str) -> Dict[str, Any]:
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
                if len(closes) >= p + 1:
                    prev_avg = _safe_round(sum(closes[-p - 1:-1]) / p)
                    slope_pct = _safe_round((avg - prev_avg) / prev_avg * 100, 2) if prev_avg else 0
                    result[f"ma{p}_slope"] = slope_pct
                    result[f"ma{p}_trend"] = "上行" if slope_pct > 0.05 else ("下行" if slope_pct < -0.05 else "走平")
            return result
        except Exception as e:
            logger.error("calculate_ma(%s) failed: %s", stock_code, e)
            return {"error": str(e)}

    _r = _batch_execute(_one, code_list)
    if _output == "json":
        return json.dumps(_r, ensure_ascii=False)
    if isinstance(_r, dict) and "error" in _r:
        return f"均线获取失败: {_r['error']}"
    if isinstance(_r, dict) and "data" in _r:
        return _format_output(_r, _output)
    code = _r.get("stock_code", "")
    periods = _r.get("periods", {})
    parts = [f"{k}:{v.get('value',0):.2f} {v.get('trend','')}" for k,v in periods.items() if isinstance(v,dict)]
    return f"{code} 均线\n" + " | ".join(parts[:5])
def get_volume_analysis(codes: str, _output: str = "markdown") -> Dict[str, Any]:
    """量能分析：返回量比、换手率、近5日成交量趋势（放量/缩量/平量）。

    Args:
        codes: 多股用逗号分隔"
        _output: "markdown"(默认) | "json"
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str) -> Dict[str, Any]:
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

            vol_trend = "数据不足"
            if len(volumes) >= 6:
                recent_avg = sum(volumes[-3:]) / 3
                earlier_avg = sum(volumes[-6:-3]) / 3
                vol_trend = "上升" if recent_avg > earlier_avg * 1.1 else ("下降" if recent_avg < earlier_avg * 0.9 else "平稳")

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

    if len(code_list) == 1:
        _r = _one(code_list[0])
        if _output == "json":
            return json.dumps(_r, ensure_ascii=False)
        if "error" in _r:
            return f"量能分析失败: {_r['error']}"
        code = _r.get("stock_code", "")
        vol = _r.get("latest_volume", 0)
        ratio = _r.get("volume_ratio", 0)
        status = _r.get("volume_status", "")
        trend = _r.get("volume_trend", "")
        vp = _r.get("vol_price_relation", "")
        vol_str = f"{vol/10000:.0f}万" if vol > 10000 else str(int(vol))
        return f"{code} 量:{vol_str} 量比:{ratio} {status} {trend} | {vp}"

    return _batch_execute(_one, code_list)
def analyze_pattern(codes: str, _output: str = "markdown") -> Dict[str, Any]:
    """K线形态识别：返回当日出现的形态信号（锤子线/十字星/吞没/三连阳等）及含义。

    Args:
        codes: 多股用逗号分隔"
        _output: "markdown"(默认) | "json"
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str) -> Dict[str, Any]:
        try:
            data = _fetch_ohlcv(stock_code, 10)
            if len(data["close"]) < 3:
                return {"error": "K线数据不足"}

            patterns = []
            o, h, l, c = data["open"], data["high"], data["low"], data["close"]
            n = len(c)

            lo, lh, ll, lc = o[-1], h[-1], l[-1], c[-1]
            body = abs(lc - lo)
            candle_range = lh - ll if lh > ll else 0.001
            upper_shadow = lh - max(lo, lc)
            lower_shadow = min(lo, lc) - ll

            if body > 0 and candle_range > 0:
                if lower_shadow >= 2 * body and upper_shadow <= body * 0.3:
                    patterns.append("锤子线（底部反转信号）")
                elif upper_shadow >= 2 * body and lower_shadow <= body * 0.3:
                    patterns.append("倒锤子线（顶部反转信号）")

            if body <= candle_range * 0.1:
                if upper_shadow > candle_range * 0.3 and lower_shadow > candle_range * 0.3:
                    patterns.append("长腿十字星（强烈犹豫信号）")
                elif upper_shadow > candle_range * 0.4 and lower_shadow < candle_range * 0.1:
                    patterns.append("墓碑线（顶部反转）")
                elif lower_shadow > candle_range * 0.4 and upper_shadow < candle_range * 0.1:
                    patterns.append("蜻蜓线（底部反转）")
                else:
                    patterns.append("十字星（犹豫信号）")

            if upper_shadow > body * 2 and upper_shadow > lower_shadow * 2:
                patterns.append("长上影线（上方压力大）")

            if lower_shadow > body * 2 and lower_shadow > upper_shadow * 2:
                patterns.append("长下影线（下方支撑强）")

            if body > candle_range * 0.85:
                if lc > lo:
                    patterns.append("光头光脚大阳线（强势）")
                else:
                    patterns.append("光头光脚大阴线（弱势）")

            if n >= 2:
                po, pc = o[-2], c[-2]
                prev_body = abs(pc - po)
                if prev_body > 0 and body > prev_body:
                    if pc < po and lc > lo and lo <= pc and lc >= po:
                        patterns.append("看涨吞没（底部反转）")
                    elif pc > po and lc < lo and lo >= pc and lc <= po:
                        patterns.append("看跌吞没（顶部反转）")

            if n >= 3:
                o1, c1 = o[-3], c[-3]
                o2, c2 = o[-2], c[-2]
                o3, c3 = o[-1], c[-1]
                body1 = abs(c1 - o1)
                body2 = abs(c2 - o2)
                body3 = abs(c3 - o3)

                if (c1 < o1 and body1 > 0 and
                        body2 < body1 * 0.3 and
                        c3 > o3 and body3 > 0 and
                        c3 > (o1 + c1) / 2):
                    patterns.append("早晨之星（底部反转，看多）")

                if (c1 > o1 and body1 > 0 and
                        body2 < body1 * 0.3 and
                        c3 < o3 and body3 > 0 and
                        c3 < (o1 + c1) / 2):
                    patterns.append("黄昏之星（顶部反转，看空）")

                if c1 > o1 and c2 > o2 and c3 > o3:
                    if body3 > body2 > body1:
                        patterns.append("三连阳递增（强势看多）")
                    else:
                        patterns.append("三连阳（多头排列）")

                if c1 < o1 and c2 < o2 and c3 < o3:
                    if body3 > body2 > body1:
                        patterns.append("三连阴递增（强势看空）")
                    else:
                        patterns.append("三连阴（空头排列）")

                if (c1 > o1 and c2 > o2 and c3 > o3 and
                        c2 > c1 and c3 > c2 and
                        o2 > o1 and o3 > o2):
                    patterns.append("红三兵（强烈看多）")

                if (c1 < o1 and c2 < o2 and c3 < o3 and
                        c2 < c1 and c3 < c2 and
                        o2 < o1 and o3 < o2):
                    patterns.append("黑三鸦（强烈看空）")

            if n >= 2:
                prev_high = h[-2]
                prev_low = l[-2]
                if l[-1] > prev_high:
                    gap_size = _safe_round((l[-1] - prev_high) / prev_high * 100, 2)
                    patterns.append(f"向上跳空缺口（幅度{gap_size}%）")
                elif h[-1] < prev_low:
                    gap_size = _safe_round((prev_low - h[-1]) / prev_low * 100, 2)
                    patterns.append(f"向下跳空缺口（幅度{gap_size}%）")

                    # ── 乌云盖顶（Dark Cloud Cover）──
            if n >= 2:
                po2, ph2, pl2, pc2 = o[-2], h[-2], l[-2], c[-2]
                if (pc2 > po2 and  # 前一根阳线
                        lo > lh and  # 高开
                        lc < po and  # 收盘低于前一根开盘
                        lc > (po2 + pc2) / 2 and  # 收盘深入前一根实体一半以上
                        abs(lc - lo) > 0):  # 当日有实体
                    patterns.append("乌云盖顶（顶部反转）")

            # ── 刺透形态（Piercing Pattern）──
            if n >= 2:
                po2, ph2, pl2, pc2 = o[-2], h[-2], l[-2], c[-2]
                if (pc2 < po2 and  # 前一根阴线
                        lo < pl2 and  # 低开
                        lc > po2 and  # 收盘高于前一根开盘
                        lc < (po2 + pc2) / 2 and  # 收盘深入前一根实体一半以上（从下方）
                        abs(lc - lo) > 0):  # 当日有实体
                    patterns.append("刺透形态（底部反转）")

            # ── 上升三法 / 下降三法 ──
            if n >= 5:
                o1, c1 = o[-5], c[-5]
                o5, c5 = o[-1], c[-1]
                # 上升三法：大阳线 → 2~3根小阴线（在第一根范围内）→ 大阳线创新高
                if (c1 > o1 and abs(c1 - o1) > candle_range * 0.5 and
                        c5 > o5 and abs(c5 - o5) > abs(c1 - o1) * 0.8 and
                        c5 > c1):
                    mid_ok = True
                    for i in range(-4, -1):
                        if c[i] > o[i] or abs(c[i] - o[i]) > abs(c1 - o1) * 0.5:
                            mid_ok = False
                            break
                        if o[i] < min(o1, c1) or c[i] > max(o1, c1):
                            mid_ok = False
                            break
                    if mid_ok:
                        patterns.append("上升三法（趋势中继看多）")

                # 下降三法：大阴线 → 2~3根小阳线（在第一根范围内）→ 大阴线创新低
                if (c1 < o1 and abs(c1 - o1) > candle_range * 0.5 and
                        c5 < o5 and abs(c5 - o5) > abs(c1 - o1) * 0.8 and
                        c5 < c1):
                    mid_ok = True
                    for i in range(-4, -1):
                        if c[i] < o[i] or abs(c[i] - o[i]) > abs(c1 - o1) * 0.5:
                            mid_ok = False
                            break
                        if o[i] > max(o1, c1) or c[i] < min(o1, c1):
                            mid_ok = False
                            break
                    if mid_ok:
                        patterns.append("下降三法（趋势中继看空）")

            # ── 弃婴形态（Abandoned Baby）──
            if n >= 3:
                o1, h1, l1, c1 = o[-3], h[-3], l[-3], c[-3]
                o2, h2, l2, c2 = o[-2], h[-2], l[-2], c[-2]
                o3, h3, l3, c3 = o[-1], h[-1], l[-1], c[-1]
                body2 = abs(c2 - o2)
                if body2 <= (h2 - l2) * 0.15:  # 中间是十字星
                    # 看涨弃婴：阴线 → 十字星（跳空低开）→ 阳线（跳空高开）
                    if c1 < o1 and h2 < l1 and c3 > o3 and l3 > h2:
                        patterns.append("看涨弃婴（强烈底部反转）")
                    # 看跌弃婴：阳线 → 十字星（跳空高开）→ 阴线（跳空低开）
                    if c1 > o1 and l2 > h1 and c3 < o3 and h3 < l2:
                        patterns.append("看跌弃婴（强烈顶部反转）")

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

    if len(code_list) == 1:
        _r = _one(code_list[0])
        if _output == "json":
            return json.dumps(_r, ensure_ascii=False)
        if "error" in _r:
            return f"形态分析失败: {_r['error']}"
        code = _r.get("stock_code", "")
        patterns = _r.get("patterns", [])
        if not patterns:
            return f"{code} 无明显形态"
        names = [p.get("pattern","") for p in patterns[:3] if isinstance(p,dict)]
        return f"{code} 形态: {', '.join(names)}"

    return _batch_execute(_one, code_list)
def get_chip_distribution(codes: str, lookback_days: int = 120, _output: str = "markdown") -> Dict[str, Any]:
    """筹码分布：返回获利比例、平均成本、90%筹码集中度、套牢/获利盘比例。

    从日K线计算筹码分布，不依赖数据源原生接口。
    算法：按日K线的 high/low 区间分配成交量到价格档位，
    用指数衰减加权（近期筹码权重更高），汇总计算各维度指标。

    Args:
        codes: 多股用逗号分隔"（也兼容 search_stock 返回的 dict）
        lookback_days: 回看天数，默认120天
        _output: "markdown"(默认) | "json"
    """
    # 兼容 search_stock 返回的 dict: {'results': [{'code': '600593', ...}], ...}
    if isinstance(codes, dict):
        results = codes.get("results", [])
        if results:
            codes = results[0].get("code", "")
        else:
            return {"error": "codes dict 中无 results", "retriable": False}

    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str) -> Dict[str, Any]:
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

    if len(code_list) == 1:
        _r = _one(code_list[0])
        if _output == "json":
            return json.dumps(_r, ensure_ascii=False)
        if "error" in _r:
            return f"筹码分析失败: {_r['error']}"
        code = _r.get("stock_code", "")
        avg_cost = _r.get("avg_cost", 0)
        price = _r.get("current_price", 0)
        profit_pct = _r.get("profit_ratio_pct", "")
        conc = _r.get("concentration_90", "")
        resistances = _r.get("resistance_prices", [])[:3]
        supports = _r.get("support_prices", [])[:3]
        md = f"{code} 平均成本:{avg_cost} 现价:{price} 获利盘:{profit_pct} 集中度:{conc}"
        if resistances:
            md += f"\n压力:{resistances}"
        if supports:
            md += f" 支撑:{supports}"
        return md

    return _batch_execute(_one, code_list)
def get_indicator_snapshot(codes: str, _output: str = "markdown") -> Dict[str, Any]:
    """指标快照：一次返回MACD/RSI/BOLL/KDJ/KD的最新数值和金叉/死叉/超买超卖状态。

    Args:
        codes: 多股用逗号分隔"
        _output: "markdown"(默认) | "json"
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str) -> Dict[str, Any]:
        try:
            data = _fetch_ohlcv(stock_code, 120)
            closes = data["close"]
            if len(closes) < 10:
                return {"error": "K线数据不足（至少需要10根）", "retriable": True}

            result = {"stock_code": stock_code, "latest_close": _safe_round(closes[-1])}

            for p in [5, 10, 20, 60, 120]:
                if len(closes) >= p:
                    result[f"ma{p}"] = _safe_round(sum(closes[-p:]) / p)

            macd = _calc_macd(closes)
            if "error" not in macd:
                result["macd"] = macd

            rsi = _calc_rsi(closes, [6, 12, 24])
            if "error" not in rsi:
                result["rsi"] = rsi

            boll = _calc_boll(closes)
            if "error" not in boll:
                result["boll"] = boll

            kdj = _calc_kdj(data["high"], data["low"], closes)
            if "error" not in kdj:
                result["kdj"] = kdj

            if len(data["volume"]) >= 5:
                vol = data["volume"][-1]
                avg5 = sum(data["volume"][-5:]) / 5
                result["volume_ratio"] = _safe_round(vol / avg5, 2) if avg5 else 0

            return result
        except Exception as e:
            logger.error("get_indicator_snapshot(%s) failed: %s", stock_code, e)
            return {"error": str(e)}

    if len(code_list) == 1:
        _r = _one(code_list[0])
        if _output == "json":
            return json.dumps(_r, ensure_ascii=False)
        if "error" in _r:
            return f"指标获取失败: {_r['error']}"
        code = _r.get("stock_code", "")
        close = _r.get("latest_close", 0)
        ma5 = _r.get("ma5", 0)
        ma10 = _r.get("ma10", 0)
        ma20 = _r.get("ma20", 0)
        vol_ratio = _r.get("volume_ratio", 0)
        macd = _r.get("macd", {})
        rsi = _r.get("rsi", {})
        kdj = _r.get("kdj", {})
        boll = _r.get("boll", {})
        return f"{code} {close} MA:{ma5:.2f}/{ma10:.2f}/{ma20:.2f} 量比:{vol_ratio}\nMACD:{macd.get('bar_trend','')} RSI6:{rsi.get('rsi6',0):.0f} K:{kdj.get('k',0):.0f} BOLL:{boll.get('position_pct',0):.0f}%"

    return _batch_execute(_one, code_list)
# ── OpenAI tool declarations ─────────────────────────────────

