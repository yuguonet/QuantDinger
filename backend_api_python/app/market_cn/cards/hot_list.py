"""同花顺热榜卡片 — 数据来源: 东财智能选股搜索"""
from datetime import datetime
from ._base import CardMeta, register

meta = CardMeta(
    id="hot_list",
    name="同花顺热榜",
    endpoint="/hot-list",
    refresh_interval=120,
    order=80,
    requires_hub=False,
)

# 东财搜索关键词
_KEYWORD = "热门股票"


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
            "hot": "",
            "change": f"{'+' if ch >= 0 else ''}{ch:.2f}%",
            "price": f"{price:.2f}",
            "current_rank_change": "",
        })

    return {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "hotList": result}


def _empty():
    return {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "hotList": []}


register(meta, fetch)
