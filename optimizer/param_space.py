"""
策略参数空间定义
每个策略模板定义：指标组合 + 可调参数范围 + 信号逻辑
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
# 策略配置构建函数（必须在 STRATEGY_TEMPLATES 之前定义）
# ============================================================

def _build_ma_crossover_config(p: dict) -> dict:
    """均线交叉 → StrategyCompiler 格式"""
    fast_type = p.get("fast_type", "ema")
    slow_type = p.get("slow_type", "sma")
    use_rsi = p.get("use_rsi_filter", False)

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
    ]
    if use_rsi:
        entry_rules.append({
            "indicator": "rsi",
            "params": {"period": p.get("rsi_period", 14), "threshold": p.get("rsi_lower", 30)},
            "operator": ">",
        })

    return {
        "name": f"MA_Cross_{fast_type}{p['fast_period']}_{slow_type}{p['slow_period']}",
        "entry_rules": entry_rules,
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": False, "value": 0},
            "trailing_stop": {"enabled": False},
        },
    }


def _build_rsi_oversold_config(p: dict) -> dict:
    use_confirm = p.get("use_confirm", False)
    entry_rules = [
        {
            "indicator": "rsi",
            "params": {"period": p["rsi_period"], "threshold": p["oversold"]},
            "operator": "cross_up",
        },
    ]
    if use_confirm:
        entry_rules.append({
            "indicator": "ma",
            "params": {"period": 20, "ma_type": "ema"},
            "operator": "price_above",
        })

    return {
        "name": f"RSI_OS_{p['rsi_period']}_{p['oversold']}_{p['overbought']}",
        "entry_rules": entry_rules,
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": False, "value": 0},
            "trailing_stop": {"enabled": False},
        },
    }


def _build_bollinger_breakout_config(p: dict) -> dict:
    return {
        "name": f"BB_Squeeze_{p['bb_period']}_{p['bb_std']}",
        "entry_rules": [
            {
                "indicator": "bollinger",
                "params": {"period": p["bb_period"], "std_dev": p["bb_std"]},
                "operator": "price_above_upper",
            },
        ],
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": False, "value": 0},
            "trailing_stop": {"enabled": False},
        },
    }


def _build_macd_crossover_config(p: dict) -> dict:
    use_hist = p.get("use_histogram", False)
    entry_rules = [
        {
            "indicator": "macd",
            "params": {
                "fast_period": p["fast_period"],
                "slow_period": p["slow_period"],
                "signal_period": p["signal_period"],
            },
            "operator": "cross_up",
        },
    ]

    return {
        "name": f"MACD_{p['fast_period']}_{p['slow_period']}_{p['signal_period']}",
        "entry_rules": entry_rules,
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": False, "value": 0},
            "trailing_stop": {"enabled": False},
        },
    }


def _build_supertrend_config(p: dict) -> dict:
    entry_rules = [
        {
            "indicator": "supertrend",
            "params": {"period": p["st_period"], "multiplier": p["st_multiplier"]},
            "signal": "trend_bullish",
        },
    ]
    if p.get("use_ema_filter"):
        entry_rules.append({
            "indicator": "ema",
            "params": {"period": p.get("ema_filter_period", 100)},
            "operator": "price_above",
        })

    return {
        "name": f"ST_{p['st_period']}_{p['st_multiplier']}",
        "entry_rules": entry_rules,
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": False, "value": 0},
            "trailing_stop": {"enabled": False},
        },
    }


def _build_kdj_crossover_config(p: dict) -> dict:
    entry_rules = [
        {
            "indicator": "kdj",
            "params": {"period": p["kdj_period"], "signal_period": p["kdj_signal"]},
            "operator": "gold_cross",
        },
    ]

    return {
        "name": f"KDJ_{p['kdj_period']}_{p['kdj_signal']}",
        "entry_rules": entry_rules,
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": False, "value": 0},
            "trailing_stop": {"enabled": False},
        },
    }


def _build_dual_rsi_config(p: dict) -> dict:
    return {
        "name": f"DualRSI_{p['fast_rsi']}_{p['slow_rsi']}",
        "entry_rules": [
            {
                "indicator": "rsi",
                "params": {"period": p["fast_rsi"], "threshold": p["entry_level"]},
                "operator": "cross_up",
            },
            {
                "indicator": "rsi",
                "params": {"period": p["slow_rsi"], "threshold": p["trend_level"]},
                "operator": ">",
            },
        ],
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": False, "value": 0},
            "trailing_stop": {"enabled": False},
        },
    }


# ============================================================
# 策略模板定义（在所有 build_config 函数之后）
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
        "constraints": [
            ("fast_period", "<", "slow_period"),
        ],
        "build_config": _build_ma_crossover_config,
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
        "constraints": [
            ("oversold", "<", "overbought"),
        ],
        "build_config": _build_rsi_oversold_config,
    },

    # ── 3. 布林带突破 ──
    "bollinger_breakout": {
        "name": "布林带收缩突破",
        "description": "布林带收缩后突破上轨做多、下轨做空。",
        "indicators": ["bollinger"],
        "params": {
            "bb_period":   _p_int(10, 40, 1),
            "bb_std":      _p_float(1.0, 3.0, 0.1),
            "confirm_bars": _p_int(1, 5, 1),
        },
        "constraints": [],
        "build_config": _build_bollinger_breakout_config,
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
        "constraints": [
            ("fast_period", "<", "slow_period"),
        ],
        "build_config": _build_macd_crossover_config,
    },

    # ── 5. SuperTrend ──
    "supertrend": {
        "name": "SuperTrend 趋势跟踪",
        "description": "SuperTrend 方向翻转时交易。",
        "indicators": ["supertrend"],
        "params": {
            "st_period":     _p_int(7, 30, 1),
            "st_multiplier": _p_float(1.5, 5.0, 0.1),
            "use_ema_filter": _p_choice([True, False]),
            "ema_filter_period": _p_int(50, 200, 10),
        },
        "constraints": [],
        "build_config": _build_supertrend_config,
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
        "build_config": _build_kdj_crossover_config,
    },

    # ── 7. 双 RSI 动量 ──
    "dual_rsi": {
        "name": "双 RSI 动量策略",
        "description": "短周期 RSI 判断入场时机，长周期 RSI 判断趋势方向。",
        "indicators": ["rsi"],
        "params": {
            "fast_rsi":   _p_int(5, 14, 1),
            "slow_rsi":   _p_int(14, 30, 1),
            "entry_level": _p_int(25, 45, 1),
            "exit_level":  _p_int(55, 80, 1),
            "trend_level": _p_int(45, 60, 1),
        },
        "constraints": [
            ("fast_rsi", "<", "slow_rsi"),
            ("entry_level", "<", "exit_level"),
        ],
        "build_config": _build_dual_rsi_config,
    },
}


def get_template(key: str) -> dict:
    if key in STRATEGY_TEMPLATES:
        return STRATEGY_TEMPLATES[key]
    # 尝试加载 A 股扩展模板
    try:
        from optimizer.strategy_templates_ashare import ASHARE_STRATEGY_TEMPLATES
        if key in ASHARE_STRATEGY_TEMPLATES:
            return ASHARE_STRATEGY_TEMPLATES[key]
    except ImportError:
        pass
    raise ValueError(f"Unknown strategy template: {key}. Available: {list(STRATEGY_TEMPLATES.keys())}")


def list_templates() -> List[str]:
    return list(STRATEGY_TEMPLATES.keys())
