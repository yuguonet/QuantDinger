"""
策略参数空间定义
每个策略模板定义：指标组合 + 可调参数范围 + 信号逻辑

每个模板提供：
  - build_strategy(params) → 标准 IndicatorStrategy 代码
  - build_config(params)   → JSON 配置（向后兼容，逐步废弃）
"""
from typing import Dict, Any, List

# ============================================================
# 参数类型：int / float / choice
# ============================================================

def _p_int(low: int, high: int, step: int = 1) -> dict:
    return {"type": "int", "low": low, "high": high, "step": step}

def _p_float(low: float, high: float, step: float = 0.001) -> dict:
    return {"type": "float", "low": low, "high": high, "step": step}

def _p_choice(choices: list) -> dict:
    return {"type": "choice", "choices": choices}


# ============================================================
# IndicatorStrategy 代码生成（新标准）
# ============================================================

def _build_ma_crossover_strategy(p: dict) -> str:
    fast_type = p.get("fast_type", "ema")
    slow_type = p.get("slow_type", "sma")
    fast_period = int(p["fast_period"])
    slow_period = int(p["slow_period"])
    use_rsi = p.get("use_rsi_filter", False)
    rsi_period = int(p.get("rsi_period", 14))
    rsi_lower = int(p.get("rsi_lower", 30))

    params_decl = [
        f"@param fast_period int {fast_period} 快线周期",
        f"@param slow_period int {slow_period} 慢线周期",
        f"@param fast_type str {fast_type} 快线类型 (sma/ema)",
        f"@param slow_type str {slow_type} 慢线类型 (sma/ema)",
    ]
    if use_rsi:
        params_decl.append(f"@param rsi_period int {rsi_period} RSI 周期")
        params_decl.append(f"@param rsi_lower int {rsi_lower} RSI 下限")

    ind_code = f"""fast_len = int(params.get('fast_period', {fast_period}))
slow_len = int(params.get('slow_period', {slow_period}))
fast_type = str(params.get('fast_type', '{fast_type}'))
slow_type = str(params.get('slow_type', '{slow_type}'))

if fast_type == 'ema':
    ma_fast = df['close'].ewm(span=fast_len, adjust=False).mean()
else:
    ma_fast = df['close'].rolling(window=fast_len).mean()

if slow_type == 'ema':
    ma_slow = df['close'].ewm(span=slow_len, adjust=False).mean()
else:
    ma_slow = df['close'].rolling(window=slow_len).mean()"""

    buy_expr = "(ma_fast > ma_slow) & (ma_fast.shift(1) <= ma_slow.shift(1))"
    sell_expr = "(ma_fast < ma_slow) & (ma_fast.shift(1) >= ma_slow.shift(1))"

    if use_rsi:
        ind_code += f"""
rsi_period = int(params.get('rsi_period', {rsi_period}))
rsi_lower = int(params.get('rsi_lower', {rsi_lower}))
delta = df['close'].diff()
gain = delta.clip(lower=0).ewm(alpha=1/rsi_period, adjust=False).mean()
loss = (-delta.clip(upper=0)).ewm(alpha=1/rsi_period, adjust=False).mean()
rs = gain / loss.replace(0, np.nan)
rsi = 100 - (100 / (1 + rs))"""
        buy_expr += " & (rsi > rsi_lower)"

    plots = [
        {"name": f"MA Fast ({fast_type} {fast_period})", "data": "ma_fast.fillna(0).tolist()", "color": "#1890ff"},
        {"name": f"MA Slow ({slow_type} {slow_period})", "data": "ma_slow.fillna(0).tolist()", "color": "#faad14"},
    ]

    from optimizer.indicator_strategy_builder import render_indicator_strategy
    return render_indicator_strategy(
        name=f"MA_Cross_{fast_type}{fast_period}_{slow_type}{slow_period}",
        description=f"均线交叉策略: {fast_type.upper()}{fast_period} 上穿 {slow_type.upper()}{slow_period} 做多，下穿做空。"
                    + (f" RSI({rsi_period})>{rsi_lower} 过滤。" if use_rsi else ""),
        params_decl=params_decl,
        strategy_defaults={"stopLossPct": 0.05, "takeProfitPct": 0.10, "tradeDirection": "long"},
        indicator_code=ind_code,
        buy_expr=buy_expr,
        sell_expr=sell_expr,
        plots=plots,
    )


def _build_rsi_oversold_strategy(p: dict) -> str:
    rsi_period = int(p["rsi_period"])
    oversold = int(p["oversold"])
    overbought = int(p["overbought"])
    use_confirm = p.get("use_confirm", False)

    params_decl = [
        f"@param rsi_period int {rsi_period} RSI 周期",
        f"@param oversold int {oversold} 超卖线",
        f"@param overbought int {overbought} 超买线",
    ]

    ind_code = f"""rsi_period = int(params.get('rsi_period', {rsi_period}))
oversold = int(params.get('oversold', {oversold}))
overbought = int(params.get('overbought', {overbought}))

delta = df['close'].diff()
gain = delta.clip(lower=0).ewm(alpha=1/rsi_period, adjust=False).mean()
loss = (-delta.clip(upper=0)).ewm(alpha=1/rsi_period, adjust=False).mean()
rs = gain / loss.replace(0, np.nan)
rsi = 100 - (100 / (1 + rs))"""

    buy_expr = "(rsi > oversold) & (rsi.shift(1) <= oversold)"
    sell_expr = f"(rsi > {overbought}) & (rsi.shift(1) <= {overbought})"

    if use_confirm:
        ind_code += """
ema_confirm = df['close'].ewm(span=20, adjust=False).mean()"""
        buy_expr += " & (df['close'] > ema_confirm)"

    plots = [
        {"name": f"RSI ({rsi_period})", "data": "rsi.fillna(50).tolist()", "color": "#722ed1", "overlay": False},
    ]

    from optimizer.indicator_strategy_builder import render_indicator_strategy
    return render_indicator_strategy(
        name=f"RSI_OS_{rsi_period}_{oversold}_{overbought}",
        description=f"RSI 超卖反弹: RSI({rsi_period}) 从 {oversold} 下方回升时买入，超买 {overbought} 时卖出。"
                    + (" EMA20 趋势确认。" if use_confirm else ""),
        params_decl=params_decl,
        strategy_defaults={"stopLossPct": 0.05, "takeProfitPct": 0.10, "tradeDirection": "long"},
        indicator_code=ind_code,
        buy_expr=buy_expr,
        sell_expr=sell_expr,
        plots=plots,
    )


def _build_bollinger_breakout_strategy(p: dict) -> str:
    bb_period = int(p["bb_period"])
    bb_std = float(p["bb_std"])

    params_decl = [
        f"@param bb_period int {bb_period} 布林带周期",
        f"@param bb_std float {bb_std} 标准差倍数",
    ]

    ind_code = f"""bb_period = int(params.get('bb_period', {bb_period}))
bb_std = float(params.get('bb_std', {bb_std}))

sma = df['close'].rolling(window=bb_period).mean()
std = df['close'].rolling(window=bb_period).std()
bb_upper = sma + (bb_std * std)
bb_lower = sma - (bb_std * std)
bb_mid = sma"""

    buy_expr = "(df['close'] > bb_upper) & (df['close'].shift(1) <= bb_upper.shift(1))"
    sell_expr = "(df['close'] < bb_lower) & (df['close'].shift(1) >= bb_lower.shift(1))"

    plots = [
        {"name": "BB Upper", "data": "bb_upper.fillna(0).tolist()", "color": "#0088FE"},
        {"name": "BB Lower", "data": "bb_lower.fillna(0).tolist()", "color": "#0088FE"},
        {"name": "BB Mid", "data": "bb_mid.fillna(0).tolist()", "color": "#8884d8"},
    ]

    from optimizer.indicator_strategy_builder import render_indicator_strategy
    return render_indicator_strategy(
        name=f"BB_Squeeze_{bb_period}_{bb_std}",
        description=f"布林带突破: 价格突破上轨({bb_period},{bb_std})做多，跌破下轨做空。",
        params_decl=params_decl,
        strategy_defaults={"stopLossPct": 0.05, "takeProfitPct": 0.10, "tradeDirection": "both"},
        indicator_code=ind_code,
        buy_expr=buy_expr,
        sell_expr=sell_expr,
        plots=plots,
    )


def _build_macd_crossover_strategy(p: dict) -> str:
    fast_period = int(p["fast_period"])
    slow_period = int(p["slow_period"])
    signal_period = int(p["signal_period"])

    params_decl = [
        f"@param fast_period int {fast_period} 快线周期",
        f"@param slow_period int {slow_period} 慢线周期",
        f"@param signal_period int {signal_period} 信号线周期",
    ]

    prefix = f"macd_{fast_period}_{slow_period}_{signal_period}"
    ind_code = f"""fast_period = int(params.get('fast_period', {fast_period}))
slow_period = int(params.get('slow_period', {slow_period}))
signal_period = int(params.get('signal_period', {signal_period}))

exp1 = df['close'].ewm(span=fast_period, adjust=False).mean()
exp2 = df['close'].ewm(span=slow_period, adjust=False).mean()
macd_line = exp1 - exp2
macd_signal = macd_line.ewm(span=signal_period, adjust=False).mean()
macd_hist = macd_line - macd_signal"""

    buy_expr = "(macd_line > macd_signal) & (macd_line.shift(1) <= macd_signal.shift(1))"
    sell_expr = "(macd_line < macd_signal) & (macd_line.shift(1) >= macd_signal.shift(1))"

    plots = [
        {"name": "MACD", "data": "macd_line.fillna(0).tolist()", "color": "#0088FE", "overlay": False},
        {"name": "Signal", "data": "macd_signal.fillna(0).tolist()", "color": "#FF8042", "overlay": False},
    ]

    from optimizer.indicator_strategy_builder import render_indicator_strategy
    return render_indicator_strategy(
        name=f"MACD_{fast_period}_{slow_period}_{signal_period}",
        description=f"MACD 金叉死叉: MACD({fast_period},{slow_period},{signal_period}) 金叉做多，死叉做空。",
        params_decl=params_decl,
        strategy_defaults={"stopLossPct": 0.05, "takeProfitPct": 0.10, "tradeDirection": "both"},
        indicator_code=ind_code,
        buy_expr=buy_expr,
        sell_expr=sell_expr,
        plots=plots,
    )


def _build_supertrend_strategy(p: dict) -> str:
    st_period = int(p["st_period"])
    st_multiplier = float(p["st_multiplier"])
    use_ema = p.get("use_ema_filter", False)
    ema_period = int(p.get("ema_filter_period", 100))

    params_decl = [
        f"@param st_period int {st_period} SuperTrend 周期",
        f"@param st_multiplier float {st_multiplier} ATR 倍数",
    ]
    if use_ema:
        params_decl.append(f"@param ema_filter_period int {ema_period} EMA 趋势过滤周期")

    ind_code = f"""st_period = int(params.get('st_period', {st_period}))
st_multiplier = float(params.get('st_multiplier', {st_multiplier}))

df['hl2'] = (df['high'] + df['low']) / 2
df['tr'] = np.maximum(df['high'] - df['low'], np.maximum(abs(df['high'] - df['close'].shift(1)), abs(df['low'] - df['close'].shift(1))))
df['atr'] = df['tr'].ewm(alpha=1/st_period, adjust=False).mean()
df['basic_upper'] = df['hl2'] + (st_multiplier * df['atr'])
df['basic_lower'] = df['hl2'] - (st_multiplier * df['atr'])
final_upper = [0.0] * len(df)
final_lower = [0.0] * len(df)
trend = [1] * len(df)
close_arr = df['close'].values
basic_upper = np.nan_to_num(df['basic_upper'].values)
basic_lower = np.nan_to_num(df['basic_lower'].values)
for i in range(1, len(df)):
    if basic_upper[i] < final_upper[i-1] or close_arr[i-1] > final_upper[i-1]:
        final_upper[i] = basic_upper[i]
    else:
        final_upper[i] = final_upper[i-1]
    if basic_lower[i] > final_lower[i-1] or close_arr[i-1] < final_lower[i-1]:
        final_lower[i] = basic_lower[i]
    else:
        final_lower[i] = final_lower[i-1]
    prev_trend = trend[i-1]
    if prev_trend == -1 and close_arr[i] > final_upper[i-1]:
        trend[i] = 1
    elif prev_trend == 1 and close_arr[i] < final_lower[i-1]:
        trend[i] = -1
    else:
        trend[i] = prev_trend
df['st_trend'] = trend"""

    buy_expr = "(df['st_trend'] == 1) & (df['st_trend'].shift(1) == -1)"
    sell_expr = "(df['st_trend'] == -1) & (df['st_trend'].shift(1) == 1)"

    if use_ema:
        ind_code += f"""
ema_filter = df['close'].ewm(span={ema_period}, adjust=False).mean()"""
        buy_expr += " & (df['close'] > ema_filter)"
        sell_expr += " | ((df['close'] < ema_filter) & (df['close'].shift(1) >= ema_filter.shift(1)))"

    plots = [
        {"name": "SuperTrend Up", "data": "pd.Series(final_lower).tolist()", "color": "#00FF00"},
        {"name": "SuperTrend Down", "data": "pd.Series(final_upper).tolist()", "color": "#FF0000"},
    ]

    from optimizer.indicator_strategy_builder import render_indicator_strategy
    return render_indicator_strategy(
        name=f"ST_{st_period}_{st_multiplier}",
        description=f"SuperTrend 趋势跟踪: ATR({st_period}) x {st_multiplier}，方向翻转时交易。"
                    + (f" EMA{ema_period} 趋势过滤。" if use_ema else ""),
        params_decl=params_decl,
        strategy_defaults={"stopLossPct": 0.05, "takeProfitPct": 0.15, "tradeDirection": "both"},
        indicator_code=ind_code,
        buy_expr=buy_expr,
        sell_expr=sell_expr,
        plots=plots,
    )


def _build_kdj_crossover_strategy(p: dict) -> str:
    kdj_period = int(p["kdj_period"])
    kdj_signal = int(p["kdj_signal"])

    params_decl = [
        f"@param kdj_period int {kdj_period} KDJ 周期",
        f"@param kdj_signal int {kdj_signal} KDJ 信号期",
    ]

    prefix = f"kdj_{kdj_period}_{kdj_signal}"
    ind_code = f"""kdj_period = int(params.get('kdj_period', {kdj_period}))
kdj_signal = int(params.get('kdj_signal', {kdj_signal}))

low_min = df['low'].rolling(window=kdj_period).min()
high_max = df['high'].rolling(window=kdj_period).max()
rsv = (df['close'] - low_min) / (high_max - low_min) * 100
k_val = rsv.ewm(alpha=1/kdj_signal, adjust=False).mean()
d_val = k_val.ewm(alpha=1/kdj_signal, adjust=False).mean()
j_val = 3 * k_val - 2 * d_val"""

    buy_expr = "(k_val > d_val) & (k_val.shift(1) <= d_val.shift(1))"
    sell_expr = "(k_val < d_val) & (k_val.shift(1) >= d_val.shift(1))"

    plots = [
        {"name": "K", "data": "k_val.fillna(50).tolist()", "color": "#8884d8", "overlay": False},
        {"name": "D", "data": "d_val.fillna(50).tolist()", "color": "#82ca9d", "overlay": False},
        {"name": "J", "data": "j_val.fillna(50).tolist()", "color": "#ffc658", "overlay": False},
    ]

    from optimizer.indicator_strategy_builder import render_indicator_strategy
    return render_indicator_strategy(
        name=f"KDJ_{kdj_period}_{kdj_signal}",
        description=f"KDJ 金叉死叉: KDJ({kdj_period},{kdj_signal}) K 上穿 D 做多，下穿做空。",
        params_decl=params_decl,
        strategy_defaults={"stopLossPct": 0.05, "takeProfitPct": 0.10, "tradeDirection": "both"},
        indicator_code=ind_code,
        buy_expr=buy_expr,
        sell_expr=sell_expr,
        plots=plots,
    )


def _build_dual_rsi_strategy(p: dict) -> str:
    fast_rsi = int(p["fast_rsi"])
    slow_rsi = int(p["slow_rsi"])
    entry_level = int(p["entry_level"])
    exit_level = int(p["exit_level"])
    trend_level = int(p["trend_level"])

    params_decl = [
        f"@param fast_rsi int {fast_rsi} 快速 RSI 周期",
        f"@param slow_rsi int {slow_rsi} 慢速 RSI 周期",
        f"@param entry_level int {entry_level} 入场 RSI 水平",
        f"@param exit_level int {exit_level} 出场 RSI 水平",
        f"@param trend_level int {trend_level} 趋势 RSI 水平",
    ]

    ind_code = f"""fast_rsi_period = int(params.get('fast_rsi', {fast_rsi}))
slow_rsi_period = int(params.get('slow_rsi', {slow_rsi}))
entry_level = int(params.get('entry_level', {entry_level}))
exit_level = int(params.get('exit_level', {exit_level}))
trend_level = int(params.get('trend_level', {trend_level}))

# 快速 RSI
delta = df['close'].diff()
gain = delta.clip(lower=0).ewm(alpha=1/fast_rsi_period, adjust=False).mean()
loss = (-delta.clip(upper=0)).ewm(alpha=1/fast_rsi_period, adjust=False).mean()
rs = gain / loss.replace(0, np.nan)
rsi_fast = 100 - (100 / (1 + rs))

# 慢速 RSI
gain2 = delta.clip(lower=0).ewm(alpha=1/slow_rsi_period, adjust=False).mean()
loss2 = (-delta.clip(upper=0)).ewm(alpha=1/slow_rsi_period, adjust=False).mean()
rs2 = gain2 / loss2.replace(0, np.nan)
rsi_slow = 100 - (100 / (1 + rs2))"""

    buy_expr = f"(rsi_fast > entry_level) & (rsi_fast.shift(1) <= entry_level) & (rsi_slow > trend_level)"
    sell_expr = f"(rsi_fast > exit_level) & (rsi_fast.shift(1) <= exit_level)"

    plots = [
        {"name": f"RSI Fast ({fast_rsi})", "data": "rsi_fast.fillna(50).tolist()", "color": "#1890ff", "overlay": False},
        {"name": f"RSI Slow ({slow_rsi})", "data": "rsi_slow.fillna(50).tolist()", "color": "#faad14", "overlay": False},
    ]

    from optimizer.indicator_strategy_builder import render_indicator_strategy
    return render_indicator_strategy(
        name=f"DualRSI_{fast_rsi}_{slow_rsi}",
        description=f"双 RSI 动量: 快速 RSI({fast_rsi}) 上穿 {entry_level} 且慢速 RSI({slow_rsi})>{trend_level} 时买入。"
                    f"快速 RSI 上穿 {exit_level} 时卖出。",
        params_decl=params_decl,
        strategy_defaults={"stopLossPct": 0.05, "takeProfitPct": 0.10, "tradeDirection": "long"},
        indicator_code=ind_code,
        buy_expr=buy_expr,
        sell_expr=sell_expr,
        plots=plots,
    )


# ============================================================
# 向后兼容：JSON config → StrategyCompiler 格式（逐步废弃）
# ============================================================

def _build_ma_crossover_config(p: dict) -> dict:
    fast_type = p.get("fast_type", "ema")
    slow_type = p.get("slow_type", "sma")
    use_rsi = p.get("use_rsi_filter", False)
    entry_rules = [
        {"indicator": "ma", "params": {"period": p["fast_period"], "ma_type": fast_type}, "operator": "cross_up"},
        {"indicator": "ma", "params": {"period": p["slow_period"], "ma_type": slow_type}, "operator": "price_above"},
    ]
    if use_rsi:
        entry_rules.append({"indicator": "rsi", "params": {"period": p.get("rsi_period", 14), "threshold": p.get("rsi_lower", 30)}, "operator": ">"})
    return {"name": f"MA_Cross_{fast_type}{p['fast_period']}_{slow_type}{p['slow_period']}", "entry_rules": entry_rules,
            "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
            "pyramiding_rules": {"enabled": False},
            "risk_management": {"stop_loss": {"enabled": False, "value": 0}, "trailing_stop": {"enabled": False}}}

def _build_rsi_oversold_config(p: dict) -> dict:
    use_confirm = p.get("use_confirm", False)
    entry_rules = [{"indicator": "rsi", "params": {"period": p["rsi_period"], "threshold": p["oversold"]}, "operator": "cross_up"}]
    if use_confirm:
        entry_rules.append({"indicator": "ma", "params": {"period": 20, "ma_type": "ema"}, "operator": "price_above"})
    return {"name": f"RSI_OS_{p['rsi_period']}_{p['oversold']}_{p['overbought']}", "entry_rules": entry_rules,
            "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
            "pyramiding_rules": {"enabled": False},
            "risk_management": {"stop_loss": {"enabled": False, "value": 0}, "trailing_stop": {"enabled": False}}}

def _build_bollinger_breakout_config(p: dict) -> dict:
    return {"name": f"BB_Squeeze_{p['bb_period']}_{p['bb_std']}",
            "entry_rules": [{"indicator": "bollinger", "params": {"period": p["bb_period"], "std_dev": p["bb_std"]}, "operator": "price_above_upper"}],
            "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
            "pyramiding_rules": {"enabled": False},
            "risk_management": {"stop_loss": {"enabled": False, "value": 0}, "trailing_stop": {"enabled": False}}}

def _build_macd_crossover_config(p: dict) -> dict:
    return {"name": f"MACD_{p['fast_period']}_{p['slow_period']}_{p['signal_period']}",
            "entry_rules": [{"indicator": "macd", "params": {"fast_period": p["fast_period"], "slow_period": p["slow_period"], "signal_period": p["signal_period"]}, "operator": "cross_up"}],
            "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
            "pyramiding_rules": {"enabled": False},
            "risk_management": {"stop_loss": {"enabled": False, "value": 0}, "trailing_stop": {"enabled": False}}}

def _build_supertrend_config(p: dict) -> dict:
    entry_rules = [{"indicator": "supertrend", "params": {"period": p["st_period"], "multiplier": p["st_multiplier"]}, "signal": "trend_bullish"}]
    if p.get("use_ema_filter"):
        entry_rules.append({"indicator": "ema", "params": {"period": p.get("ema_filter_period", 100)}, "operator": "price_above"})
    return {"name": f"ST_{p['st_period']}_{p['st_multiplier']}", "entry_rules": entry_rules,
            "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
            "pyramiding_rules": {"enabled": False},
            "risk_management": {"stop_loss": {"enabled": False, "value": 0}, "trailing_stop": {"enabled": False}}}

def _build_kdj_crossover_config(p: dict) -> dict:
    return {"name": f"KDJ_{p['kdj_period']}_{p['kdj_signal']}",
            "entry_rules": [{"indicator": "kdj", "params": {"period": p["kdj_period"], "signal_period": p["kdj_signal"]}, "operator": "gold_cross"}],
            "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
            "pyramiding_rules": {"enabled": False},
            "risk_management": {"stop_loss": {"enabled": False, "value": 0}, "trailing_stop": {"enabled": False}}}

def _build_dual_rsi_config(p: dict) -> dict:
    return {"name": f"DualRSI_{p['fast_rsi']}_{p['slow_rsi']}",
            "entry_rules": [
                {"indicator": "rsi", "params": {"period": p["fast_rsi"], "threshold": p["entry_level"]}, "operator": "cross_up"},
                {"indicator": "rsi", "params": {"period": p["slow_rsi"], "threshold": p["trend_level"]}, "operator": ">"}],
            "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
            "pyramiding_rules": {"enabled": False},
            "risk_management": {"stop_loss": {"enabled": False, "value": 0}, "trailing_stop": {"enabled": False}}}


# ============================================================
# 策略模板定义
# ============================================================

STRATEGY_TEMPLATES: Dict[str, Dict[str, Any]] = {

    # ── 1. 均线交叉 ──
    "ma_crossover": {
        "name": "均线交叉策略",
        "description": "快线上穿慢线做多，下穿做空。可叠加 RSI 过滤。",
        "indicators": ["ma", "rsi"],
        "params": {
            "fast_period":    _p_int(5, 50, 1),
            "slow_period":    _p_int(20, 200, 1),
            "fast_type":      _p_choice(["sma", "ema"]),
            "slow_type":      _p_choice(["sma", "ema"]),
            "use_rsi_filter": _p_choice([True, False]),
            "rsi_period":     _p_int(7, 21, 1),
            "rsi_lower":      _p_int(20, 40, 1),
            "rsi_upper":      _p_int(60, 80, 1),
        },
        "constraints": [("fast_period", "<", "slow_period")],
        "build_config":   _build_ma_crossover_config,
        "build_strategy": _build_ma_crossover_strategy,
    },

    # ── 2. RSI 超卖反弹 ──
    "rsi_oversold": {
        "name": "RSI 超卖反弹",
        "description": "RSI 跌破超卖线后回升买入，超买时卖出。",
        "indicators": ["rsi"],
        "params": {
            "rsi_period":  _p_int(7, 21, 1),
            "oversold":    _p_int(20, 35, 1),
            "overbought":  _p_int(65, 80, 1),
            "use_confirm": _p_choice([True, False]),
        },
        "constraints": [("oversold", "<", "overbought")],
        "build_config":   _build_rsi_oversold_config,
        "build_strategy": _build_rsi_oversold_strategy,
    },

    # ── 3. 布林带突破 ──
    "bollinger_breakout": {
        "name": "布林带收缩突破",
        "description": "布林带收缩后突破上轨做多、下轨做空。",
        "indicators": ["bollinger"],
        "params": {
            "bb_period":    _p_int(10, 40, 1),
            "bb_std":       _p_float(1.0, 3.0, 0.1),
            "confirm_bars": _p_int(1, 5, 1),
        },
        "constraints": [],
        "build_config":   _build_bollinger_breakout_config,
        "build_strategy": _build_bollinger_breakout_strategy,
    },

    # ── 4. MACD 交叉 ──
    "macd_crossover": {
        "name": "MACD 金叉死叉",
        "description": "MACD 线上穿信号线做多，下穿做空。",
        "indicators": ["macd"],
        "params": {
            "fast_period":   _p_int(8, 20, 1),
            "slow_period":   _p_int(20, 40, 1),
            "signal_period": _p_int(5, 15, 1),
            "use_histogram": _p_choice([True, False]),
            "hist_threshold": _p_float(-0.5, 0.5, 0.01),
        },
        "constraints": [("fast_period", "<", "slow_period")],
        "build_config":   _build_macd_crossover_config,
        "build_strategy": _build_macd_crossover_strategy,
    },

    # ── 5. SuperTrend ──
    "supertrend": {
        "name": "SuperTrend 趋势跟踪",
        "description": "SuperTrend 方向翻转时交易。",
        "indicators": ["supertrend"],
        "params": {
            "st_period":       _p_int(7, 30, 1),
            "st_multiplier":   _p_float(1.5, 5.0, 0.1),
            "use_ema_filter":  _p_choice([True, False]),
            "ema_filter_period": _p_int(50, 200, 10),
        },
        "constraints": [],
        "build_config":   _build_supertrend_config,
        "build_strategy": _build_supertrend_strategy,
    },

    # ── 6. KDJ 金叉 ──
    "kdj_crossover": {
        "name": "KDJ 金叉死叉",
        "description": "K 线上穿 D 线做多，下穿做空。",
        "indicators": ["kdj"],
        "params": {
            "kdj_period":  _p_int(5, 21, 1),
            "kdj_signal":  _p_int(2, 5, 1),
            "use_j_filter": _p_choice([True, False]),
            "j_upper":     _p_int(80, 100, 5),
            "j_lower":     _p_int(-10, 20, 5),
        },
        "constraints": [],
        "build_config":   _build_kdj_crossover_config,
        "build_strategy": _build_kdj_crossover_strategy,
    },

    # ── 7. 双 RSI 动量 ──
    "dual_rsi": {
        "name": "双 RSI 动量策略",
        "description": "短周期 RSI 判断入场时机，长周期 RSI 判断趋势方向。",
        "indicators": ["rsi"],
        "params": {
            "fast_rsi":    _p_int(5, 14, 1),
            "slow_rsi":    _p_int(14, 30, 1),
            "entry_level": _p_int(25, 45, 1),
            "exit_level":  _p_int(55, 80, 1),
            "trend_level": _p_int(45, 60, 1),
        },
        "constraints": [
            ("fast_rsi", "<", "slow_rsi"),
            ("entry_level", "<", "exit_level"),
        ],
        "build_config":   _build_dual_rsi_config,
        "build_strategy": _build_dual_rsi_strategy,
    },
}


def get_template(key: str) -> dict:
    if key in STRATEGY_TEMPLATES:
        return STRATEGY_TEMPLATES[key]
    try:
        from optimizer.strategy_templates_ashare import ASHARE_STRATEGY_TEMPLATES
        if key in ASHARE_STRATEGY_TEMPLATES:
            return ASHARE_STRATEGY_TEMPLATES[key]
    except ImportError:
        pass
    try:
        from optimizer.strategy_templates_mine import MY_STRATEGY_TEMPLATES
        if key in MY_STRATEGY_TEMPLATES:
            return MY_STRATEGY_TEMPLATES[key]
    except ImportError:
        pass
    try:
        from optimizer.strategy_templates_llm import LLM_STRATEGY_TEMPLATES
        if key in LLM_STRATEGY_TEMPLATES:
            return LLM_STRATEGY_TEMPLATES[key]
    except ImportError:
        pass
    try:
        from optimizer.strategies_generated import GENERATED_TEMPLATES
        if key in GENERATED_TEMPLATES:
            return GENERATED_TEMPLATES[key]
    except ImportError:
        pass
    raise ValueError(f"Unknown strategy template: {key}. Available: {list(STRATEGY_TEMPLATES.keys())}")


def list_templates() -> List[str]:
    return list(STRATEGY_TEMPLATES.keys())
