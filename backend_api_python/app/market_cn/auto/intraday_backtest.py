# -*- coding: utf-8 -*-
"""intraday_backtest.py — 盘中规则回测框架 (kline_1m 重放)

目的: 用 3-5 个月的 1m K线验证 intraday_core 中每条经验规则的真实胜率,
      为"算法与胜率完善后接入系统"提供数据依据。

候选全集 (与三策略候选日对齐): 某股票在 T 日前 11 个交易日内出现过
  涨停 或 大涨(主板≥7%/创科板≥12%) → T 日为候选日 (T 本身不是锚点日也可, 覆盖回调与反弹日)。

每个候选日: 加载当日 1m bars → 逐分钟 as-of 检测事件 → 对每个事件度量
  前向收益: to_close (事件价→当日收盘), next_open (事件价→次日开盘), m30 (事件价→30分钟后)。
聚合输出每类事件的 次数/胜率/均值。另统计 尾盘行为 → 次日开盘溢价 (用户假设验证)。

用法:
  python -m app.market_cn.auto.intraday_backtest --run --days 20 [--max-codes 1500] [--report 路径]
"""
from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict

from app.utils.logger import get_logger

logger = get_logger(__name__)


# ================================================================
# 数据加载 (复用 dragon_scan 的 1D/1m 路径)
# ================================================================

def _load_env():
    import os
    from dotenv import load_dotenv
    for _p in (os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', '.env'),
               os.path.join(os.getcwd(), '.env')):
        if os.path.isfile(_p):
            load_dotenv(_p, override=False)
            break


def fetch_daily(code, days):
    from datetime import datetime, timedelta
    from app.utils.db_market import get_market_kline_writer
    from app.data_sources.provider.adjustment import unadj_to_qfq
    end = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=int(days * 1.5))).strftime("%Y-%m-%d")
    try:
        writer = get_market_kline_writer()
        data = writer.query("CNStock", code, "1D", start_time=start, end_time=end, limit=0)
        if not data:
            return []
        return unadj_to_qfq([{"time": str(r["time"])[:10], "open": float(r["open"]),
                              "high": float(r["high"]), "low": float(r["low"]),
                              "close": float(r["close"]), "volume": float(r["volume"])}
                             for r in data], code)
    except Exception:
        return []


def fetch_1m(code, start_date):
    from app.utils.db_market import get_market_db_manager
    mgr = get_market_db_manager()
    pool = mgr._get_pool("CNStock")
    year = start_date[:4]
    table = f"kline_1m_{year}"
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT time, open, high, low, close, volume FROM \"{table}\" "
                            f"WHERE symbol = %s AND time >= %s ORDER BY time",
                            (code, f"{start_date} 09:00:00"))
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        logger.debug("[intraday_bt] 1m %s 加载失败: %s", code, e)
        return []


def all_codes():
    from app.utils.basicinfo_db import get_stock_basic_db
    return get_stock_basic_db().market_all_codes(status="active")


# ================================================================
# 候选日全集
# ================================================================

def _is_anchor(ret, gem):
    thr = 0.198 if gem else 0.098
    return ret >= thr * 0.98 or ret >= (0.12 if gem else 0.07)


def build_universe(daily_by_code, day_start, day_end):
    """返回 {code: [候选日列表]}: 锚点日(涨停/大涨)后 11 个交易日内的日子。"""
    uni = {}
    for code, bars in daily_by_code.items():
        dates = [b["time"] for b in bars]
        idx_by_date = {d: i for i, d in enumerate(dates)}
        anchors = []
        for i in range(1, len(bars)):
            ret = bars[i]["close"] / bars[i - 1]["close"] - 1
            gem = code.startswith(("30", "68"))
            if _is_anchor(ret, gem):
                anchors.append(i)
        cand = set()
        for a in anchors:
            for j in range(a + 1, min(a + 12, len(bars))):
                d = dates[j]
                if day_start <= d <= day_end:
                    cand.add(d)
        if cand:
            uni[code] = sorted(cand)
    return uni


# ================================================================
# 事件前向收益
# ================================================================

def forward_returns(mins, ev_mi, next_open):
    """事件分钟价 → 当日收盘 / 30分钟后 / 次日开盘 的收益 (%)。"""
    base = None
    for b in mins:
        if b["mi"] == ev_mi:
            base = b["c"]
            break
    if base is None or base <= 0:
        return {}
    out = {}
    last = mins[-1]
    out["to_close"] = (last["c"] / base - 1) * 100
    if len(mins) > ev_mi + 30:
        out["m30"] = (mins[ev_mi + 30]["c"] / base - 1) * 100
    if next_open:
        out["next_open"] = (next_open / base - 1) * 100
    return out


def run_backtest(days=20, max_codes=1500, universe_cap=6000):
    """主回测。返回汇总 dict。"""
    import logging as _logging
    import sys as _sys
    _logging.basicConfig(level=_logging.INFO, stream=_sys.stdout,
                         format="%(asctime)s [intraday_bt] %(message)s", force=True)
    _load_env()
    from app.market_cn.auto import dragon_store as ds
    from app.market_cn.auto.intraday_core import (
        INTRADAY_PARAMS, prep_minutes, detect_events, prev_day_curves,
        collect_dip_episodes, macd_channel_stats,
    )
    from app.utils.trading_calendar import last_finish_trading_day

    ds.ensure_tables()
    target = last_finish_trading_day()
    # 1m 窗口起点: 往前推 days 个交易日 (用日历近似: days*1.5 自然日)
    from datetime import datetime, timedelta
    start_date = (datetime.now() - timedelta(days=int(days * 1.6))).strftime("%Y-%m-%d")

    t0 = time.time()
    codes = all_codes()
    logger.info("[intraday_bt] 全市场 %d 只, 加载 1D 构建候选全集...", len(codes))
    daily_by_code = {}
    for i, code in enumerate(codes):
        bars = fetch_daily(code, 300)
        if len(bars) >= 30:
            daily_by_code[code] = bars
        if (i + 1) % 1000 == 0:
            logger.info("[intraday_bt] 1D 进度 %d/%d (%.0fs)", i + 1, len(codes), time.time() - t0)

    # 窗口内的交易日列表 (以 000001 为准)
    ref = daily_by_code.get("000001") or next(iter(daily_by_code.values()))
    trade_days = [b["time"] for b in ref if b["time"] >= start_date]
    if not trade_days:
        return {"status": "no_days"}
    day_start, day_end = trade_days[0], trade_days[-1]

    uni = build_universe(daily_by_code, day_start, day_end)
    total_days = sum(len(v) for v in uni.values())
    logger.info("[intraday_bt] 候选日全集: %d 只股票 / %d 个候选日 (窗口 %s~%s)",
                len(uni), total_days, day_start, day_end)

    # 限制规模
    codes_sel = list(uni.keys())[:max_codes]
    budget = universe_cap

    # 事件聚合
    agg = defaultdict(lambda: {"n": 0, "win_close": 0, "sum_close": 0.0,
                               "win_next": 0, "sum_next": 0.0, "n_next": 0,
                               "sum_m30": 0.0, "n_m30": 0})
    # 尾盘统计 (15分钟窗 × 14:20~14:50 异动分离)
    tail = defaultdict(lambda: {"n": 0, "sum_next": 0.0, "win_next": 0})
    # 跌破深度阈值扫描 (reclaim_strong 寻优)
    DIP_SWEEP = [0.05, 0.1, 0.2, 0.3, 0.5, 1.0]
    dip_sweep = {th: {"n": 0, "win_close": 0, "sum_close": 0.0,
                      "n_next": 0, "win_next": 0, "sum_next": 0.0} for th in DIP_SWEEP}
    # MACD 通道比值分桶 (14:00 时点计算, 分腿: 涨得多跌得少=强)
    mchg = defaultdict(lambda: {"n": 0, "win_close": 0, "sum_close": 0.0,
                                "n_next": 0, "win_next": 0, "sum_next": 0.0})

    next_open_by = {}   # (code, date) → 次日开盘
    for code, bars in daily_by_code.items():
        for i in range(1, len(bars)):
            next_open_by[(code, bars[i - 1]["time"])] = bars[i]["open"]

    processed = 0
    ev_count = 0
    for ci, code in enumerate(codes_sel):
        if budget <= 0:
            break
        days_cand = uni[code]
        raw_1m = fetch_1m(code, day_start)
        if not raw_1m:
            continue
        by_day = defaultdict(list)
        for r in raw_1m:
            by_day[str(r["time"])[:10]].append(r)
        # 按日构建分钟序列 (含前5日曲线)
        day_mins = {}
        for d in sorted(by_day):
            day_mins[d] = prep_minutes(by_day[d], volume_cumulative=False)
        dlist = sorted(day_mins)
        for d in days_cand:
            if budget <= 0:
                break
            if d not in day_mins:
                continue
            mins = day_mins[d]
            if len(mins) < 60:
                continue
            di = dlist.index(d)
            prev_days = [day_mins[x] for x in dlist[max(0, di - 5):di]]
            curve = prev_day_curves(prev_days)
            next_open = next_open_by.get((code, d))
            gem = code.startswith(("30", "68"))
            lu_pct = 19.0 if gem else 9.0
            try:
                evts = detect_events(mins, curve=curve, limit_up_pct=lu_pct)
            except Exception as e:
                logger.debug("[intraday_bt] %s %s 事件检测异常: %s", code, d, e)
                continue
            budget -= 1
            processed += 1
            # 尾盘统计: 收盘前 15 分钟 (mi 225~239) × 14:20~14:50 异动分离
            mi_map = {b["mi"]: b for b in mins}
            if 239 in mi_map and 225 in mi_map and 224 in mi_map and 199 in mi_map and 229 in mi_map:
                tail_base = mi_map[224]["c"]
                tail_chg = (mi_map[239]["c"] / tail_base - 1) * 100 if tail_base > 0 else 0
                pre_move = (mi_map[229]["c"] / mi_map[199]["c"] - 1) * 100 if mi_map[199]["c"] > 0 else 0
                pre_state = "异动" if abs(pre_move) >= 2.0 else "无异动"
                tcls = None
                if -1.0 <= tail_chg < 0:
                    tcls = "尾盘小跌"
                elif 0 <= tail_chg <= 1.0:
                    tcls = "尾盘小涨"
                no = next_open_by.get((code, d))
                if tcls and no:
                    t = tail[f"{tcls}|{pre_state}"]
                    t["n"] += 1
                    prem = (no / mi_map[239]["c"] - 1) * 100
                    t["sum_next"] += prem
                    if prem > 0:
                        t["win_next"] += 1
            # 跌破深度阈值扫描 (episode → 各阈值 reclaim 事件)
            try:
                eps = collect_dip_episodes(mins)
            except Exception:
                eps = []
            for th in DIP_SWEEP:
                for ep in eps:
                    if not ep.get("reclaimed") or ep["depth"] > th:
                        continue
                    if ep.get("minutes_to_reclaim", 999) > 15:
                        continue
                    fr = forward_returns(mins, ep["reclaim_mi"], next_open)
                    a = dip_sweep[th]
                    a["n"] += 1
                    if "to_close" in fr:
                        a["sum_close"] += fr["to_close"]
                        if fr["to_close"] > 0:
                            a["win_close"] += 1
                    if "next_open" in fr:
                        a["n_next"] += 1
                        a["sum_next"] += fr["next_open"]
                        if fr["next_open"] > 0:
                            a["win_next"] += 1
            # MACD 通道比值 (14:00 时点): 分腿统计 → 分桶
            if 200 in mi_map:
                pos200 = None
                for i2, b2 in enumerate(mins):
                    if b2["mi"] == 200:
                        pos200 = i2
                        break
                if pos200 is not None:
                    try:
                        cs = macd_channel_stats(mins, upto_idx=pos200)
                    except Exception:
                        cs = None
                    if cs and cs.get("score") is not None:
                        sc = cs["score"]
                        bucket = "强(≥60)" if sc >= 60 else ("中(40~60)" if sc >= 40 else "弱(<40)")
                        px200 = mi_map[200]["c"]
                        a = mchg[bucket]
                        a["n"] += 1
                        if px200 > 0:
                            tc = (mins[-1]["c"] / px200 - 1) * 100
                            a["sum_close"] += tc
                            if tc > 0:
                                a["win_close"] += 1
                            no = next_open_by.get((code, d))
                            if no:
                                a["n_next"] += 1
                                a["sum_next"] += (no / px200 - 1) * 100
                                if no / px200 - 1 > 0:
                                    a["win_next"] += 1
            for e in evts:
                fr = forward_returns(mins, e["mi"], next_open)
                a = agg[e["type"]]
                a["n"] += 1
                ev_count += 1
                if "to_close" in fr:
                    a["sum_close"] += fr["to_close"]
                    if fr["to_close"] > 0:
                        a["win_close"] += 1
                if "next_open" in fr:
                    a["n_next"] += 1
                    a["sum_next"] += fr["next_open"]
                    if fr["next_open"] > 0:
                        a["win_next"] += 1
                if "m30" in fr:
                    a["n_m30"] += 1
                    a["sum_m30"] += fr["m30"]
        if (ci + 1) % 300 == 0:
            logger.info("[intraday_bt] 进度 %d/%d codes, 处理候选日 %d, 事件 %d (%.0fs)",
                        ci + 1, len(codes_sel), processed, ev_count, time.time() - t0)

    summary = {}
    for k, a in agg.items():
        if k == "reclaim_strong":
            continue   # reclaim 用阈值扫描结果替代 (更精细)
        summary[k] = {
            "events": a["n"],
            "win_rate_to_close": round(a["win_close"] / max(1, a["n"]) * 100, 1),
            "avg_to_close": round(a["sum_close"] / max(1, a["n"]), 2),
            "win_rate_next_open": round(a["win_next"] / max(1, a["n_next"]) * 100, 1) if a["n_next"] else None,
            "avg_next_open": round(a["sum_next"] / max(1, a["n_next"]), 2) if a["n_next"] else None,
            "avg_m30": round(a["sum_m30"] / a["n_m30"], 2) if a["n_m30"] else None,
        }
    summary["reclaim_strong(阈值扫描)"] = {
        f"深跌≤{th}%": {"n": a["n"],
                        "win_rate_to_close": round(a["win_close"] / max(1, a["n"]) * 100, 1) if a["n"] else None,
                        "avg_to_close": round(a["sum_close"] / max(1, a["n"]), 2) if a["n"] else None,
                        "win_rate_next_open": round(a["win_next"] / max(1, a["n_next"]) * 100, 1) if a["n_next"] else None,
                        "avg_next_open": round(a["sum_next"] / max(1, a["n_next"]), 2) if a["n_next"] else None}
        for th, a in dip_sweep.items()
    }
    summary["MACD通道比值(14:00时点)"] = {
        k: {"n": a["n"],
            "win_rate_to_close": round(a["win_close"] / max(1, a["n"]) * 100, 1) if a["n"] else None,
            "avg_to_close": round(a["sum_close"] / max(1, a["n"]), 2) if a["n"] else None,
            "win_rate_next_open": round(a["win_next"] / max(1, a["n_next"]) * 100, 1) if a["n_next"] else None,
            "avg_next_open": round(a["sum_next"] / max(1, a["n_next"]), 2) if a["n_next"] else None}
        for k, a in mchg.items()
    }
    tail_summary = {k: {"n": v["n"],
                        "avg_next_open": round(v["sum_next"] / max(1, v["n"]), 2),
                        "win_rate_next_open": round(v["win_next"] / max(1, v["n"]) * 100, 1)}
                    for k, v in tail.items()}
    return {"status": "ok", "window": [day_start, day_end],
            "codes": len(codes_sel), "candidate_days": processed,
            "events_total": ev_count, "events": summary, "tail": tail_summary,
            "elapsed_sec": round(time.time() - t0, 1)}


def main():
    global INTRADAY_TAIL
    parser = argparse.ArgumentParser(description="盘中规则回测 (kline_1m 重放)")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--days", type=int, default=20, help="回测窗口(交易日)")
    parser.add_argument("--max-codes", type=int, default=1500)
    parser.add_argument("--universe-cap", type=int, default=6000, help="最多处理的候选日数")
    parser.add_argument("--report", type=str, default="", help="结果JSON输出路径")
    args = parser.parse_args()
    if not args.run:
        parser.print_help()
        return
    INTRADAY_TAIL = 30
    result = run_backtest(days=args.days, max_codes=args.max_codes,
                          universe_cap=args.universe_cap)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"报告已保存: {args.report}")


INTRADAY_TAIL = 30

if __name__ == "__main__":
    main()
