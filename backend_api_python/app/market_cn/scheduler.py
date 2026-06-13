"""
market_cn 数据刷新调度器

职责分离：
  - refresh_xxx()  — 各模块自己定义（china_market / index / emotion / ...）
  - _xxx_tick()     — 本文件，调度层，控制时段和条件
  - _schedule()     — Timer 自调度，只管间隔

三档刷新:
  - 快档: 盘中 5 分钟
  - 慢档: 盘中 30 分钟（含日级宏观/板块/国际）
  - 盘后: 非盘中 10 分钟（对比 last_finish_trading_day，到了就停）

盘中时段: 9:00~11:31, 13:00~15:01
"""

import threading
import logging
import time as _time
import os as _os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

TZ_CN = timezone(timedelta(hours=8))
_adj_running = True


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


def _refresh_daily():
    """日级数据: 宏观/板块趋势/北向持股/国际宏观（盘中也能更新）"""
    from app.market_cn.china_market import (
        refresh_sector_trend,
        refresh_sector_prediction, refresh_sector_cycle,
        refresh_sector_history,
    )
    from app.market_cn.index import (
        refresh_index_daily_kline, refresh_northbound_holdings,
    )
#    from app.data_providers.sentiment import (
#        refresh_vix, refresh_dollar_index, refresh_yield_curve,
#        refresh_put_call_ratio,
#    )
    from app.data_providers.commodities import refresh_commodities
    from app.data_providers.forex import refresh_forex_pairs

    _run_all("daily", [
        refresh_sector_trend,
        refresh_sector_prediction, refresh_sector_cycle,
        refresh_sector_history,
        refresh_index_daily_kline, refresh_northbound_holdings,
#        refresh_vix, refresh_dollar_index, refresh_yield_curve, refresh_put_call_ratio, 
        refresh_commodities, refresh_forex_pairs,
    ])


def _refresh_post_market():
    """盘后数据: 龙虎榜/北向日级/情绪历史/资金流日级（非盘中才跑）"""
    from app.market_cn.dragon_limit import refresh_dragon_tiger
    from app.market_cn.index import refresh_northbound_daily, refresh_market_fund_flow_daily
    from app.market_cn.emotion import refresh_emotion_cycle

    _run_all("post_market", [
        refresh_dragon_tiger, refresh_northbound_daily,
        refresh_market_fund_flow_daily, refresh_emotion_cycle,
    ])


def _refresh_slow():
    """盘中慢档: 贪恐/情绪/政策/新闻/全球情绪/加密 + 日级"""
    from app.market_cn.china_market import refresh_fear_greed, refresh_policy
    from app.market_cn.emotion import refresh_emotion_cycle
#    from app.market_cn.policy_analysis import refresh_financial_news, refresh_macro_news
#    from app.data_providers.sentiment import refresh_fear_greed_index, refresh_sentiment_data
    from app.data_providers.global_market import refresh_global_sentiment, refresh_global_news
#    from app.data_providers.crypto import refresh_crypto_prices, refresh_crypto_heatmap

    _run_all("slow", [
        refresh_fear_greed, refresh_policy, refresh_emotion_cycle,
#        refresh_financial_news, refresh_macro_news,
#        refresh_fear_greed_index, refresh_sentiment_data,
        refresh_global_sentiment, refresh_global_news,
#        refresh_crypto_prices, refresh_crypto_heatmap,
    ])

    # 日级数据（盘中也能更新）也放到慢档
    _refresh_daily()


def _refresh_fast():
    """盘中快档: 指数实时/北向实时/资金流/热门板块/人气/全球指数"""
    from app.market_cn.index import (
        refresh_index_realtime, refresh_northbound_realtime,
        refresh_market_fund_flow_realtime, refresh_sector_fund_flow,
    )
    from app.market_cn.china_market import refresh_hot_sectors
    from app.market_cn.dragon_limit import refresh_hot_rank
    from app.data_providers.global_market import refresh_global_indices, refresh_global_heatmap

    _run_all("fast", [
        refresh_index_realtime, refresh_northbound_realtime,
        refresh_market_fund_flow_realtime, refresh_sector_fund_flow,
        refresh_hot_sectors, refresh_hot_rank,
        refresh_global_indices, refresh_global_heatmap,
    ])


# ═══════════════════════════════════════════════════════════
#  调度层 — tick 函数统一控制时段和条件
# ═══════════════════════════════════════════════════════════


def _fast_tick():
    if _is_trading_time():
        _refresh_fast()


def _slow_tick():
    if _is_trading_time():
        _refresh_slow()


_post_market_done_today = False


def _post_market_tick():
    global _post_market_done_today
    if _post_market_done_today or _is_trading_time():
        return

    from app.utils.trading_calendar import last_finish_trading_day
    target = last_finish_trading_day()

    _refresh_post_market()

    # 用关键数据源的日期判断是否已更新到目标日
    from app.market_cn.dragon_limit import _rt_dragon_tiger
    from app.market_cn.index import _rt_nb_daily

    # _rt_dragon_tiger 是 list[dict]，取第一条的 date
    dt_date = ""
    if _rt_dragon_tiger and isinstance(_rt_dragon_tiger, list) and len(_rt_dragon_tiger) > 0:
        dt_date = _rt_dragon_tiger[0].get("date", "") if isinstance(_rt_dragon_tiger[0], dict) else ""
    elif isinstance(_rt_dragon_tiger, dict):
        dt_date = _rt_dragon_tiger.get("date", "")

    # _rt_nb_daily 是 list[dict]，取最后一条的 date（最新日期）
    nb_date = ""
    if _rt_nb_daily and isinstance(_rt_nb_daily, list) and len(_rt_nb_daily) > 0:
        last = _rt_nb_daily[-1]
        nb_date = last.get("date", "") if isinstance(last, dict) else ""

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
    定时器立即启动，按各自间隔逐步填充数据。
    """
    logger.info("[scheduler] market_cn 调度器启动")

    # 定时器立即注册（不阻塞主线程）
    _schedule("fast", _fast_tick, 300)
    _schedule("slow", _slow_tick, 1800)
    _schedule("post_market", _post_market_tick, 600)

    logger.info("[scheduler] 定时刷新已启动: fast=5min, slow=30min, post_market=10min")

    # 冷启动：后台线程拉取，不阻塞主线程
    def _cold_start():
        logger.info("[scheduler] 冷启动: 后台拉取全部数据")
        _refresh_daily()
        _refresh_post_market()
        _refresh_slow()
        _refresh_fast()
        logger.info("[scheduler] 冷启动完成，数据已加载到内存")

    t = threading.Thread(target=_cold_start, daemon=True)
    t.start()

    # 前复权因子全量更新（交易日 6:00）
    threading.Thread(target=_schedule_adj_update, daemon=True, name="adj-factors-scheduler").start()
