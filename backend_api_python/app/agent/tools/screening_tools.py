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
from app.agent.tools.registry import tool

logger = logging.getLogger(__name__)

# ── Tool functions ────────────────────────────────────────────

@tool(
    description="用指标策略批量审核股票。对每只股票执行指标代码，检查是否出现买入信号。返回每只股票的 buy/sell 信号状态和价格。",
    category="选股",
    layer="决策层",
    domain=["finance"],
)
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

@tool(
    description="列出用户收藏的选股策略列表。",
    category="选股",
    layer="决策层",
    domain=["finance"],
)
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

