#!/usr/bin/env python3
"""技术指标库 (auto/common) —— 从 dragon_core.py 逐字提取 (2026-09-07)

用途: 策略共用的纯指标计算, 与 test_dragon.py 同名函数逐字一致 (对数基准)。
关键设计点:
  - ema/rsi 返回单值或 None (长度不足); calc_macd 返回 (dif, dea, hist) 三序列或 (None,None,None);
  - MACD柱 = 2*(DIF-DEA) (国内行情软件口径, 与普通教科书的 DIF-DEA 不同, 勿"修正");
  - 形态族 (golden_cross/turning_positive/shrinking_negative) 只做布尔判定。
易错点: 全部函数只读入参序列尾部, 无未来函数问题; 但回测切片语义由调用方 (as_of) 保证。
"""
from __future__ import annotations


def ema(values, period):
    """计算EMA (指数移动平均)"""
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period  # 初始值用SMA
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def rsi(closes, period=14):
    """计算RSI (相对强弱指数)"""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    # 初始SMA
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    # EMA平滑
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - 100 / (1 + rs)


def calc_macd(closes, fast=12, slow=26, signal=9):
    """计算MACD, 返回 (dif, dea, macd_hist) 三个序列

    MACD柱 = 2*(DIF-DEA), DIF=EMA(fast)-EMA(slow), DEA=EMA(DIF,signal)
    """
    n = len(closes)
    if n < slow + signal:
        return None, None, None
    # 计算EMA序列
    ema_fast = [0.0] * n
    ema_slow = [0.0] * n
    k_f = 2 / (fast + 1)
    k_s = 2 / (slow + 1)
    ema_fast[0] = closes[0]
    ema_slow[0] = closes[0]
    for i in range(1, n):
        ema_fast[i] = closes[i] * k_f + ema_fast[i-1] * (1 - k_f)
        ema_slow[i] = closes[i] * k_s + ema_slow[i-1] * (1 - k_s)
    # DIF序列
    dif = [ema_fast[i] - ema_slow[i] for i in range(n)]
    # DEA = EMA(DIF, signal)
    dea = [0.0] * n
    k_sig = 2 / (signal + 1)
    dea[0] = dif[0]
    for i in range(1, n):
        dea[i] = dif[i] * k_sig + dea[i-1] * (1 - k_sig)
    # MACD柱 = 2*(DIF-DEA)
    hist = [2 * (dif[i] - dea[i]) for i in range(n)]
    return dif, dea, hist


def calc_bollinger_bw(closes, period=20, num_std=2):
    """计算布林带宽百分比 = (upper-lower)/middle*100, 仅返回带宽值"""
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid = sum(window) / period
    if mid <= 0:
        return None
    var = sum((x - mid) ** 2 for x in window) / period
    std = var ** 0.5
    upper = mid + num_std * std
    lower = mid - num_std * std
    return (upper - lower) / mid * 100


def calc_roc(closes, period=10):
    """计算变动率 ROC = (close[i]-close[i-period])/close[i-period]*100"""
    if len(closes) < period + 1:
        return None
    ref = closes[-1 - period]
    if ref <= 0:
        return None
    return (closes[-1] - ref) / ref * 100


def calc_psy(closes, period=12):
    """计算心理线 PSY = 过去period天中上涨天数/period*100"""
    if len(closes) < period + 1:
        return None
    up_days = 0
    for i in range(-period, 0):
        if closes[i] > closes[i-1]:
            up_days += 1
    return up_days / period * 100


def is_macd_golden_cross(dif, dea, lookback=3):
    """判断MACD是否在最近lookback根K线内发生金叉 (DIF上穿DEA)"""
    if dif is None or dea is None or len(dif) < lookback + 1:
        return False
    n = len(dif)
    if dif[n-1] < dea[n-1]:
        return False  # 当前DIF在DEA下方
    for i in range(max(0, n - lookback - 1), n - 1):
        if dif[i] < dea[i]:
            return True
    return False


def is_macd_hist_turning_positive(hist, lookback=3):
    """判断MACD柱是否在最近lookback根内由负转正 (绿柱缩短→红柱)"""
    if hist is None or len(hist) < lookback + 1:
        return False
    n = len(hist)
    if hist[n-1] <= 0:
        return False  # 当前柱还是负的
    for i in range(max(0, n - lookback - 1), n - 1):
        if hist[i] < 0:
            return True
    return False


def is_macd_hist_shrinking_negative(hist, lookback=5):
    """判断MACD绿柱是否在缩短 (负柱绝对值在减小)"""
    if hist is None or len(hist) < lookback:
        return False
    n = len(hist)
    recent = hist[n - lookback:]
    if any(h >= 0 for h in recent):
        return False
    abs_vals = [abs(h) for h in recent]
    return abs_vals[-1] < abs_vals[-2] < abs_vals[-3] if len(abs_vals) >= 3 else abs_vals[-1] < abs_vals[-2]
