#!/usr/bin/env python3
"""auto/features/env_flow.py — 大盘资金面 env 特征 (2026-09-12, 消费 hub.index_fflow)

用途: 把 kline_index_fflow 的原始分钟累计净额, 转成 D-1 可知的大盘资金面 env 特征,
     供策略 prefilter / 环境归因 / pool_check 信号级复验消费。

D-1 纪律 (防未来函数): 所有特征只用 as_of 当日之前的 bar。index_fflow 为当日累计
     语义 (EM fflow 口径, 15:00 累计=日级值), 某日主力净额 = 该日最后一根 bar 的
     main_net; 窗口净额 = 两端累计值之差 (差分)。取 D-1 整日累计不受盘中期滞后影响
     (D-1 已收盘, 与 delay host 的 ≤15min 盘中滞后无关)。

特征 (全 D-1 可知, index_code 默认沪深300):
  - main_net_d1    : 指数主力净额累计(元) 截至 D-1 收盘 (超大单+大单)
  - main_ratio_d1 : 主力净额占四档绝对值和之比% (归一, 无单位, 正=主力净流入)
  - main_net_5d    : 近 5 交易日主力净额合计(元) = daily[-1]累计 - daily[-6]累计
  - super_net_d1   : 超大单净额(元) D-1
  - main_rank20    : D-1 主力净额在 trailing 20 日分位 [0,1] (regime 强度, 高=资金强)

易错点:
  - index_fflow.time 为 bar 起始 (09:31 起); 取每日最后一根即当日累计 (与 index_minute
    的 bar 结束时刻差 1 根, 跨表对齐时注意);
  - 四档净额之和恒为 0 (互为对手盘), 分母用绝对值和避免抵消 → main_ratio 有界;
  - 数据缺失/不足 → 返回 None (fail-open, 调用方据此不否决)。

用法:
  from app.market_cn.auto.features.env_flow import env_flow_features
  f = env_flow_features("000300", as_of="2026-09-11")   # D-1 前的大盘资金面
  # 或 python env_flow.py --demo (无 DB 自测数学)
"""
from __future__ import annotations

import argparse
import os
import sys

_PROJ = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)


def _daily_series(bars):
    """原始分钟 bar → 每日最后一根 (当日累计) 序列, 按日期升序。

    返回 [(date_str, {"main_net","super_net","big_net","mid_net","small_net"}), ...]
    """
    days = {}
    for b in bars or []:
        d = str(b.get("time") or "")[:10]
        if not d:
            continue
        # 同日期后者 time 更大 → 覆盖即取当日累计最后一根
        days[d] = b
    out = []
    for d in sorted(days):
        b = days[d]
        out.append((d, {
            "main_net": float(b.get("main_net") or 0),
            "super_net": float(b.get("super_net") or 0),
            "big_net": float(b.get("big_net") or 0),
            "mid_net": float(b.get("mid_net") or 0),
            "small_net": float(b.get("small_net") or 0),
        }))
    return out


def env_flow_features(index_code="000300", as_of=None, lookback=20):
    """D-1 可知的大盘资金面 env 特征 (dict) 或 None (数据不足)。

    as_of: 决策日 (YYYY-MM-DD 或含时间); 只用严格早于 as_of 的日累计。
    """
    try:
        from app.market_cn.auto.data.hub import index_fflow
        bars = index_fflow(index_code, days=lookback + 6, as_of=as_of) or []
    except Exception:
        return None
    daily = _daily_series(bars)
    as_of_d = str(as_of)[:10] if as_of else None
    # 只保留严格早于 as_of 的日 (D-1 及以前)
    if as_of_d:
        daily = [x for x in daily if x[0] < as_of_d]
    if not daily:
        return None

    d1 = daily[-1]
    mn = d1[1]["main_net"]
    sn = d1[1]["super_net"]
    bn = d1[1]["big_net"]
    midn = d1[1]["mid_net"]
    smn = d1[1]["small_net"]
    denom = abs(sn) + abs(bn) + abs(midn) + abs(smn)
    main_ratio = (mn / denom * 100) if denom > 0 else 0.0

    # 近 5 日主力净额合计 (累计差): 取 5 个交易日前那根作基准
    main_net_5d = mn
    if len(daily) >= 6:
        main_net_5d = mn - daily[-6][1]["main_net"]
    elif len(daily) > 1:
        main_net_5d = mn - daily[0][1]["main_net"]

    # trailing 20 日分位 (含 D-1 自身)
    tail = daily[-lookback:] if len(daily) >= lookback else daily
    vals = [x[1]["main_net"] for x in tail]
    rank = (sum(1 for v in vals if v <= mn) - 1) / (len(vals) - 1) if len(vals) > 1 else 0.5

    return {
        "index_code": index_code,
        "as_of": as_of_d,
        "main_net_d1": round(mn, 2),
        "main_ratio_d1": round(main_ratio, 3),
        "main_net_5d": round(main_net_5d, 2),
        "super_net_d1": round(sn, 2),
        "main_rank20": round(rank, 3),
    }


def _demo():
    """无 DB 自测: 构造合成分钟 bar, 验证日累计聚合 + 差分 + 分位数学。"""
    print("=== env_flow --demo (合成 bar, 无 DB) ===")
    # 3 个交易日, 每日 2 根 (09:31/15:00), main_net 当日累计递增
    synth = []
    # day1 累计 main_net=1e8, day2=3e8, day3=2e8; 四档: 主力=超大+大, 余为对手
    series = [
        ("2026-09-09", 1.0e8, 6e7, 4e7, -3e7, -7e7),   # main=1e8
        ("2026-09-10", 3.0e8, 2e8, 1e8, -1e8, -2e8),   # main=3e8
        ("2026-09-11", 2.0e8, 1e8, 1e8, -5e7, -1.5e8), # main=2e8
    ]
    for d, mn, s, b, m, sm in series:
        synth.append({"time": f"{d} 09:31", "main_net": 0, "super_net": 0,
                      "big_net": 0, "mid_net": 0, "small_net": 0})
        synth.append({"time": f"{d} 15:00", "main_net": mn, "super_net": s,
                      "big_net": b, "mid_net": m, "small_net": sm})
    import types
    class _Hub:
        @staticmethod
        def index_fflow(code, days=0, as_of=None):
            return list(synth)
    import app.market_cn.auto.features.env_flow as mod
    mod.index_fflow = _Hub.index_fflow  # 猴子补丁本模块内的惰性 import 别名不可行, 直接测 _daily_series

    daily = _daily_series(synth)
    assert len(daily) == 3, f"日聚合应得 3 天, 实得 {len(daily)}"
    assert daily[-1][1]["main_net"] == 2.0e8, "D-1 累计应=2e8"
    # 验证数学经 env_flow_features (临时替换 hub)
    real = mod.index_fflow
    try:
        import app.market_cn.auto.data.hub as hubmod
        orig = hubmod.index_fflow
        hubmod.index_fflow = _Hub.index_fflow
        f = mod.env_flow_features("000300", as_of="2026-09-12")
        print("特征:", f)
        assert f["main_net_d1"] == 2.0e8
        assert f["main_net_5d"] == 2.0e8 - 1.0e8, f"5d差应=1e8, 实得 {f['main_net_5d']}"
        # 分位: D-1=2e8, 序列[1e8,3e8,2e8] → 2e8 排第2 (0-indexed 1of2) → rank=(2-1)/(3-1)=0.5
        assert abs(f["main_rank20"] - 0.5) < 1e-9, f"rank应=0.5, 实得 {f['main_rank20']}"
        denom = 1e8 + 1e8 + 5e7 + 1.5e8
        assert abs(f["main_ratio_d1"] - 2.0e8 / denom * 100) < 1e-6
        print("✅ 自测通过: 日聚合/5d差分/分位/归一 数学正确")
    finally:
        hubmod.index_fflow = orig
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="大盘资金面 env 特征 (无 DB 可用 --demo)")
    ap.add_argument("--index", default="000300")
    ap.add_argument("--as-of", default="", help="决策日 YYYY-MM-DD")
    ap.add_argument("--demo", action="store_true", help="合成 bar 自测")
    a = ap.parse_args()
    if a.demo:
        sys.exit(_demo())
    if not a.as_of:
        print("需 --as-of 或 --demo")
        sys.exit(2)
    f = env_flow_features(a.index, as_of=a.as_of)
    print(f if f else "无数据 (fail-open)")
