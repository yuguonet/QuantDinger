"""强势股卡片"""
from datetime import datetime
from ._base import CardMeta, register

meta = CardMeta(
    id="strong_stocks",
    name="强势股",
    endpoint="/strong-stocks",
    refresh_interval=60,
    order=90,
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
        zt = hub.zt_pool.get_realtime()
        if zt:
            filtered = [
                item for item in zt
                if not any(tag in str(item.get("stock_name", ""))
                           for tag in ("ST", "st", "退", "*"))
            ]
            sorted_zt = sorted(
                filtered or zt,
                key=lambda x: (safe_int(x.get("continuous_zt_days", 1)),
                               safe_float(x.get("change_percent", 0))),
                reverse=True,
            )
            for i, item in enumerate(sorted_zt[:50], 1):
                ch = safe_float(item.get("change_percent", 0))
                price = safe_float(item.get("price", 0))
                result.append({
                    "rank": i,
                    "code": str(item.get("stock_code", "")),
                    "name": str(item.get("stock_name", "")),
                    "price": f"{price:.2f}",
                    "gain": f"{'+' if ch >= 0 else ''}{ch:.2f}%",
                    "days": safe_int(item.get("continuous_zt_days", 1)),
                    "sector": str(item.get("sector", "")),
                    "reason": str(item.get("reason", "")),
                    "volume": safe_float(item.get("volume", 0)),
                    "amount": safe_float(item.get("amount", 0)),
                    "turnover_rate": safe_float(item.get("turnover_rate", 0)),
                    "seal_amount": safe_float(item.get("seal_amount", 0)),
                    "zt_time": str(item.get("zt_time", "")),
                    "open_count": safe_int(item.get("open_count", 0)),
                })
    except Exception:
        pass

    return {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "strongStocks": result}


def _empty():
    return {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "strongStocks": []}


register(meta, fetch)
