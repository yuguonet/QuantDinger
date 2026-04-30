import json
from typing import Dict, Any, List

class StrategyCompiler:
    def compile(self, config: Dict[str, Any]) -> str:
        """
        Compiles the strategy configuration JSON into executable Python code.
        """
        
        # Extract configurations
        name = config.get('name', 'Generated Strategy')
        entry_rules = config.get('entry_rules', [])
        position_config = config.get('position_config', {})
        pyramiding_rules = config.get('pyramiding_rules', {})
        risk_management = config.get('risk_management', {})
        
        # 1. Imports and Setup
        code = self._get_header(name)
        
        # 2. Parameters (Variables)
        code += self._get_parameters(position_config, pyramiding_rules, risk_management)
        
        # 3. Indicators Calculation
        code += self._get_indicators_calculation(entry_rules)
        
        # 4. Signal Logic (Entry Conditions)
        code += self._get_entry_logic(entry_rules)
        
        # 5. Core Loop (Position Management) - Based on code2.py
        code += self._get_core_loop(position_config, pyramiding_rules, risk_management)
        
        # 6. Output Formatting
        code += self._get_output_section(name, entry_rules)
        
        return code

    def _get_header(self, name):
        return f'''
# Generated Strategy: {name}
import pandas as pd
import numpy as np

# Helper function for checking signals safely
def get_val(arr, i, default=0):
    if i < 0 or i >= len(arr): return default
    return arr[i]
'''

    def _get_parameters(self, pos_config, pyr_rules, risk_mgmt):
        # Default values
        initial_size = pos_config.get('initial_size_pct', 10) / 100.0
        leverage = pos_config.get('leverage', 1)
        max_pyramiding = pos_config.get('max_pyramiding', 0)
        
        pyr_enabled = pyr_rules.get('enabled', False)
        add_size = pyr_rules.get('size_pct', 0) / 100.0 if pyr_enabled else 0
        add_threshold = pyr_rules.get('value', 0) / 100.0
        
        stop_loss = risk_mgmt.get('stop_loss', {})
        sl_enabled = stop_loss.get('enabled', False)
        sl_pct = stop_loss.get('value', 0) / 100.0 if sl_enabled else 0.0
        
        trailing = risk_mgmt.get('trailing_stop', {})
        ts_enabled = trailing.get('enabled', False)
        ts_activation = trailing.get('activation_profit', 0) / 100.0
        ts_callback = trailing.get('callback_pct', 0) / 100.0
        
        return f'''
# ===========================
# 1. Parameters
# ===========================
initial_position_pct = {initial_size}
leverage = {leverage}
max_pyramiding = {max_pyramiding}

# Pyramiding
add_position_pct = {add_size}
add_threshold_pct = {add_threshold} 

# Risk Management
stop_loss_pct = {sl_pct}
take_profit_activation = {ts_activation}
trailing_callback = {ts_callback}
'''

    def _get_indicators_calculation(self, rules):
        code = """
# ===========================
# 2. Indicators Calculation
# ===========================
"""
        calculated = set()
        
        for rule in rules:
            ind = rule.get('indicator')
            params = rule.get('params', {})
            
            if ind == 'supertrend':
                key = f"st_{params.get('period')}_{params.get('multiplier')}"
                if key not in calculated:
                    code += f"""
# SuperTrend ({params.get('period')}, {params.get('multiplier')})
period = {params.get('period', 14)}
multiplier = {params.get('multiplier', 3.0)}
df['hl2'] = (df['high'] + df['low']) / 2
df['tr'] = np.maximum(df['high'] - df['low'], np.maximum(abs(df['high'] - df['close'].shift(1)), abs(df['low'] - df['close'].shift(1))))
df['atr'] = df['tr'].ewm(alpha=1/period, adjust=False).mean()
df['basic_upper'] = df['hl2'] + (multiplier * df['atr'])
df['basic_lower'] = df['hl2'] - (multiplier * df['atr'])

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

df['st_trend'] = trend
df['st_upper'] = final_upper
df['st_lower'] = final_lower
"""
                    calculated.add(key)

            elif ind == 'ema':
                period = params.get('period', 20)
                key = f"ema_{period}"
                if key not in calculated:
                    code += f"\ndf['ema_{period}'] = df['close'].ewm(span={period}, adjust=False).mean()\n"
                    calculated.add(key)

            elif ind == 'rsi':
                period = params.get('period', 14)
                key = f"rsi_{period}"
                if key not in calculated:
                    code += f"""
# RSI ({period})
delta = df['close'].diff()
gain = (delta.where(delta > 0, 0)).rolling(window={period}).mean()
loss = (-delta.where(delta < 0, 0)).rolling(window={period}).mean()
rs = gain / loss
df['rsi_{period}'] = 100 - (100 / (1 + rs))
"""
                    calculated.add(key)

            elif ind == 'macd':
                fast = params.get('fast_period', 12)
                slow = params.get('slow_period', 26)
                signal = params.get('signal_period', 9)
                key = f"macd_{fast}_{slow}_{signal}"
                if key not in calculated:
                    code += f"""
# MACD ({fast}, {slow}, {signal})
exp1 = df['close'].ewm(span={fast}, adjust=False).mean()
exp2 = df['close'].ewm(span={slow}, adjust=False).mean()
df['macd_{fast}_{slow}_{signal}_line'] = exp1 - exp2
df['macd_{fast}_{slow}_{signal}_signal'] = df['macd_{fast}_{slow}_{signal}_line'].ewm(span={signal}, adjust=False).mean()
df['macd_{fast}_{slow}_{signal}_hist'] = df['macd_{fast}_{slow}_{signal}_line'] - df['macd_{fast}_{slow}_{signal}_signal']
"""
                    calculated.add(key)

            elif ind == 'bollinger':
                period = params.get('period', 20)
                std_dev = params.get('std_dev', 2.0)
                key = f"bb_{period}_{std_dev}"
                if key not in calculated:
                    code += f"""
# Bollinger Bands ({period}, {std_dev})
sma = df['close'].rolling(window={period}).mean()
std = df['close'].rolling(window={period}).std()
df['bb_{period}_{std_dev}_upper'] = sma + ({std_dev} * std)
df['bb_{period}_{std_dev}_lower'] = sma - ({std_dev} * std)
df['bb_{period}_{std_dev}_mid'] = sma
"""
                    calculated.add(key)

            elif ind == 'kdj':
                period = params.get('period', 9)
                signal_period = params.get('signal_period', 3)
                key = f"kdj_{period}_{signal_period}"
                if key not in calculated:
                    code += f"""
# KDJ ({period}, {signal_period})
low_min = df['low'].rolling(window={period}).min()
high_max = df['high'].rolling(window={period}).max()
rsv = (df['close'] - low_min) / (high_max - low_min) * 100
df['kdj_{period}_{signal_period}_k'] = rsv.ewm(alpha=1/{signal_period}, adjust=False).mean()
df['kdj_{period}_{signal_period}_d'] = df['kdj_{period}_{signal_period}_k'].ewm(alpha=1/{signal_period}, adjust=False).mean()
df['kdj_{period}_{signal_period}_j'] = 3 * df['kdj_{period}_{signal_period}_k'] - 2 * df['kdj_{period}_{signal_period}_d']
"""
                    calculated.add(key)

            elif ind == 'ma':
                period = params.get('period', 20)
                ma_type = params.get('ma_type', 'sma')
                key = f"ma_{ma_type}_{period}"
                if key not in calculated:
                    if ma_type == 'ema':
                         code += f"\ndf['ma_{ma_type}_{period}'] = df['close'].ewm(span={period}, adjust=False).mean()\n"
                    else:
                         code += f"\ndf['ma_{ma_type}_{period}'] = df['close'].rolling(window={period}).mean()\n"
                    calculated.add(key)

            # ============================================================
            # NEW INDICATORS (修复补全)
            # ============================================================

            elif ind == 'atr':
                period = params.get('period', 14)
                key = f"atr_{period}"
                if key not in calculated:
                    code += f"""
# ATR ({period})
df['tr_atr_{period}'] = np.maximum(df['high'] - df['low'], np.maximum(abs(df['high'] - df['close'].shift(1)), abs(df['low'] - df['close'].shift(1))))
df['atr_{period}'] = df['tr_atr_{period}'].ewm(alpha=1/{period}, adjust=False).mean()
"""
                    calculated.add(key)

            elif ind == 'atr_channel':
                period = params.get('period', 14)
                multiplier = params.get('multiplier', 2.0)
                key = f"atr_channel_{period}_{multiplier}"
                if key not in calculated:
                    code += f"""
# ATR Channel ({period}, {multiplier})
df['tr_atr_ch_{period}'] = np.maximum(df['high'] - df['low'], np.maximum(abs(df['high'] - df['close'].shift(1)), abs(df['low'] - df['close'].shift(1))))
df['atr_ch_{period}'] = df['tr_atr_ch_{period}'].ewm(alpha=1/{period}, adjust=False).mean()
# Donchian 风格通道：滚动高/低点
df['atr_ch_{period}_high'] = df['high'].rolling(window={period}).max()
df['atr_ch_{period}_low'] = df['low'].rolling(window={period}).min()
# ATR 扩展通道：在 Donchian 基础上加减 ATR 偏移
df['atr_ch_{period}_upper'] = df['atr_ch_{period}_high'] + ({multiplier} * df['atr_ch_{period}'])
df['atr_ch_{period}_lower'] = df['atr_ch_{period}_low'] - ({multiplier} * df['atr_ch_{period}'])
"""
                    calculated.add(key)

            elif ind == 'donchian_channel':
                upper_period = params.get('upper_period', 20)
                lower_period = params.get('lower_period', 10)
                key = f"donchian_{upper_period}_{lower_period}"
                if key not in calculated:
                    code += f"""
# Donchian Channel (upper={upper_period}, lower={lower_period})
df['dc_{upper_period}_{lower_period}_upper'] = df['high'].rolling(window={upper_period}).max()
df['dc_{upper_period}_{lower_period}_lower'] = df['low'].rolling(window={lower_period}).min()
df['dc_{upper_period}_{lower_period}_mid'] = (df['dc_{upper_period}_{lower_period}_upper'] + df['dc_{upper_period}_{lower_period}_lower']) / 2
"""
                    calculated.add(key)

            elif ind == 'volume':
                period = params.get('period', 20)
                key = f"vol_ma_{period}"
                if key not in calculated:
                    code += f"""
# Volume MA ({period})
df['vol_ma_{period}'] = df['volume'].rolling(window={period}).mean()
df['vol_ratio_{period}'] = df['volume'] / df['vol_ma_{period}'].replace(0, np.nan)
"""
                    calculated.add(key)

            elif ind == 'vwap':
                # VWAP = cumsum(price * volume) / cumsum(volume)，按日重置
                deviation_pct = params.get('deviation_pct', 2.0)
                key = f"vwap_{deviation_pct}"
                if key not in calculated:
                    code += f"""
# VWAP + 偏离带 ({deviation_pct}%)
# 用滚动 VWAP 近似（日线级别）
df['vwap_cum_pv'] = (df['close'] * df['volume']).rolling(window=20).sum()
df['vwap_cum_vol'] = df['volume'].rolling(window=20).sum()
df['vwap'] = df['vwap_cum_pv'] / df['vwap_cum_vol'].replace(0, np.nan)
df['vwap_upper'] = df['vwap'] * (1 + {deviation_pct} / 100)
df['vwap_lower'] = df['vwap'] * (1 - {deviation_pct} / 100)
"""
                    calculated.add(key)

            elif ind == 'bollinger_bandwidth':
                period = params.get('period', 20)
                std_dev = params.get('std_dev', 2.0)
                squeeze_percentile = params.get('squeeze_percentile', 20)
                key = f"bbw_{period}_{std_dev}_{squeeze_percentile}"
                if key not in calculated:
                    code += f"""
# Bollinger Bandwidth ({period}, {std_dev}) + Squeeze ({squeeze_percentile}%)
bb_sma = df['close'].rolling(window={period}).mean()
bb_std = df['close'].rolling(window={period}).std()
df['bbw_upper_{period}'] = bb_sma + ({std_dev} * bb_std)
df['bbw_lower_{period}'] = bb_sma - ({std_dev} * bb_std)
df['bbw_{period}'] = (df['bbw_upper_{period}'] - df['bbw_lower_{period}']) / bb_sma.replace(0, np.nan)
# 百分位排名：rolling 内计算当前值在窗口内的百分位
_bbw_win = min(120, max(len(df) - 1, 20))
def _pct_rank(x):
    if len(x) < 20:
        return np.nan
    return (np.sum(x[:-1] < x[-1]) + 0.5 * np.sum(x[:-1] == x[-1])) / (len(x) - 1) * 100
df['bbw_{period}_pctile'] = df['bbw_{period}'].rolling(window=_bbw_win, min_periods=20).apply(_pct_rank, raw=True)
"""
                    calculated.add(key)

            elif ind == 'price_volume_divergence':
                lookback = params.get('lookback', 20)
                price_ma = params.get('price_ma', 10)
                volume_ma = params.get('volume_ma', 20)
                key = f"pvd_{lookback}_{price_ma}_{volume_ma}"
                if key not in calculated:
                    code += f"""
# Price-Volume Divergence (lookback={lookback}, price_ma={price_ma}, vol_ma={volume_ma})
# 底背离: 价格创新低但成交量萎缩
df['pvd_price_low_{lookback}'] = df['low'].rolling(window={lookback}).min()
df['pvd_vol_ma_{volume_ma}'] = df['volume'].rolling(window={volume_ma}).mean()
df['pvd_price_ma_{price_ma}'] = df['close'].rolling(window={price_ma}).mean()

# 价格接近N日低点
df['pvd_near_low'] = df['close'] <= df['pvd_price_low_{lookback}'] * 1.03
# 成交量低于均量
df['pvd_vol_shrink'] = df['volume'] < df['pvd_vol_ma_{volume_ma}'] * 0.8
# 价格在下跌（当前MA低于前N日MA）
df['pvd_price_falling'] = df['pvd_price_ma_{price_ma}'] < df['pvd_price_ma_{price_ma}'].shift({lookback})

# 底背离信号: 价格在低位 + 量缩 + 之前在跌
df['pvd_bullish'] = df['pvd_near_low'] & df['pvd_vol_shrink'] & df['pvd_price_falling']
"""
                    calculated.add(key)

        return code

    def _get_entry_logic(self, rules):
        code = """
# ===========================
# 3. Entry Signal Logic
# ===========================
# Default False
df['raw_buy'] = False
df['raw_sell'] = False
"""
        conditions_buy = []
        conditions_sell = []
        
        for rule in rules:
            ind = rule.get('indicator')
            params = rule.get('params', {})
            
            if ind == 'supertrend':
                signal = rule.get('signal', 'trend_bullish')
                if signal == 'trend_bullish':
                    conditions_buy.append("(df['st_trend'] == 1) & (df['st_trend'].shift(1) == -1)")
                    conditions_sell.append("(df['st_trend'] == -1) & (df['st_trend'].shift(1) == 1)")
                elif signal == 'is_uptrend':
                    conditions_buy.append("(df['st_trend'] == 1)")
                    conditions_sell.append("(df['st_trend'] == -1)")
            
            elif ind == 'ema':
                period = params.get('period', 20)
                operator = rule.get('operator', 'price_above')
                col = f"df['ema_{period}']"
                if operator == 'price_above':
                    conditions_buy.append(f"(df['close'] > {col})")
                    conditions_sell.append(f"(df['close'] < {col})")
                elif operator == 'price_below':
                    conditions_buy.append(f"(df['close'] < {col})")
                    conditions_sell.append(f"(df['close'] > {col})")
                elif operator == 'cross_up':
                    conditions_buy.append(f"(df['close'] > {col}) & (df['close'].shift(1) <= {col}.shift(1))")
                    conditions_sell.append(f"(df['close'] < {col}) & (df['close'].shift(1) >= {col}.shift(1))")
                elif operator == 'cross_down':
                    conditions_buy.append(f"(df['close'] < {col}) & (df['close'].shift(1) >= {col}.shift(1))")
                    conditions_sell.append(f"(df['close'] > {col}) & (df['close'].shift(1) <= {col}.shift(1))")

            elif ind == 'rsi':
                period = params.get('period', 14)
                operator = rule.get('operator', '<')
                thresh = params.get('threshold', 30)
                col = f"df['rsi_{period}']"
                if operator == '<':
                    conditions_buy.append(f"({col} < {thresh})")
                    conditions_sell.append(f"({col} > {100-thresh})")
                elif operator == '>':
                    conditions_buy.append(f"({col} > {thresh})")
                    conditions_sell.append(f"({col} < {100-thresh})")
                elif operator == 'cross_up':
                    conditions_buy.append(f"({col} > {thresh}) & ({col}.shift(1) <= {thresh})")
                    conditions_sell.append(f"({col} < {100-thresh}) & ({col}.shift(1) >= {100-thresh})")
                elif operator == 'cross_down':
                    conditions_buy.append(f"({col} < {thresh}) & ({col}.shift(1) >= {thresh})")
                    conditions_sell.append(f"({col} > {100-thresh}) & ({col}.shift(1) <= {100-thresh})")

            elif ind == 'macd':
                fast = params.get('fast_period', 12)
                slow = params.get('slow_period', 26)
                signal = params.get('signal_period', 9)
                operator = rule.get('operator', 'diff_gt_dea')
                line_col = f"df['macd_{fast}_{slow}_{signal}_line']"
                sig_col = f"df['macd_{fast}_{slow}_{signal}_signal']"
                hist_col = f"df['macd_{fast}_{slow}_{signal}_hist']"
                
                if operator == 'diff_gt_dea':
                    conditions_buy.append(f"({line_col} > {sig_col})")
                    conditions_sell.append(f"({line_col} < {sig_col})")
                elif operator == 'diff_lt_dea':
                    conditions_buy.append(f"({line_col} < {sig_col})")
                    conditions_sell.append(f"({line_col} > {sig_col})")
                elif operator == 'cross_up':
                    conditions_buy.append(f"({line_col} > {sig_col}) & ({line_col}.shift(1) <= {sig_col}.shift(1))")
                    conditions_sell.append(f"({line_col} < {sig_col}) & ({line_col}.shift(1) >= {sig_col}.shift(1))")
                elif operator == 'cross_down':
                    conditions_buy.append(f"({line_col} < {sig_col}) & ({line_col}.shift(1) >= {sig_col}.shift(1))")
                    conditions_sell.append(f"({line_col} > {sig_col}) & ({line_col}.shift(1) <= {sig_col}.shift(1))")
                elif operator == 'histogram_positive':
                    conditions_buy.append(f"({hist_col} > 0) & ({hist_col}.shift(1) <= 0)")
                    conditions_sell.append(f"({hist_col} < 0) & ({hist_col}.shift(1) >= 0)")
                elif operator == 'histogram_negative':
                    conditions_buy.append(f"({hist_col} < 0) & ({hist_col}.shift(1) >= 0)")
                    conditions_sell.append(f"({hist_col} > 0) & ({hist_col}.shift(1) <= 0)")

            elif ind == 'bollinger':
                period = params.get('period', 20)
                std_dev = params.get('std_dev', 2.0)
                operator = rule.get('operator', 'price_above_upper')
                upper = f"df['bb_{period}_{std_dev}_upper']"
                lower = f"df['bb_{period}_{std_dev}_lower']"
                mid = f"df['bb_{period}_{std_dev}_mid']"
                
                if operator == 'price_above_upper':
                    conditions_buy.append(f"(df['close'] > {upper})")
                    conditions_sell.append(f"(df['close'] < {lower})")
                elif operator == 'price_below_lower':
                    conditions_buy.append(f"(df['close'] < {lower})")
                    conditions_sell.append(f"(df['close'] > {upper})")
                elif operator == 'price_above_mid':
                    conditions_buy.append(f"(df['close'] > {mid})")
                    conditions_sell.append(f"(df['close'] < {mid})")
                elif operator == 'price_below_mid':
                    conditions_buy.append(f"(df['close'] < {mid})")
                    conditions_sell.append(f"(df['close'] > {mid})")
                elif operator == 'cross_up_lower':
                    conditions_buy.append(f"(df['close'] > {lower}) & (df['close'].shift(1) <= {lower}.shift(1))")
                    conditions_sell.append(f"(df['close'] < {upper}) & (df['close'].shift(1) >= {upper}.shift(1))")
                elif operator == 'cross_down_upper':
                    conditions_buy.append(f"(df['close'] < {upper}) & (df['close'].shift(1) >= {upper}.shift(1))")
                    conditions_sell.append(f"(df['close'] > {lower}) & (df['close'].shift(1) <= {lower}.shift(1))")

            elif ind == 'kdj':
                period = params.get('period', 9)
                signal_period = params.get('signal_period', 3)
                operator = rule.get('operator', 'k_gt_d')
                k_col = f"df['kdj_{period}_{signal_period}_k']"
                d_col = f"df['kdj_{period}_{signal_period}_d']"
                j_col = f"df['kdj_{period}_{signal_period}_j']"
                
                if operator == 'k_gt_d':
                    conditions_buy.append(f"({k_col} > {d_col})")
                    conditions_sell.append(f"({k_col} < {d_col})")
                elif operator == 'k_lt_d':
                    conditions_buy.append(f"({k_col} < {d_col})")
                    conditions_sell.append(f"({k_col} > {d_col})")
                elif operator == 'gold_cross':
                    conditions_buy.append(f"({k_col} > {d_col}) & ({k_col}.shift(1) <= {d_col}.shift(1))")
                    conditions_sell.append(f"({k_col} < {d_col}) & ({k_col}.shift(1) >= {d_col}.shift(1))")
                elif operator == 'death_cross':
                    conditions_buy.append(f"({k_col} < {d_col}) & ({k_col}.shift(1) >= {d_col}.shift(1))")
                    conditions_sell.append(f"({k_col} > {d_col}) & ({k_col}.shift(1) <= {d_col}.shift(1))")

            elif ind == 'ma':
                period = params.get('period', 20)
                ma_type = params.get('ma_type', 'sma')
                operator = rule.get('operator', 'price_above')
                col = f"df['ma_{ma_type}_{period}']"
                
                if operator == 'price_above':
                    conditions_buy.append(f"(df['close'] > {col})")
                    conditions_sell.append(f"(df['close'] < {col})")
                elif operator == 'price_below':
                    conditions_buy.append(f"(df['close'] < {col})")
                    conditions_sell.append(f"(df['close'] > {col})")
                elif operator == 'cross_up':
                    conditions_buy.append(f"(df['close'] > {col}) & (df['close'].shift(1) <= {col}.shift(1))")
                    conditions_sell.append(f"(df['close'] < {col}) & (df['close'].shift(1) >= {col}.shift(1))")
                elif operator == 'cross_down':
                    conditions_buy.append(f"(df['close'] < {col}) & (df['close'].shift(1) >= {col}.shift(1))")
                    conditions_sell.append(f"(df['close'] > {col}) & (df['close'].shift(1) <= {col}.shift(1))")

            # ============================================================
            # NEW OPERATORS (修复补全)
            # ============================================================

            elif ind == 'donchian_channel':
                upper_period = params.get('upper_period', 20)
                lower_period = params.get('lower_period', 10)
                operator = rule.get('operator', 'price_break_upper')
                upper = f"df['dc_{upper_period}_{lower_period}_upper']"
                lower = f"df['dc_{upper_period}_{lower_period}_lower']"
                mid = f"df['dc_{upper_period}_{lower_period}_mid']"
                
                if operator == 'price_break_upper':
                    # 价格突破N日高点（用前一根K线的通道，避免前瞻偏差）
                    conditions_buy.append(f"(df['close'] > {upper}.shift(1))")
                    conditions_sell.append(f"(df['close'] < {lower}.shift(1))")
                elif operator == 'price_break_lower':
                    conditions_buy.append(f"(df['close'] < {lower}.shift(1))")
                    conditions_sell.append(f"(df['close'] > {upper}.shift(1))")
                elif operator == 'price_above_upper':
                    conditions_buy.append(f"(df['close'] > {upper})")
                    conditions_sell.append(f"(df['close'] < {lower})")
                elif operator == 'price_below_lower':
                    conditions_buy.append(f"(df['close'] < {lower})")
                    conditions_sell.append(f"(df['close'] > {upper})")

            elif ind == 'atr_channel':
                period = params.get('period', 14)
                multiplier = params.get('multiplier', 2.0)
                operator = rule.get('operator', 'price_above_upper')
                upper = f"df['atr_ch_{period}_upper']"
                lower = f"df['atr_ch_{period}_lower']"
                
                if operator == 'price_above_upper':
                    conditions_buy.append(f"(df['close'] > {upper}.shift(1))")
                    conditions_sell.append(f"(df['close'] < {lower}.shift(1))")
                elif operator == 'price_below_lower':
                    conditions_buy.append(f"(df['close'] < {lower}.shift(1))")
                    conditions_sell.append(f"(df['close'] > {upper}.shift(1))")

            elif ind == 'volume':
                period = params.get('period', 20)
                operator = rule.get('operator', 'volume_above_ma')
                threshold = rule.get('threshold', params.get('threshold', 1.5))
                vol_ma = f"df['vol_ma_{period}']"
                vol_ratio = f"df['vol_ratio_{period}']"
                
                if operator == 'volume_above_ma':
                    conditions_buy.append(f"(df['volume'] > {vol_ma})")
                    conditions_sell.append(f"(df['volume'] < {vol_ma})")
                elif operator == 'volume_ratio_above':
                    conditions_buy.append(f"({vol_ratio} > {threshold})")
                    conditions_sell.append(f"({vol_ratio} < {threshold})")
                elif operator == 'volume_shrink':
                    conditions_buy.append(f"({vol_ratio} < 0.8)")
                    conditions_sell.append(f"({vol_ratio} > 1.2)")

            elif ind == 'vwap':
                deviation_pct = params.get('deviation_pct', 2.0)
                operator = rule.get('operator', 'price_below_vwap_by')
                vwap_col = "df['vwap']"
                vwap_upper = "df['vwap_upper']"
                vwap_lower = "df['vwap_lower']"
                
                if operator == 'price_below_vwap_by':
                    # 价格低于VWAP超过偏离阈值 → 均值回归做多
                    conditions_buy.append(f"(df['close'] < {vwap_lower})")
                    conditions_sell.append(f"(df['close'] > {vwap_upper})")
                elif operator == 'price_above_vwap':
                    conditions_buy.append(f"(df['close'] > {vwap_col})")
                    conditions_sell.append(f"(df['close'] < {vwap_col})")
                elif operator == 'cross_up_vwap':
                    conditions_buy.append(f"(df['close'] > {vwap_col}) & (df['close'].shift(1) <= {vwap_col}.shift(1))")
                    conditions_sell.append(f"(df['close'] < {vwap_col}) & (df['close'].shift(1) >= {vwap_col}.shift(1))")

            elif ind == 'bollinger_bandwidth':
                period = params.get('period', 20)
                std_dev = params.get('std_dev', 2.0)
                squeeze_percentile = params.get('squeeze_percentile', 20)
                operator = rule.get('operator', 'below_percentile')
                pctile_col = f"df['bbw_{period}_{std_dev}_{squeeze_percentile}_pctile']"
                lower_col = f"df['bbw_lower_{period}']"
                
                if operator == 'below_percentile':
                    # 带宽处于历史低位 → 收缩
                    conditions_buy.append(f"({pctile_col} < {squeeze_percentile})")
                    conditions_sell.append(f"({pctile_col} > {100 - squeeze_percentile})")

            elif ind == 'price_volume_divergence':
                operator = rule.get('operator', 'bullish_divergence')
                pvd_col = "df['pvd_bullish']"
                lookback = params.get('lookback', 20)
                vol_ma = params.get('volume_ma', 20)
                
                if operator == 'bullish_divergence':
                    conditions_buy.append(f"({pvd_col})")
                    # 卖出: 价格创新高但量缩（用参数中的 volume_ma，不硬编码 20）
                    conditions_sell.append(f"(df['close'] > df['close'].rolling(window={lookback}).max().shift(1)) & (df['volume'] < df['vol_ma_{vol_ma}'])")

        if conditions_buy:
            code += f"\ndf['raw_buy'] = {' & '.join(conditions_buy)}\n"
        if conditions_sell:
            code += f"\ndf['raw_sell'] = {' & '.join(conditions_sell)}\n"
            
        return code

    def _get_core_loop(self, pos_config, pyr_rules, risk_mgmt):
        # This mirrors the logic in code2.py loop
        return """
# ===========================
# 4. Core Loop (Backtest)
# ===========================
open_long_signals = [False] * len(df)
add_long_signals = [False] * len(df)
close_long_signals = [False] * len(df)

open_long_text = [None] * len(df)
add_long_text = [None] * len(df)

open_long_price = [0.0] * len(df)
add_long_price = [0.0] * len(df)
close_long_price = [0.0] * len(df)
close_long_text = [None] * len(df)

open_short_signals = [False] * len(df)
add_short_signals = [False] * len(df)
close_short_signals = [False] * len(df)

open_short_price = [0.0] * len(df)
add_short_price = [0.0] * len(df)
close_short_price = [0.0] * len(df)
close_short_text = [None] * len(df)
open_short_text = [None] * len(df)
add_short_text = [None] * len(df)

position = 0 # 0, 1 (Long), -1 (Short)
position_count = 0
avg_entry_price = 0.0
last_add_price = 0.0
highest_price = 0.0 # For Long: Highest High; For Short: Lowest Low

close_arr = df['close'].values
high_arr = df['high'].values
low_arr = df['low'].values
raw_buy_arr = df['raw_buy'].values
raw_sell_arr = df['raw_sell'].values

for i in range(len(df)):
    current_close = close_arr[i]
    current_high = high_arr[i]
    current_low = low_arr[i]
    
    if position == 1:
        # Long Position
        if current_high > highest_price:
            highest_price = current_high
            
        profit_pct = (highest_price - avg_entry_price) / avg_entry_price
        current_profit_pct = (current_close - avg_entry_price) / avg_entry_price
        
        # 1. Trailing Stop
        if take_profit_activation > 0 and profit_pct >= take_profit_activation:
            drawdown = (highest_price - current_close) / avg_entry_price
            if drawdown >= trailing_callback:
                close_long_signals[i] = True
                close_long_price[i] = current_close
                close_long_text[i] = "Trailing Stop"
                position = 0
                position_count = 0
                continue
                
        # 2. Stop Loss
        if stop_loss_pct > 0:
            loss_pct = (avg_entry_price - current_low) / avg_entry_price
            if loss_pct >= stop_loss_pct:
                close_long_signals[i] = True
                close_long_price[i] = avg_entry_price * (1 - stop_loss_pct)
                close_long_text[i] = "Stop Loss"
                position = 0
                position_count = 0
                continue
                
        # 3. Signal Exit (if enabled)
        # Note: Code2 uses raw_sell_arr for exit
        if raw_sell_arr[i]:
             close_long_signals[i] = True
             close_long_price[i] = current_close
             close_long_text[i] = "Signal Exit"
             position = 0
             position_count = 0
             
             # Reverse to Short if trade_direction allows (simplified here)
             # For now we just close.
             continue
             
        # 4. Pyramiding (Add Long)
        if max_pyramiding > 0 and position_count < max_pyramiding + 1 and current_profit_pct > 0:
             # Condition: Price rise by threshold
             if add_threshold_pct > 0:
                 target_price = last_add_price * (1 + add_threshold_pct)
                 if current_high >= target_price:
                     add_long_signals[i] = True
                     add_long_price[i] = target_price
                     add_long_text[i] = "Add Long"
                     position_count += 1
                     last_add_price = target_price
             
    elif position == -1:
        # Short Position
        # For Short, highest_price tracks the LOWEST price (best profit scenario)
        if highest_price == 0: highest_price = avg_entry_price
        if current_low < highest_price:
            highest_price = current_low
            
        # Profit: (Entry - Lowest) / Entry
        profit_pct = (avg_entry_price - highest_price) / avg_entry_price
        current_profit_pct = (avg_entry_price - current_close) / avg_entry_price
        
        # 1. Trailing Stop
        if take_profit_activation > 0 and profit_pct >= take_profit_activation:
            # Drawdown: (Current - Lowest) / Entry
            drawdown = (current_close - highest_price) / avg_entry_price
            if drawdown >= trailing_callback:
                close_short_signals[i] = True
                close_short_price[i] = current_close
                close_short_text[i] = "Trailing Stop"
                position = 0
                position_count = 0
                continue

        # 2. Stop Loss
        if stop_loss_pct > 0:
            # Loss: Price went up. (High - Entry) / Entry
            loss_pct = (current_high - avg_entry_price) / avg_entry_price
            if loss_pct >= stop_loss_pct:
                close_short_signals[i] = True
                close_short_price[i] = avg_entry_price * (1 + stop_loss_pct)
                close_short_text[i] = "Stop Loss"
                position = 0
                position_count = 0
                continue

        # 3. Signal Exit
        if raw_buy_arr[i]:
             close_short_signals[i] = True
             close_short_price[i] = current_close
             close_short_text[i] = "Signal Exit"
             position = 0
             position_count = 0
             continue

        # 4. Pyramiding (Add Short)
        if max_pyramiding > 0 and position_count < max_pyramiding + 1 and current_profit_pct > 0:
             # Condition: Price drop by threshold
             if add_threshold_pct > 0:
                 target_price = last_add_price * (1 - add_threshold_pct)
                 if current_low <= target_price:
                     add_short_signals[i] = True
                     add_short_price[i] = target_price
                     add_short_text[i] = "Add Short"
                     position_count += 1
                     last_add_price = target_price

    else:
        # No Position
        if raw_buy_arr[i]:
            open_long_signals[i] = True
            open_long_price[i] = current_close
            open_long_text[i] = "Open Long"
            position = 1
            position_count = 1
            avg_entry_price = current_close
            last_add_price = current_close
            highest_price = current_close
            
        elif raw_sell_arr[i]:
            open_short_signals[i] = True
            open_short_price[i] = current_close
            open_short_text[i] = "Open Short"
            position = -1
            position_count = 1
            avg_entry_price = current_close
            last_add_price = current_close
            highest_price = current_close # Init with Entry

# Append columns
df['open_long'] = open_long_signals
df['add_long'] = add_long_signals
df['close_long'] = close_long_signals
df['open_long_price'] = [p if s else None for p, s in zip(open_long_price, open_long_signals)]
df['add_long_price'] = [p if s else None for p, s in zip(add_long_price, add_long_signals)]
df['close_long_price'] = [p if s else None for p, s in zip(close_long_price, close_long_signals)]
df['open_long_text'] = open_long_text
df['add_long_text'] = add_long_text
df['close_long_text'] = close_long_text
df['open_short'] = open_short_signals
df['add_short'] = add_short_signals
df['close_short'] = close_short_signals
df['open_short_price'] = [p if s else None for p, s in zip(open_short_price, open_short_signals)]
df['add_short_price'] = [p if s else None for p, s in zip(add_short_price, add_short_signals)]
df['close_short_price'] = [p if s else None for p, s in zip(close_short_price, close_short_signals)]
df['open_short_text'] = open_short_text
df['add_short_text'] = add_short_text
df['close_short_text'] = close_short_text
df['buy'] = open_long_signals
df['sell'] = open_short_signals

"""

    def _get_output_section(self, name, rules):
        # Generate plot configs based on indicators
        plots = []
        for rule in rules:
            ind = rule.get('indicator')
            params = rule.get('params', {})
            if ind == 'supertrend':
                plots.append({
                    "name": "SuperTrend Up", "type": "line", "data": "df['st_lower'].tolist()", "color": "#00FF00", "overlay": True
                })
                plots.append({
                    "name": "SuperTrend Down", "type": "line", "data": "df['st_upper'].tolist()", "color": "#FF0000", "overlay": True
                })
            elif ind == 'ema':
                p = params.get('period', 20)
                plots.append({
                    "name": f"EMA {p}", "type": "line", "data": f"df['ema_{p}'].tolist()", "color": "#FFA500", "overlay": True
                })
            elif ind == 'ma':
                p = params.get('period', 20)
                t = params.get('ma_type', 'sma')
                plots.append({
                    "name": f"{t.upper()} {p}", "type": "line", "data": f"df['ma_{t}_{p}'].tolist()", "color": "#FFA500", "overlay": True
                })
            elif ind == 'bollinger':
                p = params.get('period', 20)
                d = params.get('std_dev', 2.0)
                plots.append({
                    "name": "BB Upper", "type": "line", "data": f"df['bb_{p}_{d}_upper'].tolist()", "color": "#0088FE", "overlay": True
                })
                plots.append({
                    "name": "BB Lower", "type": "line", "data": f"df['bb_{p}_{d}_lower'].tolist()", "color": "#0088FE", "overlay": True
                })
            # MACD, RSI, KDJ are typically separate panes, not overlay. The `overlay` param controls this.
            elif ind == 'macd':
                f = params.get('fast_period', 12)
                s = params.get('slow_period', 26)
                si = params.get('signal_period', 9)
                plots.append({
                    "name": "MACD", "type": "line", "data": f"df['macd_{f}_{s}_{si}_line'].tolist()", "color": "#0088FE", "overlay": False
                })
                plots.append({
                    "name": "Signal", "type": "line", "data": f"df['macd_{f}_{s}_{si}_signal'].tolist()", "color": "#FF8042", "overlay": False
                })
            elif ind == 'rsi':
                p = params.get('period', 14)
                plots.append({
                    "name": f"RSI {p}", "type": "line", "data": f"df['rsi_{p}'].tolist()", "color": "#8884d8", "overlay": False
                })
            elif ind == 'kdj':
                p = params.get('period', 9)
                si = params.get('signal_period', 3)
                plots.append({
                    "name": "K", "type": "line", "data": f"df['kdj_{p}_{si}_k'].tolist()", "color": "#8884d8", "overlay": False
                })
                plots.append({
                    "name": "D", "type": "line", "data": f"df['kdj_{p}_{si}_d'].tolist()", "color": "#82ca9d", "overlay": False
                })
                plots.append({
                    "name": "J", "type": "line", "data": f"df['kdj_{p}_{si}_j'].tolist()", "color": "#ffc658", "overlay": False
                })
        
        # Convert plots to string representation valid in Python
        plots_py = "[\n"
        for p in plots:
            plots_py += f"    {{'name': '{p['name']}', 'type': '{p['type']}', 'data': {p['data']}, 'color': '{p['color']}', 'overlay': {p['overlay']}}},\n"
        plots_py += "]"

        return f"""
# ===========================
# 5. Output
# ===========================
output = {{
    "name": "{name}",
    "plots": {plots_py},
    "signals": [
        {{
            "name": "Open Long",
            "type": "buy",
            "data": df['open_long_price'].tolist(),
            "color": "#00FF00",
            "text": "Open Long"
        }},
        {{
            "name": "Add Long",
            "type": "buy",
            "data": df['add_long_price'].tolist(),
            "color": "#00DD00",
            "text": "Add Long"
        }},
        {{
            "name": "Close Long",
            "type": "sell",
            "data": df['close_long_price'].tolist(),
            "color": "#FF6600",
            "text": "Close Long"
        }},
        {{
            "name": "Open Short",
            "type": "sell",
            "data": df['open_short_price'].tolist(),
            "color": "#FF0000",
            "text": "Open Short"
        }},
        {{
            "name": "Add Short",
            "type": "sell",
            "data": df['add_short_price'].tolist(),
            "color": "#DD0000",
            "text": "Add Short"
        }},
        {{
            "name": "Close Short",
            "type": "buy",
            "data": df['close_short_price'].tolist(),
            "color": "#00CCFF",
            "text": "Close Short"
        }}
    ]
}}
"""
