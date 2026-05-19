"""强势股卡片 — 数据来源: 东财智能选股搜索"""
from datetime import datetime
from ._base import CardMeta, register

meta = CardMeta(
    id="strong_stocks",
    name="强势股",
    endpoint="/strong-stocks",
    refresh_interval=60,
    order=90,
    requires_hub=False,
)

# 东财搜索关键词
_KEYWORD = "强势股 涨停"


def fetch():
    from app.market_cn.eastmoney_search import search_stocks
    from app.data_sources.normalizer import safe_float

    raw = search_stocks(keyword=_KEYWORD, page_size=50)
    if raw.get("code") != 1:
        return _empty()

    result = []
    for i, s in enumerate(raw.get("stocks", [])[:50], 1):
        ch = safe_float(s.get("change_rate"), 0)
        price = safe_float(s.get("new_price"), 0)
        result.append({
            "rank": i,
            "code": s.get("code", ""),
            "name": s.get("name", ""),
            "price": f"{price:.2f}",
            "gain": f"{'+' if ch >= 0 else ''}{ch:.2f}%",
            "industry": s.get("industry", ""),
            "concept": s.get("concept", ""),
            "turnoverrate": f"{safe_float(s.get('turnoverrate'), 0):.2f}",
            "deal_amount": s.get("deal_amount") or "",
            "total_market_cap": s.get("total_market_cap") or "",
        })

    return {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "strongStocks": result}


def _empty():
    return {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "strongStocks": []}


register(meta, fetch)
