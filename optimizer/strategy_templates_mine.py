"""
自定义中短线策略模板（5 个）
针对 A 股 T+1、涨跌停、量价关系设计

策略清单：
  1. vol_price_resonance   - 量价共振突破（超短 1-3 天）
  2. trend_pullback_buy    - 趋势回踩买入（短中线 3-10 天）
  3. limit_up_next_day     - 涨停板次日博弈（超短 T+1）
  4. low_vol_reversal      - 低波反转（中线 5-20 天）
  5. dragon_pullback       - 龙回头（超短 2-5 天）

数据验证结果（基于 stock_stats.csv 5127 只股票）：
  策略1: 104 只选股池, 年化中位数 23.0%, 88% 胜率（全市场 12.3%, 78%）
  策略2: 34 只选股池, 年化中位数 71.2%, 100% 胜率, Sharpe 1.34
  策略3: 126 只选股池, 涨停中位数 29 次, 振幅 3.73%
  策略4: 全市场实时扫描（BB 带宽收缩信号，不做 stock_stats 预筛选）
  策略5: 518 只龙头池, 年化中位数 32.0%, Sharpe 0.60

编译器兼容性：
  - 所有指标名、运算符均与 strategy_compiler.py 一致
  - 新增指标: change_pct, open_gap, period_return, drawdown_from_high
  - 新增运算符: between, price_near
"""
from typing import Dict, Any


def _p_int(low: int, high: int, step: int = 1) -> dict:
    return {"type": "int", "low": low, "high": high, "step": step}


def _p_float(low: float, high: float, step: float = 0.001) -> dict:
    return {"type": "float", "low": low, "high": high, "step": step}


def _p_choice(choices: list) -> dict:
    return {"type": "choice", "choices": choices}


# ============================================================
# 1. 量价共振突破（超短 1-3 天）
# ============================================================
# 数据验证：
#   stock_stats.csv 中满足 short_score>0.85 + vol_price_corr>0.3 + vol_cv>1.0
#   的股票共 105 只，年化收益中位数 23.0%（全市场 12.3%），88% 胜率。
#   策略在此选股池基础上叠加实时突破信号（Donchian 通道 + 放量），
#   进一步过滤假突破，提升入场质量。
#
# 参数设计依据：
#   - vol_ratio 1.5-3.5: 对应 vol_cv 分布 p25-p75（0.855-1.27）
#   - breakout_period 5-30: 覆盖超短到短周期突破
#   - min/max_change_pct: 排除弱势股和涨停板，聚焦主升浪启动区间

def _build_vol_price_resonance_config(p: dict) -> dict:
    """量价共振突破"""
    entry_rules = [
        # 价格突破 N 日高点（前一根 K 线的通道，避免前瞻偏差）
        {
            "indicator": "donchian_channel",
            "params": {"upper_period": p["breakout_period"], "lower_period": p["breakout_period"]},
            "operator": "price_break_upper",
        },
        # 成交量放大确认
        {
            "indicator": "volume",
            "params": {"period": p["vol_ma_period"]},
            "operator": "volume_ratio_above",
            "threshold": p["vol_ratio"],
        },
        # 近期涨幅下限：排除弱势股
        {
            "indicator": "period_return",
            "params": {"lookback": 5},
            "operator": ">=",
            "threshold": p["min_change_pct"],
        },
        # 近期涨幅上限：不追涨停板
        {
            "indicator": "period_return",
            "params": {"lookback": 5},
            "operator": "<=",
            "threshold": p["max_change_pct"],
        },
    ]

    if p.get("use_trend_filter"):
        entry_rules.append({
            "indicator": "ema",
            "params": {"period": p["trend_ema_period"]},
            "operator": "price_above",
        })

    return {
        "name": f"VolPriceRes_{p['breakout_period']}_{p['vol_ratio']}",
        "entry_rules": entry_rules,
        "position_config": {
            "initial_size_pct": 100,
            "leverage": 1,
            "max_pyramiding": 0,
        },
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "type": "percentage", "value": p["stop_loss_pct"]},
            "take_profit": {"enabled": True, "type": "percentage", "value": p["take_profit_pct"]},
            "trailing_stop": {"enabled": True, "type": "trailing_pct", "value": p.get("trailing_pct", 5.0)},
            "ashare_rules": {"t_plus_1": True, "price_limit": True, "min_lot": 100},
        },
    }


# ============================================================
# 2. 趋势回踩买入（短中线 3-10 天）
# ============================================================
# 数据验证：
#   stock_stats.csv 中满足 trend_score>0.85 + sharpe>1.0 的股票共 34 只，
#   年化收益中位数 71.2%（全市场 12.3%），100% 胜率，Sharpe 中位数 1.34。
#   这是数据支撑最强的策略。
#
#   ⚠️ 注意：trend_score>0.85 是 p98 分位，池子偏小。
#   实际回测时建议先用 trend_score>0.70 做粗筛（约 300 只），
#   再由实时 EMA + RSI 信号做精筛。
#
# 参数设计依据：
#   - trend_ema_period 15-60: 覆盖短中期趋势
#   - rsi_low/high 35-65: 趋势中回调的合理区间（非超卖区）
#   - pullback_pct 1-5%: 回踩偏离度，对应 A 股正常回调幅度

def _build_trend_pullback_buy_config(p: dict) -> dict:
    """趋势回踩买入"""
    entry_rules = [
        # 价格在 EMA 上方（确认上升趋势）
        {
            "indicator": "ema",
            "params": {"period": p["trend_ema_period"]},
            "operator": "price_above",
        },
        # 价格回踩到 EMA 附近
        {
            "indicator": "ema",
            "params": {"period": p["trend_ema_period"]},
            "operator": "price_near",
            "threshold": p["pullback_pct"],
        },
        # RSI < 上限（不是超买区）
        {
            "indicator": "rsi",
            "params": {"period": p["rsi_period"], "threshold": p["rsi_high"]},
            "operator": "<",
        },
        # RSI > 下限（不是超卖区，是趋势中的回调）
        {
            "indicator": "rsi",
            "params": {"period": p["rsi_period"], "threshold": p["rsi_low"]},
            "operator": ">",
        },
    ]

    if p.get("use_vol_shrink"):
        entry_rules.append({
            "indicator": "volume",
            "params": {"period": p["vol_ma_period"]},
            "operator": "volume_ratio_below",
            "threshold": p["vol_shrink_ratio"],
        })

    return {
        "name": f"TrendPB_{p['trend_ema_period']}_{p['rsi_period']}",
        "entry_rules": entry_rules,
        "position_config": {
            "initial_size_pct": 100,
            "leverage": 1,
            "max_pyramiding": 0,
        },
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "type": "percentage", "value": p["stop_loss_pct"]},
            "trailing_stop": {"enabled": True, "type": "trailing_pct", "value": p.get("trailing_pct", 4.0)},
            "ashare_rules": {"t_plus_1": True, "price_limit": True, "min_lot": 100},
        },
    }


# ============================================================
# 3. 涨停板次日博弈（超短 T+1）
# ============================================================
# 数据验证：
#   stock_stats.csv 中满足 limit_up_count>=20 + autocorr>0.1 + amplitude>0.03
#   + limit_down_count<=10 的股票共 126 只。
#   涨停次数中位数 29，振幅中位数 3.73%，自相关中位数 0.119。
#   说明这个池子里的股票有涨停基因、有日内空间、有动量惯性。
#
# 参数设计依据：
#   - limitup_detect: 用编译器内置的涨停检测（近60日涨幅排名前5%）
#   - min_open_gap 0.5-2%: A 股正常低开幅度
#   - turn_positive_pct 0-2%: 翻红涨幅，振幅中位数 3.73% 确保有空间
#   - position_pct 30-70: 半仓博弈，控制风险

def _build_limit_up_next_day_config(p: dict) -> dict:
    """涨停板次日博弈"""
    entry_rules = [
        # 前一日涨停
        {
            "indicator": "limitup_detect",
            "params": {"lookback": 60, "top_pct": 5},
            "operator": "is_limitup",
        },
        # 当日低开
        {
            "indicator": "open_gap",
            "params": {},
            "operator": "<",
            "threshold": -p["min_open_gap"],
        },
        # 当日翻红
        {
            "indicator": "change_pct",
            "params": {},
            "operator": ">=",
            "threshold": p["turn_positive_pct"],
        },
    ]

    if p.get("use_vol_confirm"):
        entry_rules.append({
            "indicator": "volume",
            "params": {"period": p["vol_ma_period"]},
            "operator": "volume_ratio_above",
            "threshold": p["vol_ratio"],
        })

    return {
        "name": f"LimitUpND_{p['min_open_gap']}_{p['turn_positive_pct']}",
        "entry_rules": entry_rules,
        "position_config": {
            "initial_size_pct": p.get("position_pct", 50),
            "leverage": 1,
            "max_pyramiding": 0,
        },
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "type": "percentage", "value": p["stop_loss_pct"]},
            "take_profit": {"enabled": True, "type": "percentage", "value": p["take_profit_pct"]},
            "ashare_rules": {"t_plus_1": True, "price_limit": True, "min_lot": 100},
        },
    }


# ============================================================
# 4. 低波反转（中线 5-20 天）
# ============================================================
# ⚠️ 数据验证结论：
#   stock_stats.csv 中 daily_vol<0.03 + vol_cv<0.8 的股票共 528 只，
#   但年化收益中位数仅 4.5%（全市场 12.3%），Sharpe 0.15（全市场 0.26）。
#   结论：低波股票在 A 股是弱势信号，不是蓄势信号。
#
#   因此本策略不做 stock_stats 预筛选，而是全市场实时扫描：
#   用 BB 带宽收缩（实时计算）识别波动率收敛到极致的股票，
#   再叠加放量突破 + RSI 脱离超卖作为反转确认。
#
#   这样既能捕捉真正的低波蓄势股，又不会被长期弱势股干扰。
#
# 参数设计依据：
#   - squeeze_percentile 10-30: BB 带宽处于历史低位（蓄势）
#   - vol_ratio 1.3-2.5: 突破时放量确认
#   - rsi_exit_oversold 25-45: RSI 从超卖区回升

def _build_low_vol_reversal_config(p: dict) -> dict:
    """低波反转 — 全市场实时信号扫描"""
    entry_rules = [
        # 波动率收敛：BB 带宽处于历史低位
        {
            "indicator": "bollinger_bandwidth",
            "params": {
                "period": p["bb_period"],
                "std_dev": p["bb_std"],
                "squeeze_percentile": p["squeeze_percentile"],
            },
            "operator": "below_percentile",
        },
        # 放量突破
        {
            "indicator": "volume",
            "params": {"period": p["vol_ma_period"]},
            "operator": "volume_ratio_above",
            "threshold": p["vol_ratio"],
        },
        # 价格站上短期均线
        {
            "indicator": "ma",
            "params": {"period": p["ma_period"], "ma_type": "sma"},
            "operator": "price_above",
        },
        # RSI 脱离超卖区
        {
            "indicator": "rsi",
            "params": {"period": p["rsi_period"], "threshold": p["rsi_exit_oversold"]},
            "operator": "cross_up",
        },
    ]

    return {
        "name": f"LowVolRev_{p['bb_period']}_{p['squeeze_percentile']}",
        "entry_rules": entry_rules,
        "position_config": {
            "initial_size_pct": p.get("position_pct", 60),
            "leverage": 1,
            "max_pyramiding": 0,
        },
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "type": "percentage", "value": p["stop_loss_pct"]},
            "take_profit": {"enabled": True, "type": "percentage", "value": p["take_profit_pct"]},
            "trailing_stop": {"enabled": True, "type": "trailing_pct", "value": p.get("trailing_pct", 6.0)},
            "ashare_rules": {"t_plus_1": True, "price_limit": True, "min_lot": 100},
        },
    }


# ============================================================
# 5. 龙回头（超短 2-5 天）
# ============================================================
# 数据验证：
#   stock_stats.csv 中 limit_up_count>=30 的龙头股共 1082 只，
#   年化收益中位数 25.6%（全市场 12.3%），Sharpe 0.48（全市场 0.26）。
#   100% 盈利，说明龙头股整体表现远超市场。
#
#   但回撤中位数 -68.1%，说明龙头股波动极大。
#   龙回头策略的核心是：等龙头回调到支撑位再介入，不追高。
#
# 参数设计依据：
#   - lookback_period 10-40: 前期涨幅观察窗口
#   - pullback_min/max 8-35%: 回调区间，过浅没回调够，过深趋势破了
#   - vol_shrink_ratio 0.3-0.7: 回调缩量说明主力没走
#   - rsi_bounce 25-45: RSI 从超卖区回升

def _build_dragon_pullback_config(p: dict) -> dict:
    """龙回头"""
    entry_rules = [
        # 大涨后回调到买入区间
        {
            "indicator": "dragon_pullback",
            "params": {
                "high_lookback": p["lookback_period"],
                "pullback_min": p["pullback_min_pct"] / 100.0,
                "pullback_max": p["pullback_max_pct"] / 100.0,
            },
            "operator": "in_pullback_zone",
        },
        # 回调缩量
        {
            "indicator": "volume",
            "params": {"period": p["vol_ma_period"]},
            "operator": "volume_ratio_below",
            "threshold": p["vol_shrink_ratio"],
        },
        # RSI 从超卖区回升
        {
            "indicator": "rsi",
            "params": {"period": p["rsi_period"], "threshold": p["rsi_bounce"]},
            "operator": "cross_up",
        },
    ]

    if p.get("use_ma_support"):
        entry_rules.append({
            "indicator": "ema",
            "params": {"period": p["ma_support_period"]},
            "operator": "price_near",
            "threshold": p["ma_near_pct"],
        })

    return {
        "name": f"DragonPB_{p['lookback_period']}_{p['pullback_min_pct']}",
        "entry_rules": entry_rules,
        "position_config": {
            "initial_size_pct": p.get("position_pct", 70),
            "leverage": 1,
            "max_pyramiding": 0,
        },
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "type": "percentage", "value": p["stop_loss_pct"]},
            "take_profit": {"enabled": True, "type": "percentage", "value": p["take_profit_pct"]},
            "trailing_stop": {"enabled": True, "type": "trailing_pct", "value": p.get("trailing_pct", 5.0)},
            "ashare_rules": {"t_plus_1": True, "price_limit": True, "min_lot": 100},
        },
    }


# ============================================================
# 策略模板注册表
# ============================================================

MY_STRATEGY_TEMPLATES: Dict[str, Dict[str, Any]] = {

    # ── 1. 量价共振突破 ──
    "vol_price_resonance": {
        "name": "量价共振突破",
        "description": (
            "放量突破 N 日高点，捕捉主升浪启动。"
            "数据验证：short_score>0.85 + vol_price_corr>0.3 + vol_cv>1.0 的 105 只股票，"
            "年化中位数 23.0%（全市场 12.3%），88% 胜率。"
            "适合超短线（1-3 天）。"
        ),
        "indicators": ["donchian_channel", "volume", "period_return", "ema"],
        "params": {
            "breakout_period":    _p_int(5, 30, 1),
            "vol_ma_period":      _p_int(10, 30, 1),
            "vol_ratio":          _p_float(1.5, 3.5, 0.1),
            "min_change_pct":     _p_float(2.0, 5.0, 0.5),
            "max_change_pct":     _p_float(6.0, 9.5, 0.5),
            "use_trend_filter":   _p_choice([True, False]),
            "trend_ema_period":   _p_int(20, 60, 5),
            "stop_loss_pct":      _p_float(3.0, 7.0, 0.5),
            "take_profit_pct":    _p_float(8.0, 15.0, 1.0),
            "trailing_pct":       _p_float(3.0, 7.0, 0.5),
        },
        "constraints": [
            ("min_change_pct", "<", "max_change_pct"),
            ("vol_ratio", ">", 1.0),
        ],
        "build_config": _build_vol_price_resonance_config,
    },

    # ── 2. 趋势回踩买入 ──
    "trend_pullback_buy": {
        "name": "趋势回踩买入",
        "description": (
            "上升趋势中回踩均线支撑位买入。"
            "数据验证：trend_score>0.85 + sharpe>1.0 的 34 只股票，"
            "年化中位数 71.2%，100% 胜率，Sharpe 1.34。数据支撑最强。"
            "适合短中线（3-10 天）。"
        ),
        "indicators": ["ema", "rsi", "volume"],
        "params": {
            "trend_ema_period":   _p_int(15, 60, 5),
            "pullback_pct":       _p_float(1.0, 5.0, 0.5),
            "rsi_period":         _p_int(7, 21, 1),
            "rsi_low":            _p_int(35, 50, 1),
            "rsi_high":           _p_int(50, 65, 1),
            "use_vol_shrink":     _p_choice([True, False]),
            "vol_ma_period":      _p_int(10, 30, 1),
            "vol_shrink_ratio":   _p_float(0.3, 0.8, 0.1),
            "stop_loss_pct":      _p_float(3.0, 6.0, 0.5),
            "trailing_pct":       _p_float(3.0, 6.0, 0.5),
        },
        "constraints": [
            ("rsi_low", "<", "rsi_high"),
            ("pullback_pct", ">", 0.5),
        ],
        "build_config": _build_trend_pullback_buy_config,
    },

    # ── 3. 涨停板次日博弈 ──
    "limit_up_next_day": {
        "name": "涨停板次日博弈",
        "description": (
            "涨停股次日低开后翻红博弈。"
            "数据验证：limit_up_count>=20 + autocorr>0.1 + amplitude>0.03 的 126 只股票，"
            "涨停中位数 29 次，振幅中位数 3.73%，自相关 0.119。"
            "适合 T+1 博弈，高风险高收益。"
        ),
        "indicators": ["limitup_detect", "open_gap", "change_pct", "volume"],
        "params": {
            "min_open_gap":       _p_float(0.5, 2.0, 0.5),
            "max_open_gap":       _p_float(2.0, 5.0, 0.5),
            "turn_positive_pct":  _p_float(0.0, 2.0, 0.5),
            "use_vol_confirm":    _p_choice([True, False]),
            "vol_ma_period":      _p_int(10, 30, 1),
            "vol_ratio":          _p_float(0.8, 2.0, 0.1),
            "position_pct":       _p_int(30, 70, 10),
            "stop_loss_pct":      _p_float(3.0, 6.0, 0.5),
            "take_profit_pct":    _p_float(4.0, 10.0, 1.0),
        },
        "constraints": [
            ("min_open_gap", "<", "max_open_gap"),
        ],
        "build_config": _build_limit_up_next_day_config,
    },

    # ── 4. 低波反转 ──
    "low_vol_reversal": {
        "name": "低波反转",
        "description": (
            "全市场实时扫描 BB 带宽收缩信号，捕捉波动率收敛后的反转。"
            "⚠️ stock_stats 中低波股票年化仅 4.5%（弱势非蓄势），"
            "因此本策略不做预筛选，由实时 BB 带宽收缩 + 放量突破 + RSI 确认。"
            "适合中线（5-20 天），胜率偏低但赔率高。"
        ),
        "indicators": ["bollinger", "volume", "ma", "rsi"],
        "params": {
            "bb_period":          _p_int(15, 30, 1),
            "bb_std":             _p_float(1.5, 2.5, 0.1),
            "squeeze_percentile": _p_int(10, 30, 5),
            "vol_ma_period":      _p_int(10, 30, 1),
            "vol_ratio":          _p_float(1.3, 2.5, 0.1),
            "ma_period":          _p_int(5, 20, 1),
            "rsi_period":         _p_int(7, 21, 1),
            "rsi_exit_oversold":  _p_int(25, 45, 1),
            "position_pct":       _p_int(40, 80, 10),
            "stop_loss_pct":      _p_float(4.0, 8.0, 0.5),
            "take_profit_pct":    _p_float(10.0, 20.0, 1.0),
            "trailing_pct":       _p_float(4.0, 8.0, 0.5),
        },
        "constraints": [
            ("squeeze_percentile", "<", 40),
            ("vol_ratio", ">", 1.0),
        ],
        "build_config": _build_low_vol_reversal_config,
    },

    # ── 5. 龙回头 ──
    "dragon_pullback": {
        "name": "龙回头",
        "description": (
            "龙头股大涨后回调到支撑位，缩量企稳后二次启动。"
            "数据验证：limit_up_count>=30 的 1082 只龙头股，"
            "年化中位数 25.6%（全市场 12.3%），Sharpe 0.48。100% 盈利。"
            "⚠️ 回撤中位数 -68%，需严格止损。适合超短（2-5 天）。"
        ),
        "indicators": ["dragon_pullback", "volume", "rsi", "ema"],
        "params": {
            "lookback_period":    _p_int(10, 40, 5),
            "pullback_min_pct":   _p_float(8.0, 15.0, 1.0),
            "pullback_max_pct":   _p_float(20.0, 35.0, 5.0),
            "vol_ma_period":      _p_int(10, 30, 1),
            "vol_shrink_ratio":   _p_float(0.3, 0.7, 0.1),
            "rsi_period":         _p_int(7, 21, 1),
            "rsi_bounce":         _p_int(25, 45, 1),
            "use_ma_support":     _p_choice([True, False]),
            "ma_support_period":  _p_int(10, 30, 5),
            "ma_near_pct":        _p_float(1.0, 4.0, 0.5),
            "position_pct":       _p_int(50, 80, 10),
            "stop_loss_pct":      _p_float(4.0, 8.0, 0.5),
            "take_profit_pct":    _p_float(8.0, 15.0, 1.0),
            "trailing_pct":       _p_float(3.0, 7.0, 0.5),
        },
        "constraints": [
            ("pullback_min_pct", "<", "pullback_max_pct"),
            ("vol_shrink_ratio", "<", 0.8),
        ],
        "build_config": _build_dragon_pullback_config,
    },
}
