"""
统一评分模块
所有策略评分逻辑集中在此，避免多处实现不一致。

小资金策略评分哲学：
  - 集中火力干大的：奖励高收益、高盈亏比
  - 忽略 <2% 微利交易：赚小钱扣佣金没意义
  - 惩罚频繁交易：小资金不需要高频操作
  - 胜率不等于赚钱：高胜率可能赢小亏大
  - 核心看收益回撤比：Calmar 思路

A 股对数比例说明：
  - 收益使用 log(1 + r)：复利下更准确，消除百分比不对称
    （涨 100% 需要跌 50% 才回本，log 对称处理）
  - 回撤使用 log(peak/valley)：与 log 收益标尺一致
  - 微利阈值仍用简单百分比（与交易成本直观对应）
"""
import math

# avgProfit/avgLoss 单位是百分比（如 2.0 表示 2%），阈值对应 2%
SMALL_TRADE_THRESHOLD = 2.0  # 2%：平均每笔盈利 < 2% 视为噪音


def _log_return(pct: float) -> float:
    """百分比收益 → 对数收益。pct=20 表示 20%"""
    if pct <= -100:
        return -10.0  # 归零
    return math.log(1 + pct / 100.0)


def _log_drawdown(pct: float) -> float:
    """百分比回撤 → 对数回撤。pct=30 表示 30% 回撤"""
    if pct <= 0:
        return 0.0
    if pct >= 100:
        return 10.0
    return math.log(1 / (1 - pct / 100.0))


def compute_score(metrics: dict, score_fn: str = "composite") -> float:
    """
    统一评分函数

    Args:
        metrics: 回测指标 dict，包含 sharpeRatio, totalReturn, winRate,
                 maxDrawdown, profitFactor, totalTrades, avgProfit, avgLoss
        score_fn: 评分方式 "sharpe" | "return_dd_ratio" | "composite"

    Returns:
        float: 得分（越高越好）
    """
    sharpe = float(metrics.get("sharpeRatio", 0))
    win_rate = float(metrics.get("winRate", 0)) / 100.0
    max_dd_raw = float(metrics.get("maxDrawdown", 0))  # 百分比，如 15.3
    total_return_raw = float(metrics.get("totalReturn", 0))  # 百分比，如 23.5
    total_trades = int(metrics.get("totalTrades", 0))
    profit_factor = float(metrics.get("profitFactor", 0))
    avg_profit = float(metrics.get("avgProfit", 0))  # 百分比
    avg_loss = float(metrics.get("avgLoss", 0))  # 百分比

    # ── 基础过滤 ──
    if total_trades < 3:
        return -10.0

    # 平均盈利 < 2% → 策略在做无意义的微利交易
    if avg_profit > 0 and avg_profit < SMALL_TRADE_THRESHOLD:
        return -5.0

    # ── 对数转换 ──
    total_return = _log_return(total_return_raw)
    max_dd = _log_drawdown(max_dd_raw)

    # ── 有效指标修正 ──
    effective_win_rate = win_rate
    effective_pf = profit_factor
    if avg_profit > 0 and avg_profit < SMALL_TRADE_THRESHOLD:
        effective_win_rate = 0.0
        effective_pf = 1.0

    # ── 评分方式 ──
    if score_fn == "sharpe":
        return sharpe

    if score_fn == "return_dd_ratio":
        if max_dd <= 0:
            return total_return * 10
        return total_return / max_dd

    if score_fn == "composite":
        # 交易频率惩罚
        trade_penalty = 0.0
        if total_trades < 5:
            trade_penalty = (5 - total_trades) * 0.05
        elif total_trades > 30:
            trade_penalty = (total_trades - 30) * 0.06

        # 收益回撤比（Calmar，对数标尺）
        calmar = total_return / max(max_dd, 0.01) if max_dd > 0 else total_return * 5

        return (
            total_return * 0.30              # 对数收益
            + min(calmar, 5.0) * 0.20        # 对数 Calmar
            + min(effective_pf, 5.0) * 0.15  # 有效盈亏比
            + effective_win_rate * 0.10      # 有效胜率
            + sharpe * 0.15                  # 风险调整收益
            - max_dd * 0.25                  # 对数回撤惩罚
            - trade_penalty
        )

    return sharpe
