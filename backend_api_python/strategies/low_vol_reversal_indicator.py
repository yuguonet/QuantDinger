# IndicatorStrategy: 低波反转
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
# @param position_pct int 60 仓位比例%

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
position_pct = params.get('position_pct', 60)

# ── 布林带 ──
df['bb_mid'] = df['close'].rolling(window=bb_period).mean()
df['bb_std_val'] = df['close'].rolling(window=bb_period).std()
df['bb_upper'] = df['bb_mid'] + bb_std * df['bb_std_val']
df['bb_lower'] = df['bb_mid'] - bb_std * df['bb_std_val']

# ── BB 带宽（波动率收敛指标）──
df['bb_bandwidth'] = (df['bb_upper'] - df['bb_lower']) / df['bb_mid'] * 100
# 带宽的历史分位
df['bb_bw_percentile'] = df['bb_bandwidth'].rolling(window=squeeze_lookback).rank(pct=True) * 100

# ── 成交量均线 ──
df['vol_ma'] = df['volume'].rolling(window=vol_ma_period).mean()
df['vol_ratio_val'] = df['volume'] / df['vol_ma']

# ── 短期均线 ──
df['ma_short'] = df['close'].rolling(window=ma_period).mean()

# ── RSI ──
delta = df['close'].diff()
gain = delta.where(delta > 0, 0).rolling(window=rsi_period).mean()
loss = (-delta.where(delta < 0, 0)).rolling(window=rsi_period).mean()
rs = gain / loss
df['rsi'] = 100 - (100 / (1 + rs))
# RSI 前一日值（用于检测上穿）
df['rsi_prev'] = df['rsi'].shift(1)

# ── 买入条件 ──
# 1. BB 带宽处于历史低位（波动率收敛到极致）
cond_squeeze = df['bb_bw_percentile'] < squeeze_percentile
# 2. 放量突破（成交量 > 均量 × vol_ratio）
cond_volume = df['vol_ratio_val'] > vol_ratio
# 3. 价格站上短期均线
cond_above_ma = df['close'] > df['ma_short']
# 4. RSI 从超卖区回升（前一日 < 阈值，当日 >= 阈值）
cond_rsi_cross = (df['rsi_prev'] < rsi_exit_oversold) & (df['rsi'] >= rsi_exit_oversold)

df['buy'] = (cond_squeeze & cond_volume & cond_above_ma & cond_rsi_cross).fillna(False)

# ── 卖出条件 ──
# RSI 超买或价格跌破布林中轨
cond_sell_rsi = df['rsi'] > 70
cond_sell_bb = df['close'] < df['bb_mid']
cond_sell_profit = (df['close'] / df['close'].cummax() - 1) * 100 > 15.0  # 止盈 15%
df['sell'] = (cond_sell_rsi | cond_sell_bb | cond_sell_profit).fillna(False)

# ── NaN 安全处理 ──
df['buy'] = df['buy'].fillna(False).astype(bool)
df['sell'] = df['sell'].fillna(False).astype(bool)
