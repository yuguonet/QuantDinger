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
    from app.market_cn.china_market import get_fear_greed

    fear_greed = {}

    try:
        fear_greed = get_fear_greed() or {}
    except Exception:
        pass

    return {
        "fearGreed": fear_greed,
    }


register(meta, fetch)
