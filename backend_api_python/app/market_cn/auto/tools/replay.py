#!/usr/bin/env python3
"""auto/tools/replay.py — 单股单策略重放调试器 (F 阶段; P4 扩展盘中策略历史复现)

用途: 规则调试入口——给定股票代码+策略, 重放历史信号与逐笔出场,
     打印逐笔明细 (判定特征+出场原因), 供 AI/人工分析落选与胜率归因。
     与回测流水线同一钩子/同一引擎 (注册表分发), 保证所见即所得。

用法:
  python -m app.market_cn.auto.tools.replay --strategy dragon --code 000859
  python -m app.market_cn.auto.tools.replay --strategy v1 --code 000021 --days 400
  python -m app.market_cn.auto.tools.replay --strategy tail_oversold --code 000032 \
      --start-date 2026-07-15 --end-date 2026-07-21        # 盘中策略: 逐触发时间线复现
"""
from __future__ import annotations

import argparse
import json
import os


def main():
    parser = argparse.ArgumentParser(description="单股单策略重放调试")
    parser.add_argument("--strategy", required=True,
                        help="任意已注册策略 key (dragon/v1/break/tail_oversold/knife_catch/...)")
    parser.add_argument("--code", required=True)
    parser.add_argument("--days", type=int, default=300)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--json", action="store_true", help="输出原始 JSON (供 AI 统计)")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))), ".env")
        if os.path.isfile(p):
            load_dotenv(p, override=False)
    except Exception:
        pass

    from app.market_cn.auto import strategies as strat_reg
    strat_reg.autodiscover()
    key = {"dragon": "dragon_callback"}.get(args.strategy, args.strategy)  # 旧CLI名别名
    strat = strat_reg.get_strategy(key)
    if strat is None:
        print(f"策略 {key} 未注册 (可用: {sorted(strat_reg.all_strategies())})")
        return

    if strat.scan_spec.kind == "intraday_window":
        _replay_intraday(strat, args)
        return

    from app.market_cn.auto.data.hub import daily, stock_info

    try:
        stock_info = stock_info().get(args.code)
    except Exception:
        stock_info = None
    bars = daily(args.code, args.days)
    if not bars:
        print(f"{args.code}: 无K线数据")
        return
    print(f"{args.code} {args.strategy} | {len(bars)} 根 "
          f"({bars[0]['time']} ~ {bars[-1]['time']})")

    trades = strat.backtest_stock(bars, args.code, stock_info=stock_info) or []

    if args.json:
        print(json.dumps(trades, ensure_ascii=False, indent=1))
        return
    if not trades:
        print("无信号。")
        return
    for t in trades:
        print(f"  信号 {t.get('signal_date', '?')} → 买入 {t['entry_date']}@"
              f"{t['entry_price']} → 出场 {t.get('exit_day', '?')}日 "
              f"{t.get('exit_reason', '?')} @{t.get('exit_price')} "
              f"| 收益 {t['return_pct']:.2f}% 峰值 {t.get('peak_return_pct', '-')}%")
    rets = [t["return_pct"] for t in trades]
    wins = [r for r in rets if r > 0]
    print(f"共 {len(trades)} 笔 | 胜率 {len(wins)/len(trades)*100:.1f}% "
          f"| 均收 {sum(rets)/len(rets):.2f}%")


def _replay_intraday(strat, args):
    """盘中策略历史复现: 逐日 × 逐成交触发, 打印预筛/判定/信号全轨迹 (P4)。

    与回测引擎同一帧层+同一判定路径 (快照帧全市场构建, 复用缓存);
    单股回看 --days 自然日内的历史 (或 --start-date/--end-date 显式窗口)。
    """
    from datetime import datetime, timedelta
    from app.market_cn.auto.data import frames as fr
    from app.market_cn.auto.data.hub import daily
    from app.market_cn.auto.backtest import _exec_trigger_mis

    end = args.end_date or datetime.now().strftime("%Y-%m-%d")
    start = args.start_date or (datetime.strptime(end, "%Y-%m-%d")
                                - timedelta(days=args.days)).strftime("%Y-%m-%d")
    dates = [d for d in fr.trading_dates(days_back=(datetime.strptime(end, "%Y-%m-%d")
                                                    - datetime.strptime(start, "%Y-%m-%d")).days + 1,
                                         end=end) if d >= start]
    first_1m = fr.first_1m_date()
    if first_1m:
        dates = [d for d in dates if d >= first_1m]
    mis = _exec_trigger_mis(strat.scan_spec)
    if not dates or not mis:
        print(f"无可复现区间 (dates={len(dates)} triggers={mis})")
        return
    print(f"{args.code} {strat.name} 时间线复现 {dates[0]}~{dates[-1]} "
          f"| 触发 {[fr.MI_HHMM[m] for m in mis]}")

    pc_map = fr.prev_closes(dates[0])
    events = []
    for di, date in enumerate(dates):
        frame = fr.build_frame(date)
        prev_date = dates[di - 1] if di > 0 else date
        for mi in mis:
            snap = frame.snap(args.code, mi, pc_map.get(args.code))
            if snap is None:
                continue
            mkt = frame.mkt_gain(mi, pc_map)
            short = strat.intraday_shortlist({args.code: snap}, mkt)
            sigs = []
            if short:
                bars = daily(args.code, 300, as_of=prev_date)
                sigs = strat.scan_signals(
                    bars, args.code,
                    ctx={"latest": snap, "series": frame.series(args.code, mi),
                         "mkt_gain": mkt}) or []
            events.append({"date": date, "trigger": fr.MI_HHMM[mi],
                           "shortlist": bool(short), "mkt_gain": round(mkt, 2),
                           "last": snap["last"], "signals": [
                               {"score": s.score, "label": s.label, **(s.extra or {})}
                               for s in sigs]})
        for c in frame.codes:
            lc = frame.last_close(c)
            if lc > 0:
                pc_map[c] = lc

    if args.json:
        print(json.dumps(events, ensure_ascii=False, indent=1))
        return
    hits = [e for e in events if e["signals"]]
    for e in hits:
        s = e["signals"][0]
        print(f"  {e['date']} {e['trigger']} last={e['last']} mkt={e['mkt_gain']}% "
              f"→ {s['label']}")
    print(f"触发评估 {len(events)} 次 | 预筛通过 "
          f"{sum(1 for e in events if e['shortlist'])} | 信号 {len(hits)}")


if __name__ == "__main__":
    main()
