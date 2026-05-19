"""外围市场卡片 — 国际情绪指标 + 大宗商品"""
from ._base import CardMeta, register

meta = CardMeta(
    id="peripheral",
    name="外围市场",
    endpoint="/peripheral",
    refresh_interval=1800,
    order=50,
    requires_hub=False,
)


def fetch():
    from app.data_providers.global_market import get_sentiment
    try:
        return get_sentiment() or {}
    except Exception:
        return {}


register(meta, fetch)
