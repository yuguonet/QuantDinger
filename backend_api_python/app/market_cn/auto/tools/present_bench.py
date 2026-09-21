"""tools/present_bench.py — M3 达成度实测: N 策略 / 30 秒 (架构 §5.3)。

实测展示预计算管线的三级时序 (ide/present.py), 并按 §5.3 预算给出瓶颈定位:

    T-1 夜   共享 bars 加载 (BarsCache) + night 门 (全市场 × N 策略)
    D0 盘中  候选集内 day 门 (真实 1m 快照帧的 14:56 槽位)
    合计     与 30 秒预算对比 → 达标 / 未达标 + 瓶颈

策略数可超过已转写数量: `--strategies 20` 会用内存克隆 (dataclasses.replace 改 key)
把已转写的策略扩到 20 份 —— 这是**负载模拟**, 不影响任何落盘文件。

用法:
  python -m app.market_cn.auto.tools.present_bench --strategies 20
  python -m app.market_cn.auto.tools.present_bench --strategies 20 --codes 400   # 抽样快跑
"""

from __future__ import annotations

import argparse
import dataclasses
import time
from typing import Any, Dict, List

import os

try:
    from dotenv import load_dotenv
    for _p in (os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "..", "..", "..", ".env"),
               os.path.join(os.getcwd(), ".env")):
        if os.path.isfile(_p):
            load_dotenv(_p, override=False)
            break
except Exception:
    pass

BUDGET = {
    "bars_load": 8.0,      # §5.3 日线批量加载 (共享缓存)
    "night": 10.0,         # 候选集内门求值
    "intraday": 4.0,       # 合成 D0 bar + 快照读取
    "other": 8.0,          # 落库 + 推送 + 余量
}
TOTAL_BUDGET = 30.0


def _clone_specs(specs: Dict[str, Any], n: int) -> Dict[str, Any]:
    """把已转写策略内存克隆到 n 份 (纯负载模拟, 不落盘)。"""
    if len(specs) >= n:
        return dict(list(specs.items())[:n])
    base = list(specs.items())
    out = dict(specs)
    i = 0
    while len(out) < n:
        k, s = base[i % len(base)]
        i += 1
        ck = f"{k}__c{i}"
        out[ck] = dataclasses.replace(s, key=ck)
    return out


def _pick_frame_date(fr, days: int = 90):
    """取最近一个 1m 快照帧非空的交易日 + 其前一交易日 (作 T-1)。"""
    dates = fr.trading_dates(days_back=days)
    for d in reversed(dates):
        try:
            f = fr.build_frame(d)
        except Exception:
            continue
        if f is not None and len(f) > 0:
            j = dates.index(d)
            return d, (dates[j - 1] if j > 0 else None)
    return None, None


def run(n_strategies: int = 20, codes_limit: int = 0, days: int = 300,
        verbose: bool = True) -> Dict[str, Any]:
    from app.market_cn.auto.core.data import frames as fr
    from app.market_cn.auto.core.data.frames import hhmm_to_pos
    from app.market_cn.auto.core.data.hub import all_codes, stock_info as hub_si
    from app.market_cn.auto.core import present as P

    t_all = time.time()
    tm: Dict[str, float] = {}

    t = time.time()
    specs = P._load_specs(None)
    specs = _clone_specs(specs, n_strategies)
    tm["specs"] = time.time() - t

    from app.market_cn.auto.core.data.frames import hhmm_to_pos
    t = time.time()
    buy_date, prev_date = _pick_frame_date(fr)
    tm["pick_date"] = time.time() - t
    if not buy_date:
        raise SystemExit("无 1m 快照帧缓存 → 无法实测盘中段 (先跑 frames 构建)")

    t = time.time()
    codes = all_codes()
    if codes_limit:
        codes = codes[:codes_limit]
    try:
        si = hub_si()
    except Exception:
        si = {}
    tm["codes"] = time.time() - t

    if verbose:
        print(f"策略数={len(specs)} 股票数={len(codes)} 买入日 T={buy_date} (夜侧 as-of={prev_date})")
        print("-" * 72)

    # ---- 阶段 1: T-1 夜 (共享加载 + night 门) ----
    t0 = time.time()
    cache = P.BarsCache(days=days, asof=prev_date)
    usable = cache.warm(codes)
    t_load = time.time() - t0

    t0 = time.time()
    night = P.precompute_night(specs, codes, cache, stock_info=si, buy_date=buy_date,
                               progress_every=5)
    t_night = time.time() - t0

    cand: Dict[str, List[str]] = {k: sorted({h.code for h in v}) for k, v in night["hits"].items()}
    n_cand = sum(len(v) for v in cand.values())
    cand_codes = set()
    for v in cand.values():
        cand_codes |= set(v)

    # ---- 阶段 2: D0 盘中 (候选集内 day 门 + 合成 D0 bar) ----
    t0 = time.time()
    frame = fr.build_frame(buy_date)
    pc = fr.prev_closes(buy_date)
    tm["frame_build"] = time.time() - t0
    # 触发槽位: 取各策略 entry_at 的最大值 (终审时刻), 无则 14:56
    mis = []
    for k, s in specs.items():
        ic = s.meta.get("intraday") or {}
        t = ic.get("entry_at")
        mi_k = hhmm_to_pos(t) if t else hhmm_to_pos("14:56")
        if mi_k >= 0:
            mis.append(mi_k)
    mi = max(mis) if mis else hhmm_to_pos("14:56")

    # 策略插件池 (intraday_shortlist 便宜预筛与实盘同源); 克隆 key 去掉 __cN 后缀回溯
    t = time.time()
    from app.market_cn.auto import strategies as strat_reg
    strat_reg.autodiscover()
    plugins = {}
    for k in specs:
        plug = strat_reg.get_strategy(k.split("__c")[0])
        if plug is not None:
            plugins[k] = plug
    tm["plugins"] = time.time() - t

    t0 = time.time()
    snaps = frame.snaps_at(mi, pc)                 # 全市场快照 (与实盘 latest_snapshot 同口径)
    mkt = frame.mkt_gain(mi, pc)
    t_snaps = time.time() - t0

    t0 = time.time()
    intra = P.intraday_cycle(specs, night, cache, snaps,
                            series_map=lambda c: frame.series(c, mi),
                            trade_date=buy_date, mkt_gain=mkt, stock_info=si,
                            plugins=plugins)
    t_intra = time.time() - t0
    t_total = time.time() - t_all
    tm_all = t_total - (t_load + t_night + t_snaps + t_intra + tm["frame_build"])

    if verbose:
        print(f"bars 可用={usable}  (loads={cache.loads} hits={cache.hits})")
        print(f"夜候选合计={n_cand}  候选股(去重)={len(cand_codes)}")
        print(f"盘中快照={len(snaps)} mkt_gain={mkt:.3f} "
              f"求值={intra['stats']['evaluated']} 命中={intra['stats']['hit']}")
        print("-" * 72)
        rows = [
            ("框架: 策略加载/克隆", tm["specs"], 1.0),
            ("框架: 取帧日期", tm["pick_date"], 1.0),
            ("框架: 代码表+股本", tm["codes"], 1.0),
            ("框架: 插件实例", tm["plugins"], 1.0),
            ("日线加载(共享)", t_load, BUDGET["bars_load"]),
            ("夜: night 门", t_night, BUDGET["night"]),
            ("盘中: 帧构建", tm["frame_build"], BUDGET["intraday"] / 2),
            ("盘中: 快照读取", t_snaps, BUDGET["intraday"] / 2),
            ("盘中: day 门", t_intra, BUDGET["intraday"] / 2),
        ]
        for name, el, bud in rows:
            print(f"  {name:18s} {el:8.2f}s   预算 {bud:6.1f}s   "
                  f"{'OK' if el <= bud else '超预算'}")
        print(f"  {'其他/未归因':18s} {tm_all:8.2f}s")
        print(f"  {'合计 (实测)':18s} {t_total:8.2f}s   目标 {TOTAL_BUDGET:5.1f}s   "
              f"{'达标' if t_total <= TOTAL_BUDGET else '未达标'}")
        print("-" * 72)
        per = {k: len({h.code for h in v}) for k, v in night["hits"].items()}
        print("  各策略夜候选 (前 10):",
              ", ".join(f"{k}={v}" for k, v in list(per.items())[:10]))
        slow = sorted(night["stats"].items(), key=lambda kv: -kv[1].evaluated)[:5]
        print("  求值次数最多的策略:",
              ", ".join(f"{k}:{v.evaluated}" for k, v in slow))
        print("  夜段单策略耗时 (前 5):",
              ", ".join(f"{k}:{v.elapsed:.1f}s"
                        for k, v in sorted(night["stats"].items(),
                                           key=lambda kv: -kv[1].elapsed)[:5]))

    return {"total": t_total, "load": t_load, "night": t_night,
            "snaps": t_snaps, "intraday": t_intra, "candidates": n_cand,
            "codes": len(codes), "strategies": len(specs), "budget_ok": t_total <= TOTAL_BUDGET}


def main():
    ap = argparse.ArgumentParser(description="M3 展示预计算管线基准 (20 策略 / 30s)")
    ap.add_argument("--strategies", type=int, default=20)
    ap.add_argument("--codes", type=int, default=0, help="股票数上限 (0=全市场)")
    ap.add_argument("--days", type=int, default=300)
    a = ap.parse_args()
    r = run(a.strategies, codes_limit=a.codes, days=a.days)
    print(f"\nRESULT: strategies={r['strategies']} codes={r['codes']} "
          f"total={r['total']:.2f}s budget_ok={r['budget_ok']}")


if __name__ == "__main__":
    main()
