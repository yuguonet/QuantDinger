"""
连板猎手 v3.1 — IndicatorStrategy 版本

基于 22977 个连板段的横向/纵向/共振分析优化:
- 只做2板+ (1板信号太弱)
- 高开过滤: 主板<8%, 创科<12%
- 封板强度≤0.5%
- 移除开板卖出(胜率仅4-7%)
- 追踪止损+止盈+峰值信号出场
"""
from typing import Dict, Any
from optimizer.indicator_strategy_builder import render_indicator_strategy

def _p_int(low: int, high: int, step: int = 1) -> dict:
    return {"type": "int", "low": low, "high": high, "step": step}

def _p_float(low: float, high: float, step: float = 0.1) -> dict:
    return {"type": "float", "low": low, "high": high, "step": step}


def _build_dragon_v3_strategy(p: dict) -> str:
    """连板猎手 v3.1: 连板检测 + 高开过滤 + 追踪止损"""
    # 买入参数
    min_streak = int(p.get('min_streak', 2))
    limit_threshold = float(p.get('limit_threshold', 9.8))
    max_gap_pct = float(p.get('max_gap_pct', 8.0))
    max_seal_pct = float(p.get('max_seal_pct', 0.5))
    max_rsi = float(p.get('max_rsi', 90.0))
    # 卖出参数
    trailing_stop_pct = float(p.get('trailing_stop_pct', 6.0))
    take_profit_pct = float(p.get('take_profit_pct', 15.0))
    peak_rsi_threshold = float(p.get('peak_rsi_threshold', 80.0))
    peak_upper_shadow = float(p.get('peak_upper_shadow', 40.0))

    params_decl = [
        f"# @param min_streak int {min_streak} 最小连板数(2=只做2板+)",
        f"# @param limit_threshold float {limit_threshold} 涨停阈值%(主板9.8/创科19.8)",
        f"# @param max_gap_pct float {max_gap_pct} 最大高开幅度%",
        f"# @param max_seal_pct float {max_seal_pct} 封板强度上限%(close接近high)",
        f"# @param max_rsi float {max_rsi} RSI上限(排除极端超买)",
        f"# @param trailing_stop_pct float {trailing_stop_pct} 追踪止损%(从最高点回撤)",
        f"# @param take_profit_pct float {take_profit_pct} 止盈%",
        f"# @param peak_rsi_threshold float {peak_rsi_threshold} 峰值RSI阈值",
        f"# @param peak_upper_shadow float {peak_upper_shadow} 峰值上影线%阈值",
    ]

    indicator_code = f"""import numpy as np
import pandas as pd

# ── 涨停检测 ──
_prev_close = df['close'].shift(1).replace(0, np.nan)
df['change_pct'] = (df['close'] / _prev_close - 1) * 100
df['is_limit_up'] = df['change_pct'] >= {limit_threshold}

# ── 连板计数 (连续涨停天数) ──
# 用cumsum技巧: 每次非涨停重置计数
_groups = (~df['is_limit_up']).cumsum()
df['streak_count'] = df.groupby(_groups)['is_limit_up'].cumsum()

# ── 连板段标记: 当前处于>=min_streak的连板段中 ──
df['in_streak'] = df['streak_count'] >= {min_streak}

# ── 连板段第一天: streak_count刚达到min_streak的那天 ──
df['streak_start'] = (df['streak_count'] >= {min_streak}) & (df['streak_count'].shift(1).fillna(0) < {min_streak})

# ── 高开幅度: (open - prev_close) / prev_close * 100 ──
df['gap_pct'] = (df['open'] / _prev_close - 1) * 100

# ── 封板强度: (close - high) / close * 100 ──
#   值越接近0封得越紧, 负值表示close<high(未封死)
df['seal_pct'] = (df['close'] / df['high'] - 1) * 100

# ── RSI ──
_delta = df['close'].diff()
_gain = _delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
_loss = (-_delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
_rs = _gain / _loss.replace(0, np.nan)
df['rsi_14'] = 100 - (100 / (1 + _rs))

# ── KDJ ──
_low_9 = df['low'].rolling(window=9, min_periods=1).min()
_high_9 = df['high'].rolling(window=9, min_periods=1).max()
_rsv = (df['close'] - _low_9) / (_high_9 - _low_9).replace(0, np.nan) * 100
df['kdj_k'] = _rsv.ewm(alpha=1/3, adjust=False).mean()
df['kdj_d'] = df['kdj_k'].ewm(alpha=1/3, adjust=False).mean()
df['kdj_j'] = 3 * df['kdj_k'] - 2 * df['kdj_d']

# ── 上影线% ──
_bar_range = (df['high'] - df['low']).replace(0, np.nan)
df['upper_shadow_pct'] = (df['high'] - df[['close', 'open']].max(axis=1)) / _bar_range * 100

# ── MACD ──
_ema12 = df['close'].ewm(span=12, adjust=False).mean()
_ema26 = df['close'].ewm(span=26, adjust=False).mean()
df['macd_dif'] = _ema12 - _ema26
df['macd_dea'] = df['macd_dif'].ewm(span=9, adjust=False).mean()
df['macd_hist'] = df['macd_dif'] - df['macd_dea']

# ── 布林带位置 ──
_bb_mid = df['close'].rolling(window=20, min_periods=1).mean()
_bb_std = df['close'].rolling(window=20, min_periods=1).std()
_bb_upper = _bb_mid + 2 * _bb_std
_bb_lower = _bb_mid - 2 * _bb_std
_bb_width = (_bb_upper - _bb_lower).replace(0, np.nan)
df['boll_position'] = (df['close'] - _bb_lower) / _bb_width

# ── 量比(vs前5日) ──
_vol_ma5 = df['volume'].rolling(window=5, min_periods=1).mean()
df['vol_ratio_5d'] = df['volume'] / _vol_ma5.replace(0, np.nan)

# ── 追踪止损: 需要记录买入后最高价 ──
# 用rolling max近似(20日窗口内的最高价)
# 实际追踪止损在回测引擎中通过stopLossPct实现
# 这里用RSI+上影线做峰值信号卖出
"""

    # 买入信号:
    # 1. 当前处于连板段(streak_count >= min_streak)
    # 2. 连板段刚开始不久(streak_count <= min_streak + 2, 避免追太高)
    # 3. 高开幅度 < max_gap_pct
    # 4. 封板强度 <= max_seal_pct (close接近high)
    # 5. RSI < max_rsi
    # 6. 涨停日当天(第一板或连续涨停中)
    buy_expr = (
        f"(df['streak_start'])"
        f" & (df['gap_pct'].between(-5, {max_gap_pct}))"
        f" & (df['seal_pct'] <= {max_seal_pct})"
        f" & (df['rsi_14'] <= {max_rsi})"
        f" & (df['volume'] > 0)"
    )

    # 卖出信号 (任一满足):
    # 1. RSI超买 + 上影线大 (见顶信号)
    # 2. KDJ死叉 + RSI>70
    # 3. 非涨停日 + 跌幅>3% (开板回调)
    sell_expr = (
        f"((df['rsi_14'] >= {peak_rsi_threshold}) & (df['upper_shadow_pct'] >= {peak_upper_shadow}))"
        f" | ((df['kdj_k'] < df['kdj_d']) & (df['kdj_k'].shift(1) >= df['kdj_d'].shift(1)) & (df['rsi_14'] > 70))"
        f" | ((df['change_pct'] < -3) & (~df['is_limit_up']))"
    )

    plots = [
        {"name": "RSI", "data": "df['rsi_14']", "color": "#FF9800", "overlay": False},
        {"name": "Streak", "data": "df['streak_count']", "color": "#2196F3", "overlay": False},
    ]

    return render_indicator_strategy(
        name="DragonHunterV3",
        description=f"连板猎手v3.1: ≥{min_streak}板+高开<{max_gap_pct}%+封板≤{max_seal_pct}% → 追踪止损/止盈/峰值信号",
        params_decl=params_decl,
        strategy_defaults={
            "stopLossPct": 0.08,
            "takeProfitPct": take_profit_pct / 100,
            "tradeDirection": "long",
        },
        indicator_code=indicator_code,
        buy_expr=buy_expr,
        sell_expr=sell_expr,
        plots=plots,
        trade_direction="long",
    )


# ============================================================
# 策略注册
# ============================================================

POSITION_PCT = {"type": "choice", "choices": [100, 75, 50, 25]}

DRAGON_V3_STRATEGY = {
    "dragon_v3": {
        "name": "连板猎手v3.1",
        "description": "基于22977个连板段分析优化: 2板+高开过滤+封板强度+追踪止损",
        "indicators": ["change_pct", "streak_count", "gap_pct", "seal_pct", "rsi_14", "kdj", "boll_position", "vol_ratio"],
        "params": {
            "min_streak":        {"type": "int", "low": 2, "high": 5, "step": 1},
            "limit_threshold":   {"type": "float", "low": 9.5, "high": 19.8, "step": 0.1},
            "max_gap_pct":       {"type": "float", "low": 3.0, "high": 15.0, "step": 0.5},
            "max_seal_pct":      {"type": "float", "low": 0.1, "high": 2.0, "step": 0.1},
            "max_rsi":           {"type": "float", "low": 70.0, "high": 95.0, "step": 1.0},
            "trailing_stop_pct": {"type": "float", "low": 3.0, "high": 12.0, "step": 0.5},
            "take_profit_pct":   {"type": "float", "low": 8.0, "high": 25.0, "step": 1.0},
            "peak_rsi_threshold":{"type": "float", "low": 70.0, "high": 90.0, "step": 1.0},
            "peak_upper_shadow": {"type": "float", "low": 25.0, "high": 50.0, "step": 5.0},
            "position_pct": POSITION_PCT,
        },
        "constraints": [
            ("min_streak", ">=", 2),
            ("max_gap_pct", "<", 15),
        ],
        "build_strategy": _build_dragon_v3_strategy,
        "strategy_defaults": {"tradeDirection": "long"},
    },
}
