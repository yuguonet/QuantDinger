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
#  跨重启"今日已完成"标记 (2026-09-18 事故修复①)
#  问题: once_per_day 任务的 daily_done 是内存标记, backend 重启后丢失,
#        启动补跑会重复触发 dragon_scan 等 → 并发 DELETE+INSERT 死锁丢信号。
#  解决: 完成标记持久化到 DB (qd_scheduler_done), 重启后跳过已完成的今日任务。
# ═══════════════════════════════════════════════════════════

def _ensure_sched_done_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS qd_scheduler_done (
            task_name   VARCHAR(40) NOT NULL,
            trade_date  DATE NOT NULL,
            done_at     TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (task_name, trade_date)
        )
    """)


def scheduler_task_done(task_name: str, trade_date: str) -> bool:
    """DB 持久化标记: 该任务今日是否已完成 (跨重启生效)。读取失败→保守返回 False(允许补跑)。"""
    from app.utils.db import get_db_connection
    try:
        with get_db_connection() as db:
            cur = db.cursor()
            _ensure_sched_done_table(cur)
            cur.execute(
                "SELECT 1 FROM qd_scheduler_done WHERE task_name = %s AND trade_date = %s",
                (task_name, trade_date),
            )
            ok = bool(cur.fetchone())
            cur.close()
        return ok
    except Exception as e:
        logger.warning("[scheduler] 读取完成标记失败(保守补跑): %s", e)
        return False


def mark_scheduler_task_done(task_name: str, trade_date: str):
    """标记今日任务已完成 (幂等)。写入失败→忽略(下次补跑兜底), 不抛。"""
    from app.utils.db import get_db_connection
    try:
        with get_db_connection() as db:
            cur = db.cursor()
            _ensure_sched_done_table(cur)
            cur.execute(
                "INSERT INTO qd_scheduler_done (task_name, trade_date) VALUES (%s, %s) "
                "ON CONFLICT (task_name, trade_date) DO NOTHING",
                (task_name, trade_date),
            )
            cur.execute(
                "DELETE FROM qd_scheduler_done WHERE trade_date < (CURRENT_DATE - 60)"
            )
            db.commit()
            cur.close()
        logger.info("[scheduler] 标记完成: %s @ %s", task_name, trade_date)
    except Exception as e:
        logger.warning("[scheduler] 标记完成失败(忽略): %s", e)


def _done_row_name(r):
    """从游标行取 task_name。游标是 RealDictCursor → 行为 dict, 勿用 r[0] (KeyError: 0)。"""
    if isinstance(r, dict):
        return r.get("task_name")
    try:
        return r[0]
    except Exception:
        return None


def scheduler_done_names(task_prefix: str, trade_date: str) -> set:
    """批量取 trade_date 当日已标记的 task_name 集合 (prefix 过滤)。失败→空集(保守补跑)。

    2026-09-29: 原实现 r[0] 对 RealDictCursor 行抛 KeyError: 0 ⇒ 批量读恒失败 ⇒
    daily_scan 每 60s「保守补跑」全市场重复扫描。改为按列名取。
    """
    from app.utils.db import get_db_connection
    try:
        with get_db_connection() as db:
            cur = db.cursor()
            _ensure_sched_done_table(cur)
            # prefix 经 LIKE 转义: 调用方传 "daily_scan@" 这类无通配前缀, 只在末尾补 %
            cur.execute(
                "SELECT task_name FROM qd_scheduler_done "
                "WHERE trade_date = %s AND task_name LIKE %s",
                (trade_date, task_prefix + "%"),
            )
            names = set()
            for r in cur.fetchall():
                n = _done_row_name(r)
                if n:
                    names.add(n)
            cur.close()
        return names
    except Exception as e:
        logger.warning("[scheduler] 批量读完成标记失败(保守补跑): %s", e)
        return set()


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
    """龙虎榜 & 热榜持久化 — 每天 17:00 调用 (LHB(D) 交易所 ~17:00-17:30 发布)。

    当日榜未发布(total=0)时 10min 间隔重试(最多 3 次尝试), 必须先于
    dragon_scan(17:25) 完成落库 —— 2026-09-18 调度重排: 榜先落库、扫描后跑,
    杜绝"策略消费 LHB 时当日榜缺失"的时序错位 (hub.lhb 时效纪律配套)。
    """
    from app.market_cn.dragon_tiger_store import save_daily
    from app.utils.trading_calendar import is_trading_day
    today = datetime.now().strftime("%Y-%m-%d")
    for attempt in range(1, 4):
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
            # 拿到当日榜 / 非交易日 / 已到最后一次尝试 → 结束; 否则等 10min 重试
            if dt.get("total", 0) > 0 or not is_trading_day(today) or attempt >= 3:
                if dt.get("total", 0) > 0:
                    _mark_event("lhb", today)
                return
            logger.warning("[dragon_hot_daily] 当日龙虎榜尚未发布(attempt %d), 10min 后重试", attempt)
            _time.sleep(600)
        except Exception as e:
            logger.error("[dragon_hot_daily] 执行失败: %s", e)
            return


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
    """盘中: 全市场实时行情快照原始数据采集 + 派生资金流今日行。"""
    from app.market_cn.realtime_snapshot import collect_realtime_snapshot
    collect_realtime_snapshot()
    # 2026-09-26: 资金流是 snapshot 的派生视图, 采集后就地刷新今日行 (幂等)。
    # 失败不影响快照; 不另开分时任务。
    try:
        from app.market_cn.fund_flow_api import update_intraday_fund_flow
        update_intraday_fund_flow()
    except Exception as e:
        try:
            from app.market_cn.scheduler import logger as _lg
        except Exception:
            import logging
            _lg = logging.getLogger("scheduler")
        _lg.warning("[fund_flow] 盘中派生刷新失败(不影响 snapshot): %s", e)


def _mark_event(name: str, date: str = None):
    """数据任务完成后打事件点 (策略 ScanSpec.after_events 消费; 见 auto/events.py)。"""
    try:
        from app.market_cn.auto.events import mark_event
        if date is None:
            from app.utils.trading_calendar import last_finish_trading_day
            date = last_finish_trading_day()
        mark_event(name, date)
    except Exception as e:
        logger.warning("[events] mark %s 失败: %s", name, e)


# 进程内已扫缓存 (2026-09-29): DB 批量读抖动时仍不重复全市场扫描
# {trade_date: {strategy_key, ...}}。与 qd_scheduler_done 双写/双读, 任一命中即视为已完成。
_daily_scanned: dict = {}


def _daily_scan_dispatch():
    """盘后扫描分发 (2026-09-29): 策略 after_events/fire_at 就绪即触发 keys 子集。

    取代 dragon_scan 17:25 硬编码 —— 触发声明在策略 ScanSpec / config.json schedule,
    数据任务 mark_event, 本函数只做「就绪集 ∩ 未扫描」分发。无就绪时 cheap 返回。
    完成标记按 **target 交易日** 记 (勿用自然日: 早盘补跑 target=昨日)。
    """
    from app.market_cn.auto.sched import daily_fire_ready, enabled_daily_keys
    from app.market_cn.auto.scan import run_scan
    from app.utils.trading_calendar import last_finish_trading_day

    target = last_finish_trading_day()
    keys = enabled_daily_keys()
    if not keys:
        return
    done = set(scheduler_done_names("daily_scan@", target))
    done |= _daily_scanned.get(target, set())
    ready = []
    reasons = {}
    for key in keys:
        if f"daily_scan@{key}" in done or key in _daily_scanned.get(target, set()):
            continue
        ok, reason = daily_fire_ready(key, date=target)
        reasons[key] = reason
        if ok:
            ready.append(key)
    if not ready:
        return
    logger.info("[daily_scan] 就绪触发 keys=%s target=%s | %s", ready, target, reasons)
    result = run_scan(keys=tuple(ready))
    status = result.get("status") if isinstance(result, dict) else None
    if status in ("ok", "no_active_strategy"):
        _daily_scanned.setdefault(target, set()).update(ready)
        for key in ready:
            mark_scheduler_task_done(f"daily_scan@{key}", target)
        logger.info("[daily_scan] 完成 %s → %s", ready, result)
    else:
        # data_not_ready 等: 不标记, 下一轮 60s 重试 (run_scan 内部 wait_data 兜 1D)
        logger.warning("[daily_scan] 未完成(不标记, 下轮重试): %s → %s", ready, result)


def _dragon_strategy_monitor():
    """盘中: 自动策略组状态机 (开盘gap判定/预确认/收盘确认/出场检测/组对账)"""
    from app.market_cn.auto.monitor import run_monitor_safe
    run_monitor_safe()


def _dragon_strategy_knife_scan(slot=None):
    """盘中窗口: 窗口策略扫描 (按 slot 触发)。

    slot = {"hm": "HH:MM", "keys": [...]} —— 本次只跑这批策略 (None = 兼容旧的全量调用)。
    时刻与策略集合来自 auto/sched.py 事实源, 由 _intraday_trigger_slots() 分组:
      09:40 → lead_chase(早盘单点)  |  14:30 → knife_catch + tail_oversold(尾盘批)
    knife_catch: 14:56 终审; tail_oversold: 14:50 起每分钟滚动预览 + 14:56 终审。
    """
    from app.market_cn.auto.scan import run_scan_knife
    if slot:
        run_scan_knife(keys=tuple(slot["keys"]))
    else:
        run_scan_knife()


def _refresh_backfill_1m():
    """盘后: 回填当日 1m K 线"""
    from app.data_sources.backfill_db import run_1m
    run_1m()


def _sync_index_minute():
    """盘后: 指数 5m K线同步 → kline_index_5m (指数分钟不可回补, 只能向前攒;
    幂等 upsert, 800 根窗口断采一周可补回)。
    scripts/ 目录脚本, 用 importlib 按路径载入 (scripts/ 不在 app 包内)。"""
    import importlib.util
    import os as _os
    _p = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(
        _os.path.dirname(_os.path.abspath(__file__))))), "scripts", "sync_index_minute.py")
    if not _os.path.isfile(_p):
        logger.warning("[index_minute] 未找到 scripts/sync_index_minute.py, 跳过")
        return
    try:
        _spec = importlib.util.spec_from_file_location("sync_index_minute", _p)
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        r = _mod.sync()
        logger.info("[index_minute] 指数 5m 同步: %s", r)
    except Exception as e:
        logger.error("[index_minute] 执行失败: %s", e)


def _sync_index_fflow():
    """盘后: 指数大盘资金流同步 → kline_index_fflow (EM 1分钟累计, 当日240根/指数;
    只能向前攒不可回补)。scripts/ 目录脚本, 用 importlib 按路径载入。"""
    import importlib.util
    import os as _os
    _p = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(
        _os.path.dirname(_os.path.abspath(__file__))))), "scripts", "sync_index_fflow.py")
    if not _os.path.isfile(_p):
        logger.warning("[index_fflow] 未找到 scripts/sync_index_fflow.py, 跳过")
        return
    try:
        _spec = importlib.util.spec_from_file_location("sync_index_fflow", _p)
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        r = _mod.sync()
        logger.info("[index_fflow] 指数资金流同步: %s", r)
    except Exception as e:
        logger.error("[index_fflow] 执行失败: %s", e)


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
    _mark_event("minute_1m", target)

    # 指数 5m K线同步 (kline_index_5m, 指数分钟只能向前攒, 不可回补)
    _sync_index_minute()

    # 指数大盘资金流同步 (kline_index_fflow, EM 1分钟累计, 只能向前攒)
    _sync_index_fflow()
    _mark_event("index_fflow", target)

    _refresh_daily()
    _refresh_post_market()

    # 1D 最后跑，完成后触发板块统计
    result_1d = _refresh_backfill_1d()
    if result_1d.get("written", 0) > 0:
        logger.info("[post_market] 1D 写入 %d 条", result_1d["written"])
    else:
        logger.info("[post_market] 1D 无新数据 (skipped=%s)", result_1d.get("skipped"))
    _mark_event("daily_1d", target)

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
        if nb_date >= target:
            _mark_event("northbound", target)
    else:
        logger.info("[post_market] 数据未到 (dt=%s, nb=%s, 目标≥%s)，10min 后重试", dt_date, nb_date, target)
        _time.sleep(600)
        _refresh_post_market()  # 重试一次

    # 板块热度每日统计（依赖 1D 日线，必须在最后执行）
    _refresh_sector_daily()

    # 自选股标签 system 自算 (grade=1 兜底, 全量自选股并集) —— 放在最后:
    # 必须等 1D 日线到位; 复用本批次的 once_per_day 守卫与跨重启完成标记, 不另起调度器。
    # 失败不影响盘后批次结论 (标签是展示层兜底, 不参与任何策略判定)。
    try:
        from app.watchlist.job import run_daily as _label_run_daily
        _stats = _label_run_daily()
        logger.info("[label] 每日标签刷新: scope=%s written=%s skipped=%s failed=%s 接管=%s",
                    _stats.get("scope"), _stats.get("written"), _stats.get("skipped"),
                    _stats.get("failed"), _stats.get("takeover"))
    except Exception:
        logger.error("[label] 每日标签刷新失败 (不影响盘后批次)")
        import traceback as _tb
        logger.error(_tb.format_exc())

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
    once_per_slot: bool = False   # 多触发点: 每个 slot 各跑一次 (盘中窗口策略组)
    slots_done: set = field(default_factory=set)   # 当日已完成的 slot 时刻集合


def _dragon_interval():
    return 60 if _is_dragon_fast_window() else 300


# ── 盘中窗口策略组触发时刻: 以 auto/sched.py 分段声明为唯一事实源 ──
# (config.json schedule 段 → 各策略 first 的最早者; 接线 2026-09-09 用户批准)
_knife_slot_cache = {"date": "", "slots": []}
SLOT_GAP_MIN = 30        # 相邻首拍间隔 <= 30min 合并为同一批


def _hm_to_min(hm):
    """“HH:MM” → 当日分钟数 (用于时刻比较/聚类)。"""
    try:
        h, m = str(hm).split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return -1


def _intraday_trigger_slots():
    """盘中窗口策略组的**多个**触发点: [{"hm": "HH:MM", "keys": [...]}, ...] (hm 升序)。

    事实源 = auto/sched.py::all_schedules() (策略 ScanSpec / config.json schedule 段)。
    只排**已启用**策略 —— 停用策略不占触发点 (过滤是消费方职责, sched 只给事实源)。

    ★ 相邻 <= SLOT_GAP_MIN 的首拍合并为一批: 尾盘 14:30(knife_catch) + 14:50(tail_oversold)
      **必须**合并 —— 拆开则 14:30 批 sleep 到 15:00, 14:50 批因 `running` 防重入被
      跳过 ⇒ tail_oversold 当天一次都不跑。批次触发时刻 = 组内最早首拍。
    ★ 为什么不用 min(): 旧实现把所有策略压成一个时刻, 早盘/停用策略 (lead_chase
      09:40) 会把尾盘组拉到 09:40 ⇒ 09:40 触发后死等 start_hm=14:50 ⇒ 40min 超时
      放弃且 once_per_day 已标记 ⇒ 当日 knife_catch/tail_oversold 全天 0 信号
      (2026-09-28 实证 13:39:28「等待超时, 放弃本次」)。

    按天缓存 (all_schedules 会 autodiscover, 勿每 10s 调)。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    if _knife_slot_cache["date"] == today:
        return _knife_slot_cache["slots"]

    first_of = {}          # key -> (首拍, 末拍)
    try:
        from app.market_cn.auto.sched import all_schedules
        from app.market_cn.auto.strategies import is_enabled
        for key, times in all_schedules().items():
            if not times or not is_enabled(key):
                continue
            first_of[key] = (times[0], times[-1])
    except Exception as e:
        logger.warning("[scheduler] 读取 sched 时刻表失败, 盘中触发点回退 14:30: %s", e)

    slots = []
    for key, (first, _last) in sorted(first_of.items(), key=lambda kv: _hm_to_min(kv[1][0])):
        if slots and _hm_to_min(first) - _hm_to_min(slots[-1]["hm"]) <= SLOT_GAP_MIN:
            slots[-1]["keys"].append(key)
            if _hm_to_min(first) < _hm_to_min(slots[-1]["hm"]):
                slots[-1]["hm"] = first
        else:
            slots.append({"hm": first, "keys": [key]})
    for s in slots:
        s["keys"] = sorted(s["keys"])

    _knife_slot_cache.update(date=today, slots=slots)
    logger.info("[scheduler] 盘中触发点 (sched 事实源): %s",
                " | ".join(f"{s['hm']}={'+'.join(s['keys'])}" for s in slots) or "无")
    return slots


def _next_pending_slot(task, now_dt=None):
    """取一个「时刻已到 且 当日未跑过」的 slot; 无则 None。"""
    now_dt = now_dt or datetime.now()
    today = now_dt.strftime("%Y-%m-%d")
    hm_now = now_dt.strftime("%H:%M")
    for slot in _intraday_trigger_slots():
        hm = slot["hm"]
        if hm > hm_now:
            break                       # 升序, 后面的都还没到点
        if hm in task.slots_done:
            continue
        if scheduler_task_done(f"{task.name}@{hm}", today):   # 跨重启守卫
            task.slots_done.add(hm)
            continue
        return slot
    return None


def _task_trigger_hm(task):
    """任务的日级触发时刻 "HH:MM"; 无定时触发返回 None。

    多触发点任务 (once_per_slot) **不走这里** —— 时刻来自 _intraday_trigger_slots()。
    """
    if task.once_per_slot:
        return None
    if task.trigger_hour >= 0:
        return f"{task.trigger_hour:02d}:{task.trigger_minute:02d}"
    return None


def _save_fund_flow_daily():
    """资金流日度落库: 把最近 7 个交易日的 1m 派生资金流补齐(幂等、自愈)。

    为什么独立成任务而不并入 `_post_market_batch`:
      ① 后者要串行回填 1m(5221 标的 x 240 条) + 日线 + 龙虎榜, 跑完时刻不定;
         本任务放 18:00 与之解耦, 互不拖累;
      ② days=7 的"自愈"语义与 batch 的"仅当天"不同 —— 容许补前几天,
         任一天失败/漏跑, 只要还在窗口内下次自动补上。
    """
    try:
        from app.market_cn.fund_flow_api import backfill_stock_from_1m
        r = backfill_stock_from_1m(days=7)
        logger.info("[fund_flow_daily] %s", r)
        _mark_event("fund_flow")
    except Exception as e:
        logger.warning("[fund_flow_daily] 失败: %s", e)


# 任务列表
TASKS = [
    # 盘中周期任务
    Task("realtime_snapshot", _refresh_realtime_snapshot, interval=60, trading_only=True),
    Task("dragon_pools", _refresh_dragon_pools, interval=300, trading_only=True),
    Task("fast",         _refresh_fast,         interval=300, trading_only=True),
    Task("slow",         _refresh_slow,         interval=1800, trading_only=True),
    # 盘中资金流轮询 (kline_index_fflow, 每5分钟全日重写幂等; EM delay host 滞后
    # ~15分钟, 实时主站封锁自动回退, 解封后延迟降为秒级; 盘后 batch 终校准)
    Task("fflow_intraday", _sync_index_fflow, interval=300, trading_only=True),
    # 日级任务 (定时触发，一天一次)
    Task("morning_batch",     _morning_batch,     interval=86400, trading_only=False, once_per_day=True, trigger_hour=6,  trigger_minute=0),
    Task("post_market_batch", _post_market_batch, interval=86400, trading_only=False, once_per_day=True, trigger_hour=15, trigger_minute=30),
    # 龙虎榜落库 17:00 (LHB ~17:00-17:30 发布, 未发布自动重试)。完成后 mark_event("lhb"),
    # 盘后策略按 ScanSpec.after_events 消费 (不再依赖固定 17:25 时刻)。
    Task("dragon_hot_daily",  _save_dragon_hot_daily, interval=86400, trading_only=False, once_per_day=True, trigger_hour=17, trigger_minute=0),
    # 自动策略组: 盘后扫描(事件驱动分发) + 盘中状态机(60s)
    # 2026-09-29: dragon_scan 17:25 硬编码 → daily_scan 按策略 after_events/fire_at 就绪触发
    # (auto/sched.py::daily_fire_ready + auto/events.py). 数据任务 mark_event, 本任务 60s 扫就绪集。
    Task("daily_scan",    _daily_scan_dispatch,    interval=60, trading_only=False),
    # knife_scan 触发时刻由 auto/sched.py 分段声明决定 (config.json schedule 段为事实源,
    # 见 _knife_trigger_hm; Task 上的 trigger_* 仅作 sched 不可用时的兜底)
    # 盘中窗口策略组: **多触发点** (每个 slot 各跑一次, 时刻/策略集合来自 sched 事实源)。
    # 不再用 trigger_hour 单一时刻 —— 那会让 09:40(lead_chase) 把尾盘组拉早 (见 _intraday_trigger_slots)。
    Task("knife_scan",     _dragon_strategy_knife_scan, interval=86400, trading_only=True, once_per_slot=True),
    Task("dragon_monitor", _dragon_strategy_monitor, interval=60,   trading_only=True),
    # 资金流日度落库 (2026-09-27 用户裁定): `kline_15m` 已作废 => 1m 是唯一可用分钟源。
    # 放 18:00 —— 晚于 post_market_batch(15:30, 内含 1m 回填), 与之解耦;
    # days=7 自愈: 当日 1m 未到位则自动跳过, 次日补上。
    Task("fund_flow_daily", _save_fund_flow_daily, interval=86400, trading_only=False, once_per_day=True, trigger_hour=18, trigger_minute=0),
]


def _get_interval(task: Task) -> int:
    """动态间隔（dragon 自适应）。"""
    if task.name == "dragon_pools":
        return _dragon_interval()
    return task.interval


# ═══════════════════════════════════════════════════════════
#  Worker（跑完就退）
# ═══════════════════════════════════════════════════════════


def _worker(task: Task, slot=None):
    """执行单个任务，完成后退出。slot 不为 None 时是多触发点任务的一次触发。"""
    tag = f"{task.name}@{slot['hm']}" if slot else task.name
    try:
        if slot is not None:
            task.fn(slot)
        else:
            task.fn()
        # 跨重启守卫: 成功完成才标记, 失败不标记→当日仍可被补跑重试
        if slot is not None:
            task.slots_done.add(slot["hm"])
            mark_scheduler_task_done(tag, datetime.now().strftime("%Y-%m-%d"))
        elif task.once_per_day:
            mark_scheduler_task_done(task.name, datetime.now().strftime("%Y-%m-%d"))
    except Exception as e:
        logger.error("[%s] 执行失败: %s", tag, e)
    finally:
        task.running = False
    logger.debug("[%s] 线程退出", tag)


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
        trig = _task_trigger_hm(task) if task.once_per_day else None
        if not trig:
            continue
        if task.name == "post_market_batch":
            continue
        if now_dt.strftime("%H:%M") < trig:
            continue
        if not is_trading_day(today):
            continue
        # 跨重启守卫: 今日已完成则跳过启动补跑 (防重复扫描→并发死锁丢信号)
        if scheduler_task_done(task.name, today):
            task.daily_done = today
            logger.info("[scheduler] 跳过启动补跑(今日已完成): %s", task.name)
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
            # 跨重启守卫 (防御): 内存标记丢失但 DB 已记录完成 → 跳过
            if task.once_per_day and scheduler_task_done(task.name, today):
                task.daily_done = today
                continue

            # ── 多触发点任务 (盘中窗口策略组): 每个 slot 独立触发一次 ──
            #    必须早于下面的 interval 分支: slot 任务不受 interval(86400) 限制,
            #    否则第一个 slot 跑完后第二个 slot 会被「间隔未到」跳过。
            if task.once_per_slot:
                slot = _next_pending_slot(task, now_dt)
                if slot is None:
                    continue
                _launch(task, slot)
                continue

            # 日级定时任务：未到触发时刻 → 跳过 (knife_scan 时刻来自 sched 事实源)
            if task.once_per_day:
                trig = _task_trigger_hm(task)
                if trig and now_dt.strftime("%H:%M") < trig:
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


def _launch(task: Task, slot=None):
    """拉起 worker 线程。slot 不为 None 时只跑该 slot 的策略批。"""
    task.running = True
    task.last_run = _time.time()
    if task.once_per_day:
        task.daily_done = datetime.now().strftime("%Y-%m-%d")

    tag = f"{task.name}@{slot['hm']}" if slot else task.name
    t = threading.Thread(target=_worker, args=(task, slot), daemon=False, name=f"work-{tag}")
    t.start()
    logger.info("[scheduler] → %s (间隔 %ds)", tag, _get_interval(task))


# ═══════════════════════════════════════════════════════════
#  入口
# ═══════════════════════════════════════════════════════════


def start():
    """应用启动时调用。只拉一个调度线程。"""
    logger.info("[scheduler] market_cn 调度器启动 (v4 全 fire-and-forget)")
    logger.info("[scheduler] 任务: %s", ", ".join(t.name for t in TASKS))

    t = threading.Thread(target=_scheduler_loop, daemon=True, name="scheduler")
    t.start()
