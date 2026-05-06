# IndicatorStrategy: 量价共振突破
# @strategy stopLossPct 0.05
# @strategy takeProfitPct 0.12
# @strategy trailingEnabled true
# @strategy trailingStopPct 0.05
# @strategy tradeDirection long
# @param breakout_period int 20 Donchian通道周期
# @param vol_ma_period int 20 成交量均线周期
# @param vol_ratio float 2.0 放量倍数
# @param min_change_pct float 3.0 近期涨幅下限
# @param max_change_pct float 8.0 近期涨幅上限
# @param trend_ema_period int 30 趋势EMA周期
# @param use_trend_filter bool true 是否叠加趋势过滤

import pandas as pd
import numpy as np

# ── 参数 ──
breakout_period = params.get('breakout_period', 20)
vol_ma_period = params.get('vol_ma_period', 20)
vol_ratio = params.get('vol_ratio', 2.0)
min_change_pct = params.get('min_change_pct', 3.0)
max_change_pct = params.get('max_change_pct', 8.0)
trend_ema_period = params.get('trend_ema_period', 30)
use_trend_filter = params.get('use_trend_filter', True)

# ── Donchian 通道上轨（前一根 K 线的 N 日最高价，避免前瞻偏差）──
df['dc_upper'] = df['high'].rolling(window=breakout_period).max().shift(1)
df['dc_lower'] = df['low'].rolling(window=breakout_period).min().shift(1)

# ── 成交量均线 ──
df['vol_ma'] = df['volume'].rolling(window=vol_ma_period).mean()
df['vol_ratio_val'] = df['volume'] / df['vol_ma']

# ── 近期涨幅（5 日）──
df['period_ret'] = df['close'].pct_change(periods=5) * 100

# ── 趋势 EMA ──
df['ema_trend'] = df['close'].ewm(span=trend_ema_period, adjust=False).mean()

# ── 买入条件 ──
# 1. 价格突破 N 日高点
cond_breakout = df['close'] > df['dc_upper']
# 2. 成交量放大确认
cond_volume = df['vol_ratio_val'] > vol_ratio
# 3. 涨幅下限：排除弱势股
cond_min_ret = df['period_ret'] >= min_change_pct
# 4. 涨幅上限：不追涨停板
cond_max_ret = df['period_ret'] <= max_change_pct
# 5. 趋势过滤（可选）
cond_trend = df['close'] > df['ema_trend'] if use_trend_filter else True

df['buy'] = (cond_breakout & cond_volume & cond_min_ret & cond_max_ret & cond_trend).fillna(False)

# ── 卖出条件：价格跌破趋势 EMA 或从高点回撤 ──
df['highest_since_entry'] = df['close'].cummax()
df['drawdown'] = (df['close'] - df['highest_since_entry']) / df['highest_since_entry'] * 100
cond_sell_trend = df['close'] < df['ema_trend']
cond_sell_dd = df['drawdown'] < -5.0
df['sell'] = (cond_sell_trend | cond_sell_dd).fillna(False)

# ── NaN 安全处理 ──
df['buy'] = df['buy'].fillna(False).astype(bool)
df['sell'] = df['sell'].fillna(False).astype(bool)
