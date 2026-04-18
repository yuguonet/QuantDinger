"""Persist strategy runtime lines for the strategy management UI (`qd_strategy_logs`)."""

from __future__ import annotations

from app.utils.db import get_db_connection
from app.utils.logger import get_logger

logger = get_logger(__name__)


def append_strategy_log(strategy_id: int, level: str, message: str) -> None:
    """Best-effort insert; never raises to caller."""
    try:
        sid = int(strategy_id)
        lv = (level or "info").strip().lower()[:20]
        msg = str(message or "").strip()
        if not msg:
            return
        msg = msg[:8000]
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                "INSERT INTO qd_strategy_logs (strategy_id, level, message) VALUES (?, ?, ?)",
                (sid, lv, msg),
            )
            db.commit()
            cur.close()
    except Exception as e:
        logger.debug("append_strategy_log skip: %s", e)
