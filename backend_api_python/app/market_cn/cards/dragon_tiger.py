"""龙虎榜卡片 — 数据来源: 东财智能选股搜索"""
from datetime import datetime
from ._base import CardMeta, register

meta = CardMeta(
    id="dragon_tiger",
    name="龙虎榜",
    endpoint="/dragon-tiger",
    refresh_interval=120,
    order=70,
    requires_hub=False,
)

# 东财搜索关键词
_KEYWORD = "龙虎榜"


def fetch():
    from app.market_cn.eastmoney_search import search_stocks
    from app.data_sources.normalizer import safe_float

    raw = search_stocks(keyword=_KEYWORD, page_size=100)
    if raw.get("code") != 1:
        return _empty()

    result = []
    for s in raw.get("stocks", []):
        ch = safe_float(s.get("change_rate"), 0)
        price = safe_float(s.get("new_price"), 0)
        result.append({
            "code": s.get("code", ""),
            "name": s.get("name", ""),
            "industry": s.get("industry", ""),
            "change": f"{'+' if ch >= 0 else ''}{ch:.2f}%",
            "new_price": f"{price:.2f}",
            "turnoverrate": f"{safe_float(s.get('turnoverrate'), 0):.2f}",
            "deal_amount": s.get("deal_amount") or "",
            "total_market_cap": s.get("total_market_cap") or "",
        })

    return {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "dragonTigerList": result}


def _empty():
    return {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "dragonTigerList": []}


register(meta, fetch)
