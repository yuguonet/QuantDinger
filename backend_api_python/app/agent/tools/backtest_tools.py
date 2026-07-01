# -*- coding: utf-8 -*-
"""
Backtest tools — run backtests and query history.

Wraps BacktestService into Agent-callable tools.
依赖：app.services.strategy, app.services.backtest, app.services.strategy_snapshot
"""
from __future__ import annotations

import json
from app.agent.log import logger
from datetime import datetime
from typing import Any, Dict, List, Optional
# ── 显式依赖检查 ──────────────────────────────────────────────
try:
    from app.services.strategy import StrategyService
    from app.services.backtest import BacktestService
    from app.services.strategy_snapshot import StrategySnapshotResolver
    _BACKTEST_DEPS_OK = True
    _BACKTEST_DEPS_ERROR = None
except ImportError as _e:
    _BACKTEST_DEPS_OK = False
    _BACKTEST_DEPS_ERROR = str(_e)
    logger.warning("[backtest_tools] 依赖缺失: %s — 回测功能不可用", _e)

# ── Tool functions ────────────────────────────────────────────

def run_backtest(
    strategy_id: int,
    stock_code: str,
    start_date: str,
    end_date: str,
    timeframe: str = "1D",
    user_id: int = 1,
) -> Dict[str, Any]:
    """策略回测：返回指定策略在指定股票上的胜率、盈亏比、最大回撤、交易次数等绩效指标。

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

def get_backtest_history(
    strategy_id: int,
    user_id: int = 1,
    limit: int = 10,
) -> Dict[str, Any]:
    """回测历史：返回指定策略过往回测的时间、股票、绩效指标记录。

    Args:
        strategy_id: 策略 ID
        user_id: 用户 ID，默认 1
        limit: 返回条数，默认 10
    """
    if not _BACKTEST_DEPS_OK:
        return {"runs": [], "count": 0, "error": f"回测依赖缺失: {_BACKTEST_DEPS_ERROR}"}

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
                }
            d.pop("result", None)  # 不返回完整结果，太大
            runs.append(d)

        return {"runs": runs, "count": len(runs)}
    except Exception as e:
        logger.error("get_backtest_history failed: %s", e, exc_info=True)
        return {"runs": [], "count": 0, "error": str(e)}

