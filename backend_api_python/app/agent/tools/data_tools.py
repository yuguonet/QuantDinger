# -*- coding: utf-8 -*-
"""
Data tools — real-time quotes, K-lines, stock info.
Wraps DataSourceFactory into OpenAI-function-callable tools.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _get_ds(market: str = "CNStock"):
    from app.data_sources.factory import DataSourceFactory
    return DataSourceFactory.get_source(market)


# ── Re-exported from shared utils (kept for backward compat) ──
from app.agent.utils import detect_market as _detect_market


# ── Tool functions ────────────────────────────────────────────

def get_realtime_quote(stock_code: str) -> Dict[str, Any]:
    """获取股票/交易对的实时行情数据，包括最新价、涨跌幅、成交量、换手率等。"""
    market = _detect_market(stock_code)
    ds = _get_ds(market)
    try:
        result = ds.get_ticker(stock_code)
        if isinstance(result, dict) and "error" not in result:
            return {"stock_code": stock_code, "market": market, **result}
        return result if isinstance(result, dict) else {"error": "Unexpected result type"}
    except NotImplementedError:
        return {"error": f"数据源 {market} 不支持 get_ticker", "retriable": False}
    except Exception as e:
        logger.error("get_realtime_quote(%s) failed: %s", stock_code, e)
        return {"error": str(e)}


def get_daily_history(stock_code: str, days: int = 30) -> List[Dict[str, Any]]:
    """获取股票/交易对的历史日K线数据（OHLCV）。
    
    Args:
        stock_code: 股票代码（如 000001, 600519）或交易对（如 BTC/USDT）
        days: 获取天数，默认30天，最大120天
    """
    days = min(max(days, 1), 120)
    market = _detect_market(stock_code)
    ds = _get_ds(market)
    try:
        klines = ds.get_kline(stock_code, "1D", days) or []
        return klines
    except Exception as e:
        logger.error("get_daily_history(%s, %d) failed: %s", stock_code, days, e)
        return {"error": str(e)}


def get_stock_info(stock_code: str) -> Dict[str, Any]:
    """获取股票基本面信息（公司简介、行业、市值、PE、PB 等）。"""
    market = _detect_market(stock_code)
    ds = _get_ds(market)
    try:
        if hasattr(ds, "get_stock_info"):
            result = ds.get_stock_info(stock_code)
            return result if isinstance(result, dict) else {"error": "Unexpected result"}
        return {"error": f"数据源 {market} 不支持 get_stock_info", "retriable": False}
    except NotImplementedError:
        return {"error": f"数据源 {market} 不支持 get_stock_info", "retriable": False}
    except Exception as e:
        logger.error("get_stock_info(%s) failed: %s", stock_code, e)
        return {"error": str(e)}


# ── OpenAI tool declarations ─────────────────────────────────

DATA_TOOLS = [
    {
        "fn": get_realtime_quote,
        "name": "get_realtime_quote",
        "description": "获取股票或交易对的实时行情（最新价、涨跌幅、成交量、换手率、量比、PE/PB等）。",
        "parameters": {
            "type": "object",
            "properties": {
                "stock_code": {
                    "type": "string",
                    "description": "股票代码（如 000001、600519）或交易对（如 BTC/USDT）",
                },
            },
            "required": ["stock_code"],
        },
    },
    {
        "fn": get_daily_history,
        "name": "get_daily_history",
        "description": "获取历史日K线数据（OHLCV），用于趋势分析和技术指标计算。",
        "parameters": {
            "type": "object",
            "properties": {
                "stock_code": {
                    "type": "string",
                    "description": "股票代码或交易对",
                },
                "days": {
                    "type": "integer",
                    "description": "获取天数，默认30，最大120",
                    "default": 30,
                },
            },
            "required": ["stock_code"],
        },
    },
    {
        "fn": get_stock_info,
        "name": "get_stock_info",
        "description": "获取股票基本面信息（公司简介、行业分类、市值、PE、PB、ROE等）。",
        "parameters": {
            "type": "object",
            "properties": {
                "stock_code": {
                    "type": "string",
                    "description": "股票代码（如 000001、600519）",
                },
            },
            "required": ["stock_code"],
        },
    },
]
