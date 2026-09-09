#!/usr/bin/env python3
"""auto/tools/bench_scan.py — 盘中扫描 50s 预算基准 (E 阶段)

用途: 计时单轮盘中扫描的原子步骤链 (快照拉取 → 策略预筛 → 候选补数据+完整判定),
     与 50s 预算对照。不落库、不等时间窗, 盘后也可跑 (快照取最近一日)。
     不达标时才考虑多进程分片 (设计文档 §7: 多级预筛优先)。

用法: python -m app.market_cn.auto.tools.bench_scan
"""
from __future__ import annotations

import os
import time


def main():
    try:
        from dotenv import load_dotenv
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))), ".env")
        if os.path.isfile(p):
            load_dotenv(p, override=False)
    except Exception:
        pass

    from app.market_cn.auto import store, strategies as strat_reg
    from app.market_cn.auto.data.hub import all_codes, market_snapshot, stock_info

    strat_reg.autodiscover()
    active = {k: s for k, s in strat_reg.all_strategies().items()
              if strat_reg.is_enabled(k) and s.scan_spec.kind == "intraday_window"}
    if not active:
        print("无 intraday_window 策略")
        return

    t0 = time.time()
    codes = all_codes()
    t1 = time.time()
    snaps = market_snapshot(codes)
    t2 = time.time()

    # 市场均涨幅 (与 scan._mkt_gain 同口径)
    gains = []
    for s in snaps.values():
        try:
            last, pc = float(s.get("last") or 0), float(s.get("previousClose") or 0)
        except (TypeError, ValueError):
            continue
        if last > 0 and pc > 0:
            gains.append((last / pc - 1) * 100)
    mkt = sum(gains) / len(gains) if gains else 0.0

    # 各策略预筛 (必要条件超集)
    shortlists = {}
    for key, strat in active.items():
        params = strat_reg.params_override(key)
        shortlists[key] = strat.intraday_shortlist(snaps, mkt, **params)
    t3 = time.time()

    # 候选补数据 + 完整判定 (扫描的核心成本)
    n_judged = 0
    try:
        stock_info = stock_info()
    except Exception:
        stock_info = {}
    for key, strat in active.items():
        params = strat_reg.params_override(key)
        for code in shortlists[key]:
            from app.market_cn.auto.data.hub import daily, day_series
            bars = daily(code, days=60)
            series = day_series([code]).get(code) or []
            try:
                strat.scan_signals(bars, code, ctx={
                    "latest": shortlists[key][code], "series": series,
                    "mkt_gain": mkt}, **params)
                n_judged += 1
            except Exception:
                pass
    t4 = time.time()

    print(f"股票池: {len(codes)} | 快照: {len(snaps)} 只")
    print(f"步骤耗时:")
    print(f"  股票池加载    {t1 - t0:6.1f}s")
    print(f"  全市场快照    {t2 - t1:6.1f}s")
    for key in shortlists:
        print(f"  预筛[{key}]  {t3 - t2:6.1f}s  通过 {len(shortlists[key])}/{len(snaps)}")
    print(f"  候选判定      {t4 - t3:6.1f}s  ({n_judged} 股)")
    total = t4 - t0
    print(f"总计: {total:.1f}s / 预算 50s → {'达标' if total <= 50 else '超预算, 考虑预筛加严或多进程分片'}")


if __name__ == "__main__":
    main()
