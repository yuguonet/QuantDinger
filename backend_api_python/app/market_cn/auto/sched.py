#!/usr/bin/env python3
"""auto/sched.py — 调度配置化 (S 阶段, 2026-09-09)

用途: 把"分段声明 (首次时间→周期→截止)"变成唯一调度事实源。
     策略 ScanSpec (strategies/base.py) 提供默认声明; config.json 的 schedule 段
     按 per-strategy 覆盖。外部 market_cn/scheduler.py 读取本模块驱动
     (接线已完成 2026-09-09 用户批准; run_scan_knife 滚动起点同源读取本模块)。

设计点:
  - expand_times 生成**确切时刻表** (相位锚定 first, 到点触发而非 60s interval 轮询碰点),
    消除现状 _is_dragon_fast_window/_dragon_interval 式硬编码特判;
  - 跨午休 (11:31~12:59) 时刻自动剔除;
  - daily_close 类策略无盘中窗口, 由 run_scan 盘后调度触发, 不在本表。

易错点:
  - end 时刻**含**在表内 (14:50~15:00/60s → 11 拍, 末拍即终审);
  - config.json schedule 段的键须与策略 key 一致, 未声明的策略用 ScanSpec 默认。
"""
from __future__ import annotations

import json
import os

from app.utils.logger import get_logger  # 2026-09-29: 统一全仓 get_logger 口径 (同 events.py)

logger = get_logger(__name__)

_LUNCH_SKIP = ("11:31", "12:59")   # 午休时刻剔除区间 (含端点)


def expand_times(windows, interval_sec, date=None):
    """窗口 (first, end) + 间隔秒 → 确切触发时刻表 ["HH:MM", ...] (相位锚定 first)。

    2026-09-29 审计修复 (P0): interval_sec 必须 >=1 —— 负值/0 会让 `t += timedelta(...)`
    永不前进, `while t <= te` 死循环挂死整个 market_cn 调度线程 (config schedule 段
    是运维公开入口, 一次笔误 -60 即全局停摆)。非法输入一律 raise ValueError, 由
    all_schedules 逐策略兜住跳过, 不拖死其余策略。
    """
    from datetime import datetime, timedelta

    if not windows or len(windows) < 2:
        raise ValueError(f"windows 非法 (需 (first, end)): {windows!r}")
    try:
        interval_sec = int(interval_sec)
    except (TypeError, ValueError) as e:
        raise ValueError(f"interval_sec 非法: {interval_sec!r}") from e
    if interval_sec < 1:
        raise ValueError(f"interval_sec 必须 >=1 秒 (got {interval_sec}); <=0 会死循环")

    first, end = windows[0], windows[1]
    d = date or datetime.now().strftime("%Y-%m-%d")
    t = datetime.strptime(f"{d} {first}", "%Y-%m-%d %H:%M")
    te = datetime.strptime(f"{d} {end}", "%Y-%m-%d %H:%M")
    if te < t:
        raise ValueError(f"窗口 end({end}) 早于 first({first})")
    out = []
    while t <= te:
        hm = t.strftime("%H:%M")
        if not (_LUNCH_SKIP[0] <= hm <= _LUNCH_SKIP[1]):
            out.append(hm)
        t += timedelta(seconds=interval_sec)
    return out


_cfg_cache = {"mtime": None, "data": {}}


def _load_config():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    try:
        mtime = os.path.getmtime(path)
        if _cfg_cache["mtime"] == mtime:
            return _cfg_cache["data"]
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        _cfg_cache.update(mtime=mtime, data=data)
        return data
    except Exception:
        return {}


def resolve_schedule(strategy_key):
    """策略调度声明合并: config.json schedule[strategy_key] 覆盖 ScanSpec 默认。

    返回 {"kind", "windows", "interval_sec"} | None (daily_close 类无盘中窗口)。
    """
    from app.market_cn.auto.strategies import get_strategy

    try:
        st = get_strategy(strategy_key)
        spec = st.scan_spec
    except Exception:
        return None
    if spec.kind != "intraday_window" or not spec.windows:
        return None
    decl = {"kind": spec.kind, "windows": tuple(spec.windows),
            "interval_sec": spec.interval_sec}
    cfg = _load_config().get("schedule", {}).get(strategy_key, {})
    # 2026-09-29 审计修复: config 覆盖须校验 —— 负 interval / 残缺 windows 直接透传
    # 会打穿 expand_times (死循环) 或产出空时刻表; 非法覆盖回退 ScanSpec 默认并告警。
    try:
        if cfg.get("windows"):
            w = tuple(cfg["windows"])
            if len(w) >= 2:
                decl["windows"] = w
            else:
                logger.warning("[sched] %s schedule.windows=%r 非法, 用 ScanSpec 默认",
                               strategy_key, cfg.get("windows"))
        if cfg.get("interval_sec"):
            iv = int(cfg["interval_sec"])
            if iv >= 1:
                decl["interval_sec"] = iv
            else:
                logger.warning("[sched] %s schedule.interval_sec=%r 必须>=1, 用 ScanSpec 默认",
                               strategy_key, cfg.get("interval_sec"))
    except (TypeError, ValueError) as e:
        logger.warning("[sched] %s schedule 覆盖解析失败 (%s), 用 ScanSpec 默认", strategy_key, e)
    return decl


def all_schedules(date=None):
    """全策略当日时刻表: {key: ["HH:MM", ...]} (仅 intraday_window 类)。"""
    from app.market_cn.auto.strategies import autodiscover

    out = {}
    for key in autodiscover():
        decl = resolve_schedule(key)
        if decl:
            try:
                out[key] = expand_times(decl["windows"], decl["interval_sec"], date)
            except ValueError as e:
                # 单策略调度声明非法 → 跳过并告警, 不拖死其余策略的时刻表生成
                logger.error("[sched] %s 时刻表生成失败, 本日跳过: %s", key, e)
    return out


# ================================================================
# daily_close 触发声明 (2026-09-29): 事件依赖 + 不早于时刻, 取代 17:25 硬编码
# ================================================================

DAILY_FALLBACK_FIRE = "18:30"   # 过此时刻: after_events 未齐也放行 (run_scan 仍 wait_data 兜 1D)


def resolve_daily_fire(strategy_key):
    """daily_close 策略触发声明合并: config.json schedule[key] 覆盖 ScanSpec 默认。

    返回 {"after_events": (...), "fire_at": "HH:MM"|"", "kind": "daily_close"}。
    after_events 空 → 默认 ("daily_1d",) (任何日 K 策略的最小依赖)。
    """
    from app.market_cn.auto.strategies import autodiscover, get_strategy

    events = ("daily_1d",)
    fire_at = ""
    try:
        st = get_strategy(strategy_key)
        if st is None:
            autodiscover()
            st = get_strategy(strategy_key)
        if st is not None:
            spec = st.scan_spec
            if spec.after_events:
                events = tuple(spec.after_events)
            fire_at = spec.fire_at or ""
    except Exception as e:
        logger.debug("[sched] resolve_daily_fire(%s) 读 ScanSpec 失败: %s", strategy_key, e)
    cfg = _load_config().get("schedule", {}).get(strategy_key, {})
    if "after_events" in cfg:
        # 显式覆盖: 空列表 = 无事件依赖 (只受 fire_at/fallback 约束)
        events = tuple(cfg.get("after_events") or ())
    if "fire_at" in cfg:
        fire_at = (cfg.get("fire_at") or "").strip()
    return {"after_events": events, "fire_at": fire_at, "kind": "daily_close"}


def enabled_daily_keys():
    """注册表中 enabled 且 kind=daily_close 的策略 key 列表 (升序)。

    autodiscover 按天缓存; is_enabled/kind 每次现查 —— config 开关即时生效。
    """
    from datetime import datetime

    from app.market_cn.auto.strategies import all_strategies, autodiscover, is_enabled

    today = datetime.now().strftime("%Y-%m-%d")
    if enabled_daily_keys.cache["date"] != today:
        autodiscover()
        enabled_daily_keys.cache.update(
            date=today, keys=sorted(all_strategies().keys()))
    out = []
    for key in enabled_daily_keys.cache["keys"]:
        try:
            st = all_strategies().get(key)
            if st is not None and is_enabled(key) and st.scan_spec.kind == "daily_close":
                out.append(key)
        except Exception:
            continue
    return out


enabled_daily_keys.cache = {"date": "", "keys": []}


def daily_fire_ready(strategy_key, now_hm=None, date=None):
    """策略现在是否可触发盘后扫描。返回 (ready: bool, reason: str)。

    判定链: ① fire_at 未到 → 等钟点; ② after_events 未齐 → 等数据
            (过 DAILY_FALLBACK_FIRE 放行, 打印缺失集); ③ 就绪。
    已扫描去重不在本函数 — 归 scheduler 的 qd_scheduler_done。
    """
    from datetime import datetime

    from app.market_cn.auto.events import missing_events

    now_hm = now_hm or datetime.now().strftime("%H:%M")
    today = datetime.now().strftime("%Y-%m-%d")
    date = date or today
    decl = resolve_daily_fire(strategy_key)
    fire_at = decl["fire_at"]
    # 2026-09-29 审计修复 (P1): 目标日早于今天 = 补跑模式 —— 钟点/兜底时刻早已过去,
    # 不得再拿"今天"的墙钟卡 fire_at/DAILY_FALLBACK_FIRE, 否则 T 日缺事件时
    # 早盘一直 wait、15:00 target 翻转后 T 永久漏扫。
    backfill = date < today
    if fire_at and not backfill and now_hm < fire_at:
        return False, f"wait clock {fire_at}"
    missing = missing_events(decl["after_events"], date)
    if not missing:
        return True, f"events ok {list(decl['after_events'])}"
    if backfill or now_hm >= DAILY_FALLBACK_FIRE:
        return True, f"fallback {DAILY_FALLBACK_FIRE}, missing={missing}"
    return False, f"wait events {missing}"


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="打印策略分段时刻表")
    parser.add_argument("--date", default="", help="YYYY-MM-DD, 默认今天")
    args = parser.parse_args()
    for key, times in all_schedules(args.date or None).items():
        print(f"{key}: {len(times)} 拍  {times[0]} → {times[-1]}")
        print(f"  {times}")
