# IndicatorStrategy: 趋势回踩买入
# @strategy stopLossPct 0.04
# @strategy trailingEnabled true
# @strategy trailingStopPct 0.04
# @strategy tradeDirection long
# @param trend_ema_period int 30 趋势EMA周期
# @param pullback_pct float 2.0 回踩偏离度%
# @param rsi_period int 14 RSI周期
# @param rsi_low int 40 RSI下限
# @param rsi_high int 60 RSI上限
# @param vol_ma_period int 20 成交量均线周期
# @param vol_shrink_ratio float 0.6 缩量比例
# @param use_vol_shrink bool true 是否要求缩量回调

import pandas as pd
import numpy as np

# ── 参数 ──
trend_ema_period = params.get('trend_ema_period', 30)
pullback_pct = params.get('pullback_pct', 2.0)
rsi_period = params.get('rsi_period', 14)
rsi_low = params.get('rsi_low', 40)
rsi_high = params.get('rsi_high', 60)
vol_ma_period = params.get('vol_ma_period', 20)
vol_shrink_ratio = params.get('vol_shrink_ratio', 0.6)
use_vol_shrink = params.get('use_vol_shrink', True)

# ── 趋势 EMA ──
df['ema_trend'] = df['close'].ewm(span=trend_ema_period, adjust=False).mean()

# ── RSI ──
delta = df['close'].diff()
gain = delta.where(delta > 0, 0).rolling(window=rsi_period).mean()
loss = (-delta.where(delta < 0, 0)).rolling(window=rsi_period).mean()
rs = gain / loss
df['rsi'] = 100 - (100 / (1 + rs))

# ── 成交量均线 ──
df['vol_ma'] = df['volume'].rolling(window=vol_ma_period).mean()
df['vol_ratio_val'] = df['volume'] / df['vol_ma']

# ── 价格距 EMA 偏离度 ──
df['ema_deviation'] = (df['close'] / df['ema_trend'] - 1) * 100

# ── 买入条件 ──
# 1. 价格在 EMA 上方（确认上升趋势）
cond_above_ema = df['close'] > df['ema_trend']
# 2. 价格回踩到 EMA 附近（偏离度在 ±pullback_pct 以内）
cond_near_ema = df['ema_deviation'].abs() <= pullback_pct
# 3. RSI 在合理区间（非超买非超卖，是趋势中的回调）
cond_rsi_range = (df['rsi'] > rsi_low) & (df['rsi'] < rsi_high)
# 4. 缩量回调（可选）
cond_vol_shrink = df['vol_ratio_val'] < vol_shrink_ratio if use_vol_shrink else True

df['buy'] = (cond_above_ema & cond_near_ema & cond_rsi_range & cond_vol_shrink).fillna(False)

# ── 卖出条件 ──
# RSI 超买或价格跌破 EMA
cond_sell_rsi = df['rsi'] > 70
cond_sell_ema = df['close'] < df['ema_trend'] * 0.96  # 跌破 EMA 4%
df['sell'] = (cond_sell_rsi | cond_sell_ema).fillna(False)

# ── NaN 安全处理 ──
df['buy'] = df['buy'].fillna(False).astype(bool)
df['sell'] = df['sell'].fillna(False).astype(bool)
