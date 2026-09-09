#!/usr/bin/env python3
"""auto/tools/replay.py — 单股单策略重放调试器 (F 阶段)

用途: 规则调试入口——给定股票代码+策略, 重放全历史信号与逐笔出场,
     打印逐笔明细 (判定特征+出场原因), 供 AI/人工分析落选与胜率归因。
     与回测流水线同一 backtest_stock 钩子 (注册表分发), 保证所见即所得。

用法:
  python -m app.market_cn.auto.tools.replay --strategy dragon --code 000859
  python -m app.market_cn.auto.tools.replay --strategy v1 --code 000021 --days 400
"""
from __future__ import annotations

import argparse
import json
import os


def main():
    parser = argparse.ArgumentParser(description="单股单策略重放调试")
    parser.add_argument("--strategy", required=True,
                        help="任意已注册策略 key (dragon/v1/break/...)")
    parser.add_argument("--code", required=True)
    parser.add_argument("--days", type=int, default=300)
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

    # 经注册表分发 (与回测流水线同一 backtest_stock 钩子, 所见即所得)
    strat_reg.autodiscover()
    key = {"dragon": "dragon_callback"}.get(args.strategy, args.strategy)  # 旧CLI名别名
    strat = strat_reg.get_strategy(key)
    if strat is None:
        print(f"策略 {key} 未注册 (可用: {sorted(strat_reg.all_strategies())})")
        return
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


if __name__ == "__main__":
    main()
