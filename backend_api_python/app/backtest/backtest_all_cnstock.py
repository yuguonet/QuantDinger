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
║    5. 支持市场筛选（all/北证/科创/沪深/创业板，可复选）           ║
║    6. 多线程并行回测，线程数可调                                  ║
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
║    # 指定市场 + 多线程                                            ║
║    for msg in backtest_all(                                       ║
║        indicator_id=1,                                            ║
║        market_filters=["科创", "创业板"],                         ║
║        max_workers=8,                                             ║
║    ):                                                             ║
║        print(msg)                                                 ║
║                                                                  ║
║  命令行：                                                         ║
║    python -m app.services.backtest_all_cnstock --indicator-id 1   ║
║    python -m app.services.backtest_all_cnstock --indicator-id 1,2 --mode mid,long
║    python -m app.services.backtest_all_cnstock --indicator-id 1 --market 科创,创业板
║    python -m app.services.backtest_all_cnstock --indicator-id 5,16 --workers 8 --market 沪深,创业板 --mode mid
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝
"""

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any, Dict, Generator, List, Optional

# ── 加载 .env（与 run.py 一致，确保独立运行时 DATABASE_URL 等变量可用）──
try:
    from dotenv import load_dotenv
    _this_dir = os.path.dirname(os.path.abspath(__file__))
    _backend_dir = os.path.dirname(os.path.dirname(_this_dir))  # backend_api_python/
    load_dotenv(os.path.join(_backend_dir, ".env"), override=False)
    load_dotenv(os.path.join(os.path.dirname(_backend_dir), ".env"), override=False)
except Exception:
    pass

from app.utils.db import get_db_connection
from app.utils.logger import get_logger
from app.services.indicator_params import IndicatorParamsParser

logger = get_logger(__name__)


# ================================================================
#  复用 indicator_review.py 的辅助函数
# ================================================================

from app.services.indicator_review import (
    _run_backtests_parallel,
    _get_indicator_code,
    _is_buy_recency_valid,
    _run_indicator_on_stock,
    _add_to_watchlist,
    _extract_indicator_name,
    REVIEW_TIMEFRAMES,
    BACKTEST_MIN_WIN_RATE,
    BACKTEST_MIN_RETURN,
)


# ================================================================
#  参数优化评分函数
# ================================================================

def _score_bt_result(bt_result: Dict[str, Any]) -> float:
    """
    对单次回测结果计算综合评分（用于参数优化排序）。

    评分公式（与 optimizer/strategy_optimizer.py 的 composite 对齐）：
      score = sharpe*0.3 + return*0.3 + winRate*1.0 + min(profitFactor,5)*0.3 - maxDD*1.0

    交易次数 < 3 时惩罚 -10。
    """
    if not bt_result:
        return -999.0

    sharpe = float(bt_result.get("sharpeRatio", 0) or 0)
    win_rate = float(bt_result.get("winRate", 0) or 0) / 100.0
    max_dd = float(bt_result.get("maxDrawdown", 0) or 0) / 100.0
    total_return = float(bt_result.get("totalReturn", 0) or 0) / 100.0
    total_trades = int(bt_result.get("totalTrades", 0) or 0)
    profit_factor = float(bt_result.get("profitFactor", 0) or 0)

    if total_trades < 3:
        return -10.0

    trade_penalty = 0.0
    if total_trades < 10:
        trade_penalty = (10 - total_trades) * 0.08
    elif total_trades > 60:
        trade_penalty = (total_trades - 60) * 0.03

    return (
        sharpe * 0.3
        + total_return * 0.3
        + win_rate * 1.0
        + min(profit_factor, 5.0) * 0.3
        - max_dd * 1.0
        - trade_penalty
    )


def _score_bt_periods(bt_results: List[Dict[str, Any]]) -> float:
    """
    对多周期回测结果取平均评分。
    bt_results 来自 _run_backtests_parallel，每项有 .result 字段。
    """
    scores = []
    for item in (bt_results or []):
        if item is None:
            continue
        r = item.get("result")
        if r is None:
            continue
        scores.append(_score_bt_result(r))
    return sum(scores) / len(scores) if scores else -999.0


# ================================================================
#  市场筛选定义
# ================================================================

# 市场分类 → 筛选函数（基于 stock_code 前缀判断）
# 与 basicinfo_db._detect_market 逻辑一致
MARKET_CATEGORIES = {
    "all": {
        "label": "全部A股",
        "description": "沪深北全市场",
        "filter": lambda code, market: True,
    },
    "沪市主板": {
        "label": "沪市主板",
        "description": "600/601/603/605 开头",
        "filter": lambda code, market: code.startswith(("600", "601", "603", "605")),
    },
    "深市主板": {
        "label": "深市主板",
        "description": "000/001/002/003 开头",
        "filter": lambda code, market: code.startswith(("000", "001", "002", "003")),
    },
    "科创": {
        "label": "科创板",
        "description": "688/689 开头",
        "filter": lambda code, market: code.startswith(("688", "689")),
    },
    "创业板": {
        "label": "创业板",
        "description": "300/301 开头",
        "filter": lambda code, market: code.startswith(("300", "301")),
    },
    "北证": {
        "label": "北交所",
        "description": "43/82/83/87/88 开头",
        "filter": lambda code, market: code.startswith(("43", "82", "83", "87", "88")),
    },
}

# 便捷组合
MARKET_PRESETS = {
    "沪深": ["沪市主板", "深市主板"],
}


def _resolve_market_filters(filters: List[str]) -> List[str]:
    """
    解析市场筛选参数，支持预设组合展开。

    输入: ["沪深", "科创"] → 展开为 ["沪市主板", "深市主板", "科创"]
    输入: ["all"] → 返回 ["all"]
    输入: None/[] → 返回 ["all"]
    """
    if not filters:
        return ["all"]

    resolved = []
    for f in filters:
        f = f.strip()
        if not f:
            continue
        if f == "all":
            return ["all"]
        if f in MARKET_PRESETS:
            resolved.extend(MARKET_PRESETS[f])
        elif f in MARKET_CATEGORIES:
            resolved.append(f)
        else:
            logger.warning(f"未知市场分类: {f}，可选: {list(MARKET_CATEGORIES.keys())}")
    return resolved if resolved else ["all"]


def _filter_stocks_by_market(
    stocks: List[Dict[str, Any]],
    market_filters: List[str],
) -> List[Dict[str, Any]]:
    """
    按市场分类筛选股票列表。

    Args:
        stocks: 原始股票列表 [{"symbol": "600519", "name": "贵州茅台", "market_cn": "SH"}, ...]
        market_filters: 已解析的市场分类列表（来自 _resolve_market_filters）

    Returns:
        筛选后的股票列表，code 字段补上 .SH/.SZ/.BJ 后缀
    """
    if "all" in market_filters:
        # 不过滤，直接返回（补后缀）
        result = []
        for s in stocks:
            code = s.get("symbol", "")
            market_cn = s.get("market_cn", "")
            suffix = f".{market_cn}" if market_cn and not code.endswith(f".{market_cn}") else ""
            result.append({
                "code": f"{code}{suffix}",
                "name": s.get("name", ""),
                "market": "CNStock",
            })
        return result

    # 构建筛选函数列表
    filters = []
    for cat in market_filters:
        if cat in MARKET_CATEGORIES:
            filters.append(MARKET_CATEGORIES[cat]["filter"])

    if not filters:
        # 无有效筛选器，返回全部
        return _filter_stocks_by_market(stocks, ["all"])

    result = []
    for s in stocks:
        code = s.get("symbol", "")
        market_cn = s.get("market_cn", "")
        # 任一分类匹配即保留（OR 逻辑，支持复选）
        if any(f(code, market_cn) for f in filters):
            suffix = f".{market_cn}" if market_cn and not code.endswith(f".{market_cn}") else ""
            result.append({
                "code": f"{code}{suffix}",
                "name": s.get("name", ""),
                "market": "CNStock",
            })

    return result


# ================================================================
#  全A股列表获取（通过 basicinfo_db 连接 market_db）
# ================================================================

def _get_all_cnstocks(market_filters: List[str] = None) -> List[Dict[str, Any]]:
    """
    获取A股列表，通过 basicinfo_db 连接 market_db。

    数据源优先级：
      1. basicinfo_db (stock_basic_info 表，通过 market_db.py 的连接池)
      2. 降级：AKShare

    Args:
        market_filters: 市场筛选列表，如 ["科创", "创业板"]，None 或 ["all"] 返回全部

    Returns:
        [{"code": "600519.SH", "name": "贵州茅台", "market": "CNStock"}, ...]
    """
    resolved = _resolve_market_filters(market_filters)
    logger.info(f"[_get_all_cnstocks] 市场筛选: {market_filters} → {resolved}")

    # 方案1：从 basicinfo_db 取（通过 market_db.py 连接池）
    try:
        from app.utils.basicinfo_db import get_stock_basic_db
        db = get_stock_basic_db()
        all_stocks = db.get_all_stocks(status="active")
        if all_stocks:
            stocks = _filter_stocks_by_market(all_stocks, resolved)
            logger.info(
                f"[_get_all_cnstocks] basicinfo_db 共 {len(all_stocks)} 只，"
                f"筛选后 {len(stocks)} 只 (filters={resolved})"
            )
            return stocks
    except Exception as e:
        logger.warning(f"[_get_all_cnstocks] basicinfo_db 查询失败: {e}")

    # 方案2：AKShare 降级（不支持精确市场筛选，只能按交易所粗分）
    try:
        from app.market_cn.china_stock import ak_stock_basic
        df = ak_stock_basic()
        if df is not None and len(df) > 0:
            code_col = "代码" if "代码" in df.columns else "code"
            name_col = "名称" if "名称" in df.columns else "name"
            raw_stocks = []
            for _, row in df.iterrows():
                code = str(row.get(code_col, "")).strip()
                name = str(row.get(name_col, "")).strip()
                if code and len(code) == 6 and code.isdigit():
                    market_cn = _detect_market_fallback(code)
                    raw_stocks.append({
                        "symbol": code,
                        "name": name,
                        "market_cn": market_cn,
                    })
            stocks = _filter_stocks_by_market(raw_stocks, resolved)
            logger.info(
                f"[_get_all_cnstocks] AKShare 共 {len(raw_stocks)} 只，"
                f"筛选后 {len(stocks)} 只"
            )
            return stocks
    except Exception as e:
        logger.warning(f"[_get_all_cnstocks] AKShare 降级失败: {e}")

    logger.error("[_get_all_cnstocks] 所有数据源均失败，返回空列表")
    return []


def _detect_market_fallback(code: str) -> str:
    """根据代码推断交易所（AKShare 降级时用）"""
    c = (code or "").strip()
    if not c.isdigit() or len(c) != 6:
        return ""
    if c.startswith(("600", "601", "603", "605", "688", "689", "900")):
        return "SH"
    if c.startswith(("000", "001", "002", "003", "300", "301", "200")):
        return "SZ"
    if c.startswith(("43", "82", "83", "87", "88")):
        return "BJ"
    return ""


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
#  参数优化结果持久化 → qd_indicator_optimal_params
# ================================================================

def _save_optimal_params(
    user_id: int,
    indicator_id: int,
    symbol: str,
    market: str,
    timeframe: str,
    best_params: Dict[str, Any],
    score: float,
    bt_result: Dict[str, Any],
    combos_tested: int,
) -> bool:
    """
    保存/更新单只股票的最优参数到 qd_indicator_optimal_params 表。
    使用 ON CONFLICT UPSERT：同一 indicator_id × symbol × timeframe 只保留最新结果。
    """
    if not best_params:
        return False
    try:
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """INSERT INTO qd_indicator_optimal_params
                   (user_id, indicator_id, symbol, market, timeframe,
                    best_params, score, win_rate, total_return,
                    sharpe_ratio, max_drawdown, total_trades,
                    combos_tested, updated_at, created_at)
                   VALUES (%s, %s, %s, %s, %s,
                           %s, %s, %s, %s,
                           %s, %s, %s,
                           %s, NOW(), NOW())
                   ON CONFLICT (indicator_id, symbol, timeframe)
                   DO UPDATE SET
                       best_params = EXCLUDED.best_params,
                       score = EXCLUDED.score,
                       win_rate = EXCLUDED.win_rate,
                       total_return = EXCLUDED.total_return,
                       sharpe_ratio = EXCLUDED.sharpe_ratio,
                       max_drawdown = EXCLUDED.max_drawdown,
                       total_trades = EXCLUDED.total_trades,
                       combos_tested = EXCLUDED.combos_tested,
                       updated_at = NOW()
                   RETURNING id""",
                (
                    int(user_id or 1),
                    int(indicator_id),
                    str(symbol),
                    str(market or "CNStock"),
                    str(timeframe or "1D"),
                    json.dumps(best_params, ensure_ascii=False),
                    float(score or 0),
                    float(bt_result.get("winRate", 0) or 0),
                    float(bt_result.get("totalReturn", 0) or 0),
                    float(bt_result.get("sharpeRatio", 0) or 0),
                    float(bt_result.get("maxDrawdown", 0) or 0),
                    int(bt_result.get("totalTrades", 0) or 0),
                    int(combos_tested or 0),
                ),
            )
            row = cur.fetchone()
            db.commit()
            cur.close()
            return row is not None
    except Exception as e:
        logger.error(f"_save_optimal_params({symbol}) failed: {e}", exc_info=True)
        return False


def _save_optimal_params_batch(
    user_id: int,
    indicator_id: int,
    indicator_name: str,
    items: List[Dict[str, Any]],
    combos_tested: int,
) -> int:
    """
    批量保存参数优化结果。
    items: _backtest_single_stock_with_optimization 的 result 列表（已通过的）。
    返回成功写入的条数。
    """
    saved = 0
    for item in items:
        best_params = item.get("best_params")
        bt_results = item.get("bt_results", [])
        if not best_params or not bt_results:
            continue

        # 取最优周期的回测指标
        best_period = _extract_best_period(bt_results)
        if not best_period:
            continue

        # 取对应的 bt_result 详情
        bt_result_data = {}
        for bt_item in bt_results:
            if bt_item and bt_item.get("tf") == best_period.get("best_tf"):
                bt_result_data = bt_item.get("result") or {}
                break

        symbol = item.get("symbol", "")
        ok = _save_optimal_params(
            user_id=user_id,
            indicator_id=indicator_id,
            symbol=symbol,
            market="CNStock",
            timeframe=best_period.get("best_tf", "1D"),
            best_params=best_params,
            score=_score_bt_result(bt_result_data),
            bt_result=bt_result_data,
            combos_tested=combos_tested,
        )
        if ok:
            saved += 1

    logger.info(f"[_save_optimal_params_batch] {indicator_name}: {saved}/{len(items)} 条写入成功")
    return saved


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
#  单策略 + 单股票回测（进程安全版本）
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
    param_combos: Optional[List[Dict[str, Any]]] = None,
    searchable_params: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    对单只股票执行完整的审核流程（无新闻）。

    当传入 param_combos 时，对每组参数跑多周期回测，取综合评分最高的参数组合。
    未传入时走原有流程（固定参数 + 买点预筛选）。

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
        "saved_runs": [...],
        "best_params": dict,   # 参数优化时返回最优参数
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
        "best_params": None,
    }

    # ── 参数优化模式 ──
    if param_combos and len(param_combos) > 1:
        return _backtest_single_stock_with_optimization(
            indicator_code=indicator_code,
            indicator_id=indicator_id,
            indicator_name=indicator_name,
            user_id=user_id,
            symbol=symbol,
            name=name,
            market=market,
            periods=periods,
            base_params=user_params,
            param_combos=param_combos,
            searchable_params=searchable_params or [],
            save_to_db=save_to_db,
            cancelled=cancelled,
            result=result,
        )

    # ── 原有流程（固定参数）──
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
#  参数优化：单股票多参数组合回测
# ================================================================

def _backtest_single_stock_with_optimization(
    indicator_code: str,
    indicator_id: int,
    indicator_name: str,
    user_id: int,
    symbol: str,
    name: str,
    market: str,
    periods: List[Dict[str, Any]],
    base_params: Dict[str, Any],
    param_combos: List[Dict[str, Any]],
    searchable_params: List[Dict[str, Any]],
    save_to_db: bool,
    cancelled: List[bool],
    result: Dict[str, Any],
) -> Dict[str, Any]:
    """
    参数优化模式：对单只股票遍历多组参数，取综合评分最高的。

    流程：
      1. 遍历 param_combos，每组参数跑多周期回测
      2. 用 _score_bt_periods() 评分
      3. 取最高分的参数组合作为最终结果
      4. 跳过买点预筛选（不同参数可能产生不同信号）
    """
    n_combos = len(param_combos)
    logger.info(f"[param_opt] {symbol} 开始参数优化: {n_combos} 组参数 × {len(periods)} 个周期")

    best_score = -999.0
    best_bt_results = None
    best_params = None
    completed = 0

    for combo in param_combos:
        if cancelled[0]:
            result["skip_reason"] = "cancelled"
            return result

        # 合并：基础参数 + 当前组合参数
        merged = dict(base_params)
        merged.update(combo)

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
                indicator_params=merged,
                user_id=user_id,
                indicator_id=indicator_id,
            )
        except Exception as e:
            logger.debug(f"[param_opt] {symbol} params={combo} 异常: {e}")
            completed += 1
            continue

        if cancelled[0]:
            result["skip_reason"] = "cancelled"
            return result

        score = _score_bt_periods(bt_results)
        completed += 1

        if score > best_score:
            best_score = score
            best_bt_results = bt_results
            best_params = combo
            logger.debug(
                f"[param_opt] {symbol} [{completed}/{n_combos}] "
                f"NEW BEST score={score:.4f} params={combo}"
            )

    if best_bt_results is None:
        result["skip_reason"] = "backtest_no_result"
        result["bt_summary"] = "参数优化无有效结果"
        return result

    result["bt_results"] = best_bt_results
    result["best_params"] = best_params

    # 从最优结果中提取 buy/sell 信息（取第一个有结果的周期）
    for bt_item in best_bt_results:
        if bt_item is None:
            continue
        bt_r = bt_item.get("result")
        if bt_r is None:
            continue
        # 从交易记录中提取最近的买/卖价
        trades = bt_r.get("trades", [])
        for t in reversed(trades):
            ttype = t.get("type", "")
            if "open_long" in ttype and result["buy_price"] is None:
                result["buy_price"] = t.get("price")
                result["buy_date"] = t.get("time", "")[:10]
            if "close_long" in ttype and result["sell_price"] is None:
                result["sell_price"] = t.get("price")
                result["sell_date"] = t.get("time", "")[:10]
        break  # 只取第一个周期

    # ── 回测结果判断 + 写入 ──
    bt_pass = True
    bt_msg_parts = []
    now = datetime.now()

    for bt_item in best_bt_results:
        if bt_item is None:
            continue
        label = bt_item.get("label", "?")
        tf = bt_item.get("tf", "")
        months = bt_item.get("months", 0)
        bt_result = bt_item.get("result")
        error = bt_item.get("error")

        end_date = now
        start_date = end_date - timedelta(days=months * 30) if months else end_date - timedelta(days=180)
        start_date_str = start_date.strftime("%Y-%m-%d")
        end_date_str = end_date.strftime("%Y-%m-%d")

        if error or bt_result is None:
            bt_msg_parts.append(f"{label}:异常" if error else f"{label}:无结果")
            bt_pass = False
            if save_to_db:
                _save_backtest_run(
                    user_id=user_id, indicator_id=indicator_id,
                    indicator_name=indicator_name, symbol=symbol,
                    market=market, timeframe=tf,
                    start_date=start_date_str, end_date=end_date_str,
                    initial_capital=100000.0, commission=0.001,
                    trade_direction="long", indicator_code=indicator_code,
                    status="error" if error else "no_result",
                    error_message=str(error or "回测无结果"),
                )
            continue

        win_rate = bt_result.get("winRate", 0) or 0
        total_return = bt_result.get("totalReturn", 0) or 0
        period_ok = (win_rate >= BACKTEST_MIN_WIN_RATE and total_return > BACKTEST_MIN_RETURN)
        status_mark = "✓" if period_ok else "✗"
        bt_msg_parts.append(
            f"{label}:{status_mark} 收益{round(total_return, 2)}% 胜率{round(win_rate, 2)}%"
        )

        if save_to_db:
            run_id = _save_backtest_run(
                user_id=user_id, indicator_id=indicator_id,
                indicator_name=indicator_name, symbol=symbol,
                market=market, timeframe=tf,
                start_date=start_date_str, end_date=end_date_str,
                initial_capital=100000.0, commission=0.001,
                trade_direction="long", indicator_code=indicator_code,
                status="success", result=bt_result,
            )
            if run_id:
                result["saved_runs"].append(run_id)

        if not period_ok:
            bt_pass = False

    # 参数优化附加信息
    param_str = ", ".join(f"{k}={v}" for k, v in (best_params or {}).items())
    score_str = f"score={best_score:.2f}"
    result["bt_summary"] = (
        f"[参数优化 {n_combos}组] {score_str} | {param_str} | "
        + (" | ".join(bt_msg_parts) if bt_msg_parts else "回测无结果")
    )

    if not bt_pass:
        result["skip_reason"] = "backtest_failed"
        return result

    result["passed"] = True
    logger.info(f"[param_opt] {symbol} 最优参数: {param_str} score={best_score:.4f}")
    return result


# ================================================================
#  并行执行引擎
# ================================================================

def _run_stocks_parallel(
    stocks: List[Dict[str, Any]],
    indicator_code: str,
    indicator_id: int,
    indicator_name: str,
    user_id: int,
    periods: List[Dict[str, Any]],
    user_params: Dict[str, Any],
    save_to_db: bool,
    cancelled: List[bool],
    max_workers: int = 4,
    batch_size: int = 50,
    param_combos: Optional[List[Dict[str, Any]]] = None,
    searchable_params: Optional[List[Dict[str, Any]]] = None,
) -> Generator[Dict[str, Any], None, None]:
    """
    多线程并行执行全量股票回测，逐只 yield 结果。

    使用 ThreadPoolExecutor 并发回测，共享内存，适合 IO 密集型
    （K线拉取、指标沙箱执行等场景）。

    Args:
        stocks: 股票列表
        max_workers: 并行线程数（默认 4）
        batch_size: 进度推送间隔（每 N 只推送一次进度）
        其他参数同 backtest_all

    Yields:
        {"type": "progress"/"result", ...} — 与 backtest_all 的 SSE 格式对齐
    """
    total = len(stocks)

    logger.info(
        f"[_run_stocks_parallel] workers={max_workers}, stocks={total}, batch_size={batch_size}"
    )

    completed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {}
        for stock in stocks:
            if cancelled[0]:
                break
            future = executor.submit(
                _backtest_single_stock,
                indicator_code=indicator_code,
                indicator_id=indicator_id,
                indicator_name=indicator_name,
                user_id=user_id,
                symbol=stock["code"],
                name=stock["name"],
                market=stock["market"],
                periods=periods,
                user_params=user_params,
                save_to_db=save_to_db,
                cancelled=cancelled,
                param_combos=param_combos,
                searchable_params=searchable_params,
            )
            future_map[future] = stock

        for future in as_completed(future_map):
            if cancelled[0]:
                for f in future_map:
                    f.cancel()
                break

            completed += 1
            stock = future_map[future]
            try:
                # 参数优化时给更多时间（每组参数约 30s）
                n_combos = len(param_combos) if param_combos else 1
                per_stock_timeout = max(120, n_combos * 30)
                bt_result = future.result(timeout=per_stock_timeout)
                yield {
                    "type": "result",
                    "indicator_id": indicator_id,
                    "symbol": stock["code"],
                    "name": stock["name"],
                    "passed": bt_result.get("passed", False),
                    "skip_reason": bt_result.get("skip_reason", ""),
                    "bt_summary": bt_result.get("bt_summary", ""),
                    "bt_results": bt_result.get("bt_results", []),
                    "saved_runs": bt_result.get("saved_runs", []),
                    "buy_price": bt_result.get("buy_price"),
                    "buy_date": bt_result.get("buy_date"),
                    "sell_price": bt_result.get("sell_price"),
                    "sell_date": bt_result.get("sell_date"),
                    "current_price": bt_result.get("current_price"),
                    "best_params": bt_result.get("best_params"),
                    "index": completed,
                    "total": total,
                }
            except Exception as e:
                logger.error(f"[thread] {stock['code']} 异常: {e}")
                yield {
                    "type": "result",
                    "symbol": stock["code"],
                    "name": stock["name"],
                    "passed": False,
                    "skip_reason": "future_error",
                    "bt_summary": str(e),
                    "saved_runs": [],
                    "index": completed,
                    "total": total,
                }

            # 进度推送
            if completed % batch_size == 0 or completed == total:
                yield {
                    "type": "progress",
                    "status": "checking",
                    "symbol": stock["code"],
                    "name": stock["name"],
                    "index": completed,
                    "total": total,
                    "msg": f"进度 {completed}/{total} ({round(completed/total*100, 1)}%)",
                }


# ================================================================
#  结果过滤、排序、CSV 导出、自动加自选
# ================================================================

# 自动加入自选股的阈值
AUTO_WATCHLIST_WIN_RATE = 90.0   # 胜率 > 90%
AUTO_WATCHLIST_RETURN = 20.0     # 盈利 > 20%


def _extract_best_period(bt_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    从多周期回测结果中提取最优周期的指标。

    选择 winRate 最高的周期；winRate 相同时取 totalReturn 更高的。

    Returns:
        {
            "best_label": str,       # 最优周期标签
            "best_tf": str,          # 最优周期
            "win_rate": float,       # 胜率 %
            "total_return": float,   # 总收益率 %
            "sharpe_ratio": float,   # 夏普比率
            "max_drawdown": float,   # 最大回撤 %
            "total_trades": int,     # 总交易次数
        }
        无有效结果时返回空 dict
    """
    best = {}
    best_wr = -999
    best_ret = -999

    for item in (bt_results or []):
        if item is None:
            continue
        result = item.get("result")
        if result is None:
            continue

        wr = float(result.get("winRate", 0) or 0)
        ret = float(result.get("totalReturn", 0) or 0)

        # 选 winRate 最高的，平局取 totalReturn 更高的
        if (wr > best_wr) or (wr == best_wr and ret > best_ret):
            best_wr = wr
            best_ret = ret
            best = {
                "best_label": item.get("label", ""),
                "best_tf": item.get("tf", ""),
                "win_rate": wr,
                "total_return": ret,
                "sharpe_ratio": float(result.get("sharpeRatio", 0) or 0),
                "max_drawdown": float(result.get("maxDrawdown", 0) or 0),
                "total_trades": int(result.get("totalTrades", 0) or 0),
            }

    return best


def _collect_valid_result(item: Dict[str, Any], indicator_name: str) -> Optional[Dict[str, Any]]:
    """
    从单只股票的回测结果中提取有效记录。

    过滤规则：
      - 无买点信号 → 丢弃
      - 胜率 < 0   → 丢弃
      - 无有效回测结果 → 丢弃

    Returns:
        提取后的记录 dict，无效返回 None
    """
    skip_reason = item.get("skip_reason", "")
    bt_results = item.get("bt_results", [])

    # 无买点 → 丢弃
    if skip_reason in ("no_buy_signal", "indicator_error", "cancelled"):
        return None

    # 提取最优周期指标
    best = _extract_best_period(bt_results)
    if not best:
        return None

    # 胜率 < 0 → 丢弃
    if best["win_rate"] < 0:
        return None

    result = {
        "symbol": item.get("symbol", ""),
        "name": item.get("name", ""),
        "indicator_name": indicator_name,
        "best_label": best["best_label"],
        "best_tf": best["best_tf"],
        "win_rate": best["win_rate"],
        "total_return": best["total_return"],
        "sharpe_ratio": best["sharpe_ratio"],
        "max_drawdown": best["max_drawdown"],
        "total_trades": best["total_trades"],
        "buy_price": item.get("buy_price"),
        "buy_date": item.get("buy_date"),
        "sell_price": item.get("sell_price"),
        "sell_date": item.get("sell_date"),
        "current_price": item.get("current_price"),
    }
    # 参数优化时附加最优参数
    bp = item.get("best_params")
    if bp:
        result["best_params"] = json.dumps(bp, ensure_ascii=False)
    return result


def _save_results_csv(
    valid_results: List[Dict[str, Any]],
    run_params: Dict[str, Any] = None,
    output_dir: str = None,
) -> str:
    """
    将有效回测结果按日期保存为 CSV。

    排序：胜率 降序 → 夏普比率 降序
    文件名：backtest_all_cnstock-YYYYMMDD.csv
    文件头：以 # 开头的参数注释行（运行时间、策略、市场筛选等）
    默认保存到 backend_api_python/data/backtest_results/

    Args:
        valid_results: 已过滤的有效结果列表
        run_params: 本次运行参数（写入 CSV 头部注释）
        output_dir: 自定义输出目录

    Returns:
        CSV 文件路径
    """
    import csv as csv_mod

    if not valid_results:
        return ""

    # 默认输出目录
    if not output_dir:
        output_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "backtest_results",
        )
    os.makedirs(output_dir, exist_ok=True)

    # 文件名：backtest_all_cnstock-YYYYMMDD.csv
    today_str = datetime.now().strftime("%Y%m%d")
    csv_path = os.path.join(output_dir, f"backtest_all_cnstock-{today_str}.csv")

    # 排序：胜率 降序 → 夏普比率 降序
    sorted_results = sorted(
        valid_results,
        key=lambda r: (r.get("win_rate", 0), r.get("sharpe_ratio", 0)),
        reverse=True,
    )

    fieldnames = [
        "symbol", "name", "indicator_name",
        "best_label", "best_tf",
        "win_rate", "total_return", "sharpe_ratio", "max_drawdown", "total_trades",
        "buy_price", "buy_date", "sell_price", "sell_date", "current_price",
        "best_params", "run_time",
    ]

    run_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    params = run_params or {}

    # 构建参数注释行
    comment_lines = [
        f"# backtest_all_cnstock 回测结果",
        f"# 运行时间: {run_time}",
    ]
    if params.get("strategies"):
        strats = params["strategies"]
        names = [s.get("indicator_name", f"指标{s.get('indicator_id', '?')}") for s in strats]
        ids = [str(s.get("indicator_id", "?")) for s in strats]
        comment_lines.append(f"# 策略: {', '.join(names)} (ID: {', '.join(ids)})")
    if params.get("market_filters"):
        comment_lines.append(f"# 市场筛选: {', '.join(params['market_filters'])}")
    if params.get("max_workers"):
        comment_lines.append(f"# 线程数: {params['max_workers']}")
    comment_lines.append(f"# 过滤规则: 胜率<0丢弃, 无买点丢弃")
    comment_lines.append(f"# 排序: 胜率↓ → 夏普比率↓")
    comment_lines.append(f"# 自选阈值: 胜率>{AUTO_WATCHLIST_WIN_RATE}% 且 收益>{AUTO_WATCHLIST_RETURN}%")
    comment_lines.append(f"# 有效结果: {len(sorted_results)} 条")
    comment_lines.append(f"#")

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        # 写入参数注释头
        for line in comment_lines:
            f.write(line + "\n")

        writer = csv_mod.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for row in sorted_results:
            row["run_time"] = run_time
            writer.writerow({k: row.get(k, "") for k in fieldnames})

    logger.info(f"[_save_results_csv] 写入 {len(sorted_results)} 条 → {csv_path}")
    return csv_path


def _auto_watchlist_check(
    valid_results: List[Dict[str, Any]],
    user_id: int,
) -> List[Dict[str, Any]]:
    """
    检查有效结果，将胜率>90%且盈利>20%的股票加入自选股。

    Returns:
        被加入自选股的记录列表（用于 SSE 通知）
    """
    added = []
    for row in valid_results:
        wr = row.get("win_rate", 0)
        ret = row.get("total_return", 0)
        if wr > AUTO_WATCHLIST_WIN_RATE and ret > AUTO_WATCHLIST_RETURN:
            symbol = row.get("symbol", "")
            name = row.get("name", "")
            try:
                _add_to_watchlist(user_id, "CNStock", symbol, name)
                added.append(row)
                logger.info(
                    f"[auto_watchlist] ✅ {symbol} {name} "
                    f"胜率={wr}% 收益={ret}% → 加入自选"
                )
            except Exception as e:
                logger.warning(f"[auto_watchlist] {symbol} 加入自选失败: {e}")

    return added

def backtest_all(
    indicator_id: int = None,
    user_id: int = 1,
    user_params: Dict[str, Any] = None,
    review_mode: str = "mid",
    strategies: List[Dict[str, Any]] = None,
    save_to_db: bool = True,
    market_filters: List[str] = None,
    max_workers: int = 4,
    _cancelled: List[bool] = None,
) -> Generator[str, None, None]:
    """
    全A股多策略回测筛选，结果写入 qd_backtest_runs 表。

    参数：
      indicator_id:   单策略模式的指标ID（与 strategies 二选一）
      user_id:        用户ID
      user_params:    指标参数覆盖（单策略模式）
      review_mode:    默认回测模式 "short"/"mid"/"long"
      strategies:     多策略配置（优先于 indicator_id），格式：
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
      save_to_db:     是否写入 qd_backtest_runs 表
      market_filters: 市场筛选列表，可选值：
                      ["all"]         — 全部A股（默认）
                      ["北证"]        — 北交所（43/82/83/87/88）
                      ["科创"]        — 科创板（688/689）
                      ["沪深"]        — 沪深主板（600/601/603/605 + 000/001/002/003）
                      ["创业板"]      — 创业板（300/301）
                      ["科创","创业板"] — 复选：科创板 + 创业板
                      ["沪市主板"]    — 仅沪市主板
                      ["深市主板"]    — 仅深市主板
      max_workers:    并行线程数（默认 4）

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

    # 预检每个策略的指标代码 + 解析参数搜索范围
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

        # 解析参数声明 + 搜索范围
        declared = IndicatorParamsParser.parse_params(code)
        searchable = IndicatorParamsParser.get_searchable_params(declared)
        user_params = s.get("params") or {}

        # 生成参数组合（有搜索范围时）
        param_combos = None
        if searchable:
            param_combos = IndicatorParamsParser.generate_param_grid(
                declared, max_combinations=200
            )
            # 过滤掉只有 1 种值的参数（无需优化）
            has_variation = any(
                len(set(c.get(p["name"]) for c in param_combos)) > 1
                for p in searchable
            )
            if not has_variation or len(param_combos) <= 1:
                param_combos = None

        if param_combos:
            yield _sse({
                "type": "progress",
                "status": "param_opt_info",
                "indicator_id": sid,
                "indicator_name": name,
                "searchable_count": len(searchable),
                "combo_count": len(param_combos),
                "msg": f"📊 策略 {name}: 发现 {len(searchable)} 个可优化参数, "
                       f"共 {len(param_combos)} 种组合",
            })

        strategy_configs.append({
            "indicator_id": sid,
            "indicator_code": code,
            "indicator_name": name,
            "user_id": uid,
            "params": user_params,
            "periods": periods,
            "mode": mode,
            "param_combos": param_combos,
            "searchable_params": searchable,
        })

    if not strategy_configs:
        yield _sse({"type": "error", "msg": "无有效策略"})
        return

    # ── 获取A股列表（通过 basicinfo_db + 市场筛选） ──
    yield _sse({
        "type": "progress",
        "status": "loading_stocks",
        "msg": "正在获取A股列表...",
        "index": 0,
        "total": 0,
    })

    resolved_filters = _resolve_market_filters(market_filters)
    filter_labels = []
    for f in resolved_filters:
        if f == "all":
            filter_labels.append("全部A股")
        elif f in MARKET_CATEGORIES:
            filter_labels.append(MARKET_CATEGORIES[f]["label"])

    stocks = _get_all_cnstocks(market_filters)
    if not stocks:
        yield _sse({"type": "error", "msg": f"获取A股列表失败 (筛选: {', '.join(filter_labels)})"})
        return

    total_stocks = len(stocks)
    total_tasks = total_stocks * len(strategy_configs)

    logger.info(
        f"[backtest_all] strategies={len(strategy_configs)}, "
        f"stocks={total_stocks} (filters={resolved_filters}), "
        f"total_tasks={total_tasks}, max_workers={max_workers}"
    )

    yield _sse({
        "type": "progress",
        "status": "start",
        "msg": f"开始回测：{len(strategy_configs)} 个策略 × {total_stocks} 只股票 = {total_tasks} 个任务 "
               f"| 市场: {', '.join(filter_labels)} "
               f"| 线程数: {max_workers}",
        "index": 0,
        "total": total_tasks,
        "market_filters": resolved_filters,
        "max_workers": max_workers,
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

    # 收集有效回测结果（用于 CSV 导出 + 自动加自选）
    valid_results: List[Dict[str, Any]] = []
    # 收集参数优化通过的结果（用于持久化最优参数）
    opt_passed_items: List[Dict[str, Any]] = []

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
            s_param_combos = sc.get("param_combos")
            s_searchable = sc.get("searchable_params")

            # 参数优化时调整任务总数（每只股票 × 参数组合数）
            if s_param_combos and len(s_param_combos) > 1:
                opt_total = total_stocks * len(s_param_combos)
                yield _sse({
                    "type": "progress",
                    "status": "strategy_start",
                    "indicator_id": sid,
                    "indicator_name": sname,
                    "msg": f"开始策略：{sname}（{len(s_periods)} 个周期 × {total_stocks} 只 × {len(s_param_combos)} 组参数）",
                    "index": task_idx,
                    "total": total_tasks,
                })
            else:
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

            # ── 多线程并行执行 ──
            for item in _run_stocks_parallel(
                stocks=stocks,
                indicator_code=scode,
                indicator_id=sid,
                indicator_name=sname,
                user_id=s_uid,
                periods=s_periods,
                user_params=s_params,
                save_to_db=save_to_db,
                cancelled=cancelled,
                max_workers=max_workers,
                batch_size=20,
                param_combos=s_param_combos,
                searchable_params=s_searchable,
            ):
                if cancelled[0]:
                    break

                item_type = item.get("type")

                if item_type == "progress":
                    # 转发进度（补充策略信息）
                    item["indicator_id"] = sid
                    item["indicator_name"] = sname
                    task_idx = item.get("index", task_idx)
                    item["index"] = task_idx + (total_stocks * list(stats["by_strategy"].keys()).index(sid))
                    item["total"] = total_tasks
                    item["msg"] = f"[{sname}] {item.get('msg', '')}"
                    yield _sse(item)

                elif item_type == "result":
                    task_idx += 1
                    symbol = item.get("symbol", "")
                    name = item.get("name", "")
                    passed = item.get("passed", False)
                    bt_summary = item.get("bt_summary", "")
                    skip_reason = item.get("skip_reason", "")

                    stats["runs_saved"] += len(item.get("saved_runs", []))

                    # ── 收集有效结果（过滤：胜率<0 丢弃，无买点丢弃） ──
                    valid = _collect_valid_result(item, sname)
                    if valid:
                        valid_results.append(valid)

                    if passed:
                        stats["passed"] += 1
                        stats["by_strategy"][sid]["passed"] += 1
                        passed_list.append(symbol)

                        # 收集参数优化通过的结果
                        if item.get("best_params"):
                            opt_passed_items.append(item)

                        sse_msg = {
                            "type": "result",
                            "indicator_id": sid,
                            "indicator_name": sname,
                            "symbol": symbol,
                            "name": name,
                            "index": task_idx,
                            "total": total_tasks,
                            "added": True,
                            "reason": "passed",
                            "bt_summary": bt_summary,
                            "msg": f"✅ [{sname}] {symbol} {name} 通过 | {bt_summary}",
                        }
                        bp = item.get("best_params")
                        if bp:
                            sse_msg["best_params"] = bp
                        yield _sse(sse_msg)
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

        # ── CSV 导出 + 自动加自选 ──
        csv_path = ""
        if valid_results:
            # 运行参数（写入 CSV 头部注释）
            run_params = {
                "strategies": [
                    {"indicator_id": sc["indicator_id"], "indicator_name": sc["indicator_name"]}
                    for sc in strategy_configs
                ],
                "market_filters": resolved_filters,
                "max_workers": max_workers,
            }
            # 按胜率、夏普比率排序后保存 CSV
            csv_path = _save_results_csv(valid_results, run_params=run_params)
            yield _sse({
                "type": "csv_saved",
                "csv_path": csv_path,
                "valid_count": len(valid_results),
                "msg": f"📊 有效结果 {len(valid_results)} 条，已保存: {csv_path}",
            })

            # 自动加自选：胜率>90% 且 盈利>20%
            auto_added = _auto_watchlist_check(valid_results, user_id)
            if auto_added:
                for row in auto_added:
                    yield _sse({
                        "type": "watchlist_add",
                        "symbol": row["symbol"],
                        "name": row["name"],
                        "win_rate": row["win_rate"],
                        "total_return": row["total_return"],
                        "indicator_name": row["indicator_name"],
                        "msg": f"⭐ 自动加入自选: {row['symbol']} {row['name']} "
                               f"胜率={row['win_rate']}% 收益={row['total_return']}%",
                    })
                yield _sse({
                    "type": "watchlist_summary",
                    "count": len(auto_added),
                    "msg": f"⭐ 共 {len(auto_added)} 只股票自动加入自选股 "
                           f"(胜率>{AUTO_WATCHLIST_WIN_RATE}% 且 收益>{AUTO_WATCHLIST_RETURN}%)",
                })

            # ── 参数优化结果持久化 ──
            if opt_passed_items:
                # 按策略分组保存
                opt_saved_total = 0
                for sc in strategy_configs:
                    sc_combos = sc.get("param_combos")
                    if not sc_combos or len(sc_combos) <= 1:
                        continue
                    sc_items = [
                        it for it in opt_passed_items
                        if it.get("indicator_id") == sc["indicator_id"]
                    ]
                    if not sc_items:
                        continue
                    saved = _save_optimal_params_batch(
                        user_id=user_id,
                        indicator_id=sc["indicator_id"],
                        indicator_name=sc["indicator_name"],
                        items=sc_items,
                        combos_tested=len(sc_combos),
                    )
                    opt_saved_total += saved

                if opt_saved_total > 0:
                    yield _sse({
                        "type": "optimal_params_saved",
                        "count": opt_saved_total,
                        "msg": f"💾 最优参数已保存: {opt_saved_total} 条 "
                               f"(表: qd_indicator_optimal_params)",
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
            "valid_results": len(valid_results),
            "csv_path": csv_path if valid_results else "",
            "strategies": len(strategy_configs),
            "stocks": total_stocks,
            "market_filters": resolved_filters,
            "max_workers": max_workers,
            "msg": f"全部完成：{len(strategy_configs)}个策略 × {total_stocks}只股票 "
                   f"(市场:{', '.join(filter_labels)}), "
                   f"共{stats['passed']}只通过，写入{stats['runs_saved']}条回测记录 "
                   f"| 有效结果{len(valid_results)}条 "
                   f"| 线程数:{max_workers} "
                   f"| {'; '.join(summary_parts)}",
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
    parser.add_argument("--market", type=str, default="all",
                        help="市场筛选，逗号分隔 (如 科创,创业板,北证,沪深,all)")
    parser.add_argument("--workers", type=int, default=4,
                        help="并行线程数 (默认4)")
    parser.add_argument("--no-save", action="store_true", help="不写入数据库")
    args = parser.parse_args()

    indicator_ids = [int(x.strip()) for x in args.indicator_id.split(",")]
    modes = [x.strip() for x in args.mode.split(",")]
    if len(modes) < len(indicator_ids):
        modes = modes * len(indicator_ids)

    market_filters = [x.strip() for x in args.market.split(",") if x.strip()]

    strategies = []
    for i, sid in enumerate(indicator_ids):
        strategies.append({
            "indicator_id": sid,
            "mode": modes[i % len(modes)],
        })

    # 解析市场筛选标签
    resolved = _resolve_market_filters(market_filters)
    filter_labels = []
    for f in resolved:
        if f == "all":
            filter_labels.append("全部A股")
        elif f in MARKET_CATEGORIES:
            filter_labels.append(MARKET_CATEGORIES[f]["label"])

    print(f"🚀 开始全A股多策略回测")
    print(f"   策略数: {len(strategies)}")
    print(f"   指标ID: {indicator_ids}")
    print(f"   模式: {[s['mode'] for s in strategies]}")
    print(f"   市场: {', '.join(filter_labels)}")
    print(f"   线程数: {args.workers}")
    print("=" * 60)

    passed_map = {}

    for msg_str in backtest_all(
        strategies=strategies,
        user_id=args.user_id,
        save_to_db=not args.no_save,
        market_filters=market_filters,
        max_workers=args.workers,
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
                    idx = data.get("index", 0)
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

            elif msg_type == "csv_saved":
                print(f"\n💾 {data.get('msg', '')}")

            elif msg_type == "watchlist_add":
                print(f"  ⭐ {data.get('msg', '')}")

            elif msg_type == "watchlist_summary":
                print(f"\n{data.get('msg', '')}")

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
