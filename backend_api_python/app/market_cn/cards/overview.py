"""市场总览卡片 — 顶部 8 小格（指数/涨跌停/北向/情绪等）"""
from datetime import datetime
from ._base import CardMeta, register

meta = CardMeta(
    id="overview",
    name="市场总览",
    endpoint="/overview",
    refresh_interval=60,
    order=10,
    requires_hub=True,
)


def fetch():
    from app.data_sources.normalizer import safe_float, safe_int
    from app.market_cn.index import get_index_realtime

    # 指数
    sse = sse_c = szse = szse_c = cyse = cyse_c = bzse = bzse_c = 0.0
    try:
        indices = get_index_realtime(["000001", "399001", "399006", "899050"])
        code_map = {item["code"]: item for item in (indices or [])}
        idx_conf = {
            "000001": ("sse", "sse_c"), "399001": ("szse", "szse_c"),
            "399006": ("cyse", "cyse_c"), "899050": ("bzse", "bzse_c"),
        }
        for code, item in code_map.items():
            if code in idx_conf:
                price_key, pct_key = idx_conf[code]
                locals()[price_key] = safe_float(item.get("price"))
                locals()[pct_key] = safe_float(item.get("change_percent"))
    except Exception:
        pass

    # 涨停池
    limit_up = streak_height = 0
    try:
        from app.market_cn.dragon_limit import get_limit_up_pool
        zt = get_limit_up_pool()
        if zt:
            limit_up = len(zt)
            streak_height = max((safe_int(i.get("continuous_zt_days", 1)) for i in zt), default=0)
    except Exception:
        pass

    # 跌停 / 炸板
    limit_down = broken_board = 0
    try:
        from app.market_cn.dragon_limit import get_limit_down_count
        limit_down = get_limit_down_count()
    except Exception:
        pass
    try:
        from app.market_cn.dragon_limit import get_broken_board_count
        broken_board = get_broken_board_count()
    except Exception:
        pass

    # 市场快照
    north_net = emotion = 0.0
    up_count = down_count = 0
    try:
        from app.market_cn.index import get_northbound_realtime
        nb = get_northbound_realtime()
        north_net = safe_float(nb.get("total_latest_yi", 0))
    except Exception:
        pass
    try:
        from app.market_cn.fear_greed_index import fear_greed_index
        fg = fear_greed_index()
        emotion = safe_int(fg.get("composite_score", 50))
    except Exception:
        pass

    if -0.3 <= sse_c <= 0.3:
        heat = "平淡"
    elif sse_c > 0.8:
        heat = "火热"
    elif sse_c < -0.8:
        heat = "寒冷"
    else:
        heat = "温热" if sse_c > 0 else "偏冷"

    return {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sse": {"index": f"{sse:.2f}", "change": sse_c, "code": "000001"},
        "szse": {"index": f"{szse:.2f}", "change": szse_c, "code": "399001"},
        "cyse": {"index": f"{cyse:.2f}", "change": cyse_c, "code": "399006"},
        "bzse": {"index": f"{bzse:.2f}", "change": bzse_c, "code": "899050"},
        "heat": heat,
        "upCount": up_count, "downCount": down_count,
        "limitUp": limit_up, "limitDown": limit_down,
        "streakHeight": streak_height, "brokenBoard": broken_board,
        "northBound": north_net, "emotionIndex": emotion,
    }


def _empty():
    return {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sse": {"index": "0.00", "change": 0, "code": "000001"},
        "szse": {"index": "0.00", "change": 0, "code": "399001"},
        "cyse": {"index": "0.00", "change": 0, "code": "399006"},
        "bzse": {"index": "0.00", "change": 0, "code": "899050"},
        "heat": "—", "upCount": 0, "downCount": 0,
        "limitUp": 0, "limitDown": 0, "streakHeight": 0,
        "brokenBoard": 0, "northBound": 0, "emotionIndex": 50,
    }


register(meta, fetch)
