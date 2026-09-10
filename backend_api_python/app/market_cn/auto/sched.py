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

_LUNCH_SKIP = ("11:31", "12:59")   # 午休时刻剔除区间 (含端点)


def expand_times(windows, interval_sec, date=None):
    """窗口 (first, end) + 间隔秒 → 确切触发时刻表 ["HH:MM", ...] (相位锚定 first)。"""
    from datetime import datetime, timedelta

    first, end = windows[0], windows[1]
    d = date or datetime.now().strftime("%Y-%m-%d")
    t = datetime.strptime(f"{d} {first}", "%Y-%m-%d %H:%M")
    te = datetime.strptime(f"{d} {end}", "%Y-%m-%d %H:%M")
    out = []
    while t <= te:
        hm = t.strftime("%H:%M")
        if not (_LUNCH_SKIP[0] <= hm <= _LUNCH_SKIP[1]):
            out.append(hm)
        t += timedelta(seconds=interval_sec)
    return out


def _load_config():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
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
    if cfg.get("windows"):
        decl["windows"] = tuple(cfg["windows"])
    if cfg.get("interval_sec"):
        decl["interval_sec"] = int(cfg["interval_sec"])
    return decl


def all_schedules(date=None):
    """全策略当日时刻表: {key: ["HH:MM", ...]} (仅 intraday_window 类)。"""
    from app.market_cn.auto.strategies import autodiscover

    out = {}
    for key in autodiscover():
        decl = resolve_schedule(key)
        if decl:
            out[key] = expand_times(decl["windows"], decl["interval_sec"], date)
    return out


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="打印策略分段时刻表")
    parser.add_argument("--date", default="", help="YYYY-MM-DD, 默认今天")
    args = parser.parse_args()
    for key, times in all_schedules(args.date or None).items():
        print(f"{key}: {len(times)} 拍  {times[0]} → {times[-1]}")
        print(f"  {times}")
