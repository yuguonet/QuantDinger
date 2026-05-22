my_indicator_name = "连板猎手v3.1"
my_indicator_description = """# IndicatorStrategy: 连板猎手v3.1
# @strategy stopLossPct 0.08
# @strategy takeProfitPct 0.15
# @strategy trailingEnabled true
# @strategy trailingStopPct 0.05
# @strategy tradeDirection long
# @param min_streak int 2 最小连板数(2=只做2板+)
# @param limit_threshold float 9.5 涨停阈值%(主板9.5/创科19.0)
# @param max_gap_pct float 8.0 最大高开幅度%
# @param max_seal_pct float 0.5 封板强度上限%(close接近high)
# @param max_rsi float 90.0 RSI上限(排除极端超买)
# @param trailing_stop_pct float 5.0 追踪止损%(从最高点回撤)
# @param take_profit_pct float 15.0 止盈%
# @param peak_rsi_threshold float 80.0 峰值RSI阈值
# @param peak_upper_shadow float 40.0 峰值上影线%阈值
# @param position_pct int 100 仓位比例%"""

import pandas as pd
import numpy as np

# ── 参数 ──
min_streak = int(params.get('min_streak', 2))
limit_threshold = float(params.get('limit_threshold', 9.5))
max_gap_pct = float(params.get('max_gap_pct', 8.0))
max_seal_pct = float(params.get('max_seal_pct', 0.5))
max_rsi = float(params.get('max_rsi', 90.0))
trailing_stop_pct = float(params.get('trailing_stop_pct', 5.0))
take_profit_pct = float(params.get('take_profit_pct', 15.0))
peak_rsi_threshold = float(params.get('peak_rsi_threshold', 80.0))
peak_upper_shadow = float(params.get('peak_upper_shadow', 40.0))
position_pct = int(params.get('position_pct', 100))

# ── 涨停检测 ──
_prev_close = df['close'].shift(1).replace(0, np.nan)
df['change_pct'] = (df['close'] / _prev_close - 1) * 100
df['is_limit_up'] = df['change_pct'] >= limit_threshold

# ── 连板计数 (连续涨停天数) ──
_groups = (~df['is_limit_up']).cumsum()
df['streak_count'] = df.groupby(_groups)['is_limit_up'].cumsum()

# ── 连板段第一天: streak_count刚达到min_streak的那天 ──
df['streak_start'] = (df['streak_count'] >= min_streak) & (df['streak_count'].shift(1).fillna(0) < min_streak)

# ── 高开幅度: (open - prev_close) / prev_close * 100 ──
df['gap_pct'] = (df['open'] / _prev_close - 1) * 100

# ── 封板强度: (close / high - 1) * 100, 值越接近0封得越紧 ──
df['seal_pct'] = (df['close'] / df['high'] - 1) * 100

# ── RSI(14) ──
_delta = df['close'].diff()
_gain = _delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
_loss = (-_delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
_rs = _gain / _loss.replace(0, np.nan)
df['rsi_14'] = 100 - (100 / (1 + _rs))

# ── KDJ(9,3,3) ──
_low_9 = df['low'].rolling(window=9, min_periods=1).min()
_high_9 = df['high'].rolling(window=9, min_periods=1).max()
_rsv = (df['close'] - _low_9) / (_high_9 - _low_9).replace(0, np.nan) * 100
df['kdj_k'] = _rsv.ewm(alpha=1/3, adjust=False).mean()
df['kdj_d'] = df['kdj_k'].ewm(alpha=1/3, adjust=False).mean()
df['kdj_j'] = 3 * df['kdj_k'] - 2 * df['kdj_d']

# ── 上影线% ──
_bar_range = (df['high'] - df['low']).replace(0, np.nan)
df['upper_shadow_pct'] = (df['high'] - df[['close', 'open']].max(axis=1)) / _bar_range * 100

# ── 买入信号 ──
# 1. 连板段刚启动 (streak_count == min_streak)
# 2. 高开幅度在范围内
# 3. 封板强度 ≤ max_seal_pct (涨停封死)
# 4. RSI ≤ max_rsi
# 5. 有成交量
cond_streak = df['streak_start']
cond_gap = df['gap_pct'].between(-5, max_gap_pct)
cond_seal = df['seal_pct'] <= max_seal_pct
cond_rsi = df['rsi_14'] <= max_rsi
cond_vol = df['volume'] > 0

df['buy'] = (cond_streak & cond_gap & cond_seal & cond_rsi & cond_vol).fillna(False)

# ── 卖出信号 (任一满足) ──
# 1. RSI超买 + 上影线大 (见顶信号)
cond_peak = (df['rsi_14'] >= peak_rsi_threshold) & (df['upper_shadow_pct'] >= peak_upper_shadow)
# 2. KDJ死叉 + RSI>70
cond_kdj_cross = (df['kdj_k'] < df['kdj_d']) & (df['kdj_k'].shift(1) >= df['kdj_d'].shift(1)) & (df['rsi_14'] > 70)
# 3. 开板回调: 非涨停日且跌幅>3%
cond_break = (df['change_pct'] < -3) & (~df['is_limit_up'])

df['sell'] = (cond_peak | cond_kdj_cross | cond_break).fillna(False)

# ── 买卖互斥: 同Bar有买入信号则忽略卖出 ──
df.loc[df['buy'], 'sell'] = False

# ── NaN安全处理 ──
df['buy'] = df['buy'].fillna(False).astype(bool)
df['sell'] = df['sell'].fillna(False).astype(bool)

buy_marks = [df['low'].iloc[i] * 0.995 if bool(df['buy'].iloc[i]) else None for i in range(len(df))]
sell_marks = [df['high'].iloc[i] * 1.005 if bool(df['sell'].iloc[i]) else None for i in range(len(df))]

output = {
    'name': my_indicator_name,
    'plots': [
        {'name': 'RSI(14)', 'data': df['rsi_14'].tolist(), 'color': '#FF9800', 'overlay': False},
        {'name': '连板数', 'data': df['streak_count'].tolist(), 'color': '#2196F3', 'overlay': False},
    ],
    'signals': [
        {'type': 'buy', 'text': 'B', 'data': buy_marks, 'color': '#00E676'},
        {'type': 'sell', 'text': 'S', 'data': sell_marks, 'color': '#FF5252'},
    ],
}
