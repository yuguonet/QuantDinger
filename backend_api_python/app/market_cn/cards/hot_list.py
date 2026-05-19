"""同花顺热榜卡片"""
from datetime import datetime
from ._base import CardMeta, register

meta = CardMeta(
    id="hot_list",
    name="同花顺热榜",
    endpoint="/hot-list",
    refresh_interval=120,
    order=80,
    requires_hub=True,
)


def fetch():
    from app.market_cn.cards._hub_helper import get_hub
    hub = get_hub()
    if hub is None:
        return _empty()

    from app.data_sources.normalizer import safe_float, safe_int
    result = []
    try:
        hot_list = hub.hot_rank.get_realtime()
        if hot_list:
            for item in hot_list[:50]:
                ch = safe_float(item.get("change_percent", 0))
                price = safe_float(item.get("price", 0))
                result.append({
                    "rank": safe_int(item.get("rank", 0)),
                    "code": str(item.get("stock_code", "")),
                    "name": str(item.get("stock_name", "")),
                    "hot": f"{safe_float(item.get('popularity_score', 0)):.0f}",
                    "change": f"{'+' if ch >= 0 else ''}{ch:.2f}%",
                    "price": f"{price:.2f}",
                    "current_rank_change": str(item.get("current_rank_change", "")),
                })
    except Exception:
        pass

    return {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "hotList": result}


def _empty():
    return {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "hotList": []}


register(meta, fetch)
