#!/usr/bin/env python3
"""auto/events.py — 数据就绪事件总线 (2026-09-29)

用途: 把「数据任务完成」变成可声明、可查询的事件, 供策略 ScanSpec.after_events
     消费。scheduler 数据管道 mark_event → 调度器按策略声明触发扫描,
     不再在 scheduler.py 硬编码 dragon_scan 17:25 时刻。

设计点:
  - 事件名全集在 KNOWN_EVENTS; 策略只声明自己真正依赖的子集;
  - mark_event 持久化到 qd_data_events (跨重启不丢);
  - event_ready = 内存缓存 ∪ DB ∪ deriver 自愈 (重启后未 mark 也能从数据源推断);
  - 无 deriver 的事件只认 mark (minute_1m 等, 源数据核对成本高)。

易错点:
  - mark 与 deriver 要同一日期口径 (last_finish_trading_day, 勿混自然日);
  - 负缓存勿写死 — 另一线程 mark_event 后必须立刻可见 (mark 时清缓存);
  - deriver 只做「数据是否已落库」探测, 不做完整性校验 (完整性归数据任务自身)。
"""
from __future__ import annotations

from app.utils.logger import get_logger  # 2026-09-29: 统一全仓 get_logger 口径
import threading
import time

logger = get_logger(__name__)

# 策略可声明的事件名 (ScanSpec.after_events / config schedule.after_events)
KNOWN_EVENTS = (
    "daily_1d",      # 日 K 已回填 (post_market_batch)
    "minute_1m",     # 1m K 已回填 (post_market_batch)
    "lhb",           # 龙虎榜已落库 (dragon_hot_daily)
    "northbound",    # 北向日级已落库
    "fund_flow",     # 个股资金流日度已落库 (fund_flow_daily)
    "index_fflow",   # 指数大盘资金流已同步
)

_lock = threading.Lock()
_cache: dict[tuple[str, str], bool] = {}
_neg_ts: dict[tuple[str, str], float] = {}
_NEG_TTL_SEC = 60.0


# ================================================================
# deriver — 重启自愈: 从数据源反推事件是否已就绪
# ================================================================

def _derive_daily_1d(date: str) -> bool:
    from app.market_cn.auto.core.data.kline import fetch_kline_db
    bars = fetch_kline_db("000001", days=20)
    return bool(bars) and str(bars[-1]["time"])[:10] >= date


def _derive_lhb(date: str) -> bool:
    try:
        from app.market_cn.dragon_tiger_store import query_dragon_tiger
        return bool(query_dragon_tiger(trade_date=date, days=1))
    except Exception as e:
        logger.debug("[events] lhb deriver 失败: %s", e)
        return False


def _derive_northbound(date: str) -> bool:
    try:
        from app.market_cn.index import get_northbound_daily
        nb = get_northbound_daily(10)
        return bool(nb) and str(nb[-1].get("date", ""))[:10] >= date
    except Exception as e:
        logger.debug("[events] northbound deriver 失败: %s", e)
        return False


_DERIVERS = {
    "daily_1d": _derive_daily_1d,
    "lhb": _derive_lhb,
    "northbound": _derive_northbound,
}


# ================================================================
# 持久化 (镜像 qd_scheduler_done 的容错风格)
# ================================================================

def _ensure_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS qd_data_events (
            event_name  VARCHAR(32) NOT NULL,
            trade_date  DATE NOT NULL,
            done_at     TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (event_name, trade_date)
        )
    """)


def _db_has(name: str, date: str) -> bool:
    from app.utils.db import get_db_connection
    try:
        with get_db_connection() as db:
            cur = db.cursor()
            _ensure_table(cur)
            cur.execute(
                "SELECT 1 FROM qd_data_events WHERE event_name = %s AND trade_date = %s",
                (name, date),
            )
            ok = bool(cur.fetchone())
            cur.close()
        return ok
    except Exception as e:
        logger.warning("[events] 读事件表失败(按未就绪): %s", e)
        return False


def _db_mark(name: str, date: str):
    from app.utils.db import get_db_connection
    try:
        with get_db_connection() as db:
            cur = db.cursor()
            _ensure_table(cur)
            cur.execute(
                "INSERT INTO qd_data_events (event_name, trade_date) VALUES (%s, %s) "
                "ON CONFLICT (event_name, trade_date) DO NOTHING",
                (name, date),
            )
            cur.execute(
                "DELETE FROM qd_data_events WHERE trade_date < (CURRENT_DATE - 60)"
            )
            db.commit()
            cur.close()
    except Exception as e:
        logger.warning("[events] 写事件表失败(忽略, deriver 仍可自愈): %s", e)


# ================================================================
# 对外 API
# ================================================================

def mark_event(name: str, date: str) -> bool:
    """数据任务完成后调用。幂等。返回 False=未知事件名(仍落库, 便于扩展)。"""
    if name not in KNOWN_EVENTS:
        logger.warning("[events] 未知事件名 %r (date=%s) — 请扩 KNOWN_EVENTS", name, date)
    with _lock:
        _cache[(name, date)] = True
        _neg_ts.pop((name, date), None)
    _db_mark(name, date)
    logger.info("[events] mark %s @ %s", name, date)
    return name in KNOWN_EVENTS


def mark_if_ready(name: str, date: str) -> bool:
    """deriver 判定已就绪才 mark。无 deriver 的事件恒 False(请直接 mark_event)。"""
    deriver = _DERIVERS.get(name)
    if deriver is None:
        return False
    try:
        ok = bool(deriver(date))
    except Exception as e:
        logger.debug("[events] mark_if_ready(%s,%s) deriver 异常: %s", name, date, e)
        return False
    if ok:
        mark_event(name, date)
    return ok


def event_ready(name: str, date: str) -> bool:
    """事件在 date 是否就绪: 缓存 ∪ DB ∪ deriver 自愈。"""
    key = (name, date)
    with _lock:
        if _cache.get(key):
            return True
        neg_at = _neg_ts.get(key, 0.0)
        if neg_at and (time.time() - neg_at) < _NEG_TTL_SEC:
            return False
    if _db_has(name, date):
        with _lock:
            _cache[key] = True
            _neg_ts.pop(key, None)
        return True
    deriver = _DERIVERS.get(name)
    if deriver is not None:
        try:
            if deriver(date):
                mark_event(name, date)
                return True
        except Exception as e:
            logger.debug("[events] deriver(%s,%s) 异常: %s", name, date, e)
    with _lock:
        _neg_ts[key] = time.time()
    return False


def events_ready(names, date: str) -> bool:
    """全部事件就绪才 True。空序列 = 无依赖 = 就绪。"""
    return all(event_ready(n, date) for n in (names or ()))


def missing_events(names, date: str):
    """返回未就绪事件名列表 (调度日志用)。"""
    return [n for n in (names or ()) if not event_ready(n, date)]
