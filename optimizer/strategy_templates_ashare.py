"""
A 股扩展策略模板
由 LLM 基于现有模板模式批量生成，针对 A 股市场特点设计：
  - T+1 交易制度
  - 涨跌停限制（主板 10%，创业板/科创板 20%）
  - 最小交易单位 100 股
  - 换手率、量比等 A 股特色指标

策略清单：
  1. atr_breakout        - ATR 波动率突破
  2. volume_price_div    - 量价背离策略
  3. dual_ma_volume      - 双均线+成交量确认
  4. macd_kdj_resonance  - MACD+KDJ 共振
  6. price_channel        - 价格通道突破
  7. vwap_deviation       - VWAP 偏离策略
  8. ema_rsi_volume       - EMA+RSI+量能三重过滤
  9. kdj_macd_ma_triple  - KDJ+MACD+均线三重共振
"""
from typing import Dict, Any, List
from optimizer.indicator_strategy_builder import render_indicator_strategy

# 复用基础参数构建函数
def _p_int(low: int, high: int, step: int = 1) -> dict:
    return {"type": "int", "low": low, "high": high, "step": step}

def _p_float(low: float, high: float, step: float = 0.001) -> dict:
    return {"type": "float", "low": low, "high": high, "step": step}

def _p_choice(choices: list) -> dict:
    return {"type": "choice", "choices": choices}


# 小资金仓位档位
POSITION_PCT = _p_choice([100, 75, 50, 25])


# ============================================================
# 连板猎手 — 横向过滤 + 开板出场 (IndicatorStrategy)
# ============================================================

def _build_dragon_filter_strategy(p: dict) -> str:
    """连板猎手: 第一板涨停过滤 + 开板出场"""
    min_return = float(p.get('min_return', 20.0))
    max_seal = float(p.get('max_seal', 2.8))
    min_upper = float(p.get('min_upper', 2.0))
    max_upper = float(p.get('max_upper', 8.0))
    max_volatility = float(p.get('max_volatility', 10.0))
    stop_loss_pct = float(p.get('stop_loss_pct', 10.0))

    params_decl = [
        f"@param min_return float {min_return} 第一板最小涨幅%",
        f"@param max_seal float {max_seal} 封板强度上限% (close距low)",
        f"@param min_upper float {min_upper} 上影线下限% (排除一字板)",
        f"@param max_upper float {max_upper} 上影线上限% (排除冲高回落)",
        f"@param max_volatility float {max_volatility} 前5天波动率上限%",
        f"@param stop_loss_pct float {stop_loss_pct} 固定止损%",
    ]

    indicator_code = f"""# ── 当日涨幅 ──
df['change_pct'] = (df['close'] / df['close'].shift(1) - 1) * 100

# ── 封板强度: (close - low) / close * 100 ──
#   涨停封板: close ≈ high, low 接近 close → 值小(封得紧)
#   值越小封得越紧
_prev_close = df['close'].shift(1).replace(0, np.nan)
df['seal_pct'] = (df['close'] - df['low']) / df['close'] * 100
df['seal_pct'] = df['seal_pct'].fillna(999)

# ── 上影线%: (high - close) / prev_close * 100 ──
df['upper_shadow_pct'] = (df['high'] - df['close']) / _prev_close * 100

# ── 实体比: |close - open| / (high - low) ──
_bar_range = (df['high'] - df['low']).replace(0, np.nan)
df['body_ratio'] = (df['close'] - df['open']).abs() / _bar_range
df['body_ratio'] = df['body_ratio'].fillna(1.0)

# ── 5天波动率 ──
_ret = df['close'].pct_change()
df['volatility_5'] = _ret.rolling(window=5, min_periods=3).std() * 100"""

    # 买入: 第一板涨停 + 过滤条件
    # change_pct >= 9.5% 覆盖所有板块(主板10%/创业板科创板20%)
    buy_expr = (
        f"(df['change_pct'] >= 9.5)"
        f" & (df['change_pct'].shift(1) < 9.5)"       # 第一板（前一天不涨停）
        f" & (df['change_pct'] >= {min_return})"        # 涨幅阈值
        f" & (df['seal_pct'] <= {max_seal})"            # 封板强度
        f" & (df['body_ratio'] < 0.95)"                 # 排除一字板
        f" & (df['upper_shadow_pct'] >= {min_upper})"   # 上影线下限
        f" & (df['upper_shadow_pct'] <= {max_upper})"   # 上影线上限
        f" & (df['volatility_5'] <= {max_volatility})"  # 前5天波动
    )

    # 卖出: 开板（当天涨幅 < 8%，覆盖所有板块的非涨停状态）
    sell_expr = "(df['change_pct'] < 8.0)"

    plots = []

    return render_indicator_strategy(
        name="DragonFilter",
        description=f"连板猎手: 涨幅≥{min_return}% 封板≤{max_seal}% 上影≥{min_upper}% 波动≤{max_volatility}% → 开板卖出",
        params_decl=params_decl,
        strategy_defaults={"stopLossPct": stop_loss_pct / 100, "tradeDirection": "long"},
        indicator_code=indicator_code,
        buy_expr=buy_expr,
        sell_expr=sell_expr,
        plots=plots,
        trade_direction="long",
    )


# ============================================================
# 1. ATR 波动率突破
# ============================================================

def _build_atr_breakout_config(p: dict) -> dict:
    """ATR 通道突破 — 价格突破 N 倍 ATR 上轨做多"""
    entry_rules = [
        {
            "indicator": "atr_channel",
            "params": {
                "period": p["atr_period"],
                "multiplier": p["atr_multiplier"],
            },
            "operator": "price_above_upper",
        },
    ]
    if p.get("use_volume_confirm"):
        entry_rules.append({
            "indicator": "volume",
            "params": {"period": p.get("vol_ma_period", 20)},
            "operator": "volume_above_ma",
        })
    if p.get("use_trend_filter"):
        entry_rules.append({
            "indicator": "ma",
            "params": {"period": p["trend_ma_period"], "ma_type": "ema"},
            "operator": "price_above",
        })

    # 独立出场：价格跌破 ATR 下轨（趋势反转）
    exit_rules = [
        {
            "indicator": "atr_channel",
            "params": {
                "period": p["atr_period"],
                "multiplier": p["atr_multiplier"],
            },
            "operator": "price_below_lower",
        },
    ]

    return {
        "name": f"ATR_Breakout_{p['atr_period']}_{p['atr_multiplier']}",
        "entry_rules": entry_rules,
        "exit_rules": exit_rules,
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "type": "atr", "atr_period": p["atr_period"], "atr_multiplier": 2.0},
            "trailing_stop": {"enabled": True, "type": "atr", "atr_period": p["atr_period"], "atr_multiplier": p["atr_multiplier"]},
        },
    }


# ============================================================
# 2. 量价背离策略
# ============================================================

def _build_volume_price_divergence_config(p: dict) -> dict:
    """价格创新低但成交量萎缩（底背离），或价格创新高但成交量萎缩（顶背离）"""
    entry_rules = [
        {
            "indicator": "price_volume_divergence",
            "params": {
                "lookback": p["lookback_period"],
                "divergence_type": "bullish",
                "price_ma": p["price_ma_period"],
                "volume_ma": p["vol_ma_period"],
            },
            "operator": "bullish_divergence",
        },
        {
            "indicator": "rsi",
            "params": {"period": p["rsi_period"], "threshold": p["rsi_oversold"]},
            "operator": "<",
        },
    ]

    # 独立出场：RSI 超买（反弹到位）OR 价格跌破短期均线（趋势破坏）
    exit_rules = [
        {
            "indicator": "rsi",
            "params": {"period": p["rsi_period"], "threshold": p.get("exit_rsi_overbought", 65)},
            "operator": ">",
        },
        {
            "indicator": "ema",
            "params": {"period": p.get("exit_ema_period", 20)},
            "operator": "price_below",
        },
    ]

    return {
        "name": f"VolPriceDiv_{p['lookback_period']}_{p['rsi_period']}",
        "entry_rules": entry_rules,
        "exit_rules": exit_rules,
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "value": p.get("stop_loss_pct", 5.0)},
            "trailing_stop": {
                "enabled": True,
                "type": "trailing_pct",
                "activation_profit": p.get("trailing_activation", 8.0),
                "callback_pct": p.get("trailing_callback", 5.0),
            },
        },
    }


# ============================================================
# 3. 双均线 + 成交量确认
# ============================================================

def _build_dual_ma_volume_config(p: dict) -> dict:
    """快慢均线交叉 + 成交量放大确认"""
    fast_type = p.get("fast_type", "ema")
    slow_type = p.get("slow_type", "sma")

    entry_rules = [
        {
            "indicator": "ma",
            "params": {"period": p["fast_period"], "ma_type": fast_type},
            "operator": "cross_up",
        },
        {
            "indicator": "ma",
            "params": {"period": p["slow_period"], "ma_type": slow_type},
            "operator": "price_above",
        },
        {
            "indicator": "volume",
            "params": {"period": p["vol_ma_period"]},
            "operator": "volume_ratio_above",
            "threshold": p["vol_ratio"],
        },
    ]

    # 独立出场：价格跌破慢线（趋势破坏）
    exit_rules = [
        {
            "indicator": "ma",
            "params": {"period": p["slow_period"], "ma_type": slow_type},
            "operator": "price_below",
        },
    ]

    return {
        "name": f"DualMA_Vol_{fast_type}{p['fast_period']}_{slow_type}{p['slow_period']}",
        "entry_rules": entry_rules,
        "exit_rules": exit_rules,
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "value": p.get("stop_loss_pct", 5.0)},
            "trailing_stop": {
                "enabled": True,
                "type": "trailing_pct",
                "activation_profit": p.get("trailing_activation", 8.0),
                "callback_pct": p.get("trailing_callback", 5.0),
            },
        },
    }


def _build_dual_ma_volume_strategy(p: dict) -> str:
    """双均线量能确认 — IndicatorStrategy 标准格式"""
    fast_type = p.get("fast_type", "ema")
    slow_type = p.get("slow_type", "sma")
    fast_period = int(p["fast_period"])
    slow_period = int(p["slow_period"])
    vol_ma_period = int(p.get("vol_ma_period", 20))
    vol_ratio = float(p.get("vol_ratio", 1.5))
    stop_loss = float(p.get("stop_loss_pct", 5.0))

    params_decl = [
        f"@param fast_period int {fast_period} 快线周期",
        f"@param slow_period int {slow_period} 慢线周期",
        f"@param fast_type str {fast_type} 快线类型 (sma/ema)",
        f"@param slow_type str {slow_type} 慢线类型 (sma/ema)",
        f"@param vol_ma_period int {vol_ma_period} 量能均线周期",
        f"@param vol_ratio float {vol_ratio} 量比阈值",
    ]

    ind_code = f"""fast_len = int(params.get('fast_period', {fast_period}))
slow_len = int(params.get('slow_period', {slow_period}))
fast_type = str(params.get('fast_type', '{fast_type}'))
slow_type = str(params.get('slow_type', '{slow_type}'))
vol_ma_period = int(params.get('vol_ma_period', {vol_ma_period}))
vol_ratio = float(params.get('vol_ratio', {vol_ratio}))

if fast_type == 'ema':
    ma_fast = df['close'].ewm(span=fast_len, adjust=False).mean()
else:
    ma_fast = df['close'].rolling(window=fast_len).mean()

if slow_type == 'ema':
    ma_slow = df['close'].ewm(span=slow_len, adjust=False).mean()
else:
    ma_slow = df['close'].rolling(window=slow_len).mean()

vol_ma = df['volume'].rolling(window=vol_ma_period).mean()
vol_ratio_cur = df['volume'] / vol_ma.replace(0, np.nan)"""

    buy_expr = "(ma_fast > ma_slow) & (ma_fast.shift(1) <= ma_slow.shift(1)) & (vol_ratio_cur > vol_ratio)"
    sell_expr = "(ma_fast < ma_slow) & (ma_fast.shift(1) >= ma_slow.shift(1))"

    plots = [
        {"name": f"MA Fast ({fast_type} {fast_period})", "data": "ma_fast.fillna(0).tolist()", "color": "#1890ff"},
        {"name": f"MA Slow ({slow_type} {slow_period})", "data": "ma_slow.fillna(0).tolist()", "color": "#faad14"},
    ]

    from optimizer.indicator_strategy_builder import render_indicator_strategy
    return render_indicator_strategy(
        name=f"DualMA_Vol_{fast_type}{fast_period}_{slow_type}{slow_period}",
        description=f"双均线量能确认: {fast_type.upper()}{fast_period} 上穿 {slow_type.upper()}{slow_period} + 量比>{vol_ratio} 做多，下穿做空。",
        params_decl=params_decl,
        strategy_defaults={"stopLossPct": stop_loss / 100, "takeProfitPct": 0.10, "tradeDirection": "long"},
        indicator_code=ind_code,
        buy_expr=buy_expr,
        sell_expr=sell_expr,
        plots=plots,
    )


# ============================================================
# 4. MACD + KDJ 共振
# ============================================================

def _build_macd_kdj_resonance_config(p: dict) -> dict:
    """MACD 金叉 + KDJ 共振，双重确认入场 + 独立出场规则"""
    entry_rules = [
        {
            "indicator": "macd",
            "params": {
                "fast_period": p["macd_fast"],
                "slow_period": p["macd_slow"],
                "signal_period": p["macd_signal"],
            },
            "operator": "diff_gt_dea",  # MACD diff 在 DEA 上方（状态持续，信号更多）
        },
        {
            "indicator": "kdj",
            "params": {"period": p["kdj_period"], "signal_period": p["kdj_signal"]},
            "operator": "k_gt_d",  # K 值在 D 值上方（状态持续）
        },
    ]
    if p.get("use_ma_filter"):
        entry_rules.append({
            "indicator": "ma",
            "params": {"period": p["ma_filter_period"], "ma_type": "ema"},
            "operator": "price_above",
        })

    # 独立出场规则：与入场逻辑解耦
    # 设计原则：出场比入场宽松，不要被正常回调洗出去
    #
    # 只保留 MACD 死叉状态作为信号出场（diff < DEA）
    # - 与入场的 diff_gt_dea 对称，趋势真正反转才出场
    # - 去掉 EMA 出场：震荡市 EMA 假突破太多，加了反而增加噪音
    # - 风控（止损/追踪止损）兜底，不需要 EMA 再兜一层
    exit_rules = [
        {
            "indicator": "macd",
            "params": {
                "fast_period": p["macd_fast"],
                "slow_period": p["macd_slow"],
                "signal_period": p["macd_signal"],
            },
            "operator": "diff_lt_dea",  # MACD diff 跌破 DEA（状态持续）
        },
    ]

    return {
        "name": f"MACD_KDJ_{p['macd_fast']}_{p['kdj_period']}",
        "entry_rules": entry_rules,
        "exit_rules": exit_rules,
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "type": "percentage", "value": p.get("stop_loss_pct", 10.0)},
            "take_profit": {"enabled": True, "type": "percentage", "value": p.get("take_profit_pct", 25.0)},
            "trailing_stop": {
                "enabled": True,
                "type": "trailing_pct",
                "activation_profit": p.get("trailing_activation", 10.0),  # 盈利10%后激活
                "callback_pct": p.get("trailing_callback", 8.0),         # 从最高点回撤8%出场
            },
            "ashare_rules": {"t_plus_1": True, "price_limit": True, "min_lot": 100},
        },
    }


# ============================================================
# 6. 价格通道突破（Donchian Channel）
# ============================================================

def _build_price_channel_config(p: dict) -> dict:
    """Donchian 通道 N 日高点突破入场"""
    entry_rules = [
        {
            "indicator": "donchian_channel",
            "params": {
                "upper_period": p["entry_period"],
                "lower_period": p["exit_period"],
            },
            "operator": "price_break_upper",
        },
    ]
    if p.get("use_volume_filter"):
        entry_rules.append({
            "indicator": "volume",
            "params": {"period": 20},
            "operator": "volume_ratio_above",
            "threshold": 1.5,
        })

    # 独立出场：跌破 Donchian 下轨（趋势反转）OR 跌破 EMA 中轨
    exit_rules = [
        {
            "indicator": "donchian_channel",
            "params": {
                "upper_period": p["entry_period"],
                "lower_period": p["exit_period"],
            },
            "operator": "price_break_lower",
        },
        {
            "indicator": "ema",
            "params": {"period": p.get("exit_ema_period", 20)},
            "operator": "price_below",
        },
    ]

    return {
        "name": f"PriceChannel_{p['entry_period']}_{p['exit_period']}",
        "entry_rules": entry_rules,
        "exit_rules": exit_rules,
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "type": "donchian", "period": p["exit_period"]},
            "trailing_stop": {"enabled": True, "type": "donchian", "period": p["exit_period"]},
        },
    }


# ============================================================
# 7. VWAP 偏离策略
# ============================================================

def _build_vwap_deviation_config(p: dict) -> dict:
    """价格偏离 VWAP 超过阈值时反转入场（均值回归）"""
    entry_rules = [
        {
            "indicator": "vwap",
            "params": {"deviation_pct": p["deviation_pct"]},
            "operator": "price_below_vwap_by",
        },
        {
            "indicator": "rsi",
            "params": {"period": p["rsi_period"], "threshold": p["rsi_level"]},
            "operator": "<",
        },
    ]

    # 独立出场：RSI 超买（均值回归完成）OR 价格回到 EMA 上方（趋势恢复）
    exit_rules = [
        {
            "indicator": "rsi",
            "params": {"period": p["rsi_period"], "threshold": p.get("exit_rsi_overbought", 60)},
            "operator": ">",
        },
        {
            "indicator": "ema",
            "params": {"period": p.get("exit_ema_period", 20)},
            "operator": "price_above",
        },
    ]

    return {
        "name": f"VWAP_Dev_{p['deviation_pct']}_{p['rsi_period']}",
        "entry_rules": entry_rules,
        "exit_rules": exit_rules,
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "value": p.get("stop_loss_pct", 3.0)},
            "trailing_stop": {
                "enabled": True,
                "type": "trailing_pct",
                "activation_profit": p.get("trailing_activation", 5.0),
                "callback_pct": p.get("trailing_callback", 3.0),
            },
        },
    }


# ============================================================
# 9. EMA + RSI + 量能三重过滤
# ============================================================

def _build_ema_rsi_volume_config(p: dict) -> dict:
    """EMA 趋势方向 + RSI 超卖回升 + 成交量放大确认"""
    entry_rules = [
        {
            "indicator": "ema",
            "params": {"period": p["ema_period"]},
            "operator": "price_above",
        },
        {
            "indicator": "rsi",
            "params": {"period": p["rsi_period"], "threshold": p["rsi_entry"]},
            "operator": "cross_up",
        },
        {
            "indicator": "volume",
            "params": {"period": p["vol_ma_period"]},
            "operator": "volume_ratio_above",
            "threshold": p["vol_ratio"],
        },
    ]

    # 独立出场：价格跌破 EMA（趋势破坏）OR RSI 跌破阈值（动量衰竭）
    exit_rules = [
        {
            "indicator": "ema",
            "params": {"period": p["ema_period"]},
            "operator": "price_below",
        },
        {
            "indicator": "rsi",
            "params": {"period": p["rsi_period"], "threshold": p.get("exit_rsi_threshold", 35)},
            "operator": "<",
        },
    ]

    return {
        "name": f"EMA_RSI_Vol_{p['ema_period']}_{p['rsi_period']}",
        "entry_rules": entry_rules,
        "exit_rules": exit_rules,
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "value": p.get("stop_loss_pct", 5.0)},
            "trailing_stop": {
                "enabled": True,
                "type": "trailing_pct",
                "activation_profit": p.get("trailing_activation", 8.0),
                "callback_pct": p.get("trailing_callback", 5.0),
            },
        },
    }


# ============================================================
# 10. KDJ + MACD + 均线三重共振
# ============================================================

def _build_kdj_macd_ma_triple_config(p: dict) -> dict:
    """三重指标共振：KDJ K>D + MACD 柱状线翻红 + 价格在均线上方"""
    entry_rules = [
        {
            "indicator": "kdj",
            "params": {"period": p["kdj_period"], "signal_period": p["kdj_signal"]},
            "operator": "k_gt_d",  # K > D（状态持续）
        },
        {
            "indicator": "macd",
            "params": {
                "fast_period": p["macd_fast"],
                "slow_period": p["macd_slow"],
                "signal_period": p["macd_signal"],
            },
            "operator": "histogram_positive",
        },
        {
            "indicator": "ma",
            "params": {"period": p["ma_period"], "ma_type": p.get("ma_type", "ema")},
            "operator": "price_above",
        },
    ]

    # 独立出场：KDJ 死叉 OR MACD 柱状线翻绿（动量反转）
    exit_rules = [
        {
            "indicator": "kdj",
            "params": {"period": p["kdj_period"], "signal_period": p["kdj_signal"]},
            "operator": "k_lt_d",
        },
        {
            "indicator": "macd",
            "params": {
                "fast_period": p["macd_fast"],
                "slow_period": p["macd_slow"],
                "signal_period": p["macd_signal"],
            },
            "operator": "histogram_negative",
        },
    ]

    return {
        "name": f"Triple_{p['kdj_period']}_{p['macd_fast']}_{p['ma_period']}",
        "entry_rules": entry_rules,
        "exit_rules": exit_rules,
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "value": p.get("stop_loss_pct", 5.0)},
            "trailing_stop": {
                "enabled": True,
                "type": "trailing_pct",
                "activation_profit": p.get("trailing_activation", 8.0),
                "callback_pct": p.get("trailing_callback", 5.0),
            },
        },
    }


# ============================================================
# 策略模板注册表
# ============================================================

ASHARE_STRATEGY_TEMPLATES: Dict[str, Dict[str, Any]] = {

    # ── 1. ATR 波动率突破 ──
    "atr_breakout": {
        "name": "ATR 波动率突破",
        "description": "价格突破 N 倍 ATR 通道上轨时入场，适合趋势行情。A 股中常用于捕捉主升浪启动。",
        "indicators": ["atr", "volume", "ma"],
        "params": {
            "atr_period":        _p_int(10, 30, 1),
            "atr_multiplier":    _p_float(1.5, 4.0, 0.1),
            "use_volume_confirm": _p_choice([True, False]),
            "vol_ma_period":     _p_int(10, 30, 1),
            "use_trend_filter":  _p_choice([True, False]),
            "trend_ma_period":   _p_int(20, 120, 10),
            "position_pct": POSITION_PCT,
        },
        "constraints": [],
        "build_config": _build_atr_breakout_config,
        "strategy_defaults": {"tradeDirection": "long"},
    },

    # ── 2. 量价背离 ──
    "volume_price_divergence": {
        "name": "量价底背离",
        "description": "价格创新低但成交量持续萎缩，配合 RSI 超卖，捕捉底部反转机会。A 股底部形态经典信号。",
        "indicators": ["volume", "rsi", "ma"],
        "params": {
            "lookback_period":  _p_int(10, 40, 1),
            "price_ma_period":  _p_int(5, 20, 1),
            "vol_ma_period":    _p_int(10, 30, 1),
            "rsi_period":       _p_int(7, 21, 1),
            "rsi_oversold":     _p_int(20, 40, 1),
            "stop_loss_pct":    _p_float(5.0, 15.0, 0.5),
            "exit_rsi_overbought": _p_int(55, 75, 1),
            "exit_ema_period": _p_int(10, 30, 5),
            "trailing_activation": _p_float(5.0, 15.0, 1.0),
            "trailing_callback": _p_float(3.0, 8.0, 0.5),
            "position_pct": POSITION_PCT,
        },
        "constraints": [],
        "build_config": _build_volume_price_divergence_config,
        "strategy_defaults": {"tradeDirection": "long"},
    },

    # ── 3. 双均线 + 成交量 ──
    "dual_ma_volume": {
        "name": "双均线量能确认",
        "description": "快慢均线金叉 + 成交量放大确认，过滤假突破。A 股量价配合是核心逻辑。",
        "indicators": ["ma", "volume"],
        "params": {
            "fast_period":    _p_int(5, 30, 1),
            "slow_period":    _p_int(20, 120, 1),
            "fast_type":      _p_choice(["sma", "ema"]),
            "slow_type":      _p_choice(["sma", "ema"]),
            "vol_ma_period":  _p_int(10, 30, 1),
            "vol_ratio":      _p_float(1.2, 3.0, 0.1),
            "stop_loss_pct":  _p_float(5.0, 15.0, 0.5),
            "trailing_activation": _p_float(5.0, 15.0, 1.0),
            "trailing_callback": _p_float(3.0, 8.0, 0.5),
            "position_pct": POSITION_PCT,
        },
        "constraints": [
            ("fast_period", "<", "slow_period"),
        ],
        "build_config": _build_dual_ma_volume_config,
        "build_strategy": _build_dual_ma_volume_strategy,
        "strategy_defaults": {"tradeDirection": "long"},
    },

    # ── 4. MACD + KDJ 共振 ──
    "macd_kdj_resonance": {
        "name": "MACD+KDJ 共振",
        "description": "MACD 金叉与 KDJ 金叉共振确认，双重指标过滤假信号。A 股技术派常用组合。",
        "indicators": ["macd", "kdj", "ma"],
        "params": {
            "macd_fast":       _p_int(8, 16, 1),
            "macd_slow":       _p_int(20, 35, 1),
            "macd_signal":     _p_int(5, 12, 1),
            "kdj_period":      _p_int(5, 21, 1),
            "kdj_signal":      _p_int(2, 5, 1),
            "use_ma_filter":   _p_choice([True, False]),
            "ma_filter_period": _p_int(20, 120, 10),
            "stop_loss_pct":   _p_float(5.0, 15.0, 0.5),
            "take_profit_pct": _p_float(15.0, 40.0, 1.0),
            "trailing_activation": _p_float(5.0, 15.0, 1.0),
            "trailing_callback": _p_float(5.0, 12.0, 0.5),
            "position_pct": POSITION_PCT,
        },
        "constraints": [
            ("macd_fast", "<", "macd_slow"),
            ("trailing_callback", "<", "trailing_activation"),
        ],
        "build_config": _build_macd_kdj_resonance_config,
        "strategy_defaults": {"tradeDirection": "long"},
    },

    # ── 6. 价格通道突破 ──
    "price_channel": {
        "name": "Donchian 通道突破",
        "description": "价格突破 N 日最高价入场，跌破 M 日最低价出场。经典趋势跟踪策略。",
        "indicators": ["donchian_channel", "volume"],
        "params": {
            "entry_period":      _p_int(10, 60, 5),
            "exit_period":       _p_int(5, 30, 5),
            "use_volume_filter": _p_choice([True, False]),
            "exit_ema_period": _p_int(10, 30, 5),
            "position_pct": POSITION_PCT,
        },
        "constraints": [
            ("exit_period", "<", "entry_period"),
        ],
        "build_config": _build_price_channel_config,
        "strategy_defaults": {"tradeDirection": "long"},
    },

    # ── 7. VWAP 偏离 ──
    "vwap_deviation": {
        "name": "VWAP 偏离回归",
        "description": "价格偏离 VWAP 超过阈值时做均值回归。适合 A 股日内或短线交易，机构资金参考 VWAP 较多。",
        "indicators": ["vwap", "rsi"],
        "params": {
            "deviation_pct":  _p_float(1.0, 5.0, 0.1),
            "rsi_period":     _p_int(7, 21, 1),
            "rsi_level":      _p_int(25, 45, 1),
            "stop_loss_pct":  _p_float(5.0, 12.0, 0.5),
            "exit_rsi_overbought": _p_int(50, 70, 1),
            "exit_ema_period": _p_int(10, 30, 5),
            "trailing_activation": _p_float(3.0, 10.0, 1.0),
            "trailing_callback": _p_float(2.0, 6.0, 0.5),
            "position_pct": POSITION_PCT,
        },
        "constraints": [],
        "build_config": _build_vwap_deviation_config,
        "strategy_defaults": {"tradeDirection": "long"},
    },

    # ── 9. EMA + RSI + 量能三重过滤 ──
    "ema_rsi_volume": {
        "name": "EMA+RSI+量能三重过滤",
        "description": "EMA 判断趋势方向 + RSI 超卖回升 + 成交量放大确认。三重过滤提高胜率，A 股实战常用。",
        "indicators": ["ema", "rsi", "volume"],
        "params": {
            "ema_period":     _p_int(20, 120, 10),
            "rsi_period":     _p_int(7, 21, 1),
            "rsi_entry":      _p_int(25, 45, 1),
            "vol_ma_period":  _p_int(10, 30, 1),
            "vol_ratio":      _p_float(1.2, 3.0, 0.1),
            "stop_loss_pct":  _p_float(5.0, 15.0, 0.5),
            "exit_rsi_threshold": _p_int(25, 40, 1),
            "trailing_activation": _p_float(5.0, 15.0, 1.0),
            "trailing_callback": _p_float(3.0, 8.0, 0.5),
            "position_pct": POSITION_PCT,
        },
        "constraints": [],
        "build_config": _build_ema_rsi_volume_config,
        "strategy_defaults": {"tradeDirection": "long"},
    },

    # ── 10. KDJ + MACD + 均线三重共振 ──
    "kdj_macd_ma_triple": {
        "name": "KDJ+MACD+均线三重共振",
        "description": "KDJ 金叉 + MACD 柱状线翻红 + 价格站上均线，三重共振确认。A 股中线买入信号的经典组合。",
        "indicators": ["kdj", "macd", "ma"],
        "params": {
            "kdj_period":    _p_int(5, 21, 1),
            "kdj_signal":    _p_int(2, 5, 1),
            "macd_fast":     _p_int(8, 16, 1),
            "macd_slow":     _p_int(20, 35, 1),
            "macd_signal":   _p_int(5, 12, 1),
            "ma_period":     _p_int(10, 60, 5),
            "ma_type":       _p_choice(["sma", "ema"]),
            "stop_loss_pct": _p_float(5.0, 15.0, 0.5),
            "trailing_activation": _p_float(5.0, 15.0, 1.0),
            "trailing_callback": _p_float(3.0, 8.0, 0.5),
            "position_pct": POSITION_PCT,
        },
        "constraints": [
            ("macd_fast", "<", "macd_slow"),
        ],
        "build_config": _build_kdj_macd_ma_triple_config,
        "strategy_defaults": {"tradeDirection": "long"},
    },

    # ── 11. 连板猎手 ──
    "dragon_filter": {
        "name": "连板猎手",
        "description": "第一板涨停过滤(涨幅/封板/上影/波动) + 开板出场。横向过滤提纯连板信号。",
        "indicators": ["change_pct", "close_position"],
        "params": {
            "min_return":      _p_float(10.0, 30.0, 1.0),
            "max_seal":        _p_float(1.0, 5.0, 0.5),
            "min_upper":       _p_float(0.5, 5.0, 0.5),
            "max_upper":       _p_float(5.0, 15.0, 1.0),
            "max_volatility":  _p_float(3.0, 15.0, 1.0),
            "stop_loss_pct":   _p_float(5.0, 15.0, 0.5),
            "position_pct": POSITION_PCT,
        },
        "constraints": [],
        "build_strategy": _build_dragon_filter_strategy,
        "strategy_defaults": {"tradeDirection": "long"},
    },
}
