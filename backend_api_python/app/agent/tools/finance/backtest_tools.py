# -*- coding: utf-8 -*-
"""
Backtest tools — run backtests and query history.

Wraps BacktestService into Agent-callable tools.
依赖：app.services.strategy, app.services.backtest, app.services.strategy_snapshot
"""
from __future__ import annotations

from app.agent.log import logger
from datetime import datetime
from typing import Any, Dict, List, Optional
from app.agent.utils.md_format import _to_md
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

# 绩效指标键名以 BacktestService._calculate_metrics 为准：**camelCase**，且由
# _format_result 平铺在 result **顶层**（不是嵌在 metrics/summary 下）。
# 易错：2026-09-20 前这里按 snake_case 取 `total_return_pct` —— 该键根本不存在，
# 取数**恒为 0**（静默失效，且因为结果没回传而没被发现）。
_METRIC_KEYS = (
    "totalReturn", "annualReturn", "maxDrawdown", "sharpeRatio",
    "winRate", "profitFactor", "totalTrades", "totalProfit", "totalCommission",
)
# 单笔交易里值得回给模型的字段（与 BacktestService 落库 qd_backtest_trades 用的同一批键）
_TRADE_KEYS = ("time", "type", "price", "amount", "profit", "reason")


def run_backtest(
    strategy_id: int,
    stock_code: str,
    start_date: str,
    end_date: str,
    timeframe: str = "1D",
    user_id: int = 1,
) -> Dict[str, Any]:
    """策略回测：返回指定策略在指定股票上的胜率、盈亏比、最大回撤、交易次数等绩效指标。

    Returns:
        dict: {success, summary:{totalReturn, annualReturn, maxDrawdown, sharpeRatio,
        winRate, profitFactor, totalTrades, totalProfit}, trade_count,
        recent_trades:[{time,type,price,amount,profit,reason}]}

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
        # 易错（2026-09-20 前）：这里传的是空 override，**`stock_code` / `timeframe` 两个
        # 入参被静默忽略** —— 回测实际跑在策略自己配置的标的与周期上，与工具签名和描述
        # 承诺的不一致（"回测 600519"可能跑的是 000858）。resolve 的第二个参数就是
        # override 钩子（见 strategy_snapshot.resolve 的 override.get("symbol"/"timeframe")）。
        snapshot = resolver.resolve(strategy, {"symbol": stock_code, "timeframe": timeframe})
        snapshot["user_id"] = user_id
    except Exception as e:
        return {"success": False, "error": f"构建策略快照失败: {e}"}

    # 跑回测
    try:
        bt_svc = BacktestService()
        result = bt_svc.run_strategy_snapshot(snapshot, start_date=start_dt, end_date=end_dt)

        if not result:
            return {"success": False, "error": "回测返回空结果"}
        if result.get("error"):
            # script 类策略的失败路径直接返回 {"error": ...}（无 success 键）
            return {"success": False, "error": str(result["error"])}

        # 绩效指标：平铺在 result 顶层（camelCase，见 _METRIC_KEYS 注释）。
        # script 类策略的 output 键由脚本自定义，可能一个标准指标都没有 → 明确说明，
        # 免得模型以为"回测成功了但指标是空的"。
        summary = {k: result[k] for k in _METRIC_KEYS if k in result}
        if not summary:
            summary = {"note": "该策略未输出标准绩效指标（script 类策略的 output 键由脚本自定义）"}

        # 交易明细摘要：trades 可能有上千笔，只回最近 10 笔的要点字段。
        # equityCurve 是完整资金曲线（点数≈K线根数），**刻意不回传**——回传必撑爆上下文，
        # 模型要看资金曲线形态应该用 summary 里的 maxDrawdown/totalReturn。
        trades = result.get("trades") or []
        recent_trades = [
            {k: t[k] for k in _TRADE_KEYS if t.get(k) is not None}
            for t in trades[-10:]
        ]
        return {
            "success": True,
            "summary": summary,
            "trade_count": len(trades),
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
    """回测历史：返回指定策略过往回测的时间、股票、绩效指标记录。

    Returns:
        dict: {runs:[{id, strategy_id, symbol, timeframe, status, created_at,
        total_return, annual_return, win_rate, total_trades}], count}；失败→{runs:[], count:0, error}

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
            # 绩效指标直接读行上的扁平字段：BacktestService._hydrate_run_row 已把
            # result_json 里的 totalReturn/winRate/... 抽成 total_return/win_rate/... 落在行上。
            # 易错：2026-09-20 前这里自己解 result 并按 `total_return_pct` 取（键不存在 ⇒ 恒 0），
            # 既失效又与行上已有的字段重复。
            d.pop("result", None)  # 不返回完整结果，太大
            runs.append(d)

        _r = {"runs": runs, "count": len(runs)}
        return _r
    except Exception as e:
        logger.error("get_backtest_history failed: %s", e, exc_info=True)
        return {"runs": [], "count": 0, "error": str(e)}

