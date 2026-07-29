# -*- coding: utf-8 -*-
"""
Analysis tools — comprehensive technical analysis for agent.

v2: MACD / RSI / BOLL / KDJ / 多周期均线 / 改进K线形态识别
Pure-Python calculations on K-line data, no external API calls.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from app.agent.log import logger
from app.agent.tools.finance._analysis_utils import (
    _get_ds, _fetch_klines, _fetch_closes, _fetch_ohlcv, _safe_round, _calc_obv, _fetch_realtime_volume_ratio,
)

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

def analyze_trend(codes: str) -> Dict[str, Any]:
    """技术趋势综合分析：趋势类(MA/MACD/BOLL) + 动量类(RSI/KDJ) + 量能类(OBV/MFI/CMF) + 波动率(ATR/HV)。

    包含多指标共振检测、背离检测、均线收敛度、乖离率极值等高级信号。

    Args:
        codes: 多股用逗号分隔
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
        return _one(code_list[0])

    results = {}
    for code in code_list:
        try:
            results[code] = _one(code)
        except Exception as e:
            results[code] = {"error": str(e)}
    return {"count": len(results), "data": results}
def calculate_ma(codes: str, periods: str = "5,10,20,60,120") -> Dict[str, Any]:
    """均线指标：返回指定周期(5/10/20/60/120/250)的MA值、斜率和趋势方向。

    Args:
        codes: 多股用逗号分隔
        periods: 均线周期列表，默认 [5,10,20,60,120,250]
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

    if len(code_list) == 1:
        return _one(code_list[0])

    results = {}
    for code in code_list:
        try:
            results[code] = _one(code)
        except Exception as e:
            results[code] = {"error": str(e)}
    return {"count": len(results), "data": results}
def get_volume_analysis(codes: str) -> Dict[str, Any]:
    """量能分析：返回量比、换手率、近5日成交量趋势（放量/缩量/平量）。

    Args:
        codes: 多股用逗号分隔
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

            # 盘中判断
            from datetime import datetime
            now = datetime.now()
            t = now.hour * 60 + now.minute
            is_intraday = 570 <= t < 900

            # 盘中：从搜狐接口取实时量比；收盘后：用日线算
            volume_ratio = 0.0
            if is_intraday:
                vr_map = _fetch_realtime_volume_ratio([stock_code])
                vr = vr_map.get(stock_code, 0)
                if vr > 0:
                    volume_ratio = vr
            else:
                volume_ratio = _safe_round(latest_vol / avg_vol_5, 2) if avg_vol_5 else 0

            if is_intraday and volume_ratio > 0:
                status = "盘中"
            elif is_intraday:
                status = "盘中（量比未知）"
            elif volume_ratio > 2.0:
                status = "显著放量"
            elif volume_ratio > 1.5:
                status = "温和放量"
            elif volume_ratio < 0.5:
                status = "明显缩量"
            elif volume_ratio < 0.8:
                status = "温和缩量"
            else:
                status = "平量"

            # 量能趋势用前几日已完成数据，不受盘中影响
            vol_trend = "数据不足"
            if len(volumes) >= 7:
                # 排除最后一根（可能是盘中未完成），用倒数 2~7 根
                recent_avg = sum(volumes[-4:-1]) / 3
                earlier_avg = sum(volumes[-7:-4]) / 3
                vol_trend = "上升" if recent_avg > earlier_avg * 1.1 else ("下降" if recent_avg < earlier_avg * 0.9 else "平稳")

            # 量价关系
            vol_price_relation = "数据不足"
            if len(closes) >= 2 and latest_vol > 0:
                price_change = closes[-1] - closes[-2]
                if is_intraday and volume_ratio > 0:
                    # 盘中有实时量比，可以判断量价关系
                    if price_change > 0 and volume_ratio > 1.3:
                        vol_price_relation = "放量上涨（盘中）"
                    elif price_change > 0 and volume_ratio < 0.7:
                        vol_price_relation = "缩量上涨（盘中）"
                    elif price_change < 0 and volume_ratio > 1.3:
                        vol_price_relation = "放量下跌（盘中）"
                    elif price_change < 0 and volume_ratio < 0.7:
                        vol_price_relation = "缩量下跌（盘中）"
                    else:
                        vol_price_relation = "量价一般（盘中）"
                elif volume_ratio > 0:
                    if price_change > 0 and volume_ratio > 1.3:
                        vol_price_relation = "量价齐升（健康上涨）"
                    elif price_change > 0 and volume_ratio < 0.7:
                        vol_price_relation = "缩量上涨（上涨乏力）"
                    elif price_change < 0 and volume_ratio > 1.3:
                        vol_price_relation = "放量下跌（恐慌抛售）"
                    elif price_change < 0 and volume_ratio < 0.7:
                        vol_price_relation = "缩量下跌（动能减弱）"
                    else:
                        vol_price_relation = "量价配合一般"

            return {
                "stock_code": stock_code,
                "latest_volume": _safe_round(latest_vol, 0),
                "avg_volume_5d": _safe_round(avg_vol_5, 0),
                "avg_volume_20d": _safe_round(avg_vol_20, 0),
                "volume_ratio": volume_ratio,
                "volume_status": status,
                "volume_trend": vol_trend,
                "vol_price_relation": vol_price_relation,
                "is_intraday": is_intraday,
            }
        except Exception as e:
            logger.error("get_volume_analysis(%s) failed: %s", stock_code, e)
            return {"error": str(e)}

    if len(code_list) == 1:
        return _one(code_list[0])

    # 批量预取盘中量比（一次 HTTP）
    from datetime import datetime as _dt
    _t = _dt.now().hour * 60 + _dt.now().minute
    _is_intraday = 570 <= _t < 900
    _vr_map = _fetch_realtime_volume_ratio(code_list) if _is_intraday and len(code_list) > 1 else {}

    def _one_with_vr(stock_code: str) -> Dict[str, Any]:
        r = _one(stock_code)
        if "error" in r:
            return r
        # 盘中补充实时量比 + 重算量价关系
        if r.get("is_intraday") and r.get("volume_ratio", 0) == 0:
            vr = _vr_map.get(stock_code, 0)
            if vr > 0:
                r["volume_ratio"] = vr
        if r.get("is_intraday") and r.get("volume_ratio", 0) > 0:
            vr = r["volume_ratio"]
            if vr > 2.0:
                r["volume_status"] = "显著放量"
            elif vr > 1.5:
                r["volume_status"] = "温和放量"
            elif vr < 0.5:
                r["volume_status"] = "明显缩量"
            elif vr < 0.8:
                r["volume_status"] = "温和缩量"
            else:
                r["volume_status"] = "平量"
            closes = _fetch_closes(stock_code, 30)
            if len(closes) >= 2:
                pc = closes[-1] - closes[-2]
                if pc > 0 and vr > 1.3:
                    r["vol_price_relation"] = "放量上涨"
                elif pc > 0 and vr < 0.7:
                    r["vol_price_relation"] = "缩量上涨"
                elif pc < 0 and vr > 1.3:
                    r["vol_price_relation"] = "放量下跌"
                elif pc < 0 and vr < 0.7:
                    r["vol_price_relation"] = "缩量下跌"
                else:
                    r["vol_price_relation"] = "量价一般"
        return r

    results = {}
    for code in code_list:
        results[code] = _one_with_vr(code)

    return {"count": len(results), "data": results}
def analyze_pattern(codes: str) -> Dict[str, Any]:
    """K线形态识别：返回当日出现的形态信号（锤子线/十字星/吞没/三连阳等）及含义。

    Args:
        codes: 多股用逗号分隔
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
        return _one(code_list[0])

    results = {}
    for code in code_list:
        try:
            results[code] = _one(code)
        except Exception as e:
            results[code] = {"error": str(e)}
    return {"count": len(results), "data": results}
# get_chip_distribution 已迁移至 chip_distribution.py
from app.agent.tools.finance.chip_distribution import get_chip_distribution  # noqa: F401
def get_indicator_snapshot(codes: str) -> Dict[str, Any]:
    """指标快照：一次返回MACD/RSI/BOLL/KDJ/KD的最新数值和金叉/死叉/超买超卖状态。

    Args:
        codes: 多股用逗号分隔
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
                # 盘中量比从实时接口取，收盘后用日线算
                from datetime import datetime as _dt
                _t = _dt.now().hour * 60 + _dt.now().minute
                if 570 <= _t < 900:  # 盘中
                    vr_map = _fetch_realtime_volume_ratio([stock_code])
                    result["volume_ratio"] = vr_map.get(stock_code, 0)
                else:
                    result["volume_ratio"] = _safe_round(vol / avg5, 2) if avg5 else 0

            return result
        except Exception as e:
            logger.error("get_indicator_snapshot(%s) failed: %s", stock_code, e)
            return {"error": str(e)}

    if len(code_list) == 1:
        return _one(code_list[0])

    # 批量预取盘中量比（一次 HTTP）
    from datetime import datetime as _dt2
    _t2 = _dt2.now().hour * 60 + _dt2.now().minute
    _vr_map2 = _fetch_realtime_volume_ratio(code_list) if 570 <= _t2 < 900 and len(code_list) > 1 else {}

    results = {}
    for code in code_list:
        try:
            r = _one(code)
            # 多股时用批量预取的量比补充
            if "error" not in r and _vr_map2 and r.get("volume_ratio", 0) == 0:
                vr = _vr_map2.get(code, 0)
                if vr > 0:
                    r["volume_ratio"] = vr
            results[code] = r
        except Exception as e:
            results[code] = {"error": str(e)}

    return {"count": len(results), "data": results}

# ═══════════════════════════════════════════════════════════════
# 图表形态识别（从 chart_patterns.py 合并）
# ═══════════════════════════════════════════════════════════════

def _find_pivots(highs: List[float], lows: List[float],
                 left: int = 5, right: int = 5) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    """寻找局部极值点（枢轴点）。

    返回 (peaks, troughs)，每个元素为 (index, price)。
    peak: highs[i] 是 [i-left, i+right] 范围内的最高点
    trough: lows[i] 是 [i-left, i+right] 范围内的最低点
    """
    n = len(highs)
    peaks, troughs = [], []
    for i in range(left, n - right):
        is_peak = True
        for j in range(i - left, i + right + 1):
            if j == i:
                continue
            if 0 <= j < n and highs[j] > highs[i]:
                is_peak = False
                break
        if is_peak:
            peaks.append((i, highs[i]))

        is_trough = True
        for j in range(i - left, i + right + 1):
            if j == i:
                continue
            if 0 <= j < n and lows[j] < lows[i]:
                is_trough = False
                break
        if is_trough:
            troughs.append((i, lows[i]))

    return peaks, troughs

def _fit_line(points: List[Tuple[int, float]]) -> Tuple[float, float]:
    """对一组 (index, price) 做最小二乘线性拟合，返回 (slope, intercept)。"""
    if len(points) < 2:
        return 0.0, points[0][1] if points else 0.0
    n = len(points)
    sx = sum(p[0] for p in points)
    sy = sum(p[1] for p in points)
    sxx = sum(p[0] ** 2 for p in points)
    sxy = sum(p[0] * p[1] for p in points)
    denom = n * sxx - sx * sx
    if denom == 0:
        return 0.0, sy / n
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    return slope, intercept

def _line_value(slope: float, intercept: float, x: int) -> float:
    return slope * x + intercept

def _price_near(price: float, target: float, tolerance_pct: float = 3.0) -> bool:
    """判断 price 是否在 target 的 tolerance_pct% 范围内。"""
    if target == 0:
        return False
    return abs(price - target) / abs(target) * 100 <= tolerance_pct

# ═══════════════════════════════════════════════════════════════
# 缠论基础：顶底分型 + 笔
# ═══════════════════════════════════════════════════════════════

def _detect_chan_fractals(highs: List[float], lows: List[float],
                          closes: List[float]) -> Optional[Dict[str, Any]]:
    """缠论基础分析：顶底分型识别 + 笔的划分。

    分型规则（严格缠论定义）：
    - 顶分型：中间K线的高点是三根中最高，且中间K线低点也是三根中最高
    - 底分型：中间K线的低点是三根中最低，且中间K线高点也是三根中最低
    - 笔：连接相邻顶分型和底分型，至少包含 5 根 K 线（含分型共用的 K 线）
    """
    n = len(closes)
    if n < 7:
        return None

    # 1. 识别所有分型
    fractals = []  # (index, type, price)  type: 'top' / 'bottom'
    for i in range(1, n - 1):
        if (highs[i] > highs[i - 1] and highs[i] > highs[i + 1] and
                lows[i] > lows[i - 1] and lows[i] > lows[i + 1]):
            fractals.append((i, "top", highs[i]))
        elif (lows[i] < lows[i - 1] and lows[i] < lows[i + 1] and
              highs[i] < highs[i - 1] and highs[i] < highs[i + 1]):
            fractals.append((i, "bottom", lows[i]))

    if len(fractals) < 2:
        return None

    # 2. 过滤：顶底交替，同类型保留更极端的
    filtered = [fractals[0]]
    for f in fractals[1:]:
        if f[1] == filtered[-1][1]:
            if f[1] == "top" and f[2] > filtered[-1][2]:
                filtered[-1] = f
            elif f[1] == "bottom" and f[2] < filtered[-1][2]:
                filtered[-1] = f
        else:
            if f[0] - filtered[-1][0] >= 4:
                filtered.append(f)
            else:
                if f[1] == "top" and f[2] > filtered[-1][2]:
                    filtered[-1] = f
                elif f[1] == "bottom" and f[2] < filtered[-1][2]:
                    filtered[-1] = f

    if len(filtered) < 2:
        return None

    # 3. 构建笔
    strokes = []
    for i in range(len(filtered) - 1):
        start = filtered[i]
        end = filtered[i + 1]
        direction = "上" if end[1] == "top" else "下"
        strokes.append({
            "from": {"index": start[0], "type": start[1], "price": _safe_round(start[2])},
            "to": {"index": end[0], "type": end[1], "price": _safe_round(end[2])},
            "direction": direction,
            "length": abs(end[2] - start[2]),
            "bars": end[0] - start[0],
        })

    # 4. 分析当前笔的状态
    patterns = []
    signals = []

    if len(strokes) >= 2:
        last_stroke = strokes[-1]
        prev_stroke = strokes[-2]

        if last_stroke["direction"] == "上" and prev_stroke["direction"] == "上":
            if last_stroke["to"]["price"] > prev_stroke["to"]["price"]:
                if last_stroke["length"] < prev_stroke["length"] * 0.8:
                    patterns.append("缠论顶背驰（新高但笔力度减弱，可能见顶）")
                    signals.append("缠论顶背驰：上涨力度衰减")
        elif last_stroke["direction"] == "下" and prev_stroke["direction"] == "下":
            if last_stroke["to"]["price"] < prev_stroke["to"]["price"]:
                if last_stroke["length"] < prev_stroke["length"] * 0.8:
                    patterns.append("缠论底背驰（新低但笔力度减弱，可能见底）")
                    signals.append("缠论底背驰：下跌力度衰减")

        last_fractal = filtered[-1]
        if last_fractal[1] == "top":
            patterns.append(f"缠论最近顶分型（位置{_safe_round(last_fractal[2])}）")
        else:
            patterns.append(f"缠论最近底分型（位置{_safe_round(last_fractal[2])}）")

    stroke_dirs = [s["direction"] for s in strokes[-5:]] if len(strokes) >= 3 else []
    if stroke_dirs:
        if all(d == "上" for d in stroke_dirs[-2:]):
            signals.append("笔序列：连续上涨笔，多头趋势")
        elif all(d == "下" for d in stroke_dirs[-2:]):
            signals.append("笔序列：连续下跌笔，空头趋势")

    return {
        "patterns": patterns,
        "signals": signals,
        "fractal_count": len(filtered),
        "stroke_count": len(strokes),
        "recent_strokes": strokes[-3:] if len(strokes) >= 3 else strokes,
    }

def analyze_chart_patterns(codes: str) -> Dict[str, Any]:
    """图表形态识别：头肩顶/底、双顶/双底、三角形、旗形、楔形、矩形、杯柄等经典形态。

    基于枢轴点（局部极值）检测，分析多K线构成的结构性形态。

    Args:
        codes: 多股用逗号分隔
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str) -> Dict[str, Any]:
        try:
            data = _fetch_ohlcv(stock_code, 120)
            closes, highs, lows = data["close"], data["high"], data["low"]
            if len(closes) < 30:
                return {"error": "K线数据不足（至少需要30根）", "retriable": True}

            latest = closes[-1]
            patterns = []
            signals = []

            peaks_l, troughs_l = _find_pivots(highs, lows, left=8, right=8)
            peaks_s, troughs_s = _find_pivots(highs, lows, left=3, right=3)

            # ── 头肩顶 ──
            if len(peaks_l) >= 3:
                for i in range(len(peaks_l) - 2):
                    p1, p2, p3 = peaks_l[i], peaks_l[i + 1], peaks_l[i + 2]
                    h1, h2, h3 = p1[1], p2[1], p3[1]
                    if h2 > h1 and h2 > h3:
                        shoulder_diff = abs(h1 - h3) / max(h1, h3) * 100
                        if shoulder_diff < 10:
                            troughs_between = [t for t in troughs_l if p1[0] < t[0] < p3[0]]
                            if len(troughs_between) >= 2:
                                neckline_pts = [(troughs_between[0][0], troughs_between[0][1]),
                                                (troughs_between[-1][0], troughs_between[-1][1])]
                                slope, intercept = _fit_line(neckline_pts)
                                neckline_at_end = _line_value(slope, intercept, len(closes) - 1)
                                if latest < neckline_at_end:
                                    patterns.append("头肩顶（已跌破颈线，强烈看空）")
                                    target_drop = h2 - neckline_at_end
                                    signals.append(f"头肩顶目标位: {_safe_round(neckline_at_end - target_drop)}")
                                else:
                                    patterns.append("头肩顶雏形（尚未跌破颈线）")
                                break

            # ── 头肩底 ──
            if len(troughs_l) >= 3:
                for i in range(len(troughs_l) - 2):
                    t1, t2, t3 = troughs_l[i], troughs_l[i + 1], troughs_l[i + 2]
                    l1, l2, l3 = t1[1], t2[1], t3[1]
                    if l2 < l1 and l2 < l3:
                        shoulder_diff = abs(l1 - l3) / max(abs(l1), abs(l3)) * 100
                        if shoulder_diff < 10:
                            peaks_between = [p for p in peaks_l if t1[0] < p[0] < t3[0]]
                            if len(peaks_between) >= 2:
                                neckline_pts = [(peaks_between[0][0], peaks_between[0][1]),
                                                (peaks_between[-1][0], peaks_between[-1][1])]
                                slope, intercept = _fit_line(neckline_pts)
                                neckline_at_end = _line_value(slope, intercept, len(closes) - 1)
                                if latest > neckline_at_end:
                                    patterns.append("头肩底（已突破颈线，强烈看多）")
                                    target_rise = neckline_at_end - l2
                                    signals.append(f"头肩底目标位: {_safe_round(neckline_at_end + target_rise)}")
                                else:
                                    patterns.append("头肩底雏形（尚未突破颈线）")
                                break

            # ── 双顶 / 双底 ──
            if len(peaks_l) >= 2:
                for i in range(len(peaks_l) - 1):
                    p1, p2 = peaks_l[i], peaks_l[i + 1]
                    if _price_near(p1[1], p2[1], 5):
                        troughs_between = [t for t in troughs_l if p1[0] < t[0] < p2[0]]
                        if troughs_between:
                            valley = min(t[1] for t in troughs_between)
                            depth = (p1[1] + p2[1]) / 2 - valley
                            if latest < valley:
                                patterns.append("双顶/M顶（已跌破颈线，看空）")
                                signals.append(f"双顶目标位: {_safe_round(valley - depth)}")
                            else:
                                patterns.append("双顶/M顶雏形（颈线未破）")
                            break

            if len(troughs_l) >= 2:
                for i in range(len(troughs_l) - 1):
                    t1, t2 = troughs_l[i], troughs_l[i + 1]
                    if _price_near(t1[1], t2[1], 5):
                        peaks_between = [p for p in peaks_l if t1[0] < p[0] < t2[0]]
                        if peaks_between:
                            peak_val = max(p[1] for p in peaks_between)
                            depth = peak_val - (t1[1] + t2[1]) / 2
                            if latest > peak_val:
                                patterns.append("双底/W底（已突破颈线，看多）")
                                signals.append(f"双底目标位: {_safe_round(peak_val + depth)}")
                            else:
                                patterns.append("双底/W底雏形（颈线未破）")
                            break

            # ── 三角形整理 ──
            if len(peaks_s) >= 2 and len(troughs_s) >= 2:
                rp = peaks_s[-4:] if len(peaks_s) >= 4 else peaks_s[-2:]
                rt = troughs_s[-4:] if len(troughs_s) >= 4 else troughs_s[-2:]
                if len(rp) >= 2 and len(rt) >= 2:
                    p_slope, _ = _fit_line(rp)
                    t_slope, _ = _fit_line(rt)
                    lp_y = _line_value(p_slope, _fit_line(rp)[1], rp[-1][0])
                    lt_y = _line_value(t_slope, _fit_line(rt)[1], rt[-1][0])
                    spread = abs(lp_y - lt_y)
                    avg_p = (lp_y + lt_y) / 2
                    spread_pct = spread / avg_p * 100 if avg_p else 0
                    if spread_pct < 8:
                        if abs(p_slope) < abs(t_slope) * 0.3 and t_slope > 0:
                            patterns.append("上升三角形（水平阻力+上升支撑，偏多突破）")
                        elif abs(t_slope) < abs(p_slope) * 0.3 and p_slope < 0:
                            patterns.append("下降三角形（水平支撑+下降阻力，偏空突破）")
                        elif p_slope < 0 and t_slope > 0:
                            patterns.append("对称三角形（收敛整理，等待方向选择）")

            # ── 旗形 / 三角旗 ──
            if len(closes) >= 30:
                for start_i in range(max(0, len(closes) - 40), len(closes) - 10):
                    pole_end = start_i + 10
                    if pole_end >= len(closes):
                        break
                    pole_change = (closes[pole_end] - closes[start_i]) / closes[start_i] * 100
                    if abs(pole_change) > 15:
                        flag_h = highs[pole_end:]
                        flag_l = lows[pole_end:]
                        if len(flag_h) >= 5:
                            f_peaks = _find_pivots(flag_h, flag_l, 2, 2)[0]
                            f_troughs = _find_pivots(flag_h, flag_l, 2, 2)[1]
                            if len(f_peaks) >= 1 and len(f_troughs) >= 1:
                                fp_s, _ = _fit_line(f_peaks) if len(f_peaks) >= 2 else (0, f_peaks[0][1])
                                ft_s, _ = _fit_line(f_troughs) if len(f_troughs) >= 2 else (0, f_troughs[0][1])
                                d = "上涨" if pole_change > 0 else "下跌"
                                if pole_change > 0 and fp_s < 0 and ft_s < 0:
                                    if abs(fp_s - ft_s) < abs(fp_s) * 0.5:
                                        patterns.append(f"上升旗形（{d}旗杆+下行旗面，中继看多）")
                                    else:
                                        patterns.append(f"上升三角旗（{d}旗杆+收敛旗面，中继看多）")
                                    break
                                elif pole_change < 0 and fp_s > 0 and ft_s > 0:
                                    if abs(fp_s - ft_s) < abs(fp_s) * 0.5:
                                        patterns.append(f"下降旗形（{d}旗杆+上行旗面，中继看空）")
                                    else:
                                        patterns.append(f"下降三角旗（{d}旗杆+收敛旗面，中继看空）")
                                    break

            # ── 楔形 ──
            if len(peaks_s) >= 2 and len(troughs_s) >= 2:
                rp = peaks_s[-3:]
                rt = troughs_s[-3:]
                if len(rp) >= 2 and len(rt) >= 2:
                    p_slope, _ = _fit_line(rp)
                    t_slope, _ = _fit_line(rt)
                    if p_slope > 0 and t_slope > 0 and t_slope > p_slope:
                        patterns.append("上升楔形（看空，支撑上升快于阻力，终将破位）")
                    elif p_slope < 0 and t_slope < 0 and p_slope < t_slope:
                        patterns.append("下降楔形（看多，阻力下降快于支撑，终将突破）")

            # ── 矩形整理 ──
            if len(peaks_s) >= 2 and len(troughs_s) >= 2:
                rp = peaks_s[-3:]
                rt = troughs_s[-3:]
                if len(rp) >= 2 and len(rt) >= 2:
                    p_slope, _ = _fit_line(rp)
                    t_slope, _ = _fit_line(rt)
                    avg = (sum(p[1] for p in rp) + sum(t[1] for t in rt)) / (len(rp) + len(rt))
                    if abs(p_slope) / avg * 100 < 0.5 and abs(t_slope) / avg * 100 < 0.5:
                        resistance = sum(p[1] for p in rp) / len(rp)
                        support = sum(t[1] for t in rt) / len(rt)
                        box_h = (resistance - support) / support * 100 if support else 0
                        if 3 < box_h < 20:
                            if latest >= resistance * 0.98:
                                patterns.append(f"矩形整理（接近上沿阻力{_safe_round(resistance)}，关注突破）")
                            elif latest <= support * 1.02:
                                patterns.append(f"矩形整理（接近下沿支撑{_safe_round(support)}，关注破位）")
                            else:
                                patterns.append(f"矩形整理（箱体震荡，区间{_safe_round(support)}-{_safe_round(resistance)}）")

            # ── 杯柄形态 ──
            if len(troughs_l) >= 1 and len(closes) >= 40:
                deepest = min(troughs_l, key=lambda t: t[1])
                cup_idx = deepest[0]
                if 15 <= cup_idx <= len(closes) - 15:
                    left_peak = max(highs[:cup_idx]) if cup_idx > 0 else 0
                    right_area = highs[cup_idx:]
                    if len(right_area) >= 10:
                        right_peak = max(right_area[:len(right_area) // 2]) if len(right_area) >= 10 else 0
                        if left_peak > 0 and right_peak > 0 and _price_near(left_peak, right_peak, 8):
                            cup_depth = (left_peak + right_peak) / 2 - deepest[1]
                            cup_depth_pct = cup_depth / ((left_peak + right_peak) / 2) * 100
                            if 12 < cup_depth_pct < 40:
                                handle_area = closes[cup_idx + len(right_area) // 2:]
                                if len(handle_area) >= 3:
                                    handle_drop = (max(handle_area) - min(handle_area)) / max(handle_area) * 100
                                    if handle_drop < cup_depth_pct * 0.5:
                                        patterns.append("杯柄形态（底部反转看多，突破杯口即确认）")
                                        signals.append(f"杯口阻力: {_safe_round((left_peak + right_peak) / 2)}")

            # ── 缠论分型 ──
            chan_result = _detect_chan_fractals(highs, lows, closes)
            if chan_result:
                patterns.extend(chan_result.get("patterns", []))
                signals.extend(chan_result.get("signals", []))

            if not patterns:
                patterns.append("无明显图表形态")

            bullish_kw = ["看多", "突破", "底部反转", "W底", "头肩底", "下降楔形", "杯柄"]
            bearish_kw = ["看空", "破位", "顶部反转", "M顶", "头肩顶", "上升楔形"]
            bull_c = sum(1 for p in patterns for k in bullish_kw if k in p)
            bear_c = sum(1 for p in patterns for k in bearish_kw if k in p)
            pattern_score = max(0, min(100, 50 + (bull_c - bear_c) * 15))

            return {
                "stock_code": stock_code,
                "latest_close": _safe_round(latest),
                "patterns": patterns,
                "pattern_count": len([p for p in patterns if "无明显" not in p]),
                "pattern_score": pattern_score,
                "direction": "看多" if pattern_score >= 60 else ("看空" if pattern_score <= 40 else "中性"),
                "signals": signals,
            }
        except Exception as e:
            logger.error("analyze_chart_patterns(%s) failed: %s", stock_code, e)
            return {"error": str(e)}

    if len(code_list) == 1:
        return _one(code_list[0])

    results = {}
    for code in code_list:
        try:
            results[code] = _one(code)
        except Exception as e:
            results[code] = {"error": str(e)}
    return {"count": len(results), "data": results}

def get_obv_analysis(codes: str) -> Dict[str, Any]:
    """OBV（能量潮）量价分析：返回 OBV 值、趋势、与价格的背离检测。

    OBV 是累计成交量指标，价格上涨日加上成交量，价格下跌日减去成交量。
    用于验证趋势是否得到量能支撑。

    Args:
        codes: 多股用逗号分隔
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str) -> Dict[str, Any]:
        try:
            data = _fetch_ohlcv(stock_code, 120)
            closes = data["close"]
            volumes = data["volume"]
            if len(closes) < 10:
                return {"error": "K线数据不足（至少需要10根）", "retriable": True}

            obv_result = _calc_obv(closes, volumes)
            if "error" in obv_result:
                return obv_result

            vol = volumes[-1]
            avg_vol = sum(volumes[-5:]) / 5
            vol_ratio = _safe_round(vol / avg_vol, 2) if avg_vol else 0
            price_change_pct = _safe_round((closes[-1] - closes[-2]) / closes[-2] * 100, 2) if closes[-2] else 0

            if price_change_pct > 0 and obv_result["obv_trend"] == "上升":
                vol_price_assessment = "量价齐升，多头量能充足"
            elif price_change_pct < 0 and obv_result["obv_trend"] == "下降":
                vol_price_assessment = "量价齐跌，空头量能释放"
            elif price_change_pct > 0 and obv_result["obv_trend"] == "下降":
                vol_price_assessment = "价涨量缩，上涨缺乏量能支撑，注意回调"
            elif price_change_pct < 0 and obv_result["obv_trend"] == "上升":
                vol_price_assessment = "价跌量增，可能有资金低位吸筹"
            else:
                vol_price_assessment = "量价关系中性"

            return {
                "stock_code": stock_code,
                "latest_close": _safe_round(closes[-1]),
                **obv_result,
                "volume_ratio": vol_ratio,
                "price_change_pct": price_change_pct,
                "vol_price_assessment": vol_price_assessment,
            }
        except Exception as e:
            logger.error("get_obv_analysis(%s) failed: %s", stock_code, e)
            return {"error": str(e)}

    if len(code_list) == 1:
        return _one(code_list[0])

    results = {}
    for code in code_list:
        try:
            results[code] = _one(code)
        except Exception as e:
            results[code] = {"error": str(e)}
    return {"count": len(results), "data": results}

# ── OpenAI tool declarations ─────────────────────────────────

