"""龙虎榜卡片"""
from datetime import datetime, timedelta
from ._base import CardMeta, register

meta = CardMeta(
    id="dragon_tiger",
    name="龙虎榜",
    endpoint="/dragon-tiger",
    refresh_interval=120,
    order=70,
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
        today = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
        lhb = hub.dragon_tiger.get_history(start, today)
        if lhb:
            for item in lhb:
                ch = safe_float(item.get("change_percent", 0))
                result.append({
                    "code": str(item.get("stock_code", "")),
                    "name": str(item.get("stock_name", "")),
                    "reason": str(item.get("reason", ""))[:50],
                    "change": f"{'+' if ch >= 0 else ''}{ch:.2f}%",
                    "trade_date": str(item.get("trade_date", "")),
                    "buy_amount": safe_float(item.get("buy_amount", 0)),
                    "sell_amount": safe_float(item.get("sell_amount", 0)),
                    "net_amount": safe_float(item.get("net_amount", 0)),
                    "buy_seat_count": safe_int(item.get("buy_seat_count", 0)),
                    "sell_seat_count": safe_int(item.get("sell_seat_count", 0)),
                })
    except Exception:
        pass

    return {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "dragonTigerList": result}


def _empty():
    return {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "dragonTigerList": []}


register(meta, fetch)
