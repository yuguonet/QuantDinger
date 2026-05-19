"""连板天梯卡片"""
from datetime import datetime
from ._base import CardMeta, register

meta = CardMeta(
    id="streak",
    name="连板天梯",
    endpoint="/streak",
    refresh_interval=60,
    order=60,
    requires_hub=True,
)


def _parse_streak(zt_list):
    from app.data_sources.normalizer import safe_float, safe_int
    stocks, height = [], 0
    if not zt_list:
        return stocks, height
    for item in zt_list:
        days = safe_int(item.get("continuous_zt_days", 1))
        if days < 2:
            continue
        price = safe_float(item.get("price", 0))
        ch = safe_float(item.get("change_percent", 0))
        stocks.append({
            "code": str(item.get("stock_code", "")),
            "name": str(item.get("stock_name", "")),
            "days": days,
            "price": f"{price:.2f}",
            "change": f"{'+' if ch >= 0 else ''}{ch:.2f}%",
            "sector": str(item.get("sector", "")),
            "reason": str(item.get("reason", "")),
            "seal_amount": safe_float(item.get("seal_amount", 0)),
            "turnover_rate": safe_float(item.get("turnover_rate", 0)),
            "zt_time": str(item.get("zt_time", "")),
            "open_count": safe_int(item.get("open_count", 0)),
            "volume": safe_float(item.get("volume", 0)),
            "amount": safe_float(item.get("amount", 0)),
        })
        height = max(height, days)
    stocks.sort(key=lambda x: x["days"], reverse=True)
    return stocks, height


def _get_previous_trading_day():
    from datetime import timedelta
    today = datetime.now()
    wd = today.weekday()
    days_back = {0: 3, 5: 1, 6: 2}.get(wd, 1)
    return (today - timedelta(days=days_back)).strftime("%Y-%m-%d")


def fetch():
    from app.market_cn.cards._hub_helper import get_hub
    hub = get_hub()
    if hub is None:
        return _empty()

    today_stocks, today_h = [], 0
    try:
        zt = hub.zt_pool.get_realtime()
        today_stocks, today_h = _parse_streak(zt)
    except Exception:
        pass

    yest_stocks, yest_h = [], 0
    try:
        prev = _get_previous_trading_day()
        yest_zt = hub.zt_pool.get_realtime(prev)
        yest_stocks, yest_h = _parse_streak(yest_zt)
    except Exception:
        pass

    return {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "streakStocks": today_stocks, "streakHeight": today_h,
        "yesterdayStreakStocks": yest_stocks, "yesterdayStreakHeight": yest_h,
    }


def _empty():
    return {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "streakStocks": [], "streakHeight": 0,
        "yesterdayStreakStocks": [], "yesterdayStreakHeight": 0,
    }


register(meta, fetch)
