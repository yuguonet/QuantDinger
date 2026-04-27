# -*- coding: utf-8 -*-
"""
Indicator tools — run indicator strategies, list indicators, get parameters.

Wraps indicator.py execution engine and safe_exec sandbox into
Agent-callable tools.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Tool functions ────────────────────────────────────────────

def list_indicators(user_id: int = 1) -> Dict[str, Any]:
    """列出用户的所有指标策略（自建 + 购买的）。

    返回指标 ID、名称、描述、是否已购买等信息。

    Args:
        user_id: 用户 ID，默认 1
    """
    from app.utils.db import get_db_connection

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, user_id, is_buy, name, description, publish_to_community, "
                "pricing_type, price, createtime, updatetime "
                "FROM qd_indicator_codes "
                "WHERE user_id = %s OR publish_to_community = 1 "
                "ORDER BY id DESC",
                (user_id,),
            )
            rows = cur.fetchall() or []
            cur.close()

        indicators = []
        for r in rows:
            d = dict(r)
            for ts_field in ("createtime", "updatetime"):
                if d.get(ts_field) and hasattr(d[ts_field], "isoformat"):
                    d[ts_field] = d[ts_field].isoformat()
            if d.get("price") is not None:
                d["price"] = float(d["price"])
            indicators.append(d)

        return {"indicators": indicators, "count": len(indicators)}
    except Exception as e:
        logger.error("list_indicators failed: %s", e, exc_info=True)
        return {"indicators": [], "count": 0, "error": str(e)}


def get_indicator_params(indicator_id: int, user_id: int = 1) -> Dict[str, Any]:
    """获取指标策略声明的可配置参数。

    解析指标代码中的 # @param 注释，返回参数名称、类型、默认值。

    Args:
        indicator_id: 指标 ID
        user_id: 用户 ID，默认 1
    """
    from app.utils.db import get_db_connection
    from app.services.indicator_params import IndicatorParamsParser

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
            return {"params": [], "error": f"指标 {indicator_id} 不存在或无权限"}

        code = row.get("code") or ""
        name = row.get("name") or ""
        params = IndicatorParamsParser.parse_params(code)

        return {
            "indicator_id": indicator_id,
            "indicator_name": name,
            "params": params,
            "count": len(params),
        }
    except Exception as e:
        logger.error("get_indicator_params failed: %s", e, exc_info=True)
        return {"params": [], "error": str(e)}


def run_indicator_signal(
    indicator_id: int,
    stock_code: str,
    timeframe: str = "1D",
    days: int = 60,
    user_id: int = 1,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """对单只股票执行指标策略，返回最新的 buy/sell 信号和指标数据。

    拉取 K 线 → 沙箱执行指标代码 → 提取 output 中的信号和图表数据。

    Args:
        indicator_id: 指标策略 ID
        stock_code: 股票代码（如 600519, 000001, BTC/USDT）
        timeframe: K 线周期，默认 1D（可选 1H, 4H, 1W）
        days: 获取 K 线天数，默认 60
        user_id: 用户 ID，默认 1
        params: 指标参数覆盖（可选）
    """
    import pandas as pd
    import numpy as np
    from app.utils.db import get_db_connection
    from app.services.indicator_params import IndicatorParamsParser
    from app.utils.safe_exec import build_safe_builtins, safe_exec_with_validation
    from app.services.kline import KlineService
    from app.agent.utils import detect_market

    days = min(max(days, 10), 500)

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
            return {"success": False, "error": f"指标 {indicator_id} 不存在或无权限"}
        indicator_code = row.get("code") or ""
        indicator_name = row.get("name") or f"Indicator #{indicator_id}"
    except Exception as e:
        return {"success": False, "error": f"加载指标失败: {e}"}

    if not indicator_code.strip():
        return {"success": False, "error": "指标代码为空"}

    # 2. 获取 K 线
    market = detect_market(stock_code)
    try:
        kline_svc = KlineService()
        klines = kline_svc.get_kline(market=market, symbol=stock_code, timeframe=timeframe, limit=days)
        if not klines or len(klines) < 10:
            return {"success": False, "error": f"{stock_code} K线数据不足（{len(klines) if klines else 0}条）"}
    except Exception as e:
        return {"success": False, "error": f"获取K线失败: {e}"}

    # 3. 构建 DataFrame
    df = pd.DataFrame(klines)
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
        else:
            df[col] = 0.0

    # 4. 解析并合并参数
    declared_params = IndicatorParamsParser.parse_params(indicator_code)
    merged_params = IndicatorParamsParser.merge_params(declared_params, params or {})

    # 5. 沙箱执行
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
        return {
            "success": False,
            "error": f"指标执行失败: {exec_result.get('error', '未知错误')}",
            "indicator_name": indicator_name,
        }

    # 6. 提取结果
    executed_df = exec_env.get("df", df)
    output = exec_env.get("output") or {}

    # 提取 buy/sell 信号
    has_buy = False
    has_sell = False
    buy_price = None
    sell_price = None
    current_price = float(executed_df["close"].iloc[-1]) if len(executed_df) > 0 else None

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
            has_sell = True
            last_sell_idx = sell_series[sell_series].index[-1]
            try:
                sell_price = float(executed_df.loc[last_sell_idx, "close"])
            except Exception:
                pass

    # 提取 output 中的图表数据（只取最后 10 个点，避免 token 爆炸）
    plots_summary = []
    for p in output.get("plots", []):
        plot_data = p.get("data", [])
        recent = plot_data[-10:] if len(plot_data) > 10 else plot_data
        plots_summary.append({
            "name": p.get("name", ""),
            "color": p.get("color", ""),
            "overlay": p.get("overlay", True),
            "recent_values": [round(v, 4) if isinstance(v, (int, float)) else v for v in recent],
        })

    signals_summary = []
    for s in output.get("signals", []):
        sig_data = s.get("data", [])
        non_null = [(i, v) for i, v in enumerate(sig_data[-20:]) if v is not None]
        signals_summary.append({
            "type": s.get("type", ""),
            "recent_signals": non_null[-5:] if non_null else [],
        })

    # 判断信号状态
    if has_buy and not has_sell:
        signal_status = "买入信号"
    elif has_sell and not has_buy:
        signal_status = "卖出信号"
    elif has_buy and has_sell:
        signal_status = "买卖信号均有（需判断先后）"
    else:
        signal_status = "无信号"

    return {
        "success": True,
        "stock_code": stock_code,
        "indicator_id": indicator_id,
        "indicator_name": indicator_name,
        "current_price": current_price,
        "has_buy": has_buy,
        "has_sell": has_sell,
        "buy_price": buy_price,
        "sell_price": sell_price,
        "signal_status": signal_status,
        "plots": plots_summary,
        "signals": signals_summary,
        "data_points": len(executed_df),
    }


# ── OpenAI tool declarations ─────────────────────────────────

INDICATOR_TOOLS = [
    {
        "fn": list_indicators,
        "name": "list_indicators",
        "description": "列出用户的所有指标策略（自建 + 购买的），返回指标 ID、名称、描述。",
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
    {
        "fn": get_indicator_params,
        "name": "get_indicator_params",
        "description": (
            "获取指标策略声明的可配置参数。解析代码中的 # @param 注释，"
            "返回参数名称、类型、默认值和描述。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "indicator_id": {
                    "type": "integer",
                    "description": "指标 ID",
                },
                "user_id": {
                    "type": "integer",
                    "description": "用户 ID，默认 1",
                    "default": 1,
                },
            },
            "required": ["indicator_id"],
        },
    },
    {
        "fn": run_indicator_signal,
        "name": "run_indicator_signal",
        "description": (
            "对单只股票执行指标策略，返回最新的 buy/sell 信号、当前价格、"
            "指标图表数据。用于判断某只股票当前是否出现交易信号。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "indicator_id": {
                    "type": "integer",
                    "description": "指标策略 ID",
                },
                "stock_code": {
                    "type": "string",
                    "description": "股票代码（如 600519, 000001）或交易对（如 BTC/USDT）",
                },
                "timeframe": {
                    "type": "string",
                    "description": "K线周期，默认 1D（可选 1H, 4H, 1W）",
                    "default": "1D",
                },
                "days": {
                    "type": "integer",
                    "description": "获取K线天数，默认 60",
                    "default": 60,
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
            "required": ["indicator_id", "stock_code"],
        },
    },
]
