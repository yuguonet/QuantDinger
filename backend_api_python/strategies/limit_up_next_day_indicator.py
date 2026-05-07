my_indicator_name = "涨停板次日博弈"
my_indicator_description = """# IndicatorStrategy: 涨停板次日博弈
# @strategy stopLossPct 0.04
# @strategy takeProfitPct 0.08
# @strategy tradeDirection long
# @param limitup_lookback int 60 涨停检测回望周期
# @param limitup_pct float 9.5 涨停判定阈值%
# @param min_open_gap float 1.0 最低低开幅度%
# @param max_open_gap float 5.0 最大低开幅度%
# @param turn_positive_pct float 0.5 翻红涨幅%
# @param vol_ma_period int 20 成交量均线周期
# @param vol_ratio float 1.2 放量倍数
# @param use_vol_confirm bool true 是否叠加量能确认
# @param sell_gap_pct float 3.0 次日低开止损%
# @param sell_profit_pct float 8.0 日内止盈%
# @param sell_loss_pct float 4.0 日内止损%
# @param position_pct int 50 仓位比例%"""

import pandas as pd
import numpy as np

# ── 参数 ──
limitup_lookback = params.get('limitup_lookback', 60)
limitup_pct = params.get('limitup_pct', 9.5)
min_open_gap = params.get('min_open_gap', 1.0)
max_open_gap = params.get('max_open_gap', 5.0)
turn_positive_pct = params.get('turn_positive_pct', 0.5)
vol_ma_period = params.get('vol_ma_period', 20)
vol_ratio = params.get('vol_ratio', 1.2)
use_vol_confirm = params.get('use_vol_confirm', True)
sell_gap_pct = params.get('sell_gap_pct', 3.0)
sell_profit_pct = params.get('sell_profit_pct', 8.0)
sell_loss_pct = params.get('sell_loss_pct', 4.0)
position_pct = params.get('position_pct', 50)

# ── 涨停检测 ──
df['ret_1d'] = df['close'].pct_change() * 100
df['is_limitup'] = df['ret_1d'] >= limitup_pct
df['prev_limitup'] = df['is_limitup'].shift(1)

# ── 开盘缺口 ──
df['open_gap'] = (df['open'] / df['close'].shift(1) - 1) * 100

# ── 当日涨幅（从开盘价计算）──
df['intraday_ret'] = (df['close'] / df['open'] - 1) * 100

# ── 成交量均线 ──
df['vol_ma'] = df['volume'].rolling(window=vol_ma_period).mean()
vol_ma_safe = df['vol_ma'].replace(0, float('nan'))
df['vol_ratio_val'] = df['volume'] / vol_ma_safe

# ── 买入条件 ──
cond_prev_limitup = df['prev_limitup'] == True
cond_low_open = (df['open_gap'] < -min_open_gap) & (df['open_gap'] > -max_open_gap)
cond_turn_positive = df['intraday_ret'] >= turn_positive_pct
cond_volume = df['vol_ratio_val'] > vol_ratio

if use_vol_confirm:
    df['buy'] = (cond_prev_limitup & cond_low_open & cond_turn_positive & cond_volume).fillna(False)
else:
    df['buy'] = (cond_prev_limitup & cond_low_open & cond_turn_positive).fillna(False)

# ── 卖出条件（当日盘中风控）──
cond_sell_gap = df['open_gap'] < -sell_gap_pct
cond_sell_profit = df['intraday_ret'] > sell_profit_pct
cond_sell_loss = df['intraday_ret'] < -sell_loss_pct
df['sell'] = (cond_sell_gap | cond_sell_profit | cond_sell_loss).fillna(False)

# ── 买卖互斥：同Bar有买入信号则忽略卖出 ──
df.loc[df['buy'], 'sell'] = False

# ── NaN 安全处理 ──
df['buy'] = df['buy'].fillna(False).astype(bool)
df['sell'] = df['sell'].fillna(False).astype(bool)

buy_marks = [df['low'].iloc[i] * 0.995 if bool(df['buy'].iloc[i]) else None for i in range(len(df))]
sell_marks = [df['high'].iloc[i] * 1.005 if bool(df['sell'].iloc[i]) else None for i in range(len(df))]

output = {
    'name': my_indicator_name,
    'plots': [
        {'name': '日内涨幅%', 'data': df['intraday_ret'].tolist(), 'color': '#faad14', 'overlay': False},
    ],
    'signals': [
        {'type': 'buy', 'text': 'B', 'data': buy_marks, 'color': '#00E676'},
        {'type': 'sell', 'text': 'S', 'data': sell_marks, 'color': '#FF5252'},
    ],
}
