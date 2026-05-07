my_indicator_name = "趋势回踩买入"
my_indicator_description = """# IndicatorStrategy: 趋势回踩买入
# @strategy stopLossPct 0.04
# @strategy trailingEnabled true
# @strategy trailingStopPct 0.04
# @strategy tradeDirection long
# @param trend_ema_period int 30 趋势EMA周期
# @param pullback_pct float 2.0 回踩偏离度%
# @param rsi_period int 14 RSI周期
# @param rsi_low int 40 RSI下限
# @param rsi_high int 60 RSI上限
# @param rsi_overbought int 70 RSI超买阈值
# @param vol_ma_period int 20 成交量均线周期
# @param vol_shrink_ratio float 0.6 缩量比例
# @param use_vol_shrink bool true 是否要求缩量回调
# @param ema_break_pct float 4.0 跌破EMA百分比"""

import pandas as pd
import numpy as np

# ── 参数 ──
trend_ema_period = params.get('trend_ema_period', 30)
pullback_pct = params.get('pullback_pct', 2.0)
rsi_period = params.get('rsi_period', 14)
rsi_low = params.get('rsi_low', 40)
rsi_high = params.get('rsi_high', 60)
rsi_overbought = params.get('rsi_overbought', 70)
vol_ma_period = params.get('vol_ma_period', 20)
vol_shrink_ratio = params.get('vol_shrink_ratio', 0.6)
use_vol_shrink = params.get('use_vol_shrink', True)
ema_break_pct = params.get('ema_break_pct', 4.0)

# ── 趋势 EMA ──
df['ema_trend'] = df['close'].ewm(span=trend_ema_period, adjust=False).mean()

# ── RSI（防除零）──
delta = df['close'].diff()
gain = delta.where(delta > 0, 0).rolling(window=rsi_period).mean()
loss = (-delta.where(delta < 0, 0)).rolling(window=rsi_period).mean()
loss_safe = loss.copy()
loss_safe[loss_safe < 1e-10] = 1e-10
rs = gain / loss_safe
df['rsi'] = 100 - (100 / (1 + rs))

# ── 成交量均线 ──
df['vol_ma'] = df['volume'].rolling(window=vol_ma_period).mean()
vol_ma_safe = df['vol_ma'].replace(0, float('nan'))
df['vol_ratio_val'] = df['volume'] / vol_ma_safe

# ── 价格距 EMA 偏离度 ──
df['ema_deviation'] = (df['close'] / df['ema_trend'] - 1) * 100

# ── 买入条件 ──
cond_above_ema = df['close'] > df['ema_trend']
cond_near_ema = df['ema_deviation'].abs() <= pullback_pct
cond_rsi_range = (df['rsi'] > rsi_low) & (df['rsi'] < rsi_high)
cond_vol_shrink = df['vol_ratio_val'] < vol_shrink_ratio

if use_vol_shrink:
    df['buy'] = (cond_above_ema & cond_near_ema & cond_rsi_range & cond_vol_shrink).fillna(False)
else:
    df['buy'] = (cond_above_ema & cond_near_ema & cond_rsi_range).fillna(False)

# ── 卖出条件 ──
cond_sell_rsi = df['rsi'] > rsi_overbought
cond_sell_ema = df['close'] < df['ema_trend'] * (1 - ema_break_pct / 100)
df['sell'] = (cond_sell_rsi | cond_sell_ema).fillna(False)

# ── NaN 安全处理 ──
df['buy'] = df['buy'].fillna(False).astype(bool)
df['sell'] = df['sell'].fillna(False).astype(bool)

buy_marks = [df['low'].iloc[i] * 0.995 if bool(df['buy'].iloc[i]) else None for i in range(len(df))]
sell_marks = [df['high'].iloc[i] * 1.005 if bool(df['sell'].iloc[i]) else None for i in range(len(df))]

output = {
    'name': my_indicator_name,
    'plots': [
        {'name': '趋势EMA', 'data': df['ema_trend'].tolist(), 'color': '#FFD54F', 'overlay': True},
        {'name': 'RSI', 'data': df['rsi'].tolist(), 'color': '#faad14', 'overlay': False},
    ],
    'signals': [
        {'type': 'buy', 'text': 'B', 'data': buy_marks, 'color': '#00E676'},
        {'type': 'sell', 'text': 'S', 'data': sell_marks, 'color': '#FF5252'},
    ],
}
