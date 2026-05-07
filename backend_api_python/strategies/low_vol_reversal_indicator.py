my_indicator_name = "低波反转"
my_indicator_description = """# IndicatorStrategy: 低波反转
# @strategy stopLossPct 0.05
# @strategy takeProfitPct 0.15
# @strategy trailingEnabled true
# @strategy trailingStopPct 0.06
# @strategy tradeDirection long
# @param bb_period int 20 布林带周期
# @param bb_std float 2.0 布林带标准差
# @param squeeze_lookback int 120 BB带宽历史观察窗口
# @param squeeze_percentile int 20 BB带宽收缩分位
# @param vol_ma_period int 20 成交量均线周期
# @param vol_ratio float 1.5 放量倍数
# @param ma_period int 10 短期均线周期
# @param rsi_period int 14 RSI周期
# @param rsi_exit_oversold int 35 RSI脱离超卖阈值
# @param rsi_overbought int 70 RSI超买阈值
# @param position_pct int 60 仓位比例%"""

import pandas as pd
import numpy as np

# ── 参数 ──
bb_period = params.get('bb_period', 20)
bb_std = params.get('bb_std', 2.0)
squeeze_lookback = params.get('squeeze_lookback', 120)
squeeze_percentile = params.get('squeeze_percentile', 20)
vol_ma_period = params.get('vol_ma_period', 20)
vol_ratio = params.get('vol_ratio', 1.5)
ma_period = params.get('ma_period', 10)
rsi_period = params.get('rsi_period', 14)
rsi_exit_oversold = params.get('rsi_exit_oversold', 35)
rsi_overbought = params.get('rsi_overbought', 70)
position_pct = params.get('position_pct', 60)

# ── 布林带 ──
df['bb_mid'] = df['close'].rolling(window=bb_period).mean()
df['bb_std_val'] = df['close'].rolling(window=bb_period).std()
df['bb_upper'] = df['bb_mid'] + bb_std * df['bb_std_val']
df['bb_lower'] = df['bb_mid'] - bb_std * df['bb_std_val']

# ── BB 带宽（波动率收敛指标）──
bb_mid_safe = df['bb_mid'].replace(0, float('nan'))
df['bb_bandwidth'] = (df['bb_upper'] - df['bb_lower']) / bb_mid_safe * 100
df['bb_bw_percentile'] = df['bb_bandwidth'].rolling(window=squeeze_lookback).rank(pct=True) * 100

# ── 成交量均线 ──
df['vol_ma'] = df['volume'].rolling(window=vol_ma_period).mean()
vol_ma_safe = df['vol_ma'].replace(0, float('nan'))
df['vol_ratio_val'] = df['volume'] / vol_ma_safe

# ── 短期均线 ──
df['ma_short'] = df['close'].rolling(window=ma_period).mean()

# ── RSI（防除零）──
delta = df['close'].diff()
gain = delta.where(delta > 0, 0).rolling(window=rsi_period).mean()
loss = (-delta.where(delta < 0, 0)).rolling(window=rsi_period).mean()
loss_safe = loss.copy()
loss_safe[loss_safe < 1e-10] = 1e-10
rs = gain / loss_safe
df['rsi'] = 100 - (100 / (1 + rs))
df['rsi_prev'] = df['rsi'].shift(1)

# ── 买入条件 ──
cond_squeeze = df['bb_bw_percentile'] < squeeze_percentile
cond_volume = df['vol_ratio_val'] > vol_ratio
cond_above_ma = df['close'] > df['ma_short']
cond_rsi_cross = (df['rsi_prev'] < rsi_exit_oversold) & (df['rsi'] >= rsi_exit_oversold)

df['buy'] = (cond_squeeze & cond_volume & cond_above_ma & cond_rsi_cross).fillna(False)

# ── 卖出条件（技术面信号，止损止盈由框架管理）──
cond_sell_rsi = df['rsi'] > rsi_overbought
cond_sell_bb = df['close'] < df['bb_mid']
df['sell'] = (cond_sell_rsi | cond_sell_bb).fillna(False)

# ── NaN 安全处理 ──
df['buy'] = df['buy'].fillna(False).astype(bool)
df['sell'] = df['sell'].fillna(False).astype(bool)

buy_marks = [df['low'].iloc[i] * 0.995 if bool(df['buy'].iloc[i]) else None for i in range(len(df))]
sell_marks = [df['high'].iloc[i] * 1.005 if bool(df['sell'].iloc[i]) else None for i in range(len(df))]

output = {
    'name': my_indicator_name,
    'plots': [
        {'name': 'BB上轨', 'data': df['bb_upper'].tolist(), 'color': '#90CAF9', 'overlay': True},
        {'name': 'BB中轨', 'data': df['bb_mid'].tolist(), 'color': '#FFD54F', 'overlay': True},
        {'name': 'BB下轨', 'data': df['bb_lower'].tolist(), 'color': '#90CAF9', 'overlay': True},
        {'name': 'RSI', 'data': df['rsi'].tolist(), 'color': '#faad14', 'overlay': False},
        {'name': 'BB带宽%', 'data': df['bb_bandwidth'].tolist(), 'color': '#AB47BC', 'overlay': False},
    ],
    'signals': [
        {'type': 'buy', 'text': 'B', 'data': buy_marks, 'color': '#00E676'},
        {'type': 'sell', 'text': 'S', 'data': sell_marks, 'color': '#FF5252'},
    ],
}
