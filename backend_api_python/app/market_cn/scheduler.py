"""
market_cn 数据刷新调度器

职责分离：
  - refresh_xxx()  — 各模块自己定义（china_market / index / emotion / ...）
  - _xxx_tick()     — 本文件，调度层，控制时段和条件
  - _schedule()     — Timer 自调度，只管间隔

四档刷新:
  - 快档: 盘中 5 分钟（实时行情）
  - 慢档: 盘中 30 分钟（贪恐/情绪/全球）
  - 日档: 交易日 9:00 后跑一次（板块/北向持股/指数日线）
  - 盘后: 非盘中 10 分钟（龙虎榜/北向日级/资金流日级/板块统计）

盘中时段: 9:00~11:31, 13:00~15:01
"""

import threading
import logging
import time as _time
import os as _os
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

TZ_CN = timezone(timedelta(hours=8))
_adj_running = True

# ── 防重复/防堆叠 状态 ──
_daily_last_date = ""        # 日档: 记录已刷新的日期，一天只跑一次
_emotion_last_ts = 0.0        # 情绪: 最近一次刷新时间戳，防30分钟内重复
_EMOTION_MIN_INTERVAL = 1800  # 情绪数据最小刷新间隔(秒)
_cold_start_done = threading.Event()  # 冷启动完成信号，防止定时器提前触发


# ═══════════════════════════════════════════════════════════
#  时段判断
# ═══════════════════════════════════════════════════════════


def _is_trading_time():
    """判断是否在盘中时段（9:00-11:31, 13:00-15:01）"""
    from datetime import datetime
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 100 + now.minute
    return (900 <= t <= 1131) or (1300 <= t <= 1501)


# ═══════════════════════════════════════════════════════════
#  分组调用（编排各模块的 refresh_xxx）
# ═══════════════════════════════════════════════════════════


def _run_all(tag, fns):
    """批量执行 refresh 函数，逐个 try/except 兜底"""
    for fn in fns:
        try:
            fn()
        except Exception as e:
            logger.warning("[%s] %s 失败: %s", tag, fn.__name__, e)


def _run_all_parallel(tag, fns, max_workers=8):
    """并行执行 refresh 函数，每个独立 try/except 兜底"""
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fn): fn.__name__ for fn in fns}
        for f in as_completed(futures):
            name = futures[f]
            try:
                f.result()
            except Exception as e:
                logger.warning("[%s] %s 失败: %s", tag, name, e)


def _refresh_daily():
    """日级数据: 指数日线/北向持股（板块数据由 post_market → collect_sector_daily 写 DB）"""
    from app.market_cn.index import (
        refresh_index_daily_kline, refresh_northbound_holdings,
    )

    _run_all("daily", [
        refresh_index_daily_kline, refresh_northbound_holdings,
    ])


def _refresh_emotion_safe():
    """带去重的情绪数据刷新（防止多处重复调用 refresh_emotion_cycle）"""
    global _emotion_last_ts
    import time
    now = time.time()
    if now - _emotion_last_ts < _EMOTION_MIN_INTERVAL:
        return
    try:
        from app.market_cn.emotion import refresh_emotion_cycle
        refresh_emotion_cycle()
        _emotion_last_ts = now
    except Exception as e:
        logger.warning("[scheduler] refresh_emotion_cycle 失败: %s", e)


def _refresh_post_market():
    """盘后数据: 龙虎榜/北向日级/资金流日级/板块统计（非盘中才跑）"""
    from app.market_cn.dragon_limit import refresh_dragon_tiger
    from app.market_cn.index import refresh_northbound_daily, refresh_market_fund_flow_daily
    from app.market_cn.sector_history import collect_sector_daily

    _run_all("post_market", [
        refresh_dragon_tiger, refresh_northbound_daily,
        refresh_market_fund_flow_daily,
        collect_sector_daily,
    ])
    # 情绪数据独立去重，避免与 slow 重复
    _refresh_emotion_safe()


def _refresh_slow():
    """盘中慢档: 贪恐/情绪/全球（不含日级，日级由 _daily_tick 独立管理）"""
    from app.market_cn.china_market import refresh_fear_greed
    from app.data_providers.global_market import refresh_global_sentiment

    _run_all("slow", [
        refresh_fear_greed,
        refresh_global_sentiment,
    ])
    # 情绪数据独立去重
    _refresh_emotion_safe()


def _refresh_fast():
    """盘中快档: 指数实时/北向实时/资金流/热门板块/人气/全球指数"""
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


# ═══════════════════════════════════════════════════════════
#  涨跌停池 — 自适应间隔
#  9:30~10:00  → 60 秒（开盘密集变动期）
#  其它盘中    → 300 秒
#  非盘中      → 不执行
# ═══════════════════════════════════════════════════════════

_DRAGON_FAST_START = 930    # 9:30
_DRAGON_FAST_END   = 1000   # 10:00
_DRAGON_FAST_SEC   = 60     # 1 分钟
_DRAGON_SLOW_SEC   = 300    # 5 分钟


def _is_dragon_fast_window():
    """9:30~10:00 开盘密集变动期"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 100 + now.minute
    return _DRAGON_FAST_START <= t < _DRAGON_FAST_END


def _refresh_dragon_pools():
    """刷新涨跌停三池"""
    from app.market_cn.dragon_limit import refresh_zt_pool, refresh_dt_pool, refresh_broken_board
    _run_all("dragon_pools", [refresh_zt_pool, refresh_dt_pool, refresh_broken_board])


def _dragon_tick():
    """涨跌停池自适应调度"""
    _cold_start_done.wait()  # 冷启动完成前不执行
    if _is_trading_time():
        try:
            _refresh_dragon_pools()
        except Exception as e:
            logger.error("[scheduler] dragon_pools 异常: %s", e)

    interval = _DRAGON_FAST_SEC if _is_dragon_fast_window() else _DRAGON_SLOW_SEC
    _schedule("dragon_pools", _dragon_tick, interval)


# ═══════════════════════════════════════════════════════════
#  调度层 — tick 函数统一控制时段和条件
# ═══════════════════════════════════════════════════════════


def _fast_tick():
    global _post_market_done_today, _daily_last_date
    _cold_start_done.wait()  # 冷启动完成前不执行
    if _is_trading_time():
        _post_market_done_today = False  # 盘中开始，重置盘后标志
        _refresh_fast()
    # 日档: 交易日 9:00 后只跑一次
    _daily_tick()


def _slow_tick():
    _cold_start_done.wait()  # 冷启动完成前不执行
    if _is_trading_time():
        _refresh_slow()


_policy_last_date = ""


def _policy_daily_tick():
    """政策新闻: 交易日 9:00 后跑一次"""
    global _policy_last_date
    now = datetime.now()
    if now.weekday() >= 5:
        return
    today = now.strftime("%Y-%m-%d")
    if now.hour < 9 or _policy_last_date == today:
        return
    try:
        from app.market_cn.china_market import refresh_policy
        refresh_policy()
        _policy_last_date = today
        logger.info("[scheduler] 政策新闻每日刷新完成")
    except Exception as e:
        logger.warning("[scheduler] 政策新闻刷新失败: %s", e)


def _daily_tick():
    """日档数据: 交易日 9:00 后只跑一次（板块/北向持股/指数日线）"""
    global _daily_last_date
    now = datetime.now()
    if now.weekday() >= 5:
        return
    today = now.strftime("%Y-%m-%d")
    if now.hour < 9 or _daily_last_date == today:
        return
    _refresh_daily()
    _daily_last_date = today
    logger.info("[scheduler] 日档数据刷新完成")


_post_market_done_today = False


def _post_market_tick():
    global _post_market_done_today
    _cold_start_done.wait()  # 冷启动完成前不执行
    if _post_market_done_today or _is_trading_time():
        return

    from app.utils.trading_calendar import last_finish_trading_day
    target = last_finish_trading_day()

    _refresh_post_market()

    # 通过公开 API 获取数据日期，不直接读模块内部 _rt_* 变量
    dt_date = ""
    nb_date = ""
    try:
        from app.market_cn.dragon_limit import get_dragon_tiger
        dt = get_dragon_tiger()
        if dt and isinstance(dt, list) and len(dt) > 0:
            dt_date = dt[0].get("date", "") if isinstance(dt[0], dict) else ""
        elif isinstance(dt, dict):
            dt_date = dt.get("date", "")
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
        _post_market_done_today = True
        logger.info("[scheduler] 盘后刷新完成 (dt=%s, nb=%s, 目标=%s)", dt_date, nb_date, target)
    else:
        logger.info("[scheduler] 盘后数据未到 (dt=%s, nb=%s, 目标≥%s)，下次重试",
                    dt_date, nb_date, target)


# ═══════════════════════════════════════════════════════════
#  Timer 自调度
# ═══════════════════════════════════════════════════════════


_timers = {}


def _schedule(name, fn, interval):
    """Timer 自调度：interval 秒后执行 fn，执行完再调度下一次"""
    def _run():
        try:
            fn()
        except Exception as e:
            logger.error("[scheduler] %s 异常: %s", name, e)
        t = threading.Timer(interval, _run)
        t.daemon = True
        t.start()
        _timers[name] = t

    t = threading.Timer(interval, _run)
    t.daemon = True
    t.start()
    _timers[name] = t


# ═══════════════════════════════════════════════════════════
#  前复权因子自动更新
# ═══════════════════════════════════════════════════════════


def _schedule_adj_update():
    """交易日 6:00 全量更新前复权因子（失败不重试）。"""
    while _adj_running:
        now = datetime.now(TZ_CN)
        today_str = now.strftime("%Y-%m-%d")

        from app.utils.trading_calendar import is_trading_day, next_trading_day

        if is_trading_day(today_str):
            target = now.replace(hour=6, minute=0, second=0, microsecond=0)
            if now < target:
                wait = (target - now).total_seconds()
                logger.info(f"[复权因子] 今日交易日，等待 {wait:.0f}s 至 06:00")
                _time.sleep(wait)

            if not _adj_running:
                break

            try:
                from app.data_sources.provider.adjustment import update_all_factors
                count = update_all_factors()
                logger.info(f"[复权因子] 全量更新完成: {count} 只股票")
            except Exception as e:
                logger.error(f"[复权因子] 更新失败: {e}")

            # 等到下一天再检查
            next_day = target + timedelta(days=1)
            _time.sleep((next_day - datetime.now(TZ_CN)).total_seconds())
        else:
            next_td = next_trading_day(today_str)
            dt_obj = datetime.strptime(next_td, "%Y-%m-%d").replace(hour=6, minute=0, tzinfo=TZ_CN)
            wait = (dt_obj - now).total_seconds()
            logger.info(f"[复权因子] 非交易日，等待至 {next_td} 06:00 ({wait:.0f}s)")
            _time.sleep(wait)



# ═══════════════════════════════════════════════════════════
#  入口
# ═══════════════════════════════════════════════════════════


def start():
    """应用启动时调用（在 Flask app.run 之前或 after_fork）

    冷启动：后台线程拉取，不阻塞主线程。
    定时器立即启动，但需等待冷启动完成才执行（防止重复拉取）。
    """
    logger.info("[scheduler] market_cn 调度器启动")

    # 定时器立即注册（不阻塞主线程），但 tick 内部会 wait 冷启动完成
    _schedule("fast", _fast_tick, 300)        # 5 分钟，非盘中自动跳过
    _schedule("slow", _slow_tick, 1800)       # 30 分钟，非盘中自动跳过
    _schedule("dragon_pools", _dragon_tick, 60)  # 自适应间隔，首次 60 秒后启动
    _schedule("post_market", _post_market_tick, 600)  # 盘后 10 分钟，完成后自动停
    _schedule("policy_daily", _policy_daily_tick, 300)  # 政策新闻: 5 分钟轮询，9:00 后触发一次

    logger.info("[scheduler] 定时刷新已启动: fast=5min, slow=30min, dragon=adaptive, post_market=10min, policy=9:00")

    # 冷启动：后台线程拉取，不阻塞主线程
    def _cold_start():
        logger.info("[scheduler] 冷启动: 后台并行拉取全部数据")
        import time as _t
        t0 = _t.time()

        # ── 冷启动专用快档（不含 global_indices/global_heatmap，避免引入 crypto）──
        def _cold_fast():
            from app.market_cn.index import (
                refresh_index_realtime, refresh_northbound_realtime,
                refresh_market_fund_flow_realtime, refresh_sector_fund_flow,
            )
            from app.market_cn.china_market import refresh_hot_sectors
            from app.market_cn.dragon_limit import refresh_hot_rank
            _run_all("cold_fast", [
                refresh_index_realtime, refresh_northbound_realtime,
                refresh_market_fund_flow_realtime, refresh_sector_fund_flow,
                refresh_hot_sectors, refresh_hot_rank,
            ])

        # ── 冷启动: 涨跌停池（非盘中也加载一次）──
        def _cold_dragon():
            _refresh_dragon_pools()

        # ── 冷启动专用慢档（不含 _refresh_daily，避免与 daily 组重复）──
        def _cold_slow():
            from app.market_cn.china_market import refresh_fear_greed
            _run_all("cold_slow", [refresh_fear_greed])
            # 情绪数据走统一去重入口
            _refresh_emotion_safe()

        # 日档数据（冷启动时标记为已刷新，防止定时器重复拉取）
        def _cold_daily():
            global _daily_last_date
            _refresh_daily()
            _daily_last_date = datetime.now().strftime("%Y-%m-%d")

        # 5 组并行执行
        _run_all_parallel("cold", [
            _cold_daily,
            _refresh_post_market,
            _cold_slow,
            _cold_fast,
            _cold_dragon,
        ], max_workers=5)

        # 冷启动期间的情绪刷新时间戳标记（防止定时器立即重跑）
        global _emotion_last_ts
        import time
        _emotion_last_ts = time.time()

        elapsed = _t.time() - t0
        logger.info("[scheduler] 冷启动完成，耗时 %.1fs", elapsed)
        _cold_start_done.set()  # 通知所有定时器: 冷启动已完成

    t = threading.Thread(target=_cold_start, daemon=True)
    t.start()

    # 前复权因子全量更新（交易日 6:00）
    threading.Thread(target=_schedule_adj_update, daemon=True, name="adj-factors-scheduler").start()
