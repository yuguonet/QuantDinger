# -*- coding: utf-8 -*-
"""
Screener Tools — Agent 选股工具。

合并原 screen_stocks + smart_screen 为统一的 search_stocks。
数据源：eastmoney_search.search_stocks (东财智能选股) + 本地 DB fallback。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.agent.tools.screener_config import MARKET_FILTER_MAP
from app.agent.tools.screener_filters import (
    build_keyword_from_filters,
    get_screener_presets,
)
from app.agent.tools.registry import tool

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
#  东方财富 API 调用 (正本: eastmoney_search.py)
# ══════════════════════════════════════════════════════════════

def _em_search(keyword: str, page_size: int = 100) -> List[Dict[str, Any]]:
    """东财搜索封装，返回股票列表或空列表。"""
    from app.market_cn.eastmoney_search import search_stocks
    try:
        raw = search_stocks(keyword=keyword, page_size=page_size)
        return raw.get("stocks", []) if raw.get("code") == 1 else []
    except Exception as e:
        logger.warning("[东财搜索] '%s' 失败: %s", keyword, e)
        return []

def _call_eastmoney_api(keyword: str, page_size: int = 200, page_no: int = 1) -> Dict[str, Any]:
    """调东财搜索，返回原始 API 响应（code=100 格式）。供 search_stocks 的 eastmoney 模式使用。"""
    from app.market_cn.eastmoney_search import search_stocks
    # eastmoney_search.search_stocks 返回 code=1 格式，需要转换回原始 API 格式
    result = search_stocks(keyword=keyword, page_size=page_size, page_no=page_no)
    if result.get("code") == 1:
        # 转换为原始东财 API 响应格式
        return {
            "code": "100",
            "data": {
                "result": {
                    "dataList": [
                        {
                            "SECURITY_CODE": s.get("code", ""),
                            "SECURITY_SHORT_NAME": s.get("name", ""),
                            "INDUSTRY": s.get("industry", ""),
                            "CONCEPT": s.get("concept", ""),
                            "NEWEST_PRICE": s.get("new_price"),
                            "CHG": s.get("change_rate"),
                            "HIGH_PRICE": s.get("high_price"),
                            "LOW_PRICE": s.get("low_price"),
                            "PRE_CLOSE_PRICE": s.get("pre_close_price"),
                            "TRADE_VOLUME": s.get("volume"),
                            "TRADING_VOLUMES": s.get("deal_amount"),
                            "QRR": s.get("volume_ratio"),
                            "TURNOVER_RATE": s.get("turnoverrate"),
                            "AMPLITUDE": s.get("amplitude"),
                            "PE_DYNAMIC": s.get("pe9"),
                            "PB_NEW_MRQ": s.get("pbnewmrq"),
                            "TOEAL_MARKET_VALUE": s.get("total_market_cap"),
                            "FREE_CAP": s.get("free_cap"),
                        }
                        for s in result.get("stocks", [])
                    ],
                    "total": result.get("total", 0),
                },
            },
        }
    return {"code": "0", "msg": result.get("msg", "搜索失败")}

def _parse_stock_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """将东方财富返回的单只股票解析为标准格式。"""
    from app.data_sources.normalizer import safe_float as _sf
    return {
        "code": item.get("SECURITY_CODE", ""),
        "name": item.get("SECURITY_SHORT_NAME", ""),
        "industry": item.get("INDUSTRY", ""),
        "concept": item.get("CONCEPT", ""),
        "new_price": _sf(item.get("NEWEST_PRICE")),
        "change_rate": _sf(item.get("CHG")),
        "high_price": _sf(item.get("HIGH_PRICE")),
        "low_price": _sf(item.get("LOW_PRICE")),
        "pre_close_price": _sf(item.get("PRE_CLOSE_PRICE")),
        "volume": _sf(item.get("TRADE_VOLUME")),
        "deal_amount": item.get("TRADING_VOLUMES") or item.get("TRADE_AMOUNT"),
        "volume_ratio": item.get("QRR"),
        "turnover_rate": _sf(item.get("TURNOVER_RATE")),
        "amplitude": _sf(item.get("AMPLITUDE")),
        "pe_dynamic": item.get("PE_DYNAMIC") or item.get("PE9"),
        "pb_mrq": item.get("PB_NEW_MRQ"),
        "total_market_cap": item.get("TOEAL_MARKET_VALUE") or item.get("TOTAL_MARKET_CAP"),
        "free_cap": item.get("FREE_CAP"),
    }

# ══════════════════════════════════════════════════════════════
#  核心选股工具
# ══════════════════════════════════════════════════════════════

@tool(
    description="统一选股工具：根据条件从全市场筛选股票。支持自然语言条件（如 'PE<20 半导体'、'净利增长>15%'）和结构化 filters。source='eastmoney' 使用东财智能选股（130+条件），'local_db' 查本地数据库，'auto' 自动选择。当用户要求选股、筛选股票时使用此工具。",
    category="选股",
    layer="决策层",
    domain=["finance"],
)
def search_stocks(
    query: str = "",
    source: str = "auto",
    filters: Optional[Dict[str, Any]] = None,
    market: str = "全部",
    top_n: int = 50,
) -> Dict[str, Any]:
    """统一选股工具：根据条件从全市场筛选股票。

    支持自然语言条件（如 "PE<20 半导体"）和结构化 filters 字典。
    source 参数控制数据源：auto(东财优先,本地DB兜底) / eastmoney / local_db。

    Args:
        query: 自然语言选股条件（如 "半导体 净利增长>15%"、"PE在5到20之间"）
        source: 数据源 — auto(自动选择) / eastmoney(东财智能选股) / local_db(本地数据库)
        filters: 结构化筛选条件字典（可选，与 query 互补）
        market: 市场筛选（全部/A股/科创板/创业板/港股/美股/ETF基金）
        top_n: 返回数量上限，默认50，最大200
    """
    top_n = min(max(top_n, 1), 200)

    # 如果有 filters 但没 query，从 filters 生成 keyword
    if filters and not query:
        query = build_keyword_from_filters(filters)
        if market == "全部" and filters.get("_market"):
            market = filters["_market"]

    if not query or not query.strip():
        return {"error": "选股条件不能为空（传入 query 或 filters）", "retriable": False}

    search_keyword = query.strip()
    if market and market != "全部" and market in MARKET_FILTER_MAP:
        search_keyword = f"{market} {search_keyword}"

    # ── eastmoney / auto 模式 ──
    if source in ("eastmoney", "auto"):
        raw = _call_eastmoney_api(search_keyword, page_size=top_n)
        if str(raw.get("code")) == "100":
            data = raw.get("data", {})
            result = data.get("result", {})
            stocks_raw = result.get("dataList", [])
            total = result.get("total", len(stocks_raw))
            stocks = [_parse_stock_item(s) for s in stocks_raw]
            return {
                "source": "eastmoney",
                "keyword": query,
                "market": market,
                "total": total,
                "count": len(stocks),
                "stocks": stocks,
            }
        elif source == "eastmoney":
            return {"error": raw.get("msg", "东财选股搜索失败"), "retriable": True}
        # auto 模式下东财失败，继续 fallback

    # ── local_db 模式 / auto fallback ──
    if source in ("local_db", "auto"):
        return _search_local_db(query, market, top_n)

    return {"error": f"未知数据源: {source}", "retriable": False}

def _search_local_db(keyword: str, market: str = "CNStock", limit: int = 50) -> Dict[str, Any]:
    """本地 DB 选股（cnstock_selection 表）。"""
    from app.utils.db import get_db_connection

    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT MAX(date) AS d FROM cnstock_selection")
            row = cur.fetchone() or {}
            target_date = str(row.get("d") or "")
            if not target_date:
                return {"source": "local_db", "stocks": [], "count": 0, "message": "选股数据为空"}

            cur.execute(
                "SELECT * FROM cnstock_selection WHERE date = %s ORDER BY id DESC LIMIT %s",
                (target_date, limit),
            )
            rows = cur.fetchall() or []
            cur.close()

        stocks = []
        for r in rows:
            d = dict(r)
            for k in ("change_rate", "turnover_rate", "volume_ratio", "new_price"):
                if d.get(k) is not None:
                    try:
                        d[k] = float(d[k])
                    except (ValueError, TypeError):
                        pass
            stock = {}
            for k in ("code", "name", "industry", "concept", "change_rate",
                       "turnover_rate", "volume_ratio", "new_price", "market"):
                if k in d:
                    stock[k] = d[k]
            stocks.append(stock)

        return {"source": "local_db", "date": target_date, "count": len(stocks), "stocks": stocks}
    except Exception as e:
        logger.error("_search_local_db failed: %s", e, exc_info=True)
        return {"source": "local_db", "stocks": [], "count": 0, "error": str(e)}

# ══════════════════════════════════════════════════════════════
#  工具声明
# ══════════════════════════════════════════════════════════════

# Legacy list — kept for backward compat during migration; safe to remove later.