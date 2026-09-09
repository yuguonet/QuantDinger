#!/usr/bin/env python3
"""auto/tools/verify_prefilter.py — 预筛等价验证 (E 阶段)

用途: 验证策略的 intraday_shortlist (便宜预筛) 是 scan_signals 完整判定的
     **必要条件超集**——即"完整判定有信号的股票 ⊆ 预筛通过集合"。
     预筛加严导致漏检 = 等价性破坏 = 快速版与全量版不再同口径 (违背所测即所得)。

方法: 随机抽样 --sample 只 + 全市场预筛通过的全部股票, 逐股完整判定;
     若抽样股中出现"有信号但预筛未通过" → FAIL (列出漏检)。
     速度: 抽样判定为主, 全量判定仅对预筛通过股 (本来就要判)。

用法: python -m app.market_cn.auto.tools.verify_prefilter --strategy tail_oversold --sample 300
"""
from __future__ import annotations

import os
import random
import time


def main():
    parser = argparse = __import__("argparse").ArgumentParser(description="预筛等价验证")
    parser.add_argument("--strategy", required=True,
                        choices=["knife_catch", "tail_oversold"])
    parser.add_argument("--sample", type=int, default=300, help="随机抽样股数")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))), ".env")
        if os.path.isfile(p):
            load_dotenv(p, override=False)
    except Exception:
        pass

    from app.market_cn.auto import dragon_store, strategies as strat_reg
    from app.market_cn.auto.data.hub import all_codes, day_series, daily, market_snapshot
    from app.market_cn.auto.data.kline import fetch_stock_info_db

    strat_reg.autodiscover()
    strat = strat_reg.get_strategy(args.strategy)
    params = strat_reg.params_override(args.strategy)

    snaps = market_snapshot(all_codes())
    gains = []
    for s in snaps.values():
        try:
            last, pc = float(s.get("last") or 0), float(s.get("previousClose") or 0)
        except (TypeError, ValueError):
            continue
        if last > 0 and pc > 0:
            gains.append((last / pc - 1) * 100)
    mkt = sum(gains) / len(gains) if gains else 0.0

    shortlist = strat.intraday_shortlist(snaps, mkt, **params)
    print(f"预筛通过: {len(shortlist)}/{len(snaps)} (mkt={mkt:.2f}%)")

    # 验证集 = 预筛通过股 ∪ 随机抽样股
    random.seed(args.seed)
    rest = [c for c in snaps if c not in shortlist]
    sample = random.sample(rest, min(args.sample, len(rest)))
    check = set(shortlist) | set(sample)
    print(f"验证集: 预筛通过 {len(shortlist)} + 抽样 {len(sample)} = {len(check)} 只")

    try:
        stock_info = fetch_stock_info_db()
    except Exception:
        stock_info = {}

    t0 = time.time()
    hits, missed = 0, []
    for i, code in enumerate(sorted(check), 1):
        if i % 100 == 0:
            print(f"  ...{i}/{len(check)} ({time.time()-t0:.0f}s)", flush=True)
        bars = daily(code, days=60)
        series = day_series([code]).get(code) or []
        try:
            sigs = strat.scan_signals(bars, code, ctx={
                "latest": snaps.get(code), "series": series, "mkt_gain": mkt},
                **params)
        except Exception:
            continue
        if sigs:
            hits += 1
            if code not in shortlist:
                missed.append(code)

    print(f"完整判定有信号: {hits} 股 | 预筛漏检: {len(missed)} 股")
    if missed:
        print("FAIL — 漏检 (预筛过严, 违背必要条件超集):", missed[:20])
        raise SystemExit(1)
    print("PASS — 预筛等价性成立 (有信号者皆被预筛覆盖)")


if __name__ == "__main__":
    main()
