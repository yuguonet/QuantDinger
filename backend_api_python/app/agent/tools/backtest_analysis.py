# -*- coding: utf-8 -*-
"""策略回测分析 — 评估用户策略的历史胜率、盈亏比、最大回撤。"""
from __future__ import annotations

from typing import Any, Dict


def _algo_analyze(stock_code: str, stock_name: str, tool_results: dict, call_tool_fn=None):
    """对用户策略跑历史回测，返回评分(0-100)、方向、胜率、盈亏比、最大回撤。无策略时返回 hold/50。

    Args:
        stock_code: 股票代码
        stock_name: 股票名称
        tool_results: 工具调用结果字典（需含 list_strategies）
        call_tool_fn: 工具调用函数（可选）
    """
    factors = []
    best_score = 50.0
    best_strategy = None

    strategies = tool_results.get("list_strategies", {})
    strat_list = strategies.get("strategies", []) if isinstance(strategies, dict) else strategies

    if not strat_list:
        return {"action": "hold", "score": 50, "direction": "neutral",
                "signal": "无用户策略", "confidence": "low", "factors": [], "analysis": "无策略可回测", "status": "ok"}

    for strat in strat_list[:3]:
        strat_id = strat.get("id")
        strat_name = strat.get("name", f"策略{strat_id}")
        if not strat_id: continue

        bt_result = None
        if call_tool_fn:
            try: bt_result = call_tool_fn("run_backtest", strategy_id=strat_id, stock_code=stock_code)
            except Exception: pass

        if isinstance(bt_result, dict) and "error" not in bt_result:
            win_rate = bt_result.get("win_rate", 0)
            profit_loss_ratio = bt_result.get("profit_loss_ratio", 0)
            max_drawdown = bt_result.get("max_drawdown", 0)

            if win_rate >= 60 and profit_loss_ratio >= 2: score = 75
            elif win_rate >= 50 and profit_loss_ratio >= 1.5: score = 60
            elif win_rate < 40 or max_drawdown > 30: score = 30
            else: score = 50

            if score > best_score: best_score = score; best_strategy = strat_name
            factors.append({"name": f"回测:{strat_name}", "value": f"胜率{win_rate:.0%} 盈亏比{profit_loss_ratio:.1f} 回撤{max_drawdown:.0%}", "score": score})

    if not factors:
        return {"action": "hold", "score": 50, "direction": "neutral",
                "signal": "回测未产生结果", "confidence": "low", "factors": [], "analysis": "无数据", "status": "ok"}

    direction = "bullish" if best_score >= 60 else ("bearish" if best_score <= 40 else "neutral")
    return {
        "action": "hold", "score": best_score,
        "direction": direction, "confidence": "medium",
        "signal": f"最佳策略:{best_strategy}" if best_strategy else "回测完成",
        "factors": factors, "analysis": f"回测{len(factors)}个策略，最佳:{best_strategy}", "status": "ok",
    }

def backtest_analysis(stock_code: str, stock_name: str = "") -> dict:
    """一站式回测：自动列出用户策略 → 逐个跑回测 → 返回最佳策略评分。等价于 list_strategies + run_backtest。

    Args:
        stock_code: 股票代码，如 "600066"
        stock_name: 股票名称，可选
    """
        
    results = {}
    try: results["list_strategies"] = list_strategies()
    except Exception as e: results["list_strategies"] = {"error": str(e)}

    def call_tool_fn(name, **kwargs):
        if name == "run_backtest": return run_backtest(**kwargs)
        raise ValueError(f"Unknown tool: {name}")

    return _algo_analyze(stock_code, stock_name, results, call_tool_fn=call_tool_fn)


    main()


# ── 内联自 trading_tools.py + backtest_tools.py ──

def list_strategies(user_id: int = 1) -> Dict[str, Any]:
    """列出用户的所有交易策略（含运行状态）。

    返回策略 ID、名称、类型、状态、交易对、时间框架等信息。

    Args:
        user_id: 用户 ID，默认 1
    """
    if not _TRADING_DEPS_OK:
        return {"strategies": [], "count": 0, "error": f"交易依赖缺失: {_TRADING_DEPS_ERROR}"}

    try:
        svc = StrategyService()
        items = svc.list_strategies(user_id=user_id)

        strategies = []
        for s in items or []:
            strategies.append({
                "id": s.get("id"),
                "name": s.get("name", ""),
                "strategy_type": s.get("strategy_type", ""),
                "status": s.get("status", ""),
                "symbol": s.get("symbol", ""),
                "market": s.get("market", ""),
                "timeframe": s.get("timeframe", ""),
                "created_at": str(s.get("created_at", "")),
            })

        return {"strategies": strategies, "count": len(strategies)}
    except Exception as e:
        logger.error("list_strategies failed: %s", e, exc_info=True)
        return {"strategies": [], "count": 0, "error": str(e)}

def run_backtest(
    strategy_id: int,
    stock_code: str,
    start_date: str,
    end_date: str,
    timeframe: str = "1D",
    user_id: int = 1,
) -> Dict[str, Any]:
    """对指定策略在指定股票上跑历史回测，返回绩效指标。

    Args:
        strategy_id: 策略 ID
        stock_code: 股票代码（如 600519）或交易对（如 BTC/USDT）
        start_date: 回测开始日期 YYYY-MM-DD
        end_date: 回测结束日期 YYYY-MM-DD
        timeframe: K 线周期，默认 1D（可选 1H, 4H, 1W）
        user_id: 用户 ID，默认 1
    """
    if not _BACKTEST_DEPS_OK:
        return {"success": False, "error": f"回测依赖缺失: {_BACKTEST_DEPS_ERROR}"}

    # 参数校验
    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    except ValueError:
        return {"success": False, "error": "日期格式错误，请使用 YYYY-MM-DD"}

    if end_dt <= start_dt:
        return {"success": False, "error": "结束日期必须晚于开始日期"}

    days_diff = (end_dt - start_dt).days
    tf_limits = {"1m": 30, "5m": 180, "15m": 365, "30m": 365, "1H": 730, "4H": 730, "1D": 1095, "1W": 1095}
    max_days = tf_limits.get(timeframe, 1095)
    if days_diff > max_days:
        return {"success": False, "error": f"{timeframe} 周期最多回测 {max_days} 天，您选择了 {days_diff} 天"}

    # 获取策略
    try:
        svc = StrategyService()
        strategy = svc.get_strategy(strategy_id, user_id=user_id)
        if not strategy:
            return {"success": False, "error": f"策略 {strategy_id} 不存在"}
    except Exception as e:
        return {"success": False, "error": f"获取策略失败: {e}"}

    # 构建快照
    try:
        resolver = StrategySnapshotResolver(user_id=user_id)
        snapshot = resolver.resolve(strategy, {})
        snapshot["user_id"] = user_id
    except Exception as e:
        return {"success": False, "error": f"构建策略快照失败: {e}"}

    # 跑回测
    try:
        bt_svc = BacktestService()
        result = bt_svc.run_strategy_snapshot(snapshot, start_date=start_dt, end_date=end_dt)

        if not result:
            return {"success": False, "error": "回测返回空结果"}

        # 提取关键绩效指标
        summary = {
            "total_return_pct": result.get("total_return_pct", 0),
        }

        # 交易明细摘要（最近 10 笔）
        trades = result.get("trades") or []
        recent_trades = []
        for t in trades[-10:]:
            recent_trades.append({
                "type": t.get("type", ""),
            })

        return {
            "success": True,
        }
    except Exception as e:
        logger.error("run_backtest failed: %s", e, exc_info=True)
        return {"success": False, "error": f"回测执行失败: {e}"}
