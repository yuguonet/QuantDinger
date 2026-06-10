"""国内市场数据卡片 — 贪婪恐惧指数 + AI政策解读 + 板块历史趋势"""
from ._base import CardMeta, register

meta = CardMeta(
    id="macro",
    name="国内宏观",
    endpoint="/macro",
    refresh_interval=1800,
    order=40,
    requires_hub=False,
)


def fetch():
    from app.market_cn.china_market import get_fear_greed, get_policy
    from app.market_cn.sector_history import get_sector_history as _get_sector_history

    fear_greed = {}
    policy = {}
    sector_history = {}

    try:
        fear_greed = get_fear_greed() or {}
    except Exception:
        pass
    try:
        policy = get_policy() or {}
    except Exception:
        pass
    try:
        sector_history = _get_sector_history(board_type="industry", days=30) or {}
    except Exception:
        pass

    return {
        "fearGreed": fear_greed,
        "policy": policy,
        "sectorHistory": sector_history,
    }


register(meta, fetch)
