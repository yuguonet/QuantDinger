"""Financial news and economic calendar data providers."""
from __future__ import annotations

import random
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple

from app.utils.logger import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Symbol / Market detection
# ═══════════════════════════════════════════════════════════════════════════════

_CRYPTO_NAMES = {
    "BTC", "ETH", "USDT", "BNB", "SOL", "XRP", "DOGE", "ADA", "AVAX", "DOT",
    "MATIC", "LINK", "UNI", "SHIB", "LTC", "BCH", "ATOM", "FIL", "APT", "ARB",
    "OP", "NEAR", "FTM", "AAVE", "MKR", "GRT", "IMX", "SAND", "MANA", "AXS",
    "PEPE", "WIF", "BONK", "FET", "RNDR", "INJ", "TIA", "SEI", "SUI", "JUP",
}

_FOREX_QUOTES = {"USD", "EUR", "JPY", "GBP", "CHF", "AUD", "NZD", "CAD", "CNY", "HKD"}

# Common non-stock abbreviations to avoid mis-classifying as USStock
_NON_STOCK_ABBR = {
    "GDP", "CPI", "PPI", "PMI", "ETF", "IPO", "PE", "PB", "ROE", "EPS",
    "GTC", "IOC", "FOK", "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD",
}


def _detect_market(symbol: str, name: str = "") -> str:
    """Auto-detect market type from symbol format and optional name.

    Rules (priority order):
    1. Contains '/' → Crypto (BTC/USDT) or Forex (EUR/USD)
    2. 6-digit number → CNStock (600519)
    3. 5-digit number starting with '0' → HKStock (00700)
    4. 4-digit number → HKStock (0700, 9988)
    5. 1-5 uppercase letters → USStock (AAPL), excluding common abbreviations
    6. Name-based hints
    7. Fallback to 'unknown'
    """
    sym = symbol.strip().upper()
    name_up = name.strip().upper()

    if not sym:
        return "unknown"

    # --- 1. Slash-separated pair: Crypto vs Forex ---
    if "/" in sym:
        base, quote = sym.split("/", 1)
        if base in _CRYPTO_NAMES or quote in _CRYPTO_NAMES or quote == "USDT":
            return "Crypto"
        if base in _FOREX_QUOTES and quote in _FOREX_QUOTES:
            return "Forex"
        return "Crypto"

    # --- 2. Pure digits ---
    if sym.isdigit():
        length = len(sym)
        if length == 6 and sym.startswith(("60", "00", "30", "68")):
            return "CNStock"
        # 北交所 8xxxxx / 4xxxxx (5位)
        if length == 5 and sym.startswith(("8", "4")):
            return "CNStock"
        if length in (4, 5) and sym.startswith("0"):
            return "HKStock"
        if length == 4:
            return "HKStock"

    # --- 3. Pure letters (1-5 uppercase) → USStock ---
    if re.match(r"^[A-Z]{1,5}$", sym) and sym not in _NON_STOCK_ABBR:
        return "USStock"

    # --- 4. Name-based hints ---
    if any(kw in name_up for kw in ("比特币", "BTC", "以太坊", "ETH", "加密")):
        return "Crypto"
    if any(kw in name_up for kw in ("美股", "纳斯达克", "标普", "AAPL", "TSLA")):
        return "USStock"
    if any(kw in name_up for kw in ("A股", "沪深", "上证", "深证")):
        return "CNStock"
    if any(kw in name_up for kw in ("港股", "恒生")):
        return "HKStock"

    return "unknown"


def _build_stock_queries(symbol: str, name: str, market: str) -> Tuple[List[str], List[str]]:
    """Build (cn_queries, en_queries) for individual stock news search."""
    sym = symbol.strip()
    # 避免 name 为空时出现前导空格
    display = f"{name}({sym})" if name else sym
    name_part = name if name else ""

    if market == "Crypto":
        base = sym.split("/")[0] if "/" in sym else sym
        cn = [f"{base} 最新消息", f"{base} 价格分析"]
        en = [f"{base} crypto news", f"{base} price analysis today"]
        if name_part:
            cn.append(f"{name_part} 新闻")
            en.append(f"{name_part} latest news")
        else:
            cn.append(f"{base} 行情")
            en.append(f"{base} latest news")
        return cn, en

    elif market == "USStock":
        cn = [f"{display} 最新消息", f"{display} 财报分析", f"{sym} 美股新闻"]
        en_parts = [f"{sym} stock news", f"{sym} earnings analysis", f"{sym} latest news today"]
        if name_part:
            en_parts = [f"{name_part} {sym} stock news", f"{sym} earnings analysis", f"{sym} latest news today"]
        return cn, en_parts

    elif market == "CNStock":
        cn = [f"{display} 最新消息", f"{display} 公告", f"{sym} 股票新闻"]
        en = [f"{sym} China stock news", f"{sym} A-share news"]
        if name_part:
            en.insert(0, f"{name_part} {sym} China stock news")
        return cn, en

    elif market == "HKStock":
        cn = [f"{display} 最新消息", f"{display} 港股新闻", f"{sym} 公告"]
        en = [f"{sym} Hong Kong stock news", f"{sym} HKEX news"]
        if name_part:
            en.insert(0, f"{name_part} {sym} Hong Kong stock news")
        return cn, en

    elif market == "Forex":
        cn = [f"{display} 汇率走势", f"{sym} 外汇分析"]
        en = [f"{sym} forex news", f"{sym} exchange rate analysis"]
        return cn, en

    elif market == "Futures":
        cn = [f"{display} 期货行情", f"{sym} 期货分析"]
        en = [f"{sym} futures news", f"{sym} commodity analysis"]
        return cn, en

    else:
        cn = [f"{display} 最新消息", f"{sym} 新闻"]
        en = [f"{sym} latest news", f"{sym} news today"]
        return cn, en


# ═══════════════════════════════════════════════════════════════════════════════
# Market / category queries (行情 vs 政策 严格分离)
# ═══════════════════════════════════════════════════════════════════════════════

# 行情类：只聚焦价格 / 走势 / 资金面 / 技术分析
_MARKET_QUERIES: Dict[str, Dict[str, List[str]]] = {
    "Crypto": {
        "cn": ["比特币行情走势", "以太坊价格分析", "加密货币资金流向", "数字货币技术分析"],
        "en": ["bitcoin price analysis", "ethereum market trend",
               "crypto fund flow", "cryptocurrency technical analysis"],
    },
    "USStock": {
        "cn": ["美股行情走势", "纳斯达克标普最新行情", "美股资金流向", "美股技术分析"],
        "en": ["US stock market today", "S&P 500 NASDAQ latest",
               "US stock fund flow", "US stock technical analysis"],
    },
    "CNStock": {
        "cn": ["A股行情走势", "沪深指数最新行情", "A股资金流向北向资金", "A股技术分析"],
        "en": ["China A-share market today", "CSI 300 Shanghai index",
               "China stock fund flow", "A-share technical analysis"],
    },
    "HKStock": {
        "cn": ["港股行情走势", "恒生指数最新行情", "港股资金流向南向资金", "港股技术分析"],
        "en": ["Hong Kong stock market today", "Hang Seng index latest",
               "HK stock fund flow", "Hong Kong stock technical analysis"],
    },
    "Forex": {
        "cn": ["外汇行情走势", "美元指数最新", "人民币汇率走势", "欧元日元行情分析"],
        "en": ["forex market today", "US dollar index latest",
               "EUR USD exchange rate", "Japanese yen forex analysis"],
    },
    "Futures": {
        "cn": ["期货行情走势", "原油期货最新价格", "黄金期货行情", "大宗商品价格走势"],
        "en": ["futures market today", "crude oil futures price",
               "gold futures latest", "commodity price trend"],
    },
}

# 政策类：中国 / 国际严格分开
_POLICY_QUERIES: Dict[str, Dict[str, List[str]]] = {
    "MacroCN": {
        "cn": [
            "中国人民银行货币政策",
            "国务院经济政策",
            "中国CPI PPI经济数据",
            "中国GDP经济增长数据",
            "中国财政政策减税降费",
            "中国房地产调控政策",
            "中国就业数据",
            "中国贸易进出口数据",
            "中国产业政策新能源芯片",
        ],
        "en": [
            "People's Bank of China monetary policy",
            "China State Council economic policy",
            "China CPI PPI economic data",
            "China GDP growth data",
            "China fiscal policy tax",
            "China housing market regulation",
            "China employment data",
            "China trade import export data",
            "China industrial policy",
        ],
    },
    "MacroIntl": {
        "cn": [
            "美联储利率决议",
            "欧洲央行货币政策",
            "日本央行利率决议",
            "美国CPI通胀数据",
            "美国非农就业数据",
            "美国GDP经济数据",
            "国际贸易关税政策",
            "OPEC原油产量政策",
            "英国央行利率决议",
        ],
        "en": [
            "Federal Reserve interest rate decision",
            "European Central Bank monetary policy",
            "Bank of Japan interest rate decision",
            "US CPI inflation data",
            "US non-farm payrolls employment",
            "US GDP economic data",
            "international trade tariff policy",
            "OPEC oil production policy",
            "Bank of England interest rate decision",
        ],
    },
}

_POLICY_KEYS = set(_POLICY_QUERIES.keys())


def _search_queries(
    search: Any,
    queries: List[str],
    lang_key: str,
    num_results: int = 5,
    search_days: int = 1,
) -> List[Dict[str, Any]]:
    """Execute a list of search queries and collect results.

    Centralises the try/except + result-mapping pattern so
    ``fetch_financial_news`` stays readable.
    """
    items: List[Dict[str, Any]] = []
    for query in queries:
        try:
            # search.search() 里 days 参数才实际控制时间范围；
            # date_restrict 在 days 有默认值时会被跳过，所以传 days 即可。
            results = search.search(query, num_results=num_results, days=search_days)
            for r in results:
                items.append({
                    "title": r.get("title", ""),
                    "link": r.get("link", ""),
                    "snippet": r.get("snippet", ""),
                    "source": r.get("source", ""),
                    "published": r.get("published", ""),
                    "category": query,
                    "lang": lang_key,
                })
        except Exception:
            pass
    return items


# ═══════════════════════════════════════════════════════════════════════════════
# Main entry point
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_financial_news(
    lang: str = "all",
    market: str = "all",
    symbol: str = "",
    name: str = "",
) -> Dict[str, List[Dict[str, Any]]]:
    """Fetch financial news using search service.

    Supports three modes:
    1. **Individual stock** — pass ``symbol`` (and optionally ``name``); market
       is auto-detected if not given.
    2. **Market / category** — pass ``market`` without ``symbol``; returns
       aggregated market or policy news.
    3. **All** — ``market='all'`` and no ``symbol``; returns everything.

    Args:
        lang:   'cn' | 'en' | 'all'
        market: 行情类 'Crypto'|'USStock'|'CNStock'|'HKStock'|'Forex'|'Futures'
                政策类 'MacroCN'|'MacroIntl'
                'all' = 全部
        symbol: 股票代码/交易对, e.g. 'AAPL', '600519', 'BTC/USDT'
        name:   股票/币种名称, e.g. '苹果', '贵州茅台', 'Bitcoin'

    Returns:
        Dict with 'cn' / 'en' lists.  Each item carries:
        - market, group ('market_news' | 'policy_news' | 'stock_news'), category
    """
    result: Dict[str, List[Dict[str, Any]]] = {"cn": [], "en": []}

    try:
        from app.services.search import SearchService
        search = SearchService()

        # 搜索时间窗口：行情用 1 天，政策用 3 天（政策传播慢）
        MARKET_DAYS = 1
        POLICY_DAYS = 3
        STOCK_DAYS = 2

        # ── Mode 1: Individual stock search ─────────────────────────────
        if symbol:
            detected_market = market if market != "all" else _detect_market(symbol, name)
            cn_q, en_q = _build_stock_queries(symbol, name, detected_market)

            if lang in ("all", "cn"):
                for item in _search_queries(search, cn_q, "cn", search_days=STOCK_DAYS):
                    item.update({
                        "market": detected_market, "group": "stock_news",
                        "symbol": symbol, "stock_name": name,
                    })
                    result["cn"].append(item)

            if lang in ("all", "en"):
                for item in _search_queries(search, en_q, "en", search_days=STOCK_DAYS):
                    item.update({
                        "market": detected_market, "group": "stock_news",
                        "symbol": symbol, "stock_name": name,
                    })
                    result["en"].append(item)

        # ── Mode 2 & 3: Market / category / all ─────────────────────────
        else:
            if market == "all":
                queries: Dict[str, Dict[str, List[str]]] = {}
                queries.update(_MARKET_QUERIES)
                queries.update(_POLICY_QUERIES)
            elif market in _MARKET_QUERIES:
                queries = {market: _MARKET_QUERIES[market]}
            elif market in _POLICY_QUERIES:
                queries = {market: _POLICY_QUERIES[market]}
            else:
                # 未知 market 值 → 返回全部（优雅降级）
                queries = {}
                queries.update(_MARKET_QUERIES)
                queries.update(_POLICY_QUERIES)

            for mkt, lang_queries in queries.items():
                is_policy = mkt in _POLICY_KEYS
                group = "policy_news" if is_policy else "market_news"
                days = POLICY_DAYS if is_policy else MARKET_DAYS

                if lang in ("all", "cn"):
                    for item in _search_queries(search, lang_queries.get("cn", []), "cn", search_days=days):
                        item.update({"market": mkt, "group": group})
                        result["cn"].append(item)

                if lang in ("all", "en"):
                    for item in _search_queries(search, lang_queries.get("en", []), "en", search_days=days):
                        item.update({"market": mkt, "group": group})
                        result["en"].append(item)

        # ── Dedup & truncate ─────────────────────────────────────────────
        for lang_key in ["cn", "en"]:
            seen: set = set()
            unique = []
            for news in result[lang_key]:
                link = news.get("link", "")
                if link and link not in seen:
                    seen.add(link)
                    unique.append(news)
            result[lang_key] = unique[:20]

    except Exception as e:
        logger.error("Failed to fetch financial news: %s", e)

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# Economic calendar (template-based, no real API yet)
# ═══════════════════════════════════════════════════════════════════════════════

_SAMPLE_EVENTS = [
    {"name": "美国非农就业数据", "name_en": "US Non-Farm Payrolls", "country": "US", "importance": "high", "forecast": "180K", "previous": "175K", "impact_if_above": "bullish", "impact_if_below": "bearish", "impact_desc": "高于预期利多美元/美股，低于预期利空", "impact_desc_en": "Above forecast: bullish USD/stocks; Below: bearish"},
    {"name": "美联储利率决议", "name_en": "Fed Interest Rate Decision", "country": "US", "importance": "high", "forecast": "5.25%", "previous": "5.25%", "impact_if_above": "bearish", "impact_if_below": "bullish", "impact_desc": "加息利空股市/加密货币，降息利多", "impact_desc_en": "Rate hike: bearish stocks/crypto; Cut: bullish"},
    {"name": "美国CPI月率", "name_en": "US CPI m/m", "country": "US", "importance": "high", "forecast": "0.3%", "previous": "0.4%", "impact_if_above": "bearish", "impact_if_below": "bullish", "impact_desc": "CPI高于预期增加加息预期，利空股市", "impact_desc_en": "Higher CPI increases rate hike expectations, bearish stocks"},
    {"name": "欧洲央行利率决议", "name_en": "ECB Interest Rate Decision", "country": "EU", "importance": "high", "forecast": "4.50%", "previous": "4.50%", "impact_if_above": "bearish", "impact_if_below": "bullish", "impact_desc": "加息利空欧股，利多欧元", "impact_desc_en": "Rate hike: bearish EU stocks, bullish EUR"},
    {"name": "日本央行利率决议", "name_en": "BoJ Interest Rate Decision", "country": "JP", "importance": "high", "forecast": "0.10%", "previous": "0.10%", "impact_if_above": "bullish", "impact_if_below": "bearish", "impact_desc": "加息预期利多日元，利空日股", "impact_desc_en": "Rate hike expectation: bullish JPY, bearish Nikkei"},
    {"name": "美国初请失业金人数", "name_en": "US Initial Jobless Claims", "country": "US", "importance": "medium", "forecast": "215K", "previous": "212K", "impact_if_above": "bearish", "impact_if_below": "bullish", "impact_desc": "失业人数上升利空美元，利多黄金", "impact_desc_en": "Rising claims: bearish USD, bullish gold"},
    {"name": "英国央行利率决议", "name_en": "BoE Interest Rate Decision", "country": "UK", "importance": "high", "forecast": "5.25%", "previous": "5.25%", "impact_if_above": "bullish", "impact_if_below": "bearish", "impact_desc": "加息利多英镑，利空英股", "impact_desc_en": "Rate hike: bullish GBP, bearish UK stocks"},
    {"name": "美国零售销售月率", "name_en": "US Retail Sales m/m", "country": "US", "importance": "medium", "forecast": "0.4%", "previous": "0.6%", "impact_if_above": "bullish", "impact_if_below": "bearish", "impact_desc": "零售数据强劲利多美元和美股", "impact_desc_en": "Strong retail: bullish USD and stocks"},
    {"name": "OPEC月度报告", "name_en": "OPEC Monthly Report", "country": "INTL", "importance": "medium", "forecast": "-", "previous": "-", "impact_if_above": "bullish", "impact_if_below": "bearish", "impact_desc": "减产预期利多原油，增产预期利空", "impact_desc_en": "Production cut: bullish oil; Increase: bearish"},
]


def get_economic_calendar() -> List[Dict[str, Any]]:
    """Generate economic calendar events with impact indicators."""
    today = datetime.now()
    events = []

    for i, evt in enumerate(_SAMPLE_EVENTS):
        days_offset = i % 14 - 5
        event_date = today + timedelta(days=days_offset)
        hour = (8 + (i * 3)) % 24

        is_released = event_date.date() < today.date() or (
            event_date.date() == today.date() and hour < today.hour
        )

        actual_value = None
        actual_impact = None
        expected_impact = evt["impact_if_above"]

        if is_released:
            forecast_num = "".join(filter(lambda x: x.isdigit() or x == ".", evt["forecast"]))
            if forecast_num:
                try:
                    base = float(forecast_num)
                    variation = random.uniform(-0.15, 0.15)
                    actual_num = base * (1 + variation)
                    if "K" in evt["forecast"]:
                        actual_value = f"{actual_num:.0f}K"
                    elif "%" in evt["forecast"]:
                        actual_value = f"{actual_num:.2f}%"
                    else:
                        actual_value = f"{actual_num:.2f}"
                    if actual_num > base:
                        actual_impact = evt["impact_if_above"]
                    elif actual_num < base:
                        actual_impact = evt["impact_if_below"]
                    else:
                        actual_impact = "neutral"
                except Exception:
                    actual_value = evt["forecast"]
                    actual_impact = "neutral"
            else:
                actual_value = evt["forecast"]
                actual_impact = "neutral"

        events.append({
            "id": i + 1,
            "name": evt["name"], "name_en": evt["name_en"],
            "country": evt["country"],
            "date": event_date.strftime("%Y-%m-%d"),
            "time": f"{hour:02d}:30",
            "importance": evt["importance"],
            "actual": actual_value, "forecast": evt["forecast"], "previous": evt["previous"],
            "impact_if_above": evt["impact_if_above"], "impact_if_below": evt["impact_if_below"],
            "impact_desc": evt["impact_desc"], "impact_desc_en": evt["impact_desc_en"],
            "expected_impact": expected_impact, "actual_impact": actual_impact,
            "is_released": is_released,
        })

    events.sort(key=lambda x: (x["date"], x["time"]))
    return events
