my_indicator_name = "量价共振突破"
my_indicator_description = """# IndicatorStrategy: 量价共振突破
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
# @param max_drawdown_pct float 5.0 最大回撤止损百分比"""

# ── 参数 ──
breakout_period = params.get('breakout_period', 20)
vol_ma_period = params.get('vol_ma_period', 20)
vol_ratio = params.get('vol_ratio', 2.0)
min_change_pct = params.get('min_change_pct', 3.0)
max_change_pct = params.get('max_change_pct', 8.0)
trend_ema_period = params.get('trend_ema_period', 30)
use_trend_filter = params.get('use_trend_filter', True)
max_drawdown_pct = params.get('max_drawdown_pct', 5.0)

# ── Donchian 通道（前一根 K 线的 N 日极值，避免前瞻偏差）──
df['dc_upper'] = df['high'].rolling(window=breakout_period).max().shift(1)

# ── 成交量均线 ──
df['vol_ma'] = df['volume'].rolling(window=vol_ma_period).mean()
vol_ma_safe = df['vol_ma'].replace(0, float('nan'))
df['vol_ratio_val'] = df['volume'] / vol_ma_safe

# ── 近期涨幅（5 日）──
df['period_ret'] = df['close'].pct_change(periods=5) * 100

# ── 趋势 EMA ──
df['ema_trend'] = df['close'].ewm(span=trend_ema_period, adjust=False).mean()

# ── 买入条件 ──
cond_breakout = df['close'] > df['dc_upper']
cond_volume = df['vol_ratio_val'] > vol_ratio
cond_min_ret = df['period_ret'] >= min_change_pct
cond_max_ret = df['period_ret'] <= max_change_pct
cond_trend = df['close'] > df['ema_trend']

if use_trend_filter:
    df['buy'] = (cond_breakout & cond_volume & cond_min_ret & cond_max_ret & cond_trend).fillna(False)
else:
    df['buy'] = (cond_breakout & cond_volume & cond_min_ret & cond_max_ret).fillna(False)

# ── 卖出条件 ──
df['highest_since_entry'] = df['close'].cummax()
df['drawdown'] = (df['close'] - df['highest_since_entry']) / df['highest_since_entry'] * 100
cond_sell_trend = df['close'] < df['ema_trend']
cond_sell_dd = df['drawdown'] < -max_drawdown_pct
df['sell'] = (cond_sell_trend | cond_sell_dd).fillna(False)

# ── NaN 安全处理 ──
df['buy'] = df['buy'].fillna(False).astype(bool)
df['sell'] = df['sell'].fillna(False).astype(bool)

buy_marks = [df['low'].iloc[i] * 0.995 if bool(df['buy'].iloc[i]) else None for i in range(len(df))]
sell_marks = [df['high'].iloc[i] * 1.005 if bool(df['sell'].iloc[i]) else None for i in range(len(df))]

output = {
    'name': my_indicator_name,
    'plots': [
        {'name': 'DC上轨', 'data': df['dc_upper'].tolist(), 'color': '#90CAF9', 'overlay': True},
        {'name': '趋势EMA', 'data': df['ema_trend'].tolist(), 'color': '#FFD54F', 'overlay': True},
    ],
    'signals': [
        {'type': 'buy', 'text': 'B', 'data': buy_marks, 'color': '#00E676'},
        {'type': 'sell', 'text': 'S', 'data': sell_marks, 'color': '#FF5252'},
    ],
}
