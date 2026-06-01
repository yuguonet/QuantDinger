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

def resolve_stock_name(stock_code: str) -> Dict[str, Any]:
    """根据股票代码获取中文名称。

    Args:
        stock_code: 股票代码（如 600519、000001）或交易对（如 BTC/USDT）
    """
    market = _detect_market(stock_code)
    try:
        from app.services.symbol_name import resolve_symbol_name
        name = resolve_symbol_name(market, stock_code)
        if name:
            return {"stock_code": stock_code, "name": name, "market": market}
        return {"stock_code": stock_code, "name": None, "market": market, "message": "未找到对应名称"}
    except Exception as e:
        logger.error("resolve_stock_name(%s) failed: %s", stock_code, e)
        return {"stock_code": stock_code, "error": str(e)}


def search_stock_by_name(keyword: str, market: str = "CNStock", limit: int = 10) -> Dict[str, Any]:
    """根据中文名称或关键词搜索股票代码。

    支持模糊匹配，如输入"茅台"可找到"贵州茅台 600519"。

    Args:
        keyword: 搜索关键词（中文股票名称、代码片段等）
        market: 市场，默认 CNStock（可选：CNStock、HKStock、Crypto、USStock）
        limit: 返回数量上限，默认10
    """
    if not keyword or not keyword.strip():
        return {"error": "搜索关键词不能为空", "retriable": False}

    limit = min(max(limit, 1), 50)
    try:
        from app.data.market_symbols_seed import search_symbols
        results = search_symbols(market, keyword.strip(), limit)
        if results:
            return {
                "keyword": keyword,
                "market": market,
                "results": [{"code": r["symbol"], "name": r.get("name", ""), "market": r.get("market", market)} for r in results],
                "count": len(results),
            }

        # Fallback: 从 basicinfo_db 查（A股）
        if market == "CNStock":
            try:
                from app.utils.basicinfo_db import get_stock_basic_db
                db = get_stock_basic_db()
                # 尝试按名称搜
                stocks = db.search_by_name(keyword.strip())
                if stocks:
                    return {
                        "keyword": keyword,
                        "market": market,
                        "results": [{"code": s.get("code", ""), "name": s.get("name", ""), "market": "CNStock"} for s in stocks[:limit]],
                        "count": len(stocks[:limit]),
                    }
            except Exception:
                pass

        return {"keyword": keyword, "market": market, "results": [], "count": 0, "message": "未找到匹配的股票"}
    except Exception as e:
        logger.error("search_stock_by_name(%s) failed: %s", keyword, e)
        return {"keyword": keyword, "results": [], "count": 0, "error": str(e)}


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


def get_daily_history(stock_code: str, days: int = 60) -> List[Dict[str, Any]]:
    """获取股票/交易对的历史日K线数据（OHLCV）。
    
    Args:
        stock_code: 股票代码（如 000001, 600519）或交易对（如 BTC/USDT）
        days: 获取天数，默认60天，最大250天
    """
    days = min(max(days, 1), 250)
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
            if isinstance(result, dict):
                return result
            if isinstance(result, str):
                # 某些数据源返回字符串，尝试 JSON 解析
                try:
                    import json as _json
                    parsed = _json.loads(result)
                    if isinstance(parsed, dict):
                        return parsed
                except (ValueError, TypeError):
                    pass
                # 解析失败，包装为 dict
                return {"stock_code": stock_code, "info_text": result}
            return {"stock_code": stock_code, "raw_result": str(result)[:2000] if result else None}
        return {"error": f"数据源 {market} 不支持 get_stock_info", "retriable": False}
    except NotImplementedError:
        return {"error": f"数据源 {market} 不支持 get_stock_info", "retriable": False}
    except Exception as e:
        logger.error("get_stock_info(%s) failed: %s", stock_code, e)
        return {"error": str(e)}


# ── OpenAI tool declarations ─────────────────────────────────

DATA_TOOLS = [
    {
        "fn": resolve_stock_name,
        "name": "resolve_stock_name",
        "description": "根据股票代码获取中文名称。如输入 600519 返回贵州茅台。",
        "parameters": {
            "type": "object",
            "properties": {
                "stock_code": {
                    "type": "string",
                    "description": "股票代码（如 600519、000001）或交易对（如 BTC/USDT）",
                },
            },
            "required": ["stock_code"],
        },
    },
    {
        "fn": search_stock_by_name,
        "name": "search_stock_by_name",
        "description": "根据中文名称或关键词搜索股票代码。支持模糊匹配，如输入茅台可找到贵州茅台(600519)。当用户提供中文股票名称但没有代码时，必须先用此工具查到代码再进行后续分析。",
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "搜索关键词（中文股票名称、代码片段等）",
                },
                "market": {
                    "type": "string",
                    "description": "市场，默认 CNStock",
                    "default": "CNStock",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回数量上限，默认10",
                    "default": 10,
                },
            },
            "required": ["keyword"],
        },
    },
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
        "description": "获取股票/交易对的历史日K线数据（OHLCV：开盘价/最高价/最低价/收盘价/成交量）。这是获取原始K线数据的核心工具，用于趋势分析和技术指标计算。当用户要求查看K线、行情数据、历史价格时必须使用此工具。",
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
