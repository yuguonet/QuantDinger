"""国内宏观数据卡片 — GDP/CPI/PPI/PMI/M2 + 贪婪恐惧 + 政策解读"""
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
    from app.market_cn.china_market import get_china_macro, get_fear_greed, get_policy
    from app.market_cn.sector_history import get_sector_history as _get_sector_history

    macro = {}
    fear_greed = {}
    policy = {}
    sector_history = {}

    try:
        macro = get_china_macro() or {}
    except Exception:
        pass
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
        "macro": macro,
        "fearGreed": fear_greed,
        "policy": policy,
        "sectorHistory": sector_history,
    }


register(meta, fetch)
