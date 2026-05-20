"""
IndicatorStrategy 代码生成器

将模板参数直接渲染为标准 IndicatorStrategy 代码：
  - # @param 声明可调参数
  - # @strategy 声明风控默认值
  - df['buy'] / df['sell'] 标准信号
  - output 标准图表输出

替代原来的 JSON config → StrategyCompiler 字符串拼接链路。
"""
from typing import Dict, Any, List, Optional


# ============================================================
# 指标代码片段（复用）
# ============================================================

def _code_ma(period: int, ma_type: str = "sma", col: str = None) -> str:
    """生成 MA 计算代码"""
    col = col or f"ma_{ma_type}_{period}"
    if ma_type == "ema":
        return f"df['{col}'] = df['close'].ewm(span={period}, adjust=False).mean()"
    return f"df['{col}'] = df['close'].rolling(window={period}).mean()"


def _code_ema(period: int, col: str = None) -> str:
    col = col or f"ema_{period}"
    return f"df['{col}'] = df['close'].ewm(span={period}, adjust=False).mean()"


def _code_rsi(period: int = 14, col: str = None) -> str:
    col = col or f"rsi_{period}"
    return f"""delta = df['close'].diff()
gain = delta.clip(lower=0).ewm(alpha=1/{period}, adjust=False).mean()
loss = (-delta.clip(upper=0)).ewm(alpha=1/{period}, adjust=False).mean()
rs = gain / loss.replace(0, np.nan)
df['{col}'] = 100 - (100 / (1 + rs))"""


def _code_macd(fast: int = 12, slow: int = 26, signal: int = 9, prefix: str = None) -> str:
    prefix = prefix or f"macd_{fast}_{slow}_{signal}"
    return f"""exp1 = df['close'].ewm(span={fast}, adjust=False).mean()
exp2 = df['close'].ewm(span={slow}, adjust=False).mean()
df['{prefix}_line'] = exp1 - exp2
df['{prefix}_signal'] = df['{prefix}_line'].ewm(span={signal}, adjust=False).mean()
df['{prefix}_hist'] = df['{prefix}_line'] - df['{prefix}_signal']"""


def _code_bollinger(period: int = 20, std_dev: float = 2.0, prefix: str = None) -> str:
    prefix = prefix or f"bb_{period}_{std_dev}"
    return f"""sma = df['close'].rolling(window={period}).mean()
std = df['close'].rolling(window={period}).std()
df['{prefix}_upper'] = sma + ({std_dev} * std)
df['{prefix}_lower'] = sma - ({std_dev} * std)
df['{prefix}_mid'] = sma"""


def _code_kdj(period: int = 9, signal_period: int = 3, prefix: str = None) -> str:
    prefix = prefix or f"kdj_{period}_{signal_period}"
    return f"""low_min = df['low'].rolling(window={period}).min()
high_max = df['high'].rolling(window={period}).max()
rsv = (df['close'] - low_min) / (high_max - low_min) * 100
df['{prefix}_k'] = rsv.ewm(alpha=1/{signal_period}, adjust=False).mean()
df['{prefix}_d'] = df['{prefix}_k'].ewm(alpha=1/{signal_period}, adjust=False).mean()
df['{prefix}_j'] = 3 * df['{prefix}_k'] - 2 * df['{prefix}_d']"""


def _code_supertrend(period: int = 14, multiplier: float = 3.0) -> str:
    return f"""period = {period}
multiplier = {multiplier}
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
df['st_lower'] = final_lower"""


def _code_atr(period: int = 14, col: str = None) -> str:
    col = col or f"atr_{period}"
    return f"""df['tr_{period}'] = np.maximum(df['high'] - df['low'], np.maximum(abs(df['high'] - df['close'].shift(1)), abs(df['low'] - df['close'].shift(1))))
df['{col}'] = df['tr_{period}'].ewm(alpha=1/{period}, adjust=False).mean()"""


def _code_volume_ma(period: int = 20) -> str:
    return f"""df['vol_ma_{period}'] = df['volume'].rolling(window={period}).mean()
df['vol_ratio_{period}'] = df['volume'] / df['vol_ma_{period}'].replace(0, np.nan)"""


def _code_donchian(upper_period: int = 20, lower_period: int = 10, prefix: str = None) -> str:
    prefix = prefix or f"dc_{upper_period}_{lower_period}"
    return f"""df['{prefix}_upper'] = df['high'].rolling(window={upper_period}).max()
df['{prefix}_lower'] = df['low'].rolling(window={lower_period}).min()
df['{prefix}_mid'] = (df['{prefix}_upper'] + df['{prefix}_lower']) / 2"""


# ============================================================
# 信号辅助
# ============================================================

def _edge_trigger(condition: str) -> str:
    """将条件表达式转为边沿触发（避免连续信号）"""
    return f"({condition}) & (~({condition}).shift(1).fillna(False))"


# ============================================================
# 标准模板渲染
# ============================================================

def render_indicator_strategy(
    name: str,
    description: str,
    params_decl: List[str],
    strategy_defaults: Dict[str, Any],
    indicator_code: str,
    buy_expr: str,
    sell_expr: str,
    plots: List[Dict[str, Any]],
    trade_direction: str = "long",
) -> str:
    """
    渲染标准 IndicatorStrategy 代码。

    Args:
        name: 策略名称
        description: 策略描述
        params_decl: # @param 声明列表
        strategy_defaults: # @strategy 键值对
        indicator_code: 指标计算代码
        buy_expr: 买入条件表达式（bool Series）
        sell_expr: 卖出条件表达式（bool Series）
        plots: 图表输出列表
        trade_direction: long / short / both

    Returns:
        完整的 IndicatorStrategy Python 代码
    """
    # 构建 @param 声明
    param_lines = "\n".join(f"# {d}" for d in params_decl) if params_decl else ""

    # 构建 @strategy 声明
    strategy_lines = ""
    if strategy_defaults:
        lines = []
        for k, v in strategy_defaults.items():
            if isinstance(v, bool):
                lines.append(f"# @strategy {k} {'true' if v else 'false'}")
            else:
                lines.append(f"# @strategy {k} {v}")
        strategy_lines = "\n".join(lines)

    # 构建 plots 代码
    plots_code = "[\n"
    for p in plots:
        plots_code += f"        {{\n"
        plots_code += f"            \"name\": \"{p['name']}\",\n"
        plots_code += f"            \"data\": {p['data']},\n"
        plots_code += f"            \"color\": \"{p['color']}\",\n"
        plots_code += f"            \"overlay\": {p.get('overlay', True)}\n"
        plots_code += f"        }},\n"
    plots_code += "    ]"

    # 拼接完整代码
    code = f'''my_indicator_name = "{name}"
my_indicator_description = """
{description}
"""

{param_lines}
{strategy_lines}

df = df.copy()

{indicator_code}

# ── 信号生成 ──
raw_buy = ({buy_expr})
raw_sell = ({sell_expr})

df['buy'] = (raw_buy.fillna(False) & (~raw_buy.shift(1).fillna(False))).astype(bool)
df['sell'] = (raw_sell.fillna(False) & (~raw_sell.shift(1).fillna(False))).astype(bool)

# ── A 股 T+1 过滤: 买入后不能立即卖出 ──
# 买入信号在 bar i 产生 → 回测引擎在 bar i+1 open 成交 → bar i+1 为 T+1 日不能卖
# 需要屏蔽买入信号后 1 根 bar 内的 sell 信号
_trade_dir = '{trade_direction}'
if _trade_dir in ('long', 'both', ''):
    _t1_mask = pd.Series(False, index=df.index)
    _buy_positions = df.index[df['buy']]
    for _bi in _buy_positions:
        _pos = df.index.get_loc(_bi)
        # 屏蔽 T+1 日（买入后 1 根 bar）的卖出信号
        if isinstance(_pos, int):
            _t1_end = min(_pos + 1 + 1, len(df))
            _t1_mask.iloc[_pos + 1:_t1_end] = True
        else:
            # slice (MultiIndex)
            _start = _pos.start + 1 if hasattr(_pos, 'start') else _pos + 1
            _end = min(_start + 1, len(df))
            _t1_mask.iloc[_start:_end] = True
    df['sell'] = df['sell'] & ~_t1_mask

# ── 图表标记 ──
buy_marks = [df['low'].iloc[i] * 0.995 if df['buy'].iloc[i] else None for i in range(len(df))]
sell_marks = [df['high'].iloc[i] * 1.005 if df['sell'].iloc[i] else None for i in range(len(df))]

output = {{
    "name": my_indicator_name,
    "plots": {plots_code},
    "signals": [
        {{
            "type": "buy",
            "text": "B",
            "data": buy_marks,
            "color": "#00E676"
        }},
        {{
            "type": "sell",
            "text": "S",
            "data": sell_marks,
            "color": "#FF5252"
        }}
    ]
}}
'''
    return code
