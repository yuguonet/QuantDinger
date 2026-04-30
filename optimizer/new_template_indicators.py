"""
新模板的指标代码生成函数
添加到 wf_validate_direct.py 的 generate_indicator_code 函数中

用法：将以下函数复制到 wf_validate_direct.py，并在 generate_indicator_code 中添加分支
"""


def _gen_vwap_rsi_confirm(p):
    """VWAP 偏离 + RSI 超卖双确认"""
    vwap_dev = p.get("vwap_dev_pct", 2.0)
    rsi_period = p.get("rsi_period", 14)
    rsi_level = p.get("rsi_level", 33)
    use_vol = p.get("use_vol_filter", False)
    vol_ma = p.get("vol_ma_period", 15)
    vol_ratio = p.get("vol_ratio", 1.3)

    code = f"""
import pandas as pd
import numpy as np

# VWAP
df['vwap_pv'] = (df['close'] * df['volume']).rolling(window=20).sum()
df['vwap_vol'] = df['volume'].rolling(window=20).sum()
df['vwap'] = df['vwap_pv'] / df['vwap_vol'].replace(0, np.nan)
df['vwap_lower'] = df['vwap'] * (1 - {vwap_dev} / 100)
df['vwap_upper'] = df['vwap'] * (1 + {vwap_dev} / 100)

# RSI
delta = df['close'].diff()
gain = (delta.where(delta > 0, 0)).rolling(window={rsi_period}).mean()
loss = (-delta.where(delta < 0, 0)).rolling(window={rsi_period}).mean()
rs = gain / loss.replace(0, 0.0001)
df['rsi'] = 100 - (100 / (1 + rs))

df['buy'] = (df['close'] < df['vwap_lower']) & (df['rsi'] < {rsi_level})
df['sell'] = (df['close'] > df['vwap_upper'])
"""
    if use_vol:
        code += f"""
df['vol_ma'] = df['volume'].rolling(window={vol_ma}).mean()
df['vol_ratio'] = df['volume'] / df['vol_ma'].replace(0, np.nan)
df['buy'] = df['buy'] & (df['vol_ratio'] > {vol_ratio})
"""
    return code


def _gen_rsi_bollinger_support(p):
    """RSI 超卖 + BB 下轨支撑"""
    rsi_period = p.get("rsi_period", 14)
    rsi_level = p.get("rsi_level", 33)
    bb_period = p.get("bb_period", 20)
    bb_std = p.get("bb_std", 2.0)
    use_vol = p.get("use_vol_filter", False)
    vol_ma = p.get("vol_ma_period", 15)
    vol_ratio = p.get("vol_ratio", 1.3)

    code = f"""
import pandas as pd
import numpy as np

# RSI
delta = df['close'].diff()
gain = (delta.where(delta > 0, 0)).rolling(window={rsi_period}).mean()
loss = (-delta.where(delta < 0, 0)).rolling(window={rsi_period}).mean()
rs = gain / loss.replace(0, 0.0001)
df['rsi'] = 100 - (100 / (1 + rs))

# Bollinger Bands
sma = df['close'].rolling(window={bb_period}).mean()
std = df['close'].rolling(window={bb_period}).std()
df['bb_upper'] = sma + ({bb_std} * std)
df['bb_lower'] = sma - ({bb_std} * std)

df['buy'] = (df['rsi'] < {rsi_level}) & (df['close'] < df['bb_lower'])
df['sell'] = (df['close'] > df['bb_upper'])
"""
    if use_vol:
        code += f"""
df['vol_ma'] = df['volume'].rolling(window={vol_ma}).mean()
df['vol_ratio'] = df['volume'] / df['vol_ma'].replace(0, np.nan)
df['buy'] = df['buy'] & (df['vol_ratio'] > {vol_ratio})
"""
    return code


def _gen_vwap_macd_volume(p):
    """VWAP 偏离 + MACD 金叉 + 放量"""
    vwap_dev = p.get("vwap_dev_pct", 2.0)
    macd_fast = p.get("macd_fast", 12)
    macd_slow = p.get("macd_slow", 26)
    macd_signal = p.get("macd_signal", 9)
    vol_ma = p.get("vol_ma_period", 15)
    vol_ratio = p.get("vol_ratio", 1.3)

    return f"""
import pandas as pd
import numpy as np

# VWAP
df['vwap_pv'] = (df['close'] * df['volume']).rolling(window=20).sum()
df['vwap_vol'] = df['volume'].rolling(window=20).sum()
df['vwap'] = df['vwap_pv'] / df['vwap_vol'].replace(0, np.nan)
df['vwap_lower'] = df['vwap'] * (1 - {vwap_dev} / 100)
df['vwap_upper'] = df['vwap'] * (1 + {vwap_dev} / 100)

# MACD
exp1 = df['close'].ewm(span={macd_fast}, adjust=False).mean()
exp2 = df['close'].ewm(span={macd_slow}, adjust=False).mean()
df['macd_line'] = exp1 - exp2
df['macd_signal_line'] = df['macd_line'].ewm(span={macd_signal}, adjust=False).mean()
df['macd_hist'] = df['macd_line'] - df['macd_signal_line']

# Volume
df['vol_ma'] = df['volume'].rolling(window={vol_ma}).mean()
df['vol_ratio'] = df['volume'] / df['vol_ma'].replace(0, np.nan)

# Buy: VWAP 偏离 + MACD 金叉 + 放量
df['macd_cross_up'] = (df['macd_line'] > df['macd_signal_line']) & (df['macd_line'].shift(1) <= df['macd_signal_line'].shift(1))
df['buy'] = (df['close'] < df['vwap_lower']) & df['macd_cross_up'] & (df['vol_ratio'] > {vol_ratio})
df['sell'] = (df['close'] > df['vwap_upper'])
"""


def _gen_kdj_vwap_reversal(p):
    """KDJ 超卖 + VWAP 偏离"""
    kdj_k = p.get("kdj_k", 9)
    kdj_d = p.get("kdj_d", 3)
    kdj_j = p.get("kdj_j", 3)
    kdj_oversold = p.get("kdj_oversold", 20)
    vwap_dev = p.get("vwap_dev_pct", 2.0)
    use_rsi = p.get("use_rsi_filter", False)
    rsi_period = p.get("rsi_period", 14)
    rsi_level = p.get("rsi_level", 33)

    code = f"""
import pandas as pd
import numpy as np

# KDJ
low_min = df['low'].rolling(window={kdj_k}).min()
high_max = df['high'].rolling(window={kdj_k}).max()
rsv = (df['close'] - low_min) / (high_max - low_min).replace(0, 0.0001) * 100
df['kdj_k'] = rsv.ewm(com={kdj_d - 1}, adjust=False).mean()
df['kdj_d'] = df['kdj_k'].ewm(com={kdj_j - 1}, adjust=False).mean()
df['kdj_j'] = 3 * df['kdj_k'] - 2 * df['kdj_d']

# VWAP
df['vwap_pv'] = (df['close'] * df['volume']).rolling(window=20).sum()
df['vwap_vol'] = df['volume'].rolling(window=20).sum()
df['vwap'] = df['vwap_pv'] / df['vwap_vol'].replace(0, np.nan)
df['vwap_lower'] = df['vwap'] * (1 - {vwap_dev} / 100)
df['vwap_upper'] = df['vwap'] * (1 + {vwap_dev} / 100)

df['buy'] = (df['kdj_j'] < {kdj_oversold}) & (df['close'] < df['vwap_lower'])
df['sell'] = (df['close'] > df['vwap_upper'])
"""
    if use_rsi:
        code += f"""
delta = df['close'].diff()
gain = (delta.where(delta > 0, 0)).rolling(window={rsi_period}).mean()
loss = (-delta.where(delta < 0, 0)).rolling(window={rsi_period}).mean()
rs = gain / loss.replace(0, 0.0001)
df['rsi'] = 100 - (100 / (1 + rs))
df['buy'] = df['buy'] & (df['rsi'] < {rsi_level})
"""
    return code


def _gen_ema_rsi_pullback(p):
    """EMA 多头排列 + RSI 回调买入"""
    ema_fast = p.get("ema_fast", 10)
    ema_slow = p.get("ema_slow", 30)
    rsi_period = p.get("rsi_period", 14)
    rsi_pullback = p.get("rsi_pullback", 42)

    return f"""
import pandas as pd
import numpy as np

# EMA 多头排列
df['ema_fast'] = df['close'].ewm(span={ema_fast}, adjust=False).mean()
df['ema_slow'] = df['close'].ewm(span={ema_slow}, adjust=False).mean()
df['ema_trend_up'] = df['ema_fast'] > df['ema_slow']

# RSI
delta = df['close'].diff()
gain = (delta.where(delta > 0, 0)).rolling(window={rsi_period}).mean()
loss = (-delta.where(delta < 0, 0)).rolling(window={rsi_period}).mean()
rs = gain / loss.replace(0, 0.0001)
df['rsi'] = 100 - (100 / (1 + rs))

# Buy: 趋势向上 + RSI 回调到 pullback 区间
df['buy'] = df['ema_trend_up'] & (df['rsi'] < {rsi_pullback})
# Sell: RSI 进入超买区 或 EMA 死叉
df['sell'] = (df['rsi'] > 70) | (~df['ema_trend_up'])
"""


# ============================================================
# 需要在 wf_validate_direct.py 的 generate_indicator_code 中添加：
# ============================================================
# elif template_key == "vwap_rsi_confirm":
#     return _gen_vwap_rsi_confirm(p)
# elif template_key == "rsi_bollinger_support":
#     return _gen_rsi_bollinger_support(p)
# elif template_key == "vwap_macd_volume":
#     return _gen_vwap_macd_volume(p)
# elif template_key == "kdj_vwap_reversal":
#     return _gen_kdj_vwap_reversal(p)
# elif template_key == "ema_rsi_pullback":
#     return _gen_ema_rsi_pullback(p)
