# -*- coding: utf-8 -*-
"""
Backtest tools — run backtests and query history.

Wraps BacktestService into Agent-callable tools.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Tool functions ────────────────────────────────────────────

def run_backtest(
    strategy_id: int,
    stock_code: str,
    start_date: str,
    end_date: str,
    timeframe: str = "1D",
    user_id: int = 1,
) -> Dict[str, Any]:
    """对指定策略在指定股票上跑历史回测，返回绩效指标。

    执行策略代码在历史 K 线上模拟交易，计算收益率、胜率、最大回撤等。

    Args:
        strategy_id: 策略 ID
        stock_code: 股票代码（如 600519）或交易对（如 BTC/USDT）
        start_date: 回测开始日期 YYYY-MM-DD
        end_date: 回测结束日期 YYYY-MM-DD
        timeframe: K 线周期，默认 1D（可选 1H, 4H, 1W）
        user_id: 用户 ID，默认 1
    """
    from app.services.strategy import StrategyService
    from app.services.backtest import BacktestService
    from app.services.strategy_snapshot import StrategySnapshotResolver

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
            "win_rate": result.get("win_rate", 0),
            "total_trades": result.get("total_trades", 0),
            "max_drawdown_pct": result.get("max_drawdown_pct", 0),
            "sharpe_ratio": result.get("sharpe_ratio", 0),
            "profit_factor": result.get("profit_factor", 0),
            "initial_capital": result.get("initial_capital", 0),
            "final_equity": result.get("final_equity", 0),
            "best_trade_pct": result.get("best_trade_pct", 0),
            "worst_trade_pct": result.get("worst_trade_pct", 0),
            "avg_trade_pct": result.get("avg_trade_pct", 0),
        }

        # 交易明细摘要（最近 10 笔）
        trades = result.get("trades") or []
        recent_trades = []
        for t in trades[-10:]:
            recent_trades.append({
                "type": t.get("type", ""),
                "price": t.get("price", 0),
                "amount": t.get("amount", 0),
                "profit": t.get("profit", 0),
                "profit_pct": t.get("profit_pct", 0),
                "time": str(t.get("time", "")),
            })

        return {
            "success": True,
            "strategy_id": strategy_id,
            "strategy_name": strategy.get("name", ""),
            "stock_code": stock_code,
            "timeframe": timeframe,
            "period": f"{start_date} ~ {end_date}",
            "days": days_diff,
            "summary": summary,
            "recent_trades": recent_trades,
        }
    except Exception as e:
        logger.error("run_backtest failed: %s", e, exc_info=True)
        return {"success": False, "error": f"回测执行失败: {e}"}


def get_backtest_history(
    strategy_id: int,
    user_id: int = 1,
    limit: int = 10,
) -> Dict[str, Any]:
    """查询策略的历史回测记录。

    Args:
        strategy_id: 策略 ID
        user_id: 用户 ID，默认 1
        limit: 返回条数，默认 10
    """
    from app.services.backtest import BacktestService

    limit = min(max(limit, 1), 50)

    try:
        bt_svc = BacktestService()
        rows = bt_svc.list_runs(
            user_id=user_id,
            strategy_id=strategy_id,
            limit=limit,
            offset=0,
        )

        runs = []
        for r in rows or []:
            d = dict(r)
            for ts_field in ("created_at", "started_at", "completed_at"):
                if d.get(ts_field) and hasattr(d[ts_field], "isoformat"):
                    d[ts_field] = d[ts_field].isoformat()
            # 提取关键指标
            result_data = d.get("result")
            if isinstance(result_data, str):
                try:
                    result_data = json.loads(result_data)
                except Exception:
                    result_data = {}
            if isinstance(result_data, dict):
                d["summary"] = {
                    "total_return_pct": result_data.get("total_return_pct", 0),
                    "win_rate": result_data.get("win_rate", 0),
                    "total_trades": result_data.get("total_trades", 0),
                    "max_drawdown_pct": result_data.get("max_drawdown_pct", 0),
                }
            d.pop("result", None)  # 不返回完整结果，太大
            runs.append(d)

        return {"runs": runs, "count": len(runs)}
    except Exception as e:
        logger.error("get_backtest_history failed: %s", e, exc_info=True)
        return {"runs": [], "count": 0, "error": str(e)}


# ── OpenAI tool declarations ─────────────────────────────────

BACKTEST_TOOLS = [
    {
        "fn": run_backtest,
        "name": "run_backtest",
        "description": (
            "对指定策略在指定股票上跑历史回测。执行策略代码模拟交易，返回收益率、"
            "胜率、最大回撤、夏普比率等绩效指标和最近交易明细。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "strategy_id": {
                    "type": "integer",
                    "description": "策略 ID",
                },
                "stock_code": {
                    "type": "string",
                    "description": "股票代码（如 600519）或交易对（如 BTC/USDT）",
                },
                "start_date": {
                    "type": "string",
                    "description": "回测开始日期 YYYY-MM-DD",
                },
                "end_date": {
                    "type": "string",
                    "description": "回测结束日期 YYYY-MM-DD",
                },
                "timeframe": {
                    "type": "string",
                    "description": "K线周期，默认 1D",
                    "default": "1D",
                },
                "user_id": {
                    "type": "integer",
                    "description": "用户 ID，默认 1",
                    "default": 1,
                },
            },
            "required": ["strategy_id", "stock_code", "start_date", "end_date"],
        },
    },
    {
        "fn": get_backtest_history,
        "name": "get_backtest_history",
        "description": "查询策略的历史回测记录列表，返回每次回测的关键绩效指标。",
        "parameters": {
            "type": "object",
            "properties": {
                "strategy_id": {
                    "type": "integer",
                    "description": "策略 ID",
                },
                "user_id": {
                    "type": "integer",
                    "description": "用户 ID，默认 1",
                    "default": 1,
                },
                "limit": {
                    "type": "integer",
                    "description": "返回条数，默认 10",
                    "default": 10,
                },
            },
            "required": ["strategy_id"],
        },
    },
]
