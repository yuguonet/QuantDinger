"""情绪周期卡片 — 情绪指数历史折线图数据"""
from datetime import datetime
from ._base import CardMeta, register

meta = CardMeta(
    id="emotion_cycle",
    name="情绪周期",
    endpoint="/emotion-cycle",
    refresh_interval=300,
    order=30,
    requires_hub=False,
)


def fetch():
    try:
        from app.market_cn.emotion import get_emotion_history
        result = get_emotion_history()
        return {"history": result.get("history", [])}
    except Exception:
        return {"history": []}


register(meta, fetch)
