"""连板天梯卡片 — 数据来源: 东财智能选股搜索"""
from datetime import datetime
from ._base import CardMeta, register

meta = CardMeta(
    id="streak",
    name="连板天梯",
    endpoint="/streak",
    refresh_interval=60,
    order=60,
    requires_hub=False,
)

# 东财搜索关键词
_KEYWORD = "连续涨停 连板"


def fetch():
    from app.market_cn.eastmoney_search import search_stocks
    from app.data_sources.normalizer import safe_float

    raw = search_stocks(keyword=_KEYWORD, page_size=100)
    if raw.get("code") != 1:
        return _empty()

    stocks = []
    for s in raw.get("stocks", []):
        ch = safe_float(s.get("change_rate"), 0)
        price = safe_float(s.get("new_price"), 0)
        stocks.append({
            "code": s.get("code", ""),
            "name": s.get("name", ""),
            "price": f"{price:.2f}",
            "change": f"{'+' if ch >= 0 else ''}{ch:.2f}%",
            "change_raw": ch,
            "industry": s.get("industry", ""),
            "concept": s.get("concept", ""),
            "turnoverrate": f"{safe_float(s.get('turnoverrate'), 0):.2f}",
            "deal_amount": s.get("deal_amount") or "",
            "total_market_cap": s.get("total_market_cap") or "",
        })

    stocks.sort(key=lambda x: x.get("change_raw", 0), reverse=True)
    for s in stocks:
        s.pop("change_raw", None)
    height = len(stocks)

    return {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "streakStocks": stocks,
        "streakHeight": height,
        "yesterdayStreakStocks": [],
        "yesterdayStreakHeight": 0,
    }


def _empty():
    return {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "streakStocks": [],
        "streakHeight": 0,
        "yesterdayStreakStocks": [],
        "yesterdayStreakHeight": 0,
    }


register(meta, fetch)
