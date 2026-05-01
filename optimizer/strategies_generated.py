"""LLM Phase 2 生成的策略模板"""

def _p_int(low, high, step=1): return {'type': 'int', 'low': low, 'high': high, 'step': step}
def _p_float(low, high, step=0.01): return {'type': 'float', 'low': low, 'high': high, 'step': step}
def _p_choice(choices): return {'type': 'choice', 'choices': choices}


# ── RSI VWAP Volume共振 ──
def _build_rsi_vwap_volume_config(p: dict) -> dict:
    entry_rules = [
        {"indicator": "rsi", "params": {"period": p.get("rsi_period", 14), "threshold": p.get("rsi_threshold", 30)}, "operator": "<"},
        {"indicator": "volume", "params": {"period": p.get("vol_ma_period", 20)}, "operator": "volume_ratio_above", "threshold": p.get("vol_ratio", 1.5)},
        {"indicator": "vwap", "params": {"deviation_pct": 2.0}, "operator": "price_below_vwap_by"},
    ]
    return {
        "name": f"RSI VWAP Volume共振_{p.get('rsi_period', 14)}",
        "entry_rules": entry_rules,
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "value": p.get("stop_loss_pct", 3.0)},
            "trailing_stop": {"enabled": False},
        },
    }

# ── adaptive_volatility ──
def _build_adaptive_volatility_config(p: dict) -> dict:
    entry_rules = [
        {"indicator": "rsi", "params": {"period": p.get("rsi_period", 14), "threshold": p.get("rsi_threshold", 30)}, "operator": "<"},
        {"indicator": "bollinger", "params": {"period": 20, "std_dev": 2.0}, "operator": "price_below_lower"},
        {"indicator": "volume", "params": {"period": p.get("vol_ma_period", 20)}, "operator": "volume_ratio_above", "threshold": p.get("vol_ratio", 1.5)},
    ]
    return {
        "name": f"adaptive_volatility_{p.get('rsi_period', 14)}",
        "entry_rules": entry_rules,
        "position_config": {"initial_size_pct": 10, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "value": p.get("stop_loss_pct", 3.0)},
            "trailing_stop": {"enabled": False},
        },
    }

# ── ema_rsi_volume ──
def _build_ema_rsi_volume_config(p: dict) -> dict:
    entry_rules = [
        {"indicator": "rsi", "params": {"period": p.get("rsi_period", 14), "threshold": p.get("rsi_threshold", 30)}, "operator": "<"},
        {"indicator": "ma", "params": {"period": 20, "ma_type": "ema"}, "operator": "price_above"},
        {"indicator": "volume", "params": {"period": p.get("vol_ma_period", 20)}, "operator": "volume_ratio_above", "threshold": p.get("vol_ratio", 1.5)},
    ]
    return {
        "name": f"ema_rsi_volume_{p.get('rsi_period', 14)}",
        "entry_rules": entry_rules,
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "value": p.get("stop_loss_pct", 3.0)},
            "trailing_stop": {"enabled": False},
        },
    }

# ─ 均线KDJ动量 ──
def _build_kdj_config(p: dict) -> dict:
    entry_rules = [
        {"indicator": "rsi", "params": {"period": p.get("rsi_period", 14), "threshold": p.get("rsi_threshold", 30)}, "operator": "<"},
        {"indicator": "kdj", "params": {"k_period": 9, "d_period": 3}, "operator": "gold_cross"},
        {"indicator": "volume", "params": {"period": p.get("vol_ma_period", 20)}, "operator": "volume_ratio_above", "threshold": p.get("vol_ratio", 1.5)},
    ]
    return {
        "name": f"均线KDJ动量_{p.get('rsi_period', 14)}",
        "entry_rules": entry_rules,
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "value": p.get("stop_loss_pct", 3.0)},
            "trailing_stop": {"enabled": False},
        },
    }

# ── bollinger_macd_volume ──
def _build_bollinger_macd_volume_config(p: dict) -> dict:
    entry_rules = [
        {"indicator": "macd", "params": {"fast_period": 12, "slow_period": 26, "signal_period": 9}, "operator": "diff_lt_dea"},
        {"indicator": "bollinger", "params": {"period": 20, "std_dev": 2.0}, "operator": "price_below_lower"},
        {"indicator": "volume", "params": {"period": p.get("vol_ma_period", 20)}, "operator": "volume_ratio_above", "threshold": p.get("vol_ratio", 1.5)},
    ]
    return {
        "name": f"bollinger_macd_volume_{p.get('rsi_period', 14)}",
        "entry_rules": entry_rules,
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "value": p.get("stop_loss_pct", 3.0)},
            "trailing_stop": {"enabled": False},
        },
    }


GENERATED_TEMPLATES = {
    "rsi_vwap_volume": {
        "name": "RSI VWAP Volume共振",
        "description": "LLM 生成的策略（自动重建）",
        "indicators": ["rsi", "volume", "vwap"],
        "params": {
            "rsi_period": {"type": "int", "low": 10, "high": 20, "step": 1},
            "rsi_threshold": {"type": "int", "low": 25, "high": 40, "step": 1},
            "vol_ma_period": {"type": "int", "low": 10, "high": 30, "step": 1},
            "vol_ratio": {"type": "float", "low": 1.2, "high": 2.5, "step": 0.1},
            "stop_loss_pct": {"type": "float", "low": 2.0, "high": 5.0, "step": 0.5},
        },
        "constraints": [],
        "build_config": _build_rsi_vwap_volume_config,
    },
    "adaptive_volatility": {
        "name": "adaptive_volatility",
        "description": "LLM 生成的策略（自动重建）",
        "indicators": ["rsi", "bollinger", "volume"],
        "params": {
            "rsi_period": {"type": "int", "low": 10, "high": 20, "step": 1},
            "rsi_threshold": {"type": "int", "low": 25, "high": 40, "step": 1},
            "vol_ma_period": {"type": "int", "low": 10, "high": 30, "step": 1},
            "vol_ratio": {"type": "float", "low": 1.2, "high": 2.5, "step": 0.1},
            "stop_loss_pct": {"type": "float", "low": 2.0, "high": 5.0, "step": 0.5},
        },
        "constraints": [],
        "build_config": _build_adaptive_volatility_config,
    },
    "ema_rsi_volume": {
        "name": "ema_rsi_volume",
        "description": "LLM 生成的策略（自动重建）",
        "indicators": ["rsi", "ma", "volume"],
        "params": {
            "rsi_period": {"type": "int", "low": 10, "high": 20, "step": 1},
            "rsi_threshold": {"type": "int", "low": 25, "high": 40, "step": 1},
            "vol_ma_period": {"type": "int", "low": 10, "high": 30, "step": 1},
            "vol_ratio": {"type": "float", "low": 1.2, "high": 2.5, "step": 0.1},
            "stop_loss_pct": {"type": "float", "low": 2.0, "high": 5.0, "step": 0.5},
        },
        "constraints": [],
        "build_config": _build_ema_rsi_volume_config,
    },
    "kdj": {
        "name": "均线KDJ动量",
        "description": "LLM 生成的策略（自动重建）",
        "indicators": ["rsi", "kdj", "volume"],
        "params": {
            "rsi_period": {"type": "int", "low": 10, "high": 20, "step": 1},
            "rsi_threshold": {"type": "int", "low": 25, "high": 40, "step": 1},
            "vol_ma_period": {"type": "int", "low": 10, "high": 30, "step": 1},
            "vol_ratio": {"type": "float", "low": 1.2, "high": 2.5, "step": 0.1},
            "stop_loss_pct": {"type": "float", "low": 2.0, "high": 5.0, "step": 0.5},
        },
        "constraints": [],
        "build_config": _build_kdj_config,
    },
    "bollinger_macd_volume": {
        "name": "bollinger_macd_volume",
        "description": "LLM 生成的策略（自动重建）",
        "indicators": ["macd", "bollinger", "volume"],
        "params": {
            "rsi_period": {"type": "int", "low": 10, "high": 20, "step": 1},
            "rsi_threshold": {"type": "int", "low": 25, "high": 40, "step": 1},
            "vol_ma_period": {"type": "int", "low": 10, "high": 30, "step": 1},
            "vol_ratio": {"type": "float", "low": 1.2, "high": 2.5, "step": 0.1},
            "stop_loss_pct": {"type": "float", "low": 2.0, "high": 5.0, "step": 0.5},
        },
        "constraints": [],
        "build_config": _build_bollinger_macd_volume_config,
    },
}
