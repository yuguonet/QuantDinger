"""情绪指数定时采集 + 存储 + 查询"""
'''部署时只需要在 cache_file.py 注册表 + __init__.py 加启动调用 + .env 开开关，就可以跑了'''
import os
import logging
import threading
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# ==================== 配置 ====================

ENABLED = os.getenv("EMOTION_COLLECTOR_ENABLED", "false").lower() == "true"
INTERVAL_SECONDS = int(os.getenv("EMOTION_COLLECTOR_INTERVAL", "60"))  # 默认 60 秒
RETENTION_DAYS = int(os.getenv("EMOTION_COLLECTOR_RETENTION_DAYS", "30"))  # 默认保留 30 天

TABLE = "cnd_emotion_history"


# ==================== 调度器 ====================

class EmotionScheduler:
    """后台情绪采集调度器（单线程 Timer，轻量无外部依赖）"""

    def __init__(self, hub, db):
        """
        Args:
            hub: AShareDataHub 实例
            db: cache_db 实例
        """
        self.hub = hub
        self.db = db
        self._timer = None
        self._running = False

    def start(self):
        if not ENABLED:
            logger.info("[EmotionScheduler] 未启用（EMOTION_COLLECTOR_ENABLED=false）")
            return
        # 避免 Flask reloader 双开
        debug = os.getenv("PYTHON_API_DEBUG", "false").lower() == "true"
        if debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
            return
        self._running = True
        logger.info(
            f"[EmotionScheduler] 已启动，间隔 {INTERVAL_SECONDS}s，"
            f"保留 {RETENTION_DAYS} 天"
        )
        self._schedule_next()

    def stop(self):
        self._running = False
        if self._timer:
            self._timer.cancel()
            self._timer = None
        logger.info("[EmotionScheduler] 已停止")

    def _schedule_next(self):
        if not self._running:
            return
        self._timer = threading.Timer(INTERVAL_SECONDS, self._tick)
        self._timer.daemon = True
        self._timer.start()

    def _tick(self):
        """单次采集任务（仅在交易日 9:15-15:00 执行）"""
        try:
            # 检查是否为交易日
            from app.interfaces.trading_calendar import is_trading_day_today
            if not is_trading_day_today():
                logger.debug("[EmotionScheduler] 非交易日，跳过")
                return
            # 检查是否在交易时间段 (9:15-15:00)
            now = datetime.now()
            current_minutes = now.hour * 60 + now.minute
            if current_minutes < 9 * 60 + 15 or current_minutes >= 15 * 60:
                logger.debug(f"[EmotionScheduler] 非交易时段 ({now.strftime('%H:%M')})，跳过")
                return
            self._collect()
        except Exception as e:
            logger.error(f"[EmotionScheduler] 采集异常: {e}")
        finally:
            self._schedule_next()

    def _collect(self):
        """采集情绪 + 入库 + 清理过期（通过 cache_db 公开 API）"""
        snap = self.hub.market_snapshot.get_realtime()
        if not snap:
            logger.warning("[EmotionScheduler] 快照返回空，跳过")
            return

        emotion = int(snap.get("emotion", 50))
        now = datetime.now()
        cutoff = (now - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
        row = {
            "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
            "trade_date": now.strftime("%Y-%m-%d"),
            "emotion": emotion,
            "up_count": int(snap.get("up_count", 0)),
            "down_count": int(snap.get("down_count", 0)),
            "limit_up": int(snap.get("limit_up", 0)),
            "limit_down": int(snap.get("limit_down", 0)),
            "north_net_flow": float(snap.get("north_net_flow", 0)),
        }

        try:
            self.db.upsert_and_prune(
                TABLE,
                rows=[row],
                prune_column="timestamp",
                keep_after=cutoff,
            )
            logger.debug(f"[EmotionScheduler] 已记录: emotion={emotion} at {row['timestamp']}")
        except Exception as e:
            logger.error(f"[EmotionScheduler] 写入失败: {e}")


# ==================== 查询 ====================

def query_emotion_history(db, date: str = None, hours: int = None) -> list:
    """查询情绪历史（通过 cache_db 公开 API）

    Args:
        db: cache_db 实例
        date: 查询日期 YYYY-MM-DD，默认当天
        hours: 最近 N 小时（优先级高于 date）

    Returns:
        [{"time": "09:31", "value": 62}, ...]
    """
    try:
        conditions = {}
        if hours:
            cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
            # 使用 query 获取全部，再按 timestamp 过滤（query_between_dates 需要日期格式）
            rows = db.query(TABLE, order_by="timestamp")
            return [
                {"time": str(r["timestamp"])[11:16], "value": int(r["emotion"])}
                for r in rows
                if r.get("timestamp") and r.get("emotion") is not None
                and str(r["timestamp"]) >= cutoff
            ]
        elif date:
            conditions = {"trade_date": date}
        else:
            conditions = {"trade_date": datetime.now().strftime("%Y-%m-%d")}

        rows = db.query(TABLE, conditions=conditions, order_by="timestamp")
        return [
            {"time": str(r["timestamp"])[11:16], "value": int(r["emotion"])}
            for r in rows
            if r.get("timestamp") and r.get("emotion") is not None
        ]
    except Exception as e:
        logger.error(f"[EmotionScheduler] 查询失败: {e}")
        return []
