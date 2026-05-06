# IndicatorStrategy: 涨停板次日博弈
# @strategy stopLossPct 0.04
# @strategy takeProfitPct 0.08
# @strategy tradeDirection long
# @param limitup_lookback int 60 涨停检测回望周期
# @param limitup_top_pct int 5 涨停排名百分位
# @param min_open_gap float 1.0 最低低开幅度%
# @param turn_positive_pct float 0.5 翻红涨幅%
# @param vol_ma_period int 20 成交量均线周期
# @param vol_ratio float 1.2 放量倍数
# @param use_vol_confirm bool true 是否叠加量能确认
# @param position_pct int 50 仓位比例%

import pandas as pd
import numpy as np

# ── 参数 ──
limitup_lookback = params.get('limitup_lookback', 60)
limitup_top_pct = params.get('limitup_top_pct', 5)
min_open_gap = params.get('min_open_gap', 1.0)
turn_positive_pct = params.get('turn_positive_pct', 0.5)
vol_ma_period = params.get('vol_ma_period', 20)
vol_ratio = params.get('vol_ratio', 1.2)
use_vol_confirm = params.get('use_vol_confirm', True)
position_pct = params.get('position_pct', 50)

# ── 涨停检测 ──
# 近 N 日涨幅排名，排名前 top_pct% 视为涨停基因
df['ret_1d'] = df['close'].pct_change() * 100
df['ret_rank'] = df['ret_1d'].rolling(window=limitup_lookback).rank(pct=True) * 100
# 当日涨幅 >= 9.5% 视为涨停（考虑四舍五入）
df['is_limitup'] = df['ret_1d'] >= 9.5
# 前一日涨停
df['prev_limitup'] = df['is_limitup'].shift(1)

# ── 开盘缺口 ──
df['open_gap'] = (df['open'] / df['close'].shift(1) - 1) * 100

# ── 当日涨幅（从开盘价计算）──
df['intraday_ret'] = (df['close'] / df['open'] - 1) * 100

# ── 成交量均线 ──
df['vol_ma'] = df['volume'].rolling(window=vol_ma_period).mean()
df['vol_ratio_val'] = df['volume'] / df['vol_ma']

# ── 买入条件 ──
# 1. 前一日涨停
cond_prev_limitup = df['prev_limitup'] == True
# 2. 当日低开（开盘缺口为负，幅度在 min_open_gap% 以内）
cond_low_open = (df['open_gap'] < -min_open_gap) & (df['open_gap'] > -5.0)
# 3. 当日翻红（从低开到涨幅 >= turn_positive_pct%）
cond_turn_positive = df['intraday_ret'] >= turn_positive_pct
# 4. 量能确认（可选）
cond_volume = df['vol_ratio_val'] > vol_ratio if use_vol_confirm else True

df['buy'] = (cond_prev_limitup & cond_low_open & cond_turn_positive & cond_volume).fillna(False)

# ── 卖出条件 ──
# 次日低开或涨幅回吐
df['next_open_gap'] = (df['open'] / df['close'].shift(1) - 1) * 100
cond_sell_gap = df['next_open_gap'] < -2.0  # 次日低开超 2%
cond_sell_profit = df['intraday_ret'] > 8.0  # 止盈 8%
cond_sell_loss = df['intraday_ret'] < -4.0   # 止损 4%
df['sell'] = (cond_sell_gap | cond_sell_profit | cond_sell_loss).fillna(False)

# ── NaN 安全处理 ──
df['buy'] = df['buy'].fillna(False).astype(bool)
df['sell'] = df['sell'].fillna(False).astype(bool)
