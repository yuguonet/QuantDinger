"""
基于 Phase 1 WF 验证数据洞察的新策略模板

数据来源：81 只 WF 通过股票的参数规律分析
核心发现：
  - VWAP 偏离 1.5~3.0% 是 A 股均值回归的黄金区间
  - RSI 超卖区 28~38（比传统 30 更宽）
  - BB 标准差 1.8~2.2 高度集中
  - 成交量放大 1.2~1.7 倍是有效确认
  - 止损 2.0~4.5%，VWAP 类偏紧，RSI/MACD 类偏松

新模板设计原则：
  1. 用数据验证过的参数区间，不拍脑袋
  2. 组合已有模板中被证明有效的局部特征
  3. 增加 ATR 动态止损选项
  4. 每个模板都有明确的数据依据
"""
from typing import Dict, Any


def _p_int(low: int, high: int, step: int = 1) -> dict:
    return {"type": "int", "low": low, "high": high, "step": step}


def _p_float(low: float, high: float, step: float = 0.1) -> dict:
    return {"type": "float", "low": low, "high": high, "step": step}


def _p_choice(choices: list) -> dict:
    return {"type": "choice", "choices": choices}


# ============================================================
# 1. VWAP + RSI 双确认
# ============================================================
# 数据依据：
#   - vwap_bollinger_squeeze 39 只通过，vwap_volume_confirm 26 只通过
#   - RSI 超卖区 28~38 对 41 只股票有效
#   - 两者结合 = 均值回归 + 超卖动量 双重过滤
# 设计：VWAP 偏离触发 + RSI 超卖确认 + 放量验证

def _build_vwap_rsi_confirm_config(p: dict) -> dict:
    entry_rules = [
        {
            "indicator": "vwap",
            "params": {"deviation_pct": p["vwap_dev_pct"]},
            "operator": "price_below_vwap_by",
        },
        {
            "indicator": "rsi",
            "params": {"period": p["rsi_period"]},
            "operator": "<",
            "threshold": p["rsi_level"],
        },
    ]
    if p.get("use_vol_filter"):
        entry_rules.append({
            "indicator": "volume",
            "params": {"period": p["vol_ma_period"]},
            "operator": "volume_ratio_above",
            "threshold": p["vol_ratio"],
        })

    return {
        "name": f"VWAP_RSI_{p['vwap_dev_pct']}_{p['rsi_level']}",
        "entry_rules": entry_rules,
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "value": p.get("stop_loss_pct", 3.0)},
            "trailing_stop": {"enabled": False},
        },
    }


# ============================================================
# 2. RSI + 布林带下轨支撑
# ============================================================
# 数据依据：
#   - rsi_volume_divergence 10 只通过，RSI 超卖区 28~38
#   - vwap_bollinger_squeeze 39 只通过，BB std 1.8~2.2
#   - RSI 超卖 + 价格触及 BB 下轨 = 双重超卖确认
# 设计：RSI 超卖 + BB 下轨 + 可选成交量确认

def _build_rsi_bollinger_support_config(p: dict) -> dict:
    entry_rules = [
        {
            "indicator": "rsi",
            "params": {"period": p["rsi_period"]},
            "operator": "<",
            "threshold": p["rsi_level"],
        },
        {
            "indicator": "bollinger",
            "params": {"period": p["bb_period"], "std_dev": p["bb_std"]},
            "operator": "price_below_lower",
        },
    ]
    if p.get("use_vol_filter"):
        entry_rules.append({
            "indicator": "volume",
            "params": {"period": p["vol_ma_period"]},
            "operator": "volume_ratio_above",
            "threshold": p["vol_ratio"],
        })

    return {
        "name": f"RSI_BB_{p['rsi_level']}_{p['bb_period']}_{p['bb_std']}",
        "entry_rules": entry_rules,
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "value": p.get("stop_loss_pct", 3.0)},
            "trailing_stop": {"enabled": False},
        },
    }


# ============================================================
# 3. VWAP + MACD 金叉 + 放量
# ============================================================
# 数据依据：
#   - macd_vol_divergence 5 只通过，MACD fast 12~14, slow 23~29
#   - vwap 偏离 1.5~3.0% 对 65 只有效
#   - vol_ratio 1.2~1.7 对 26 只有效
# 设计：VWAP 偏离 + MACD 金叉 + 放量三重确认

def _build_vwap_macd_volume_config(p: dict) -> dict:
    entry_rules = [
        {
            "indicator": "vwap",
            "params": {"deviation_pct": p["vwap_dev_pct"]},
            "operator": "price_below_vwap_by",
        },
        {
            "indicator": "macd",
            "params": {"fast": p["macd_fast"], "slow": p["macd_slow"], "signal": p["macd_signal"]},
            "operator": "gold_cross",
        },
        {
            "indicator": "volume",
            "params": {"period": p["vol_ma_period"]},
            "operator": "volume_ratio_above",
            "threshold": p["vol_ratio"],
        },
    ]

    return {
        "name": f"VWAP_MACD_{p['vwap_dev_pct']}_{p['macd_fast']}_{p['macd_slow']}",
        "entry_rules": entry_rules,
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "value": p.get("stop_loss_pct", 3.5)},
            "trailing_stop": {"enabled": False},
        },
    }


# ============================================================
# 4. KDJ 超卖 + VWAP 偏离
# ============================================================
# 数据依据：
#   - KDJ 在 A 股是经典指标，但现有模板未使用
#   - VWAP 偏离 1.5~3.0% 对 65 只有效
#   - KDJ J 值 < 0 是极端超卖信号
# 设计：KDJ 超卖 + VWAP 偏离 + 可选 RSI 确认

def _build_kdj_vwap_reversal_config(p: dict) -> dict:
    entry_rules = [
        {
            "indicator": "kdj",
            "params": {"k_period": p["kdj_k"], "d_period": p["kdj_d"], "j_period": p["kdj_j"]},
            "operator": "below",
            "threshold": p["kdj_oversold"],
        },
        {
            "indicator": "vwap",
            "params": {"deviation_pct": p["vwap_dev_pct"]},
            "operator": "price_below_vwap_by",
        },
    ]
    if p.get("use_rsi_filter"):
        entry_rules.append({
            "indicator": "rsi",
            "params": {"period": p["rsi_period"]},
            "operator": "<",
            "threshold": p["rsi_level"],
        })

    return {
        "name": f"KDJ_VWAP_{p['kdj_oversold']}_{p['vwap_dev_pct']}",
        "entry_rules": entry_rules,
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "value": p.get("stop_loss_pct", 3.5)},
            "trailing_stop": {"enabled": False},
        },
    }


# ============================================================
# 5. EMA 多周期趋势 + RSI 动量
# ============================================================
# 数据依据：
#   - A 股均线系统（5/10/20/60）是技术分析基础
#   - RSI 超卖区 28~38 对 41 只有效
#   - 但纯趋势跟踪在小盘股上失败（Phase 1 淘汰了 dual_ma_volume）
#   - 改进：EMA 多头排列 + RSI 回调买入（趋势中找买点）

def _build_ema_rsi_pullback_config(p: dict) -> dict:
    entry_rules = [
        {
            "indicator": "ema",
            "params": {"period": p["ema_fast"]},
            "operator": "above",
            "threshold": 0,  # placeholder, 实际逻辑在 compiler
        },
        {
            "indicator": "ema",
            "params": {"period": p["ema_slow"]},
            "operator": "above",
            "threshold": 0,
        },
        {
            "indicator": "rsi",
            "params": {"period": p["rsi_period"]},
            "operator": "<",
            "threshold": p["rsi_pullback"],
        },
    ]

    return {
        "name": f"EMA_RSI_PB_{p['ema_fast']}_{p['ema_slow']}_{p['rsi_pullback']}",
        "entry_rules": entry_rules,
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "value": p.get("stop_loss_pct", 3.5)},
            "trailing_stop": {"enabled": False},
        },
    }


# ============================================================
# 模板注册表
# ============================================================

NEW_STRATEGY_TEMPLATES = {
    "vwap_rsi_confirm": {
        "name": "VWAP + RSI 双确认",
        "description": "VWAP 偏离 + RSI 超卖双重确认。数据依据：vwap 65 只 + rsi 41 只通过 WF",
        "indicators": ["vwap", "rsi", "volume"],
        "params": {
            "vwap_dev_pct": _p_float(1.5, 3.5, 0.1),
            "rsi_period": _p_int(10, 18, 1),
            "rsi_level": _p_int(28, 40, 1),
            "use_vol_filter": _p_choice([True, False]),
            "vol_ma_period": _p_int(10, 20, 1),
            "vol_ratio": _p_float(1.2, 1.8, 0.1),
            "stop_loss_pct": _p_float(2.0, 4.0, 0.5),
        },
        "constraints": [
            ("rsi_level", "<", 45),
            ("vwap_dev_pct", ">", 1.0),
        ],
        "build_config": _build_vwap_rsi_confirm_config,
    },

    "rsi_bollinger_support": {
        "name": "RSI + 布林带下轨支撑",
        "description": "RSI 超卖 + BB 下轨双重超卖确认。数据依据：rsi 10 只 + bb 39 只通过 WF",
        "indicators": ["rsi", "bollinger", "volume"],
        "params": {
            "rsi_period": _p_int(10, 18, 1),
            "rsi_level": _p_int(28, 40, 1),
            "bb_period": _p_int(12, 25, 1),
            "bb_std": _p_float(1.6, 2.4, 0.1),
            "use_vol_filter": _p_choice([True, False]),
            "vol_ma_period": _p_int(10, 20, 1),
            "vol_ratio": _p_float(1.2, 1.8, 0.1),
            "stop_loss_pct": _p_float(2.5, 4.5, 0.5),
        },
        "constraints": [
            ("rsi_level", "<", 45),
            ("bb_std", ">", 1.4),
            ("bb_period", ">", 10),
        ],
        "build_config": _build_rsi_bollinger_support_config,
    },

    "vwap_macd_volume": {
        "name": "VWAP + MACD 金叉 + 放量",
        "description": "三重确认：VWAP 均值回归 + MACD 动量转向 + 成交量放大。数据依据：vwap 65 + macd 5 + vol 26",
        "indicators": ["vwap", "macd", "volume"],
        "params": {
            "vwap_dev_pct": _p_float(1.5, 3.5, 0.1),
            "macd_fast": _p_int(10, 16, 1),
            "macd_slow": _p_int(20, 30, 1),
            "macd_signal": _p_int(7, 12, 1),
            "vol_ma_period": _p_int(10, 20, 1),
            "vol_ratio": _p_float(1.2, 1.8, 0.1),
            "stop_loss_pct": _p_float(2.5, 4.5, 0.5),
        },
        "constraints": [
            ("macd_fast", "<", "macd_slow"),
            ("macd_signal", "<", "macd_fast"),
            ("vwap_dev_pct", ">", 1.0),
        ],
        "build_config": _build_vwap_macd_volume_config,
    },

    "kdj_vwap_reversal": {
        "name": "KDJ 超卖 + VWAP 偏离",
        "description": "KDJ 极端超卖 + VWAP 均值回归。A 股经典 KDJ 指标与 VWAP 的新组合",
        "indicators": ["kdj", "vwap", "rsi"],
        "params": {
            "kdj_k": _p_int(5, 14, 1),
            "kdj_d": _p_int(3, 9, 1),
            "kdj_j": _p_int(3, 9, 1),
            "kdj_oversold": _p_int(10, 30, 5),
            "vwap_dev_pct": _p_float(1.5, 3.5, 0.1),
            "use_rsi_filter": _p_choice([True, False]),
            "rsi_period": _p_int(10, 18, 1),
            "rsi_level": _p_int(28, 40, 1),
            "stop_loss_pct": _p_float(2.5, 4.5, 0.5),
        },
        "constraints": [
            ("kdj_oversold", "<", 35),
            ("vwap_dev_pct", ">", 1.0),
        ],
        "build_config": _build_kdj_vwap_reversal_config,
    },

    "ema_rsi_pullback": {
        "name": "EMA 多头 + RSI 回调买入",
        "description": "EMA 多头排列确认趋势 + RSI 回调找买点。解决纯趋势跟踪在 A 股小盘股失败的问题",
        "indicators": ["ema", "rsi"],
        "params": {
            "ema_fast": _p_int(5, 15, 1),
            "ema_slow": _p_int(20, 60, 5),
            "rsi_period": _p_int(10, 18, 1),
            "rsi_pullback": _p_int(35, 50, 1),
            "stop_loss_pct": _p_float(2.5, 5.0, 0.5),
        },
        "constraints": [
            ("ema_fast", "<", "ema_slow"),
            ("rsi_pullback", "<", 55),
        ],
        "build_config": _build_ema_rsi_pullback_config,
    },
}
