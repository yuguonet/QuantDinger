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
        from app.interfaces.cache_file import cache_db
        from app.interfaces.emotion_scheduler import query_emotion_history
        db = cache_db()
        history = query_emotion_history(db, hours=4)
        return {"history": history or []}
    except Exception:
        return {"history": []}


register(meta, fetch)
