# IndicatorStrategy: 龙回头
# @strategy stopLossPct 0.05
# @strategy takeProfitPct 0.12
# @strategy trailingEnabled true
# @strategy trailingStopPct 0.05
# @strategy tradeDirection long
# @param lookback_period int 20 前期涨幅观察窗口
# @param pullback_min_pct float 10.0 最小回调幅度%
# @param pullback_max_pct float 25.0 最大回调幅度%
# @param vol_ma_period int 20 成交量均线周期
# @param vol_shrink_ratio float 0.5 缩量比例
# @param rsi_period int 14 RSI周期
# @param rsi_bounce int 35 RSI从超卖回升阈值
# @param ma_support_period int 20 支撑均线周期
# @param ma_near_pct float 2.0 价格距均线距离%
# @param use_ma_support bool true 是否叠加均线支撑
# @param position_pct int 70 仓位比例%

import pandas as pd
import numpy as np

# ── 参数 ──
lookback_period = params.get('lookback_period', 20)
pullback_min_pct = params.get('pullback_min_pct', 10.0)
pullback_max_pct = params.get('pullback_max_pct', 25.0)
vol_ma_period = params.get('vol_ma_period', 20)
vol_shrink_ratio = params.get('vol_shrink_ratio', 0.5)
rsi_period = params.get('rsi_period', 14)
rsi_bounce = params.get('rsi_bounce', 35)
ma_support_period = params.get('ma_support_period', 20)
ma_near_pct = params.get('ma_near_pct', 2.0)
use_ma_support = params.get('use_ma_support', True)
position_pct = params.get('position_pct', 70)

# ── 前期高点（N 日内最高价）──
df['recent_high'] = df['high'].rolling(window=lookback_period).max()
# 回调幅度（从高点下跌百分比）
df['pullback_pct'] = (df['recent_high'] - df['close']) / df['recent_high'] * 100

# ── 成交量均线 ──
df['vol_ma'] = df['volume'].rolling(window=vol_ma_period).mean()
df['vol_ratio_val'] = df['volume'] / df['vol_ma']

# ── RSI ──
delta = df['close'].diff()
gain = delta.where(delta > 0, 0).rolling(window=rsi_period).mean()
loss = (-delta.where(delta < 0, 0)).rolling(window=rsi_period).mean()
rs = gain / loss
df['rsi'] = 100 - (100 / (1 + rs))
df['rsi_prev'] = df['rsi'].shift(1)

# ── 支撑均线 ──
df['ema_support'] = df['close'].ewm(span=ma_support_period, adjust=False).mean()
df['ma_deviation'] = (df['close'] / df['ema_support'] - 1) * 100

# ── 涨停基因检测（近 N 日内涨停次数）──
df['ret_1d'] = df['close'].pct_change() * 100
df['is_limitup'] = df['ret_1d'] >= 9.5
df['limitup_count'] = df['is_limitup'].rolling(window=lookback_period * 3).sum()

# ── 买入条件 ──
# 1. 前期大涨后回调到买入区间（pullback_min% ~ pullback_max%）
cond_pullback_zone = (df['pullback_pct'] >= pullback_min_pct) & (df['pullback_pct'] <= pullback_max_pct)
# 2. 回调缩量（成交量 < 均量 × vol_shrink_ratio）
cond_vol_shrink = df['vol_ratio_val'] < vol_shrink_ratio
# 3. RSI 从超卖区回升（前一日 < 阈值，当日 >= 阈值）
cond_rsi_bounce = (df['rsi_prev'] < rsi_bounce) & (df['rsi'] >= rsi_bounce)
# 4. 均线支撑（可选）：价格在支撑均线附近
cond_ma_support = df['ma_deviation'].abs() <= ma_near_pct if use_ma_support else True
# 5. 有龙头基因（前期有涨停）
cond_dragon = df['limitup_count'] >= 1

df['buy'] = (cond_pullback_zone & cond_vol_shrink & cond_rsi_bounce & cond_ma_support & cond_dragon).fillna(False)

# ── 卖出条件 ──
# RSI 超买或价格创新高后回撤
cond_sell_rsi = df['rsi'] > 70
df['highest_since_buy'] = df['close'].cummax()
df['drawdown_from_high'] = (df['close'] - df['highest_since_buy']) / df['highest_since_buy'] * 100
cond_sell_dd = df['drawdown_from_high'] < -5.0
cond_sell_profit = df['pullback_pct'] < 0  # 价格回到前高附近
df['sell'] = (cond_sell_rsi | cond_sell_dd).fillna(False)

# ── NaN 安全处理 ──
df['buy'] = df['buy'].fillna(False).astype(bool)
df['sell'] = df['sell'].fillna(False).astype(bool)
