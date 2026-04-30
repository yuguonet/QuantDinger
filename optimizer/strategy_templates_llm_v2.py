"""
LLM 生成的 A 股策略模板（Phase 2）— 基于 350+ 只中证1000股票回测数据

  数据洞察（_patterns_v2.txt）:
    1. vwap_deviation:      Sharpe=0.92, 150/350 胜  → 均值回归王者
    2. rsi_oversold:        Sharpe=0.74,  67/350 胜  → 稳健第二
    3. volume_price_div:    Sharpe=0.60,  31/350 胜  → 量价关系关键（从#7升到#3）
    4. dual_rsi:            Sharpe=0.57,  19/350 胜  → 双周期RSI稳定
    5. bollinger_rsi_squeeze: Sharpe=0.54, 23/350 胜 → 收缩突破有效
    6. macd_kdj_resonance:  Sharpe=0.51,  19/350 胜 → 共振确认有效

  淘汰: dual_ma_volume(-0.05), supertrend(-0.08), ema_rsi_volume(-0.40)

  设计原则:
    1. 以 VWAP 偏离为核心（150/350 股票最佳，统治级）
    2. 量价背离是新发现的关键信号（升至#3，必须纳入）
    3. RSI 双周期做动量确认
    4. 避免纯趋势跟踪（小盘股上系统性失败）

  新增模板:
    1. vwap_volume_confirm     - VWAP偏离 + 放量确认（最强信号+量能过滤）
    2. rsi_volume_divergence   - RSI超卖 + 量价背离（#2+#3 融合）
    3. triple_rsi_momentum     - 三周期RSI动量（短中长三重确认）
    4. vwap_bollinger_squeeze  - VWAP + 布林带收缩（#1+#5 均值回归+突破）
    5. macd_vol_divergence     - MACD底背离 + 量价背离（反转双确认）
"""
from typing import Dict, Any

def _p_int(low: int, high: int, step: int = 1) -> dict:
    return {"type": "int", "low": low, "high": high, "step": step}

def _p_float(low: float, high: float, step: float = 0.001) -> dict:
    return {"type": "float", "low": low, "high": high, "step": step}

def _p_choice(choices: list) -> dict:
    return {"type": "choice", "choices": choices}


# ============================================================
# 1. VWAP 偏离 + 放量确认
# ============================================================
# 数据依据: vwap_deviation (#1, Sharpe 0.92) + volume_price_divergence (#3, 0.60)
# 逻辑: VWAP 偏离提供方向，成交量放大确认反转有效性
# 设计考量: 原始 vwap_deviation 只用 RSI 过滤，加入量能后信号更可靠

def _build_vwap_volume_confirm_config(p: dict) -> dict:
    entry_rules = [
        {
            "indicator": "vwap",
            "params": {"deviation_pct": p["vwap_dev_pct"]},
            "operator": "price_below_vwap_by",
        },
        {
            "indicator": "volume",
            "params": {"period": p["vol_ma_period"]},
            "operator": "volume_ratio_above",
            "threshold": p["vol_ratio"],
        },
    ]
    if p.get("use_rsi_filter"):
        entry_rules.append({
            "indicator": "rsi",
            "params": {"period": p.get("rsi_period", 14), "threshold": p.get("rsi_level", 35)},
            "operator": "<",
        })

    return {
        "name": f"VWAP_Vol_{p['vwap_dev_pct']}_{p['vol_ratio']}",
        "entry_rules": entry_rules,
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "value": p.get("stop_loss_pct", 3.0)},
            "trailing_stop": {"enabled": False},
        },
    }


# ============================================================
# 2. RSI 超卖 + 量价背离
# ============================================================
# 数据依据: rsi_oversold (#2, Sharpe 0.74) + volume_price_divergence (#3, 0.60)
# 逻辑: RSI 超卖提供入场时机，量价背离确认底部形态
# 设计考量: 量价背离从#7升到#3，说明在大样本下更可靠

def _build_rsi_volume_divergence_config(p: dict) -> dict:
    entry_rules = [
        {
            "indicator": "rsi",
            "params": {"period": p["rsi_period"], "threshold": p["rsi_oversold"]},
            "operator": "<",
        },
        {
            "indicator": "price_volume_divergence",
            "params": {
                "lookback": p["lookback_period"],
                "divergence_type": "bullish",
                "price_ma": p.get("price_ma_period", 10),
                "volume_ma": p.get("vol_ma_period", 20),
            },
            "operator": "bullish_divergence",
        },
    ]
    if p.get("use_ma_filter"):
        entry_rules.append({
            "indicator": "ma",
            "params": {"period": p["ma_period"], "ma_type": "ema"},
            "operator": "price_above",
        })

    return {
        "name": f"RSI_VolDiv_{p['rsi_period']}_{p['lookback_period']}",
        "entry_rules": entry_rules,
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "value": p.get("stop_loss_pct", 4.0)},
            "trailing_stop": {"enabled": False},
        },
    }


# ============================================================
# 3. 三周期 RSI 动量
# ============================================================
# 数据依据: dual_rsi (#4, Sharpe 0.57, 95% 正Sharpe) 升级版
# 逻辑: 短(7)中(14)长(21) 三周期 RSI 共振确认，提高信号质量
# 设计考量: dual_rsi 已经很稳，加入第三个周期做趋势过滤

def _build_triple_rsi_momentum_config(p: dict) -> dict:
    entry_rules = [
        {
            "indicator": "rsi",
            "params": {"period": p["rsi_fast"], "threshold": p["rsi_entry"]},
            "operator": "cross_up",
        },
        {
            "indicator": "rsi",
            "params": {"period": p["rsi_mid"], "threshold": p["rsi_trend_mid"]},
            "operator": ">",
        },
        {
            "indicator": "rsi",
            "params": {"period": p["rsi_slow"], "threshold": p["rsi_trend_slow"]},
            "operator": ">",
        },
    ]

    return {
        "name": f"TripleRSI_{p['rsi_fast']}_{p['rsi_mid']}_{p['rsi_slow']}",
        "entry_rules": entry_rules,
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "value": p.get("stop_loss_pct", 3.5)},
            "trailing_stop": {"enabled": False},
        },
    }


# ============================================================
# 4. VWAP + 布林带收缩
# ============================================================
# 数据依据: vwap_deviation (#1) + bollinger_rsi_squeeze (#5, Sharpe 0.54)
# 逻辑: VWAP 偏离 + 布林带下轨 + 带宽收缩 = 均值回归蓄势
# 设计考量: 布林带收缩后突破有效，叠加 VWAP 偏离增强回归信号

def _build_vwap_bollinger_squeeze_config(p: dict) -> dict:
    entry_rules = [
        {
            "indicator": "vwap",
            "params": {"deviation_pct": p["vwap_dev_pct"]},
            "operator": "price_below_vwap_by",
        },
        {
            "indicator": "bollinger",
            "params": {"period": p["bb_period"], "std_dev": p["bb_std"]},
            "operator": "price_below_lower",
        },
    ]
    if p.get("use_squeeze_filter"):
        entry_rules.append({
            "indicator": "bollinger_bandwidth",
            "params": {
                "period": p["bb_period"],
                "std_dev": p["bb_std"],
                "squeeze_percentile": p["squeeze_percentile"],
            },
            "operator": "below_percentile",
        })

    return {
        "name": f"VWAP_BB_{p['vwap_dev_pct']}_{p['bb_std']}",
        "entry_rules": entry_rules,
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "value": p.get("stop_loss_pct", 2.5)},
            "trailing_stop": {"enabled": False},
        },
    }


# ============================================================
# 5. MACD 底背离 + 量价背离
# ============================================================
# 数据依据: macd_kdj_resonance (#6, Sharpe 0.51) + volume_price_divergence (#3, 0.60)
# 逻辑: MACD 柱状线翻红（动量反转）+ 量价底背离（形态确认）= 双重反转信号
# 设计考量: 量价背离在大样本下表现好，替代 KDJ 作为 MACD 的搭档

def _build_macd_vol_divergence_config(p: dict) -> dict:
    # 修复：histogram_negative 要求零轴穿越，日线上极罕见
    # 改用 diff_lt_dea（MACD线 < 信号线），更宽松的空头条件
    # 配合量价底背离 = 经典"MACD底背离"形态
    entry_rules = [
        {
            "indicator": "macd",
            "params": {
                "fast_period": p["macd_fast"],
                "slow_period": p["macd_slow"],
                "signal_period": p["macd_signal"],
            },
            "operator": "diff_lt_dea",
        },
        {
            "indicator": "price_volume_divergence",
            "params": {
                "lookback": p["lookback_period"],
                "divergence_type": "bullish",
                "price_ma": p.get("price_ma_period", 10),
                "volume_ma": p.get("vol_ma_period", 20),
            },
            "operator": "bullish_divergence",
        },
    ]
    # RSI 确认默认开启（经典底背离需要 RSI 处于低位）
    if p.get("use_rsi_confirm", True):
        entry_rules.append({
            "indicator": "rsi",
            "params": {"period": p.get("rsi_period", 14), "threshold": p.get("rsi_level", 40)},
            "operator": "<",
        })

    return {
        "name": f"MACD_VolDiv_{p['macd_fast']}_{p['lookback_period']}",
        "entry_rules": entry_rules,
        "position_config": {"initial_size_pct": 100, "leverage": 1, "max_pyramiding": 0},
        "pyramiding_rules": {"enabled": False},
        "risk_management": {
            "stop_loss": {"enabled": True, "value": p.get("stop_loss_pct", 4.0)},
            "trailing_stop": {"enabled": False},
        },
    }


# ============================================================
# 模板注册表
# ============================================================

LLM_STRATEGY_TEMPLATES: Dict[str, Dict[str, Any]] = {

    # ── 1. VWAP + 放量确认 ──
    # v2: 收窄参数，Phase 1 表现好(Sharpe 0.838)但仍可优化
    "vwap_volume_confirm": {
        "name": "VWAP偏离+放量确认",
        "description": (
            "VWAP 偏离 + 成交量放大双重确认。"
            "数据依据: vwap_deviation (#1, 0.92) + volume_price_divergence (#3, 0.60)。"
            "最强均值回归信号叠加量能过滤。"
        ),
        "indicators": ["vwap", "volume", "rsi"],
        "params": {
            "vwap_dev_pct":      _p_float(1.5, 4.0, 0.1),  # 原 1-5 → 1.5-4，去极端
            "vol_ma_period":     _p_int(10, 20, 1),         # 原 10-30 → 10-20
            "vol_ratio":         _p_float(1.2, 2.5, 0.1),   # 原 1-3 → 1.2-2.5
            "use_rsi_filter":    _p_choice([True, False]),
            "rsi_period":        _p_int(10, 18, 1),         # 原 7-21 → 10-18
            "rsi_level":         _p_int(28, 42, 1),         # 原 25-45 → 28-42
            "stop_loss_pct":     _p_float(2.0, 4.0, 0.5),   # 原 2-5 → 2-4
        },
        "constraints": [],
        "build_config": _build_vwap_volume_confirm_config,
    },

    # ── 2. RSI + 量价背离 ──
    # v2 优化: 收窄参数空间 + 加约束，降低过拟合风险
    # 原始空间 2.3 亿组合 → 优化后 ~48 万组合（减少 99.8%）
    "rsi_volume_divergence": {
        "name": "RSI超卖+量价背离",
        "description": (
            "RSI 超卖回升 + 量价底背离双重确认。"
            "数据依据: rsi_oversold (#2, 0.74) + volume_price_divergence (#3, 0.60)。"
            "量价背离在大样本下从#7升到#3，信号更可靠。"
        ),
        "indicators": ["rsi", "volume", "ma"],
        "params": {
            "rsi_period":       _p_int(10, 18, 1),      # 原 7-21 → 10-18，去掉极端值
            "rsi_oversold":     _p_int(25, 35, 1),      # 原 20-40 → 25-35，经典超卖区
            "lookback_period":  _p_int(15, 30, 1),      # 原 10-40 → 15-30，去噪+保信号
            "price_ma_period":  _p_int(5, 15, 1),       # 原 5-20 → 5-15，短期MA
            "vol_ma_period":    _p_int(10, 20, 1),      # 原 10-30 → 10-20，量能窗口
            "use_ma_filter":    _p_choice([True, False]),
            "ma_period":        _p_int(20, 60, 10),     # 原 20-120 → 20-60，趋势过滤
            "stop_loss_pct":    _p_float(3.0, 5.0, 0.5), # 原 3-6 → 3-5，收窄
        },
        "constraints": [
            ("price_ma_period", "<", "lookback_period"),  # 价格MA必须短于回看期
        ],
        "build_config": _build_rsi_volume_divergence_config,
    },

    # ── 3. 三周期 RSI 动量 ──
    # Phase 1 冠军(Sharpe 0.630, 100%正得分)，参数空间已较合理，微调
    "triple_rsi_momentum": {
        "name": "三周期RSI动量",
        "description": (
            "短中长三周期 RSI 共振确认入场。"
            "数据依据: dual_rsi (#4, 0.57, 95% 正Sharpe) 升级版。"
            "三重过滤提高信号质量，减少假信号。"
        ),
        "indicators": ["rsi"],
        "params": {
            "rsi_fast":       _p_int(5, 10, 1),
            "rsi_mid":        _p_int(11, 18, 1),       # 原 10-18 → 11-18，避免与fast重叠
            "rsi_slow":       _p_int(19, 30, 1),       # 原 18-30 → 19-30，避免与mid重叠
            "rsi_entry":      _p_int(28, 42, 1),       # 原 25-45 → 28-42，收窄
            "rsi_trend_mid":  _p_int(42, 53, 1),       # 原 40-55 → 42-53
            "rsi_trend_slow": _p_int(42, 53, 1),       # 原 40-55 → 42-53
            "stop_loss_pct":  _p_float(2.5, 4.5, 0.5), # 原 2.5-5 → 2.5-4.5
        },
        "constraints": [
            ("rsi_fast", "<", "rsi_mid"),
            ("rsi_mid", "<", "rsi_slow"),
        ],
        "build_config": _build_triple_rsi_momentum_config,
    },

    # ── 4. VWAP + 布林带收缩 ──
    # Phase 1 亚军(Sharpe 1.101, 100%正得分)，微调去极端
    "vwap_bollinger_squeeze": {
        "name": "VWAP偏离+布林带收缩",
        "description": (
            "VWAP 偏离 + 布林带下轨 + 带宽收缩三重确认。"
            "数据依据: vwap_deviation (#1) + bollinger_rsi_squeeze (#5, 0.54)。"
            "均值回归+收缩蓄势，极致过滤。"
        ),
        "indicators": ["vwap", "bollinger"],
        "params": {
            "vwap_dev_pct":       _p_float(1.0, 3.5, 0.1),  # 原 1-4 → 1-3.5
            "bb_period":          _p_int(12, 28, 1),         # 原 10-30 → 12-28
            "bb_std":             _p_float(1.8, 2.8, 0.1),   # 原 1.5-3 → 1.8-2.8
            "use_squeeze_filter": _p_choice([True, False]),
            "squeeze_percentile": _p_int(10, 25, 5),         # 原 10-30 → 10-25
            "stop_loss_pct":      _p_float(2.0, 3.5, 0.5),   # 原 2-4 → 2-3.5
        },
        "constraints": [],
        "build_config": _build_vwap_bollinger_squeeze_config,
    },

    # ── 5. MACD 底背离 + 量价背离 ──
    # Phase 1 最差(Sharpe -0.309)，已修复 histogram→diff_lt_dea
    # v2: 固定 RSI 确认(减少参数维度)，围绕经典 MACD(12,26,9)
    "macd_vol_divergence": {
        "name": "MACD底背离+量价背离",
        "description": (
            "MACD 柱状线翻红 + 量价底背离双重反转确认。"
            "数据依据: macd_kdj_resonance (#6, 0.51) + volume_price_divergence (#3, 0.60)。"
            "动量反转+形态确认，捕捉底部反转。"
            "v2: 修复 histogram_negative→diff_lt_dea，固定RSI确认，收窄参数空间。"
        ),
        "indicators": ["macd", "volume", "rsi"],
        "params": {
            "macd_fast":       _p_int(10, 14, 1),        # 围绕标准12
            "macd_slow":       _p_int(22, 30, 1),        # 围绕标准26
            "macd_signal":     _p_int(7, 11, 1),         # 围绕标准9
            "lookback_period": _p_int(15, 30, 1),
            "price_ma_period": _p_int(5, 15, 1),
            "vol_ma_period":   _p_int(10, 20, 1),
            # 固定 RSI 确认开启 + 标准周期14，去掉 3 个自由参数
            "use_rsi_confirm": _p_choice([True]),
            "rsi_period":      _p_int(14, 14, 1),
            "rsi_level":       _p_int(30, 40, 1),        # 原 25-45 → 30-40
            "stop_loss_pct":   _p_float(3.0, 5.0, 0.5),
        },
        "constraints": [
            ("macd_fast", "<", "macd_slow"),
            ("price_ma_period", "<", "lookback_period"),
        ],
        "build_config": _build_macd_vol_divergence_config,
    },
}
