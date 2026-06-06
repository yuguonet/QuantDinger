"""龙虎榜卡片 — 数据来源: dragon_limit (HTTP 东财搜索 + AkShare 兜底)"""
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


def fetch():
    from app.market_cn.dragon_limit import get_dragon_tiger

    raw = get_dragon_tiger()
    if not raw:
        return _empty()

    from app.data_sources.normalizer import safe_float

    result = []
    for s in raw:
        ch = safe_float(s.get("change_rate") or s.get("change_percent"), 0)
        price = safe_float(s.get("new_price") or s.get("close_price") or s.get("price"), 0)
        result.append({
            "code": s.get("code") or s.get("stock_code", ""),
            "name": s.get("name") or s.get("stock_name", ""),
            "industry": s.get("industry") or s.get("sector", ""),
            "change": f"{'+' if ch >= 0 else ''}{ch:.2f}%",
            "new_price": f"{price:.2f}",
            "turnoverrate": f"{safe_float(s.get('turnoverrate') or s.get('turnover_rate'), 0):.2f}",
            "deal_amount": s.get("deal_amount") or s.get("amount") or "",
            "total_market_cap": s.get("total_market_cap") or "",
        })

    return {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "dragonTigerList": result}


def _empty():
    return {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "dragonTigerList": []}


register(meta, fetch)
