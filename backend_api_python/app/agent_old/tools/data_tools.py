# -*- coding: utf-8 -*-
"""
Data tools — real-time quotes, K-lines, stock info.
Wraps DataSourceFactory into OpenAI-function-callable tools.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from app.agent.tools.registry import tool

logger = logging.getLogger(__name__)

def _get_ds(market: str = "CNStock"):
    from app.data_sources.factory import DataSourceFactory
    return DataSourceFactory.get_source(market)

# ── Re-exported from shared utils (kept for backward compat) ──
from app.data_sources.market_detector import detect_market as _detect_market

# ── Tool functions ────────────────────────────────────────────

@tool(
    description="根据中文名称或关键词搜索股票代码。支持模糊匹配，如输入茅台可找到贵州茅台(600519)。当用户提供中文股票名称但没有代码时，必须先用此工具查到代码再进行后续分析。",
    category="名称查询",
    layer="数据层",
    domain=[],
)
def search_stock_by_name(keyword: str, market: str = "CNStock", limit: int = 10) -> Dict[str, Any]:
    """根据中文名称或关键词搜索股票代码,支持模糊搜索。

    Args:
        keyword: 搜索关键词（中文股票名称、代码片段等）
        market: 市场，默认 CNStock（可选：CNStock、HKStock、Crypto、USStock）
        limit: 返回数量上限，默认10
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return {"error": "搜索关键词不能为空", "retriable": False}

    limit = min(max(limit, 1), 50)
    try:
        from app.utils.basicinfo_db import get_stock_basic_db
        matches = get_stock_basic_db().search_stocks(keyword, limit=limit)
        return {
            "keyword": keyword,
            "market": market,
            "results": [
                {"code": m["symbol"], "name": m.get("name", ""), "market": m.get("market_cn", market)}
                for m in matches
            ],
            "count": len(matches),
        }
    except Exception as e:
        logger.error("search_stock_by_name(%s) failed: %s", keyword, e)
        return {"keyword": keyword, "results": [], "count": 0, "error": str(e)}

@tool(
    description="获取股票或交易对的实时行情（最新价、涨跌幅、成交量、换手率、量比、PE/PB等）。",
    category="行情数据",
    layer="数据层",
    domain=["finance"],
)
def get_realtime_quote(stock_code: str) -> Dict[str, Any]:
    """获取股票/交易对的实时行情数据，包括最新价、涨跌幅、成交量、换手率等。"""
    market = _detect_market(stock_code) or "CNStock"
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

@tool(
    description="获取股票/交易对的K线数据（OHLCV：开盘价/最高价/最低价/收盘价/成交量）。支持多周期：1m/5m/15m/30m/1H/4H/1D/1W。这是获取原始K线数据的核心工具，用于趋势分析和技术指标计算。当用户要求查看K线、行情数据、历史价格时必须使用此工具。",
    category="行情数据",
    layer="数据层",
    domain=["finance"],
)
def agent_get_kline(stock_code: str, timeframe: str = "1D", days: int = 60, market: str = "") -> List[Dict[str, Any]]:
    """获取股票/交易对的K线数据（OHLCV）。

    Args:
        stock_code: 股票代码（如 000001, 600519）或交易对（如 BTC/USDT）
        timeframe: K线周期，可选值: 1m, 5m, 15m, 30m, 1H, 4H, 1D, 1W。默认 1D（日线）
        days: 获取天数，默认60天，最大250天（仅对日线及以上周期有意义）
        market: 市场类型，可选值: CNStock, HKStock, Crypto, Forex, USStock, Futures, MOEX。
                留空则自动推断（A股6位数字→CNStock, HK前缀→HKStock, USDT结尾→Crypto 等）。
                当自动推断不准时（如美股代码、期货合约）需手动指定。
    """
    valid_timeframes = {"1m", "5m", "15m", "30m", "1H", "4H", "1D", "1W"}
    if timeframe not in valid_timeframes:
        return []
    days = min(max(days, 1), 250)
    if market:
        from app.data_sources.factory import DataSourceFactory
        market = DataSourceFactory.normalize_market(market)
    else:
        market = _detect_market(stock_code) or "CNStock"
    ds = _get_ds(market)
    try:
        klines = ds.get_kline(stock_code, timeframe, days) or []
        # 精简返回：缩短字段名、四舍五入价格，大幅减少 token 消耗
        compact = []
        for k in klines:
            compact.append({
                "t": k.get("date", k.get("timestamp", "")),
                "o": round(k.get("open", 0), 2),
                "h": round(k.get("high", 0), 2),
                "l": round(k.get("low", 0), 2),
                "c": round(k.get("close", 0), 2),
                "v": k.get("volume", 0),
            })
        return compact
    except Exception as e:
        logger.error("get_kline(%s, %s, %d) failed: %s", stock_code, timeframe, days, e)
        return []
@tool(
    description="获取股票基本面信息（行业、概念、市值、PE、PB等）。",
    category="行情数据",
    layer="数据层",
    domain=["finance"],
)
def get_stock_info(stock_code: str) -> Dict[str, Any]:
    """获取股票基本面信息（行业、概念、市值、PE、PB 等）+ 实时估值补全。"""
    market = _detect_market(stock_code) or "CNStock"
    ds = _get_ds(market)
    result: Dict[str, Any] = {}

    # 1) 尝试数据源原生 get_stock_info
    try:
        if hasattr(ds, "get_stock_info"):
            info = ds.get_stock_info(stock_code)
            if isinstance(info, dict) and not info.get("error"):
                result = info
            elif isinstance(info, str):
                try:
                    import json as _json
                    parsed = _json.loads(info)
                    if isinstance(parsed, dict):
                        result = parsed
                    else:
                        result = {"stock_code": stock_code, "info_text": info}
                except (ValueError, TypeError):
                    result = {"stock_code": stock_code, "info_text": info}
    except NotImplementedError:
        pass
    except Exception as e:
        logger.warning("get_stock_info(%s) datasource failed: %s", stock_code, e)

    # 2) 兜底：HTTP 实时拉取（双源互补）
    if not result:
        try:
            from app.utils.cn_stock_info import get_cn_stock_info
            info = get_cn_stock_info(stock_code)
            if info and not info.get("error"):
                result = info
        except Exception as e:
            logger.warning("get_stock_info(%s) cn_stock_info failed: %s", stock_code, e)

    # 3) 兜底：本地缓存 basicinfo_db
    if not result:
        try:
            from app.utils.basicinfo_db import get_stock_basic_db
            from app.data_sources.normalizer import strip_market_prefix

            sym = strip_market_prefix(stock_code)
            db = get_stock_basic_db()
            stock = db.get_stock(sym)
            if stock:
                result = {"stock_code": sym}
                if stock.get("name"):
                    result["name"] = stock["name"]
                if stock.get("industry"):
                    result["industry"] = stock["industry"]
                concepts_str = stock.get("concepts", "")
                if concepts_str:
                    result["concepts"] = [c.strip() for c in concepts_str.split(",") if c.strip()]
                if stock.get("total_shares"):
                    result["total_shares"] = stock["total_shares"]
                if stock.get("circ_shares"):
                    result["circ_shares"] = stock["circ_shares"]
                if stock.get("pe_ratio"):
                    result["pe_ratio"] = stock["pe_ratio"]
                if stock.get("pb_ratio"):
                    result["pb_ratio"] = stock["pb_ratio"]
                if stock.get("market_cn"):
                    result["market_cn"] = stock["market_cn"]
                if stock.get("list_date"):
                    result["list_date"] = stock["list_date"]
                result["source"] = "basicinfo_db"
        except Exception as e:
            logger.warning("get_stock_info(%s) basicinfo_db fallback failed: %s", stock_code, e)

    if not result:
        return {"error": f"无法获取 {stock_code} 的基本面信息", "retriable": False}

    # 4) 腾讯实时估值补全（PE/PB/市值/换手率/量比等）
    try:
        from app.agent.tools.quote_tools import _tencent_quote_raw, _stock_code_normalize
        code = _stock_code_normalize(stock_code)
        tq = _tencent_quote_raw([code])
        q = tq.get(code)
        if q:
            result.setdefault("price", q.get("price"))
            result.setdefault("change_pct", q.get("change_pct"))
            # 估值：优先用腾讯的（实时），basicinfo_db 的是静态值
            if q.get("pe_ttm"):
                result["pe_ttm"] = q["pe_ttm"]
            if q.get("pe_static"):
                result["pe_static"] = q["pe_static"]
            if q.get("pb"):
                result["pb"] = q["pb"]
            if q.get("mcap_yi"):
                result["mcap_yi"] = q["mcap_yi"]
            if q.get("float_mcap_yi"):
                result["float_mcap_yi"] = q["float_mcap_yi"]
            if q.get("turnover_pct") is not None:
                result["turnover_pct"] = q["turnover_pct"]
            if q.get("vol_ratio") is not None:
                result["vol_ratio"] = q["vol_ratio"]
            if q.get("amplitude_pct") is not None:
                result["amplitude_pct"] = q["amplitude_pct"]
            if q.get("amount_wan"):
                result["amount_wan"] = q["amount_wan"]
            if q.get("limit_up"):
                result["limit_up"] = q["limit_up"]
            if q.get("limit_down"):
                result["limit_down"] = q["limit_down"]
    except Exception as e:
        logger.debug("get_stock_info(%s) 腾讯估值补全跳过: %s", stock_code, e)

    return result

