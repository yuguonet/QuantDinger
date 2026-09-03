"""
market_cn 数据刷新调度器 v4 — 全 fire-and-forget

一个调度线程管时间，到点拉 worker 线程，跑完就退。
没有 daemon 循环，没有 while True 空转。

调度线程每 10 秒检查一次，到期的任务拉新线程执行。
"""

import threading
import logging
import time as _time
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)

TZ_CN = timezone(timedelta(hours=8))

_emotion_last_ts = 0.0
_EMOTION_MIN_INTERVAL = 1800


# ═══════════════════════════════════════════════════════════
#  时段判断
# ═══════════════════════════════════════════════════════════


def _is_trading_time():
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 100 + now.minute
    return (900 <= t <= 1131) or (1300 <= t <= 1501)


def _is_dragon_fast_window():
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 100 + now.minute
    return 930 <= t < 1000


# ═══════════════════════════════════════════════════════════
#  工具函数
# ═══════════════════════════════════════════════════════════


def _run_all(tag, fns):
    for fn in fns:
        try:
            fn()
        except Exception as e:
            logger.warning("[%s] %s 失败: %s", tag, fn.__name__, e)


def _refresh_emotion_safe():
    global _emotion_last_ts
    now = _time.time()
    if now - _emotion_last_ts < _EMOTION_MIN_INTERVAL:
        return
    try:
        from app.market_cn.emotion import refresh_emotion_cycle
        refresh_emotion_cycle()
        _emotion_last_ts = now
    except Exception as e:
        logger.warning("[emotion] refresh_emotion_cycle 失败: %s", e)


# ═══════════════════════════════════════════════════════════
#  数据刷新函数
# ═══════════════════════════════════════════════════════════


def _refresh_dragon_pools():
    from app.market_cn.dragon_limit import refresh_zt_pool, refresh_dt_pool, refresh_broken_board
    _run_all("dragon_pools", [refresh_zt_pool, refresh_dt_pool, refresh_broken_board])


def _refresh_fast():
    from app.market_cn.index import (
        refresh_index_realtime, refresh_northbound_realtime,
        refresh_market_fund_flow_realtime, refresh_sector_fund_flow,
    )
    from app.market_cn.china_market import refresh_hot_sectors
    from app.market_cn.dragon_limit import refresh_hot_rank
    _run_all("fast", [
        refresh_index_realtime, refresh_northbound_realtime,
        refresh_market_fund_flow_realtime, refresh_sector_fund_flow,
        refresh_hot_sectors, refresh_hot_rank,
    ])


def _refresh_slow():
    from app.market_cn.china_market import refresh_fear_greed
    from app.data_providers.global_market import refresh_global_sentiment
    _run_all("slow", [refresh_fear_greed, refresh_global_sentiment])
    _refresh_emotion_safe()


def _refresh_post_market():
    from app.market_cn.dragon_limit import refresh_dragon_tiger
    from app.market_cn.index import refresh_northbound_daily, refresh_market_fund_flow_daily
    _run_all("post_market", [
        refresh_dragon_tiger, refresh_northbound_daily,
        refresh_market_fund_flow_daily,
    ])
    _refresh_emotion_safe()


def _refresh_sector_daily():
    """板块日级统计，依赖 1D 日线数据，必须在 backfill 1D 之后调用。"""
    from app.market_cn.sector_history import collect_sector_daily
    try:
        collect_sector_daily()
    except Exception as e:
        logger.warning("[sector_daily] collect_sector_daily 失败: %s", e)


def _save_dragon_hot_daily():
    """龙虎榜 & 热榜持久化 — 每天 18:00 调用，写入 PostgreSQL。"""
    from app.market_cn.dragon_tiger_store import save_daily
    try:
        result = save_daily()
        dt = result.get("dragon_tiger", {})
        hr = result.get("hot_rank", {})
        logger.info(
            "[dragon_hot_daily] 完成: 龙虎榜 %d/%d, 热榜 %d/%d, 状态=%s",
            dt.get("written", 0), dt.get("total", 0),
            hr.get("written", 0), hr.get("total", 0),
            result.get("status", "unknown"),
        )
    except Exception as e:
        logger.error("[dragon_hot_daily] 执行失败: %s", e)


def _refresh_daily():
    from app.market_cn.index_daily import sync_index_daily
    from app.market_cn.index import refresh_northbound_holdings
    _run_all("daily", [sync_index_daily, refresh_northbound_holdings])


def _refresh_policy():
    from app.market_cn.china_market import refresh_policy
    refresh_policy()


def _refresh_backfill_15m():
    from app.data_sources.backfill_db import run_15m
    run_15m()


def _refresh_realtime_snapshot():
    """盘中: 全市场实时行情快照原始数据采集"""
    from app.market_cn.realtime_snapshot import collect_realtime_snapshot
    collect_realtime_snapshot()


def _dragon_strategy_scan():
    """盘后: 龙回头Pro 全市场扫描 (1D 就绪后判定, 写 qd_dragon_signals)"""
    from app.market_cn.auto.dragon_scan import run_scan
    run_scan()


def _dragon_strategy_monitor():
    """盘中: 龙回头Pro 状态机 (开盘gap判定/预确认/收盘确认/出场检测/组对账)"""
    from app.market_cn.auto.dragon_monitor import run_monitor_safe
    run_monitor_safe()


def _refresh_backfill_1m():
    """盘后: 回填当日 1m K 线"""
    from app.data_sources.backfill_db import run_1m
    run_1m()


def _refresh_backfill_1d() -> dict:
    """覆写 1D，返回 {status, written, skipped}。"""
    from app.data_sources.backfill_db import run_1d
    return run_1d()


def _refresh_adj_factors():
    from app.data_sources.provider.adjustment import update_all_factors
    count = update_all_factors()
    logger.info("[adj_factors] 全量更新完成: %d 只股票", count)


def _morning_batch():
    """早盘日级串行: 复权因子(6:00) → 政策新闻。"""
    _refresh_adj_factors()
    _refresh_policy()


# 盘后批次完成事件，EvalWorker 等此事件后再执行回溯验证
import threading as _threading
post_market_done = _threading.Event()

def _post_market_batch():
    """盘后日级串行: 1m → 日档 → 龙虎榜/北向/资金流 → 1D → 板块统计，重试至数据到位后退出。"""
    from app.utils.trading_calendar import last_finish_trading_day
    target = last_finish_trading_day()

    # 1m K线回填 (mootdx, 每标的240条) — 替代原 15m，精度更高
    _refresh_backfill_1m()

    _refresh_daily()
    _refresh_post_market()

    # 1D 最后跑，完成后触发板块统计
    result_1d = _refresh_backfill_1d()
    if result_1d.get("written", 0) > 0:
        logger.info("[post_market] 1D 写入 %d 条", result_1d["written"])
    else:
        logger.info("[post_market] 1D 无新数据 (skipped=%s)", result_1d.get("skipped"))

    # 检测数据是否到位
    dt_date = nb_date = ""
    try:
        from app.market_cn.dragon_limit import get_dragon_tiger
        dt = get_dragon_tiger()
        if dt and isinstance(dt, list) and len(dt) > 0:
            dt_date = dt[0].get("date", "") if isinstance(dt[0], dict) else ""
    except Exception:
        pass
    try:
        from app.market_cn.index import get_northbound_daily
        nb = get_northbound_daily(10)
        if nb and isinstance(nb, list) and len(nb) > 0:
            nb_date = nb[-1].get("date", "") if isinstance(nb[-1], dict) else ""
    except Exception:
        pass

    if dt_date >= target and nb_date >= target:
        logger.info("[post_market] 数据到位 (dt=%s, nb=%s, 目标=%s)", dt_date, nb_date, target)
    else:
        logger.info("[post_market] 数据未到 (dt=%s, nb=%s, 目标≥%s)，10min 后重试", dt_date, nb_date, target)
        _time.sleep(600)
        _refresh_post_market()  # 重试一次

    # 板块热度每日统计（依赖 1D 日线，必须在最后执行）
    _refresh_sector_daily()

    # 通知 EvalWorker: 盘后批次完成，K线数据已就绪
    post_market_done.set()
    logger.info("[post_market] 盘后批次完成，已通知 EvalWorker")


# ═══════════════════════════════════════════════════════════
#  盘后完成检测
# ═══════════════════════════════════════════════════════════





# ═══════════════════════════════════════════════════════════
#  Task 定义
# ═══════════════════════════════════════════════════════════


@dataclass
class Task:
    name: str
    fn: Callable                  # 要执行的函数
    interval: int                 # 间隔秒数
    trading_only: bool = True     # 仅盘中执行
    last_run: float = 0.0         # 上次执行时间戳
    running: bool = False         # 是否有线程在跑（防重入）
    once_per_day: bool = False    # 一天只跑一次
    daily_done: str = ""          # 一天一次的日期标记
    trigger_hour: int = -1        # 定时触发: 小时 (-1=不定时)
    trigger_minute: int = 0       # 定时触发: 分钟


def _dragon_interval():
    return 60 if _is_dragon_fast_window() else 300


# 任务列表
TASKS = [
    # 盘中周期任务
    Task("realtime_snapshot", _refresh_realtime_snapshot, interval=60, trading_only=True),
    Task("dragon_pools", _refresh_dragon_pools, interval=300, trading_only=True),
    Task("fast",         _refresh_fast,         interval=300, trading_only=True),
    Task("slow",         _refresh_slow,         interval=1800, trading_only=True),
    # 日级任务 (定时触发，一天一次)
    Task("morning_batch",     _morning_batch,     interval=86400, trading_only=False, once_per_day=True, trigger_hour=6,  trigger_minute=0),
    Task("post_market_batch", _post_market_batch, interval=86400, trading_only=False, once_per_day=True, trigger_hour=15, trigger_minute=30),
    Task("dragon_hot_daily",  _save_dragon_hot_daily, interval=86400, trading_only=False, once_per_day=True, trigger_hour=18, trigger_minute=0),
    # 龙回头Pro 自动化: 盘后扫描(1D就绪后) + 盘中状态机(60s)
    Task("dragon_scan",    _dragon_strategy_scan,    interval=86400, trading_only=False, once_per_day=True, trigger_hour=16, trigger_minute=30),
    Task("dragon_monitor", _dragon_strategy_monitor, interval=60,   trading_only=True),
]


def _get_interval(task: Task) -> int:
    """动态间隔（dragon 自适应）。"""
    if task.name == "dragon_pools":
        return _dragon_interval()
    return task.interval


# ═══════════════════════════════════════════════════════════
#  Worker（跑完就退）
# ═══════════════════════════════════════════════════════════


def _worker(task: Task):
    """执行单个任务，完成后退出。"""
    try:
        task.fn()
    except Exception as e:
        logger.error("[%s] 执行失败: %s", task.name, e)
    finally:
        task.running = False
    logger.debug("[%s] 线程退出", task.name)


# ═══════════════════════════════════════════════════════════
#  调度线程（唯一常驻线程）
# ═══════════════════════════════════════════════════════════


def _scheduler_loop():
    """每 10 秒检查一次，到期任务拉新线程执行。"""
    logger.info("[scheduler] 调度线程启动，每 10 秒检查一次")

    now_dt = datetime.now()
    today = now_dt.strftime("%Y-%m-%d")

    # 首次立即执行周期任务
    for task in TASKS:
        if not task.once_per_day:
            _launch(task)

    # 启动补跑：日级任务触发时刻已过且未执行过，立即补跑
    # 跳过 post_market_batch（会唤醒 EvalWorker，与 mq-worker 产生导入竞争）
    from app.utils.trading_calendar import is_trading_day
    for task in TASKS:
        if not task.once_per_day or task.trigger_hour < 0:
            continue
        if task.name == "post_market_batch":
            continue
        if now_dt.hour < task.trigger_hour or (
            now_dt.hour == task.trigger_hour and now_dt.minute < task.trigger_minute
        ):
            continue
        if not is_trading_day(today):
            continue
        logger.info("[scheduler] 启动补跑: %s (今日 %02d:%02d 已过)",
                     task.name, task.trigger_hour, task.trigger_minute)
        _launch(task)

    while True:
        _time.sleep(10)
        now = _time.time()
        now_dt = datetime.now()
        today = now_dt.strftime("%Y-%m-%d")

        for task in TASKS:
            # 已有线程在跑 → 跳过
            if task.running:
                continue

            # 一天一次 + 今天已跑 → 跳过
            if task.once_per_day and task.daily_done == today:
                continue

            # 日级定时任务：未到触发时刻 → 跳过
            if task.once_per_day and task.trigger_hour >= 0:
                if now_dt.hour < task.trigger_hour or (
                    now_dt.hour == task.trigger_hour and now_dt.minute < task.trigger_minute
                ):
                    continue
                # 非交易日跳过
                from app.utils.trading_calendar import is_trading_day
                if not is_trading_day(today):
                    continue

            # 盘中限制
            if task.trading_only and not _is_trading_time():
                continue

            # 周期间隔未到 → 跳过
            if not task.once_per_day:
                interval = _get_interval(task)
                if now - task.last_run < interval:
                    continue

            _launch(task)


def _launch(task: Task):
    """拉起 worker 线程。"""
    task.running = True
    task.last_run = _time.time()
    if task.once_per_day:
        task.daily_done = datetime.now().strftime("%Y-%m-%d")

    t = threading.Thread(target=_worker, args=(task,), daemon=False, name=f"work-{task.name}")
    t.start()
    logger.info("[scheduler] → %s (间隔 %ds)", task.name, _get_interval(task))


# ═══════════════════════════════════════════════════════════
#  入口
# ═══════════════════════════════════════════════════════════


def start():
    """应用启动时调用。只拉一个调度线程。"""
    logger.info("[scheduler] market_cn 调度器启动 (v4 全 fire-and-forget)")
    logger.info("[scheduler] 任务: %s", ", ".join(t.name for t in TASKS))

    t = threading.Thread(target=_scheduler_loop, daemon=True, name="scheduler")
    t.start()
