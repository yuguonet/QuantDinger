"""
A 股扩展策略模板
由 LLM 基于现有模板模式批量生成，针对 A 股市场特点设计：
  - T+1 交易制度
  - 涨跌停限制（主板 10%，创业板/科创板 20%）
  - 最小交易单位 100 股
  - 换手率、量比等 A 股特色指标

策略清单：
  1. atr_breakout        - ATR 波动率突破（海龟交易法变体）
  2. volume_price_div    - 量价背离策略
  3. dual_ma_volume      - 双均线+成交量确认
  4. bollinger_rsi_squeeze - 布林带收缩+RSI 过滤
  5. macd_kdj_resonance  - MACD+KDJ 共振
  6. price_channel        - 价格通道突破
  7. turtle_trading       - 海龟交易法（完整版）
  8. vwap_deviation       - VWAP 偏离策略
  9. ema_rsi_volume       - EMA+RSI+量能三重过滤
  10. kdj_macd_ma_triple  - KDJ+MACD+均线三重共振
"""
from typing import Dict, Any, List

# 复用基础参数构建函数
def _p_int(low: int, high: int, step: int = 1) -> dict:
    return {"type": "int", "low": low, "high": high, "step": step}

def _p_float(low: float, high: float, step: float = 0.001) -> dict:
    return {"type": "float", "low": low, "high": high, "step": step}

def _p_choice(choices: list) -> dict:
    return {"type": "choice", "choices": choices}


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

    return {
        "name": f"ATR_Breakout_{p['atr_period']}_{p['atr_multiplier']}",
        "entry_rules": entry_rules,
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

    return {
        "name": f"VolPriceDiv_{p['lookback_period']}_{p['rsi_period']}",
        "entry_rules": entry_rules,
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "value": p.get("stop_loss_pct", 5.0)},
            "trailing_stop": {"enabled": False},
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

    return {
        "name": f"DualMA_Vol_{fast_type}{p['fast_period']}_{slow_type}{p['slow_period']}",
        "entry_rules": entry_rules,
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "value": p.get("stop_loss_pct", 5.0)},
            "trailing_stop": {"enabled": False},
        },
    }


# ============================================================
# 4. 布林带收缩 + RSI 过滤
# ============================================================

def _build_bollinger_rsi_squeeze_config(p: dict) -> dict:
    """布林带带宽收窄到低位 + RSI 脱离超卖区 → 突破入场"""
    entry_rules = [
        {
            "indicator": "bollinger_bandwidth",
            "params": {
                "period": p["bb_period"],           # 编译器期望 'period'
                "std_dev": p["bb_std"],             # 编译器期望 'std_dev'
                "squeeze_percentile": p["squeeze_percentile"],
            },
            "operator": "below_percentile",
        },
        {
            "indicator": "rsi",
            "params": {"period": p["rsi_period"], "threshold": p["rsi_exit_oversold"]},
            "operator": "cross_up",
        },
    ]

    return {
        "name": f"BB_RSI_Squeeze_{p['bb_period']}_{p['bb_std']}",
        "entry_rules": entry_rules,
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "value": p.get("stop_loss_pct", 5.0)},
            "trailing_stop": {"enabled": False},
        },
    }


# ============================================================
# 5. MACD + KDJ 共振
# ============================================================

def _build_macd_kdj_resonance_config(p: dict) -> dict:
    """MACD 金叉 + KDJ 金叉共振，双重确认"""
    entry_rules = [
        {
            "indicator": "macd",
            "params": {
                "fast_period": p["macd_fast"],
                "slow_period": p["macd_slow"],
                "signal_period": p["macd_signal"],
            },
            "operator": "cross_up",
        },
        {
            "indicator": "kdj",
            "params": {"period": p["kdj_period"], "signal_period": p["kdj_signal"]},
            "operator": "gold_cross",
        },
    ]
    if p.get("use_ma_filter"):
        entry_rules.append({
            "indicator": "ma",
            "params": {"period": p["ma_filter_period"], "ma_type": "ema"},
            "operator": "price_above",
        })

    return {
        "name": f"MACD_KDJ_{p['macd_fast']}_{p['kdj_period']}",
        "entry_rules": entry_rules,
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "value": p.get("stop_loss_pct", 5.0)},
            "trailing_stop": {"enabled": False},
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

    return {
        "name": f"PriceChannel_{p['entry_period']}_{p['exit_period']}",
        "entry_rules": entry_rules,
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "type": "donchian", "period": p["exit_period"]},
            "trailing_stop": {"enabled": True, "type": "donchian", "period": p["exit_period"]},
        },
    }


# ============================================================
# 7. 海龟交易法（完整版）
# ============================================================

def _build_turtle_trading_config(p: dict) -> dict:
    """经典海龟交易法：20日突破入场，10日突破出场，ATR 止损，金字塔加仓"""
    entry_rules = [
        {
            "indicator": "donchian_channel",
            "params": {"upper_period": p["entry_breakout"], "lower_period": p["exit_breakout"]},
            "operator": "price_break_upper",
        },
    ]

    return {
        "name": f"Turtle_{p['entry_breakout']}_{p['exit_breakout']}",
        "entry_rules": entry_rules,
        "position_config": {
            "initial_size_pct": p.get("initial_position_pct", 25),
            "leverage": 1,
            "max_pyramiding": p.get("max_adds", 4),
            "add_atr_multiplier": p.get("add_atr_mult", 0.5),
        },
        "pyramiding_rules": {
            "enabled": True,
            "max_adds": p.get("max_adds", 4),
            "add_interval_atr": p.get("add_atr_mult", 0.5),
        },
        "risk_management": {
            "stop_loss": {"enabled": True, "type": "atr", "atr_period": p["atr_period"], "atr_multiplier": p["atr_stop_mult"]},
            "trailing_stop": {"enabled": False},
        },
    }


# ============================================================
# 8. VWAP 偏离策略
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

    return {
        "name": f"VWAP_Dev_{p['deviation_pct']}_{p['rsi_period']}",
        "entry_rules": entry_rules,
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "value": p.get("stop_loss_pct", 3.0)},
            "trailing_stop": {"enabled": False},
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

    return {
        "name": f"EMA_RSI_Vol_{p['ema_period']}_{p['rsi_period']}",
        "entry_rules": entry_rules,
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "value": p.get("stop_loss_pct", 5.0)},
            "trailing_stop": {"enabled": True, "type": "trailing_pct", "value": p.get("trailing_pct", 3.0)},
        },
    }


# ============================================================
# 10. KDJ + MACD + 均线三重共振
# ============================================================

def _build_kdj_macd_ma_triple_config(p: dict) -> dict:
    """三重指标共振：KDJ 金叉 + MACD 柱状线翻红 + 价格在均线上方"""
    entry_rules = [
        {
            "indicator": "kdj",
            "params": {"period": p["kdj_period"], "signal_period": p["kdj_signal"]},
            "operator": "gold_cross",
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

    return {
        "name": f"Triple_{p['kdj_period']}_{p['macd_fast']}_{p['ma_period']}",
        "entry_rules": entry_rules,
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "value": p.get("stop_loss_pct", 5.0)},
            "trailing_stop": {"enabled": False},
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
        },
        "constraints": [],
        "build_config": _build_atr_breakout_config,
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
            "stop_loss_pct":    _p_float(3.0, 8.0, 0.5),
        },
        "constraints": [],
        "build_config": _build_volume_price_divergence_config,
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
            "stop_loss_pct":  _p_float(3.0, 8.0, 0.5),
        },
        "constraints": [
            ("fast_period", "<", "slow_period"),
        ],
        "build_config": _build_dual_ma_volume_config,
    },

    # ── 4. 布林带收缩 + RSI ──
    "bollinger_rsi_squeeze": {
        "name": "布林带收缩+RSI 突破",
        "description": "布林带带宽收窄至低位（蓄势），RSI 脱离超卖区时入场。A 股横盘突破常用形态。",
        "indicators": ["bollinger", "rsi"],
        "params": {
            "bb_period":          _p_int(10, 30, 1),
            "bb_std":             _p_float(1.5, 3.0, 0.1),
            "squeeze_percentile": _p_int(10, 30, 5),
            "rsi_period":         _p_int(7, 21, 1),
            "rsi_exit_oversold":  _p_int(25, 45, 1),
            "stop_loss_pct":      _p_float(3.0, 8.0, 0.5),
        },
        "constraints": [],
        "build_config": _build_bollinger_rsi_squeeze_config,
    },

    # ── 5. MACD + KDJ 共振 ──
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
            "stop_loss_pct":   _p_float(3.0, 8.0, 0.5),
        },
        "constraints": [
            ("macd_fast", "<", "macd_slow"),
        ],
        "build_config": _build_macd_kdj_resonance_config,
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
        },
        "constraints": [
            ("exit_period", "<", "entry_period"),
        ],
        "build_config": _build_price_channel_config,
    },

    # ── 7. 海龟交易法 ──
    "turtle_trading": {
        "name": "海龟交易法",
        "description": "经典海龟系统：20日突破入场，10日突破出场，ATR 止损，金字塔加仓。适合趋势明显的 A 股行情。",
        "indicators": ["donchian_channel", "atr"],
        "params": {
            "entry_breakout":     _p_int(10, 40, 5),
            "exit_breakout":      _p_int(5, 20, 5),
            "atr_period":         _p_int(10, 30, 1),
            "atr_stop_mult":      _p_float(1.5, 3.0, 0.1),
            "max_adds":           _p_int(1, 6, 1),
            "add_atr_mult":       _p_float(0.3, 1.0, 0.1),
            "initial_position_pct": _p_int(10, 50, 5),
        },
        "constraints": [
            ("exit_breakout", "<", "entry_breakout"),
        ],
        "build_config": _build_turtle_trading_config,
    },

    # ── 8. VWAP 偏离 ──
    "vwap_deviation": {
        "name": "VWAP 偏离回归",
        "description": "价格偏离 VWAP 超过阈值时做均值回归。适合 A 股日内或短线交易，机构资金参考 VWAP 较多。",
        "indicators": ["vwap", "rsi"],
        "params": {
            "deviation_pct":  _p_float(1.0, 5.0, 0.1),
            "rsi_period":     _p_int(7, 21, 1),
            "rsi_level":      _p_int(25, 45, 1),
            "stop_loss_pct":  _p_float(2.0, 5.0, 0.5),
        },
        "constraints": [],
        "build_config": _build_vwap_deviation_config,
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
            "stop_loss_pct":  _p_float(3.0, 8.0, 0.5),
            "trailing_pct":   _p_float(2.0, 5.0, 0.5),
        },
        "constraints": [],
        "build_config": _build_ema_rsi_volume_config,
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
            "stop_loss_pct": _p_float(3.0, 8.0, 0.5),
        },
        "constraints": [
            ("macd_fast", "<", "macd_slow"),
        ],
        "build_config": _build_kdj_macd_ma_triple_config,
    },
}
