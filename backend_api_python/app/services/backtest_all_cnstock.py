"""
╔══════════════════════════════════════════════════════════════════╗
║                  全A股多策略回测筛选                              ║
╠══════════════════════════════════════════════════════════════════╣
║                                                                  ║
║  功能：                                                           ║
║    1. 支持同时传入多个策略（指标ID + 自定义周期配置）              ║
║    2. 每个策略独立跑全A股回测                                     ║
║    3. 结果写入 qd_backtest_runs 表（复用已有表结构）              ║
║    4. 去掉新闻模块                                               ║
║                                                                  ║
║  用法：                                                           ║
║    from app.services.backtest_all_cnstock import backtest_all     ║
║                                                                  ║
║    # 单策略                                                       ║
║    for msg in backtest_all(indicator_id=1, user_id=1):            ║
║        print(msg)                                                 ║
║                                                                  ║
║    # 多策略 + 自定义周期                                          ║
║    strategies = [                                                 ║
║        {                                                          ║
║            "indicator_id": 1,                                     ║
║            "name": "RSI策略",                                     ║
║            "periods": [                                           ║
║                {"tf": "1D", "months": 6, "label": "6月线"},       ║
║                {"tf": "1D", "months": 3, "label": "3月线"},       ║
║            ],                                                     ║
║        },                                                         ║
║        {                                                          ║
║            "indicator_id": 2,                                     ║
║            "name": "MACD策略",                                    ║
║            "periods": [                                           ║
║                {"tf": "1W", "months": 12, "label": "年线"},       ║
║            ],                                                     ║
║        },                                                         ║
║    ]                                                              ║
║    for msg in backtest_all(strategies=strategies, user_id=1):     ║
║        print(msg)                                                 ║
║                                                                  ║
║  命令行：                                                         ║
║    python -m app.services.backtest_all_cnstock --indicator-id 1   ║
║    python -m app.services.backtest_all_cnstock --indicator-id 5,16 --mode mid,long
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝
"""

import hashlib
import json
import queue
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any, Dict, Generator, List, Optional

import pandas as pd
import numpy as np

from app.utils.db import get_db_connection
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ================================================================
#  复用 indicator_review.py 的辅助函数
# ================================================================

from app.services.indicator_review import (
    _run_backtest,
    _run_backtests_parallel,
    _get_indicator_code,
    _get_stock_kline,
    _extract_date_from_df,
    _is_buy_recency_valid,
    _safe_float,
    _get_current_price_from_df,
    _get_current_price_ticker,
    _run_indicator_on_stock,
    _add_to_watchlist,
    _extract_indicator_name,
    REVIEW_TIMEFRAMES,
    BACKTEST_MIN_WIN_RATE,
    BACKTEST_MIN_RETURN,
)


# ================================================================
#  全A股列表获取
# ================================================================

def _get_all_cnstocks() -> List[Dict[str, Any]]:
    """
    获取全A股列表。

    优先级：
      1. 从 cnstock_selection 表取（已有数据，含 code/name/market）
      2. 降级：从 AKShare 拉取实时列表

    返回: [{"code": "000001.SH", "name": "平安银行", "market": "CNStock"}, ...]
    """
    stocks = []

    # 方案1：从数据库取（最快）
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT DISTINCT code, name FROM cnstock_selection "
                "WHERE date = (SELECT MAX(date) FROM cnstock_selection) "
                "ORDER BY code"
            )
            rows = cur.fetchall() or []
            cur.close()

        if rows:
            for r in rows:
                code = str(r.get("code") or "").strip()
                name = str(r.get("name") or "").strip()
                if code:
                    if not code.endswith((".SH", ".SZ")):
                        suffix = ".SH" if code.startswith("6") else ".SZ"
                        code = code + suffix
                    stocks.append({"code": code, "name": name, "market": "CNStock"})
            logger.info(f"[_get_all_cnstocks] 从 cnstock_selection 取到 {len(stocks)} 只")
            return stocks
    except Exception as e:
        logger.warning(f"[_get_all_cnstocks] cnstock_selection 查询失败: {e}")

    # 方案2：AKShare 降级
    try:
        from app.market_cn.china_stock import ak_stock_basic
        df = ak_stock_basic()
        if df is not None and len(df) > 0:
            code_col = "代码" if "代码" in df.columns else "code"
            name_col = "名称" if "名称" in df.columns else "name"
            for _, row in df.iterrows():
                code = str(row.get(code_col, "")).strip()
                name = str(row.get(name_col, "")).strip()
                if code:
                    if not code.endswith((".SH", ".SZ")):
                        suffix = ".SH" if code.startswith("6") else ".SZ"
                        code = code + suffix
                    stocks.append({"code": code, "name": name, "market": "CNStock"})
            logger.info(f"[_get_all_cnstocks] 从 AKShare 取到 {len(stocks)} 只")
            return stocks
    except Exception as e:
        logger.warning(f"[_get_all_cnstocks] AKShare 降级失败: {e}")

    logger.error("[_get_all_cnstocks] 所有数据源均失败，返回空列表")
    return stocks


# ================================================================
#  结果持久化 → qd_backtest_runs
# ================================================================

def _save_backtest_run(
    user_id: int,
    indicator_id: int,
    indicator_name: str,
    symbol: str,
    market: str,
    timeframe: str,
    start_date: str,
    end_date: str,
    initial_capital: float,
    commission: float,
    trade_direction: str,
    indicator_code: str,
    status: str = "success",
    error_message: str = "",
    result: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    """
    写入一条回测记录到 qd_backtest_runs 表。

    与 BacktestService._save_run() 逻辑一致，result_json 存完整回测结果。
    返回 run_id (qd_backtest_runs.id)，失败返回 None。
    """
    try:
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """INSERT INTO qd_backtest_runs
                   (user_id, indicator_id, strategy_id, strategy_name, run_type,
                    market, symbol, timeframe,
                    start_date, end_date,
                    initial_capital, commission, slippage, leverage, trade_direction,
                    strategy_config, config_snapshot, engine_version, code_hash,
                    status, error_message, result_json, created_at)
                   VALUES (%s, %s, %s, %s, %s,
                           %s, %s, %s,
                           %s, %s,
                           %s, %s, %s, %s, %s,
                           %s, %s, %s, %s,
                           %s, %s, %s, NOW())
                   RETURNING id""",
                (
                    int(user_id or 1),
                    int(indicator_id),
                    None,  # strategy_id
                    str(indicator_name or ""),
                    "indicator",
                    str(market or "CNStock"),
                    str(symbol or ""),
                    str(timeframe or ""),
                    str(start_date or ""),
                    str(end_date or ""),
                    float(initial_capital or 100000),
                    float(commission or 0.001),
                    0.0,   # slippage
                    1,     # leverage
                    str(trade_direction or "long"),
                    "{}",  # strategy_config
                    "{}",  # config_snapshot
                    "backtest_all-v1",
                    hashlib.sha256(str(indicator_code or "").encode("utf-8")).hexdigest() if indicator_code else "",
                    str(status or "success"),
                    str(error_message or ""),
                    json.dumps(result or {}, ensure_ascii=False) if result else "",
                ),
            )
            row = cur.fetchone()
            run_id = row["id"] if row else None

            # 写入交易明细（如果有）
            if run_id and status == "success" and isinstance(result, dict):
                for idx, trade in enumerate((result.get("trades") or []), start=1):
                    cur.execute(
                        """INSERT INTO qd_backtest_trades
                           (run_id, user_id, strategy_id, trade_index, trade_time,
                            trade_type, side, price, amount, profit, balance,
                            reason, payload_json, created_at)
                           VALUES (%s, %s, %s, %s, %s,
                                   %s, %s, %s, %s, %s, %s,
                                   %s, %s, NOW())""",
                        (
                            int(run_id),
                            int(user_id or 1),
                            None,
                            idx,
                            str(trade.get("time") or ""),
                            str(trade.get("type") or ""),
                            str(trade.get("side") or ""),
                            float(trade.get("price") or 0),
                            float(trade.get("amount") or 0),
                            float(trade.get("profit") or 0),
                            float(trade.get("balance") or 0),
                            str(trade.get("reason") or trade.get("close_reason") or ""),
                            json.dumps(trade or {}, ensure_ascii=False),
                        ),
                    )

                # 写入权益曲线（如果有）
                for idx, pt in enumerate((result.get("equityCurve") or []), start=1):
                    cur.execute(
                        """INSERT INTO qd_backtest_equity_points
                           (run_id, point_index, point_time, point_value, created_at)
                           VALUES (%s, %s, %s, %s, NOW())""",
                        (
                            int(run_id),
                            idx,
                            str(pt.get("time") or ""),
                            float(pt.get("value") or 0),
                        ),
                    )

            db.commit()
            cur.close()
        return run_id
    except Exception as e:
        logger.error(f"_save_backtest_run({symbol}) failed: {e}", exc_info=True)
        return None


# ================================================================
#  SSE 工具
# ================================================================

def _sse(data: Dict[str, Any]) -> str:
    """格式化 SSE 消息"""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


# ================================================================
#  周期配置规范化
# ================================================================

def _normalize_periods(periods: Any, mode: str) -> List[Dict[str, Any]]:
    """
    将用户传入的周期配置规范化。

    接受格式：
      - None / 空 → 使用 REVIEW_TIMEFRAMES[mode] 默认配置
      - [{"tf": "1D", "months": 6, "label": "6月线"}, ...]  → 原样返回
      - ["1D:6", "1W:12"]  → 简写，自动补 label
    """
    if not periods:
        cfg = REVIEW_TIMEFRAMES.get(mode, REVIEW_TIMEFRAMES["mid"])
        return cfg["periods"]

    result = []
    for p in periods:
        if isinstance(p, dict):
            result.append({
                "tf": p.get("tf", "1D"),
                "months": int(p.get("months", 6)),
                "label": p.get("label", f"{p.get('months', 6)}个月"),
            })
        elif isinstance(p, str):
            parts = p.split(":")
            tf = parts[0].strip()
            months = int(parts[1].strip()) if len(parts) > 1 else 6
            result.append({"tf": tf, "months": months, "label": f"{months}个月({tf})"})
    return result


# ================================================================
#  单策略 + 单股票回测
# ================================================================

def _backtest_single_stock(
    indicator_code: str,
    indicator_id: int,
    indicator_name: str,
    user_id: int,
    symbol: str,
    name: str,
    market: str,
    periods: List[Dict[str, Any]],
    user_params: Dict[str, Any],
    save_to_db: bool,
    cancelled: List[bool],
) -> Dict[str, Any]:
    """
    对单只股票执行完整的审核流程（无新闻）。

    返回:
      {
        "passed": bool,
        "skip_reason": str,
        "buy_price": float,
        "buy_date": str,
        "sell_price": float,
        "sell_date": str,
        "current_price": float,
        "bt_results": [...],
        "bt_summary": str,
        "saved_runs": [...],   # 写入 qd_backtest_runs 的 run_id 列表
      }
    """
    result = {
        "passed": False,
        "skip_reason": "",
        "buy_price": None,
        "buy_date": None,
        "sell_price": None,
        "sell_date": None,
        "current_price": None,
        "bt_results": [],
        "bt_summary": "",
        "saved_runs": [],
    }

    # ── Step 1: 指标执行 ──
    try:
        indicator_result = _run_indicator_on_stock(
            indicator_code, market, symbol, user_params, _cancelled=cancelled
        )
        if indicator_result.get("cancelled"):
            result["skip_reason"] = "cancelled"
            return result
    except Exception as e:
        result["skip_reason"] = "indicator_error"
        return result

    if not indicator_result["success"]:
        result["skip_reason"] = "indicator_error"
        return result

    result["buy_price"] = indicator_result.get("buy_price")
    result["buy_date"] = indicator_result.get("buy_date")
    result["sell_price"] = indicator_result.get("sell_price")
    result["sell_date"] = indicator_result.get("sell_date")
    result["current_price"] = indicator_result.get("current_price")

    # ── Step 2: 买点信号判断 ──
    if not indicator_result["has_buy_signal"]:
        result["skip_reason"] = "no_buy_signal"
        return result

    current_price = indicator_result["current_price"]
    buy_price = indicator_result["buy_price"]

    if current_price is not None and buy_price is not None and current_price > buy_price:
        result["skip_reason"] = "price_above_buy"
        return result

    # 买点时效性
    buy_date_str = indicator_result.get("buy_date") or ""
    executed_df = indicator_result.get("_executed_df")
    if buy_date_str and executed_df is not None and "buy" in executed_df.columns:
        try:
            buy_series = executed_df["buy"].astype(bool)
            if buy_series.any():
                last_buy_idx = buy_series[buy_series].index.tolist()[-1]
                if not _is_buy_recency_valid(executed_df, last_buy_idx, max_trading_days=3):
                    result["skip_reason"] = "buy_too_old"
                    return result
        except Exception:
            pass

    # ── Step 3: 买卖逻辑校验 ──
    sell_price = indicator_result.get("sell_price")
    sell_date_str = indicator_result.get("sell_date") or ""

    if sell_price is None:
        result["skip_reason"] = "no_sell_signal"
        return result

    if buy_price is not None and buy_price > sell_price:
        result["skip_reason"] = "buy_after_sell"
        return result

    if buy_date_str and sell_date_str:
        try:
            buy_dt = datetime.strptime(buy_date_str, "%Y-%m-%d")
            sell_dt = datetime.strptime(sell_date_str, "%Y-%m-%d")
            if buy_dt < sell_dt:
                result["skip_reason"] = "buy_before_sell"
                return result
        except ValueError:
            pass

    # ── Step 4: 多周期回测 ──
    try:
        bt_results = _run_backtests_parallel(
            cancelled=cancelled,
            periods=periods,
            max_workers=len(periods),
            indicator_code=indicator_code,
            market=market,
            symbol=symbol,
            initial_capital=100000.0,
            commission=0.001,
            trade_direction="long",
            indicator_params=user_params,
            user_id=user_id,
            indicator_id=indicator_id,
        )
    except Exception as e:
        logger.warning(f"[_backtest_single_stock] {symbol} 回测异常: {e}")
        result["skip_reason"] = "backtest_error"
        return result

    if cancelled[0]:
        result["skip_reason"] = "cancelled"
        return result

    result["bt_results"] = bt_results or []

    # ── 回测结果判断 + 写入 qd_backtest_runs ──
    bt_pass = True
    bt_fail_reason = ""
    bt_msg_parts = []

    if not bt_results:
        result["skip_reason"] = "backtest_no_result"
        result["bt_summary"] = "回测无结果"
        return result

    # 计算回测日期范围（用于 start_date/end_date 字段）
    now = datetime.now()

    for bt_item in bt_results:
        if bt_item is None:
            continue
        label = bt_item.get("label", "?")
        tf = bt_item.get("tf", "")
        months = bt_item.get("months", 0)
        bt_result = bt_item.get("result")
        error = bt_item.get("error")

        # 计算该周期的日期范围
        end_date = now
        start_date = end_date - timedelta(days=months * 30) if months else end_date - timedelta(days=180)
        start_date_str = start_date.strftime("%Y-%m-%d")
        end_date_str = end_date.strftime("%Y-%m-%d")

        if error:
            bt_msg_parts.append(f"{label}:异常")
            bt_pass = False
            if not bt_fail_reason:
                bt_fail_reason = f"{label}回测异常: {error}"

            # 写入失败记录
            if save_to_db:
                _save_backtest_run(
                    user_id=user_id,
                    indicator_id=indicator_id,
                    indicator_name=indicator_name,
                    symbol=symbol,
                    market=market,
                    timeframe=tf,
                    start_date=start_date_str,
                    end_date=end_date_str,
                    initial_capital=100000.0,
                    commission=0.001,
                    trade_direction="long",
                    indicator_code=indicator_code,
                    status="error",
                    error_message=str(error),
                )
            continue

        if bt_result is None:
            bt_msg_parts.append(f"{label}:无结果")
            bt_pass = False
            if not bt_fail_reason:
                bt_fail_reason = f"{label}回测无结果"

            if save_to_db:
                _save_backtest_run(
                    user_id=user_id,
                    indicator_id=indicator_id,
                    indicator_name=indicator_name,
                    symbol=symbol,
                    market=market,
                    timeframe=tf,
                    start_date=start_date_str,
                    end_date=end_date_str,
                    initial_capital=100000.0,
                    commission=0.001,
                    trade_direction="long",
                    indicator_code=indicator_code,
                    status="no_result",
                    error_message="回测无结果",
                )
            continue

        win_rate = bt_result.get("winRate", 0) or 0
        total_return = bt_result.get("totalReturn", 0) or 0

        period_ok = (win_rate >= BACKTEST_MIN_WIN_RATE and total_return > BACKTEST_MIN_RETURN)
        status_mark = "✓" if period_ok else "✗"
        bt_msg_parts.append(
            f"{label}:{status_mark} 收益{round(total_return, 2)}% 胜率{round(win_rate, 2)}%"
        )

        # 写入 qd_backtest_runs（无论通过与否都写，方便后续分析）
        if save_to_db:
            run_id = _save_backtest_run(
                user_id=user_id,
                indicator_id=indicator_id,
                indicator_name=indicator_name,
                symbol=symbol,
                market=market,
                timeframe=tf,
                start_date=start_date_str,
                end_date=end_date_str,
                initial_capital=100000.0,
                commission=0.001,
                trade_direction="long",
                indicator_code=indicator_code,
                status="success",
                result=bt_result,
            )
            if run_id:
                result["saved_runs"].append(run_id)

        if not period_ok:
            bt_pass = False
            if not bt_fail_reason:
                reasons = []
                if total_return <= BACKTEST_MIN_RETURN:
                    reasons.append(f"收益率{round(total_return, 2)}%≤0")
                if win_rate < BACKTEST_MIN_WIN_RATE:
                    reasons.append(f"胜率{round(win_rate, 2)}%<{BACKTEST_MIN_WIN_RATE}%")
                bt_fail_reason = f"{label}: {', '.join(reasons)}"

    result["bt_summary"] = " | ".join(bt_msg_parts) if bt_msg_parts else "回测无结果"

    if not bt_pass:
        result["skip_reason"] = "backtest_failed"
        return result

    # ── 全部通过 ──
    result["passed"] = True
    return result


# ================================================================
#  主入口：多策略 + 全A股回测
# ================================================================

def backtest_all(
    indicator_id: int = None,
    user_id: int = 1,
    user_params: Dict[str, Any] = None,
    review_mode: str = "mid",
    strategies: List[Dict[str, Any]] = None,
    save_to_db: bool = True,
    _cancelled: List[bool] = None,
) -> Generator[str, None, None]:
    """
    全A股多策略回测筛选，结果写入 qd_backtest_runs 表。

    参数：
      indicator_id:  单策略模式的指标ID（与 strategies 二选一）
      user_id:       用户ID
      user_params:   指标参数覆盖（单策略模式）
      review_mode:   默认回测模式 "short"/"mid"/"long"
      strategies:    多策略配置（优先于 indicator_id），格式：
                     [
                       {
                         "indicator_id": 1,
                         "name": "RSI策略",
                         "params": {},
                         "periods": [
                           {"tf": "1D", "months": 6, "label": "6月线"},
                         ],
                         "mode": "mid",
                       },
                       ...
                     ]
      save_to_db:    是否写入 qd_backtest_runs 表

    yield: SSE 格式字符串
    """
    cancelled = _cancelled or [False]

    # ── 规范化策略列表 ──
    if not strategies:
        if not indicator_id:
            yield _sse({"type": "error", "msg": "请指定 indicator_id 或 strategies"})
            return
        strategies = [{
            "indicator_id": indicator_id,
            "params": user_params or {},
            "mode": review_mode,
        }]

    # 预检每个策略的指标代码
    strategy_configs = []
    for s in strategies:
        sid = s.get("indicator_id")
        if not sid:
            continue
        uid = s.get("user_id", user_id)
        code = _get_indicator_code(sid, uid)
        if not code:
            yield _sse({"type": "error", "msg": f"指标ID {sid} 不存在或无权访问"})
            return
        mode = s.get("mode", review_mode)
        periods = _normalize_periods(s.get("periods"), mode)
        name = s.get("name") or _extract_indicator_name(code) or f"指标{sid}"
        strategy_configs.append({
            "indicator_id": sid,
            "indicator_code": code,
            "indicator_name": name,
            "user_id": uid,
            "params": s.get("params") or {},
            "periods": periods,
            "mode": mode,
        })

    if not strategy_configs:
        yield _sse({"type": "error", "msg": "无有效策略"})
        return

    # ── 获取全A股列表 ──
    yield _sse({
        "type": "progress",
        "status": "loading_stocks",
        "msg": "正在获取全A股列表...",
        "index": 0,
        "total": 0,
    })

    stocks = _get_all_cnstocks()
    if not stocks:
        yield _sse({"type": "error", "msg": "获取A股列表失败"})
        return

    total_stocks = len(stocks)
    total_tasks = total_stocks * len(strategy_configs)

    logger.info(f"[backtest_all] strategies={len(strategy_configs)}, "
                f"stocks={total_stocks}, total_tasks={total_tasks}")

    yield _sse({
        "type": "progress",
        "status": "start",
        "msg": f"开始回测：{len(strategy_configs)} 个策略 × {total_stocks} 只股票 = {total_tasks} 个任务",
        "index": 0,
        "total": total_tasks,
    })

    # ── 统计 ──
    stats = {
        "total": total_tasks,
        "passed": 0,
        "skipped": 0,
        "errors": 0,
        "runs_saved": 0,
        "by_strategy": {},
    }
    for sc in strategy_configs:
        stats["by_strategy"][sc["indicator_id"]] = {
            "passed": 0, "skipped": 0, "name": sc["indicator_name"],
        }

    task_idx = 0

    try:
        for sc in strategy_configs:
            if cancelled[0]:
                break

            sid = sc["indicator_id"]
            sname = sc["indicator_name"]
            scode = sc["indicator_code"]
            s_uid = sc["user_id"]
            s_params = sc["params"]
            s_periods = sc["periods"]

            yield _sse({
                "type": "progress",
                "status": "strategy_start",
                "indicator_id": sid,
                "indicator_name": sname,
                "msg": f"开始策略：{sname}（{len(s_periods)} 个周期 × {total_stocks} 只）",
                "index": task_idx,
                "total": total_tasks,
            })

            passed_list = []

            for stock_idx, stock in enumerate(stocks):
                if cancelled[0]:
                    break

                task_idx += 1
                symbol = stock.get("code", "")
                name = stock.get("name", "")
                market = stock.get("market", "CNStock")

                # 进度推送（每 20 只推一次）
                if stock_idx % 20 == 0 or stock_idx == total_stocks - 1:
                    yield _sse({
                        "type": "progress",
                        "status": "checking",
                        "indicator_id": sid,
                        "indicator_name": sname,
                        "symbol": symbol,
                        "name": name,
                        "index": task_idx,
                        "total": total_tasks,
                        "stock_index": stock_idx + 1,
                        "stock_total": total_stocks,
                        "msg": f"[{sname}] {stock_idx+1}/{total_stocks} {symbol} {name}",
                    })

                # 执行单股票回测
                bt_result = _backtest_single_stock(
                    indicator_code=scode,
                    indicator_id=sid,
                    indicator_name=sname,
                    user_id=s_uid,
                    symbol=symbol,
                    name=name,
                    market=market,
                    periods=s_periods,
                    user_params=s_params,
                    save_to_db=save_to_db,
                    cancelled=cancelled,
                )

                if cancelled[0]:
                    break

                # 统计写入的 run 数量
                stats["runs_saved"] += len(bt_result.get("saved_runs", []))

                # ── 统计 ──
                if bt_result["passed"]:
                    stats["passed"] += 1
                    stats["by_strategy"][sid]["passed"] += 1
                    passed_list.append(symbol)

                    yield _sse({
                        "type": "result",
                        "indicator_id": sid,
                        "indicator_name": sname,
                        "symbol": symbol,
                        "name": name,
                        "index": task_idx,
                        "total": total_tasks,
                        "added": True,
                        "reason": "passed",
                        "bt_summary": bt_result["bt_summary"],
                        "msg": f"✅ [{sname}] {symbol} {name} 通过 | {bt_result['bt_summary']}",
                    })

                    _add_to_watchlist(s_uid, market, symbol, name)
                else:
                    stats["skipped"] += 1
                    stats["by_strategy"][sid]["skipped"] += 1

            # ── 单策略完成摘要 ──
            yield _sse({
                "type": "strategy_done",
                "indicator_id": sid,
                "indicator_name": sname,
                "passed": len(passed_list),
                "skipped": stats["by_strategy"][sid]["skipped"],
                "passed_list": passed_list,
                "msg": f"策略 {sname} 完成：通过 {len(passed_list)} 只",
            })

        # ── 全部完成 ──
        summary_parts = []
        for sid, s_stats in stats["by_strategy"].items():
            summary_parts.append(f"{s_stats['name']}:{s_stats['passed']}通过")

        yield _sse({
            "type": "done",
            "total": total_tasks,
            "passed": stats["passed"],
            "skipped": stats["skipped"],
            "errors": stats["errors"],
            "runs_saved": stats["runs_saved"],
            "strategies": len(strategy_configs),
            "stocks": total_stocks,
            "msg": f"全部完成：{len(strategy_configs)}个策略 × {total_stocks}只股票，"
                   f"共{stats['passed']}只通过，写入{stats['runs_saved']}条回测记录 | "
                   f"{'; '.join(summary_parts)}",
        })

    except GeneratorExit:
        logger.info(f"[backtest_all] client disconnected at task {task_idx}/{total_tasks}")
        return
    except Exception as e:
        logger.error(f"[backtest_all] unexpected error at task {task_idx}: {e}", exc_info=True)
        yield _sse({"type": "error", "msg": f"回测异常中断: {str(e)}"})


# ================================================================
#  命令行入口
# ================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="全A股多策略回测筛选")
    parser.add_argument("--indicator-id", type=str, required=True,
                        help="指标ID，多个用逗号分隔 (如 1,2,3)")
    parser.add_argument("--user-id", type=int, default=1, help="用户ID (默认1)")
    parser.add_argument("--mode", type=str, default="mid",
                        help="回测模式，多个用逗号分隔 (如 mid,long)")
    parser.add_argument("--no-save", action="store_true", help="不写入数据库")
    args = parser.parse_args()

    indicator_ids = [int(x.strip()) for x in args.indicator_id.split(",")]
    modes = [x.strip() for x in args.mode.split(",")]
    if len(modes) < len(indicator_ids):
        modes = modes * len(indicator_ids)

    strategies = []
    for i, sid in enumerate(indicator_ids):
        strategies.append({
            "indicator_id": sid,
            "mode": modes[i % len(modes)],
        })

    print(f"🚀 开始全A股多策略回测")
    print(f"   策略数: {len(strategies)}")
    print(f"   指标ID: {indicator_ids}")
    print(f"   模式: {[s['mode'] for s in strategies]}")
    print("=" * 60)

    passed_map = {}

    for msg_str in backtest_all(
        strategies=strategies,
        user_id=args.user_id,
        save_to_db=not args.no_save,
    ):
        if msg_str.startswith("data: "):
            try:
                data = json.loads(msg_str[6:].strip())
            except json.JSONDecodeError:
                continue

            msg_type = data.get("type")
            if msg_type == "progress":
                status = data.get("status", "")
                if status == "checking":
                    idx = data.get("stock_index", 0)
                    if idx % 50 == 0:
                        print(f"\r⏳ [{data.get('indicator_name', '')}] "
                              f"{data.get('msg', '')}", end="", flush=True)
                else:
                    print(f"\n📌 {data.get('msg', '')}")

            elif msg_type == "result":
                if data.get("added"):
                    sid = data.get("indicator_id")
                    if sid not in passed_map:
                        passed_map[sid] = []
                    passed_map[sid].append(data.get("symbol", ""))
                    print(f"  ✅ {data.get('msg', '')}")

            elif msg_type == "strategy_done":
                print(f"\n{'─' * 40}")
                print(f"📊 {data.get('msg', '')}")
                print(f"{'─' * 40}")

            elif msg_type == "done":
                print(f"\n{'=' * 60}")
                print(f"🏁 {data.get('msg', '')}")

            elif msg_type == "error":
                print(f"\n❌ {data.get('msg', '')}")

    if passed_map:
        print(f"\n{'=' * 60}")
        print("📋 通过的股票汇总:")
        for sid, symbols in passed_map.items():
            print(f"\n  策略 {sid} ({len(symbols)} 只):")
            for s in symbols[:20]:
                print(f"    - {s}")
            if len(symbols) > 20:
                print(f"    ... 还有 {len(symbols) - 20} 只")
