# -*- coding: utf-8 -*-
"""
Screening tools — stock screening (选股) and indicator-based review.

Wraps xuangu.py selection logic and indicator_review.py validation
into Agent-callable tools.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Tool functions ────────────────────────────────────────────

def screen_stocks(
    conditions: Optional[Dict[str, Any]] = None,
    market: str = "CNStock",
    limit: int = 50,
    date: str = "",
) -> Dict[str, Any]:
    """根据条件从全市场筛选股票（初选）。

    查询 cnstock_selection 表，支持按行业、概念、涨跌幅、换手率等条件过滤。
    返回候选股票列表。

    Args:
        conditions: 筛选条件字典，可选键：
            - industry (str): 行业名称
            - concept (str): 概念关键词
            - min_change_rate (float): 最低涨跌幅 %
            - max_change_rate (float): 最高涨跌幅 %
            - min_turnover (float): 最低换手率 %
            - min_volume_ratio (float): 最低量比
        market: 市场，默认 CNStock
        limit: 返回数量上限，默认 50
        date: 交易日期（YYYY-MM-DD），空则取最新
    """
    from app.utils.db import get_db_connection

    conditions = conditions or {}
    limit = min(max(limit, 1), 200)

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            # 确定日期
            if date:
                target_date = date
            else:
                cur.execute("SELECT MAX(date) AS d FROM cnstock_selection")
                row = cur.fetchone() or {}
                target_date = str(row.get("d") or "")
                if not target_date:
                    return {"stocks": [], "count": 0, "message": "选股数据为空，请先同步数据"}

            # 构建 WHERE 子句（列名容错：cnstock_selection 表结构由同步服务动态创建）
            where_parts = ["date = %s"]
            params: list = [target_date]

            # 可选条件：列不存在时静默跳过
            _optional_conditions = [
                ("industry", "ILIKE", "LIKE"),
                ("concept", "ILIKE", "LIKE"),
            ]
            for col, pg_op, fallback_op in _optional_conditions:
                val = conditions.get(col)
                if val:
                    where_parts.append(f"{col} {pg_op} %s")
                    params.append(f"%{val}%")

            _numeric_conditions = [
                ("min_change_rate", "change_rate", ">="),
                ("max_change_rate", "change_rate", "<="),
                ("min_turnover", "turnover_rate", ">="),
                ("min_volume_ratio", "volume_ratio", ">="),
            ]
            for param_key, col, op in _numeric_conditions:
                val = conditions.get(param_key)
                if val is not None:
                    try:
                        where_parts.append(f"{col} {op} %s")
                        params.append(float(val))
                    except (ValueError, TypeError):
                        pass

            where_sql = " AND ".join(where_parts)
            params.append(limit)

            # 用 SELECT * 避免列名假设，结果按字典键取值
            cur.execute(
                f"SELECT * FROM cnstock_selection "
                f"WHERE {where_sql} ORDER BY id DESC LIMIT %s",
                tuple(params),
            )
            rows = cur.fetchall() or []
            cur.close()

        stocks = []
        for r in rows:
            d = dict(r)
            # 安全转换数值字段（列可能不存在）
            for k in ("change_rate", "turnover_rate", "volume_ratio", "new_price",
                       "close", "open", "high", "low", "volume"):
                if d.get(k) is not None:
                    try:
                        d[k] = float(d[k])
                    except (ValueError, TypeError):
                        pass
            # 只保留需要的字段，去掉内部字段
            stock = {}
            for k in ("code", "name", "industry", "concept", "change_rate",
                       "turnover_rate", "volume_ratio", "new_price", "close",
                       "open", "high", "low", "volume", "market"):
                if k in d:
                    stock[k] = d[k]
            stocks.append(stock)

        return {
            "date": target_date,
            "stocks": stocks,
            "count": len(stocks),
        }
    except Exception as e:
        logger.error("screen_stocks failed: %s", e, exc_info=True)
        return {"stocks": [], "count": 0, "error": str(e)}


def review_stocks_with_indicator(
    stock_codes: List[str],
    indicator_id: int,
    user_id: int = 1,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """用指标策略批量审核股票，检查是否出现买入信号。

    对每只股票拉取 K 线数据，沙箱执行指标代码，提取 buy/sell 信号。

    Args:
        stock_codes: 股票代码列表，如 ["600519", "000001"]
        indicator_id: 指标策略 ID
        user_id: 用户 ID（默认 1）
        params: 指标参数覆盖（可选）
    """
    from app.utils.db import get_db_connection
    from app.services.indicator_params import IndicatorParamsParser
    from app.utils.safe_exec import build_safe_builtins, safe_exec_with_validation
    from app.services.kline import KlineService
    from app.agent.utils import detect_market

    if not stock_codes:
        return {"results": [], "count": 0, "message": "未提供股票代码"}
    stock_codes = [str(c).strip() for c in stock_codes if c]
    if not stock_codes:
        return {"results": [], "count": 0, "message": "未提供股票代码"}
    if len(stock_codes) > 50:
        return {"results": [], "count": 0, "message": "单次最多审核50只股票"}

    # 1. 加载指标代码
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT code, name FROM qd_indicator_codes "
                "WHERE id = %s AND (user_id = %s OR publish_to_community = 1)",
                (indicator_id, user_id),
            )
            row = cur.fetchone()
            cur.close()
        if not row:
            return {"results": [], "count": 0, "error": f"指标 {indicator_id} 不存在或无权限"}
        indicator_code = row.get("code") or ""
        indicator_name = row.get("name") or f"Indicator #{indicator_id}"
    except Exception as e:
        return {"results": [], "count": 0, "error": f"加载指标失败: {e}"}

    if not indicator_code.strip():
        return {"results": [], "count": 0, "error": "指标代码为空"}

    # 2. 解析参数
    declared_params = IndicatorParamsParser.parse_params(indicator_code)
    merged_params = IndicatorParamsParser.merge_params(declared_params, params or {})

    # 3. 逐只执行
    import pandas as pd
    import numpy as np
    kline_svc = KlineService()
    results = []

    for code in stock_codes:
        market = detect_market(code)
        try:
            klines = kline_svc.get_kline(market=market, symbol=code, timeframe="1D", limit=200)
            if not klines or len(klines) < 10:
                results.append({"code": code, "has_buy": False, "error": "K线数据不足"})
                continue

            df = pd.DataFrame(klines)
            for col in ("open", "high", "low", "close", "volume"):
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
                else:
                    df[col] = 0.0

            exec_env = {
                "df": df.copy(),
                "pd": pd,
                "np": np,
                "params": merged_params,
                "output": None,
            }
            exec_env["__builtins__"] = build_safe_builtins()

            exec_result = safe_exec_with_validation(
                code=indicator_code,
                exec_globals=exec_env,
                exec_locals=exec_env,
                timeout=30,
            )

            if not exec_result.get("success"):
                results.append({
                    "code": code,
                    "has_buy": False,
                    "error": f"指标执行失败: {exec_result.get('error', '未知')}",
                })
                continue

            executed_df = exec_env.get("df", df)
            has_buy = False
            buy_price = None
            sell_price = None

            if "buy" in executed_df.columns:
                buy_series = executed_df["buy"].astype(bool)
                if buy_series.any():
                    has_buy = True
                    last_buy_idx = buy_series[buy_series].index[-1]
                    try:
                        buy_price = float(executed_df.loc[last_buy_idx, "close"])
                    except Exception:
                        pass

            if "sell" in executed_df.columns:
                sell_series = executed_df["sell"].astype(bool)
                if sell_series.any():
                    last_sell_idx = sell_series[sell_series].index[-1]
                    try:
                        sell_price = float(executed_df.loc[last_sell_idx, "close"])
                    except Exception:
                        pass

            current_price = float(executed_df["close"].iloc[-1]) if len(executed_df) > 0 else None

            results.append({
                "code": code,
                "has_buy": has_buy,
                "buy_price": buy_price,
                "sell_price": sell_price,
                "current_price": current_price,
            })

        except Exception as e:
            results.append({"code": code, "has_buy": False, "error": str(e)})

    buy_count = sum(1 for r in results if r.get("has_buy"))
    return {
        "indicator_id": indicator_id,
        "indicator_name": indicator_name,
        "results": results,
        "count": len(results),
        "buy_count": buy_count,
    }


def list_user_selection_strategies(user_id: int = 1) -> Dict[str, Any]:
    """列出用户收藏的选股策略。

    Args:
        user_id: 用户 ID
    """
    from app.utils.db import get_db_connection

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, name, conditions, description, created_at "
                "FROM qd_user_strategies WHERE user_id = %s ORDER BY updated_at DESC",
                (user_id,),
            )
            rows = cur.fetchall() or []
            cur.close()

        strategies = []
        for r in rows:
            d = dict(r)
            if d.get("created_at") and hasattr(d["created_at"], "isoformat"):
                d["created_at"] = d["created_at"].isoformat()
            try:
                d["conditions"] = json.loads(d["conditions"]) if d.get("conditions") else []
            except Exception:
                pass
            strategies.append(d)

        return {"strategies": strategies, "count": len(strategies)}
    except Exception as e:
        logger.error("list_user_selection_strategies failed: %s", e)
        return {"strategies": [], "count": 0, "error": str(e)}


# ── OpenAI tool declarations ─────────────────────────────────

SCREENING_TOOLS = [
    {
        "fn": screen_stocks,
        "name": "screen_stocks",
        "description": (
            "从全市场筛选股票（初选）。查询选股数据库，支持按行业、概念、涨跌幅、"
            "换手率、量比等条件过滤。返回候选股票列表。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "conditions": {
                    "type": "object",
                    "description": "筛选条件，可选键：industry(行业), concept(概念), "
                                   "min_change_rate(最低涨跌幅%), max_change_rate(最高涨跌幅%), "
                                   "min_turnover(最低换手率%), min_volume_ratio(最低量比)",
                },
                "market": {
                    "type": "string",
                    "description": "市场，默认 CNStock",
                    "default": "CNStock",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回数量上限，默认50，最大200",
                    "default": 50,
                },
                "date": {
                    "type": "string",
                    "description": "交易日期 YYYY-MM-DD，空则取最新",
                },
            },
        },
    },
    {
        "fn": review_stocks_with_indicator,
        "name": "review_stocks_with_indicator",
        "description": (
            "用指标策略批量审核股票。对每只股票执行指标代码，检查是否出现买入信号。"
            "返回每只股票的 buy/sell 信号状态和价格。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "stock_codes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "股票代码列表，如 ['600519', '000001']",
                },
                "indicator_id": {
                    "type": "integer",
                    "description": "指标策略 ID",
                },
                "user_id": {
                    "type": "integer",
                    "description": "用户 ID，默认 1",
                    "default": 1,
                },
                "params": {
                    "type": "object",
                    "description": "指标参数覆盖（可选）",
                },
            },
            "required": ["stock_codes", "indicator_id"],
        },
    },
    {
        "fn": list_user_selection_strategies,
        "name": "list_user_selection_strategies",
        "description": "列出用户收藏的选股策略列表。",
        "parameters": {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "integer",
                    "description": "用户 ID，默认 1",
                    "default": 1,
                },
            },
        },
    },
]
