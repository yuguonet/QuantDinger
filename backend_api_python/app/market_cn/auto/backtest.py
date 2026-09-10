#!/usr/bin/env python3
"""auto/backtest.py — 框架内全市场回测流水线 (B 阶段, 2026-09-09; 09-10 分发插件化)

用途: 把 test_dragon.py 的"全市场回测流水线"收进框架。本文件只做**薄编排**:
     全市场循环 → hub.daily 取数 → 策略钩子 backtest_stock → trades → 标准统计。
     策略枚举判定经注册表分发 (strategies 插件的 backtest_stock 钩子, 2026-09-10 起),
     **新建策略零改动本文件** — 插件内实现 backtest_stock 即自动进入流水线。

设计点:
  - 与实盘同一份 scan_signals (as_of 切片语义), 对数 PASS 后 test_dragon 双同步约定作废;
  - 编排层无规则: 去重/预过滤锚点/D1过滤/预筛/出场模拟都在各插件 backtest_stock 内
    (2026-09-10 晚裁定: 出场模拟是策略专用规则, 归各策略文件; backtest.py 只留
    通用引擎 — 枚举分发/统计/时间线引擎, 见 run_all_intraday);
  - 数据走 hub.daily (与 test_dragon.fetch_kline_db 逐字等价: 窗口取数+qfq, 已验证)。

易错点:
  - 枚举终点 n-1: 最后一根无 D+1, 不能做 D0 (约定在插件循环内);
  - 未实现 backtest_stock 的策略 (盘中窗口类 tail/knife) run_all 直接报错提示;
  - run 输出 trades 含 tech_score 等字段, 与 tmp/ 基线 JSON 字段对齐供逐笔对数。
"""
from __future__ import annotations

import os
import time


def is_st_stock(code):
    """显式 ST 过滤 (与 test_dragon 一致): 无股票名称数据时依赖涨停阈值自然排除。"""
    return False


# ================================================================
# 全市场流水线 (编排层: 经注册表分发, 无策略名分支)
# ================================================================

def _run_meta(strat, days, start_date, end_date):
    """回测元信息 (2026-09-10 P2-2): 实验溯源用 — git 版本 + 实际生效参数 + 窗口。

    git_sha 取不到 (非 git 环境/无 git) 时为 "unknown", 绝不因溯源失败影响回测本身。
    """
    import subprocess
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            timeout=3, stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        sha = "unknown"
    try:
        params = strat.merged_params(None)
    except Exception:
        params = {}
    return {"git_sha": sha, "strategy": strat.key, "days": days,
            "start_date": start_date, "end_date": end_date,
            "effective_params": params}


def run_all(strategy="dragon", days=300, codes=None, stock_info=None,
            use_prefilter=True, progress_every=500, start_date=None, end_date=None,
            probe=None):
    """全市场回测 (策略经注册表分发, 按 scan_spec.kind 选路径)。

    strategy: 任意已注册策略 key。
      - daily_close 类 (dragon/v1/break): 日线枚举快路径 (backtest_stock 钩子)。
      - intraday_window 类 (tail/knife): 时间线引擎 (1m 快照帧重建, 与实盘同判定路径)。
    probe: 调试探针 (probe.Probe, None=关闭)。回测只负责验证, 探针数据存档供 AI 分析。
    返回 {"trades": [...], "stats": {...}}; trades 直接可 json.dump 与基线对数。
    """
    from app.market_cn.auto import strategies as strat_reg
    from app.market_cn.auto.strategies.base import StrategyBase

    strat_reg.autodiscover()
    strat = strat_reg.get_strategy(strategy)
    if strat is None:
        raise ValueError(f"strategy={strategy} 未注册 (可用: {sorted(strat_reg.all_strategies())})")

    if strat.scan_spec.kind == "intraday_window":
        res = run_all_intraday(strat, days=days, codes=codes,
                               start_date=start_date, end_date=end_date,
                               probe=probe)
        res["meta"] = _run_meta(strat, days, start_date, end_date)
        return res

    if type(strat).backtest_stock is StrategyBase.backtest_stock:
        raise ValueError(f"strategy={strategy} 未实现日线枚举回测钩子 backtest_stock")

    from app.market_cn.auto.data.hub import all_codes, daily
    from app.market_cn.auto.data.hub import stock_info as _hub_stock_info

    if codes is None:
        codes = all_codes()
    if stock_info is None:
        try:
            stock_info = _hub_stock_info()  # U1~U3 依赖 (缺失则跳过, 会放行)
        except Exception:
            stock_info = {}
    t0 = time.time()
    trades = []
    n_ok = 0
    for k, code in enumerate(codes, 1):
        if is_st_stock(code):
            continue
        bars = daily(code, days)
        if not bars:
            continue
        trades.extend(strat.backtest_stock(
            bars, code,
            stock_info=stock_info.get(code) if stock_info else None,
            use_prefilter=use_prefilter, probe=probe) or [])
        n_ok += 1
        if progress_every and k % progress_every == 0:
            print(f"[{k}/{len(codes)}] trades={len(trades)} "
                  f"({time.time() - t0:.0f}s)", flush=True)
    return {"trades": trades, "stats": _summary(trades), "codes_ok": n_ok,
            "elapsed": round(time.time() - t0, 1),
            "meta": _run_meta(strat, days, start_date, end_date)}


# ================================================================
# 时间线引擎 (intraday_window 类: 1m 快照帧重建, 与实盘同判定路径)
# ================================================================

def _exec_trigger_mis(spec):
    """ScanSpec → 成交触发槽位列表 (entry_at 终审语义: 只回该时刻)。"""
    from app.market_cn.auto.data.frames import hhmm_to_pos
    if spec.entry_at:
        mi = hhmm_to_pos(spec.entry_at)
        return [mi] if mi >= 0 else []
    from app.market_cn.auto.sched import expand_times
    mis = []
    for t in expand_times(spec.windows, spec.interval_sec):
        mi = hhmm_to_pos(t)
        if mi >= 0 and mi not in mis:
            mis.append(mi)
    return sorted(mis)


def run_all_intraday(strat, days=120, codes=None, start_date=None, end_date=None,
                     progress_every=1, probe=None):
    """intraday_window 策略全市场回测 (时间线引擎)。

    数据通道混用: 1m 快照帧 (盘中判定+入场价) + 日线 (策略上下文 as-of D-1 / 次日开盘出场),
    快照通道由 kline_1m 重建 (终审口径, 与 realtime_snapshot 有分钟级微差属已知边界)。
    触发语义 "bar 开盘触发": 信息截至 p-1 收盘 + bar[p].open 已出现, 入场即 bar[p].open。
    出场: 次交易日日线开盘价 (与 tail/knife 基线 "D1 开盘卖" 一致)。
    probe: 调试探针 (None=零开销)。sample 由引擎按 (股,日) 聚合产出 — 每日每股只留
    最晚触发槽位的评估记录 (数据外壳: stage/rule_trace 来自策略门打点 + ctx 摘要 +
    以触发价为入场基准的 d1 开盘/收盘标签); shortlist 之外的廉价预筛拒绝不采样。
    """
    from app.market_cn.auto.data import frames as fr
    from app.market_cn.auto.data.hub import daily
    from app.market_cn.auto.probe import DayTrace as _SlotTrace

    t0 = time.time()
    all_dates = fr.trading_dates(days_back=days, end=end_date)
    dates = all_dates
    first_1m = fr.first_1m_date()
    if first_1m:
        dates = [d for d in dates if d >= first_1m]     # 1m 覆盖之前的天直接跳过 (空帧浪费)
    if start_date:
        dates = [d for d in dates if d >= str(start_date)[:10]]
    if not dates:
        return {"trades": [], "stats": _summary([]), "codes_ok": 0, "elapsed": 0}
    # 首日 prev_date: 取覆盖起点前一交易日 (2026-09-10 修复: 原首日 prev_date=date →
    # as_of 含当日日线, 单日复现/窗口首日成未来函数, knife 单日 87笔 vs 窗口同日 52笔口径)
    _i0 = all_dates.index(dates[0]) if dates[0] in all_dates else -1
    _first_prev = all_dates[_i0 - 1] if _i0 > 0 else None
    code_set = set(codes) if codes else None
    mis = _exec_trigger_mis(strat.scan_spec)
    if not mis:
        raise ValueError(f"{strat.key}: scan_spec 无有效成交触发时刻 "
                         f"(entry_at={strat.scan_spec.entry_at!r} windows={strat.scan_spec.windows})")

    pc_map = fr.prev_closes(dates[0])                   # {code: 前一1m日收盘(qfq)}
    # ST 过滤与实盘 scan 同口径 (name 含 'ST' 排除, 含 *ST)
    from app.market_cn.auto.data.hub import stock_info as _hub_stock_info
    try:
        _si = _hub_stock_info()
    except Exception:
        _si = {}

    def _st_ok(code):
        nm = (_si.get(code) or {}).get("name", "") or ""
        return "ST" not in nm.upper()

    # 日线 per-run memo (2026-09-10 提速): 同股跨槽位重复判定曾反复打库
    # (实测 knife 151 天 37440 次幸存→37440 次 daily() 查询 ≈ 250s)。单次运行内
    # fetch_kline_db 返回恒定 → memo 全量 bars + as_of 本地切片, 语义等价。
    _daily_memo = {}

    def _daily_asof(code, prev_date):
        bars = _daily_memo.get(code)
        if bars is None:
            bars = _daily_memo.setdefault(code, daily(code, 300))
        if prev_date:
            return [b for b in bars if str(b["time"])[:10] <= str(prev_date)[:10]]
        return bars

    trades, seen = [], set()
    dbg = {} if probe is not None else None   # debug: code -> 当日最晚槽位评估记录 (日终统一落盘)
    for di, date in enumerate(dates):
        frame = fr.build_frame(date)
        if len(frame) == 0:
            continue
        prev_date = dates[di - 1] if di > 0 else _first_prev
        if prev_date is None:       # 覆盖起点前再无交易日 → 无日线上下文, 该日无法判定
            continue
        # 日级必要条件超集预筛 (策略钩子, 默认 None=不预筛): 平静日整日跳过,
        # 免建 31 槽 × 全市场快照 (B 档提速; 钩子契约见 StrategyBase.day_prefilter)
        day_sel = strat.day_prefilter(frame, pc_map)
        if day_sel is not None:
            day_sel = set(day_sel)
            if code_set is not None:
                day_sel &= code_set
            if not day_sel:
                if progress_every and (di + 1) % progress_every == 0:
                    print(f"[{di + 1}/{len(dates)}] {date} 日级预筛=0 跳过 "
                          f"累计={len(trades)} ({time.time() - t0:.0f}s)", flush=True)
                pc_map = _rollover_pc(frame, pc_map)
                continue
        else:
            day_sel = code_set
        n_sig_day = 0
        for mi in mis:
            snaps = frame.snaps_at(mi, pc_map, codes=day_sel)
            mkt = frame.mkt_gain(mi, pc_map)
            short = strat.intraday_shortlist(snaps, mkt) or {}
            for code, snap in short.items():
                if (code, date) in seen or not _st_ok(code):
                    continue                            # 每股每日首信号成交; ST 与实盘同排除
                bars = _daily_asof(code, prev_date)   # 截至D-1 (插件契约: bars[-1]=昨日)
                slot_tr = _SlotTrace() if probe is not None else None
                sigs = strat.scan_signals(
                    bars, code,
                    ctx={"latest": snap, "series": frame.series(code, mi),
                         "mkt_gain": mkt}, probe=slot_tr) or []
                if probe is not None:
                    rank = getattr(strat, "PROBE_STAGE_RANK", {})
                    stage = max((t["stage"] for t in slot_tr.items),
                                key=lambda s: rank.get(s, 0), default="no_gate")
                    dbg[code] = {"code": code, "d0_date": date,
                                 "trigger": frames_hhmm(mi), "stage": stage,
                                 "rule_trace": slot_tr.items,
                                 "ctx": {"mkt_gain": round(mkt, 2) if mkt is not None else None,
                                         "last": round(float(snap.get("last") or 0), 3)},
                                 "entry0": float(snap.get("last") or 0)}
                if not sigs:
                    continue
                s = sigs[0]
                seen.add((code, date))
                entry_price = float(snap["last"])
                if entry_price <= 0:
                    continue
                # 出场: 次交易日日线开盘 (D1 开盘卖)
                full = _daily_asof(code, None)
                nxt = next((b for b in full if str(b["time"])[:10] > date), None)
                if nxt is None or float(nxt["open"]) <= 0:
                    continue
                exit_price = float(nxt["open"])
                trades.append({
                    "code": code, "signal_date": date, "entry_date": date,
                    "entry_price": round(entry_price, 3), "buy_mode": "intraday_trigger",
                    "trigger": strat.scan_spec.entry_at or frames_hhmm(mi),
                    "exit_date": str(nxt["time"])[:10], "exit_price": round(exit_price, 3),
                    "exit_day": 1, "exit_reason": "d1_open",
                    "return_pct": round((exit_price / entry_price - 1) * 100, 2),
                    **(s.extra or {}),
                })
                n_sig_day += 1
        # debug 样本日终落盘: 以触发价为入场基准, D+1 开盘/收盘为标签 (视野不足不硬凑)
        if dbg:
            for code, rec in dbg.items():
                entry0 = rec.pop("entry0", 0)
                labels = {}
                if entry0 > 0:
                    labels["entry_trigger"] = round(entry0, 3)
                    full_d = _daily_asof(code, None)
                    nxt = next((b for b in full_d if str(b["time"])[:10] > date), None)
                    if nxt is not None and float(nxt["open"]) > 0:
                        labels["ret_d1o"] = round((float(nxt["open"]) / entry0 - 1) * 100, 2)
                        labels["ret_d1c"] = round((float(nxt["close"]) / entry0 - 1) * 100, 2)
                probe.sample(labels=labels, **rec)
            dbg.clear()
        # pc_map 结转 (当日 1m 最后一根 close)
        pc_map = _rollover_pc(frame, pc_map)
        if progress_every and (di + 1) % progress_every == 0:
            print(f"[{di + 1}/{len(dates)}] {date} shortlist后信号={n_sig_day} "
                  f"累计={len(trades)} ({time.time() - t0:.0f}s)", flush=True)
    return {"trades": trades, "stats": _summary(trades), "codes_ok": len(dates),
            "elapsed": round(time.time() - t0, 1)}


def frames_hhmm(mi):
    from app.market_cn.auto.data.frames import MI_HHMM
    return MI_HHMM[mi] if 0 <= mi < len(MI_HHMM) else ""


def _rollover_pc(frame, pc_map):
    """pc_map 结转: 当日 1m 最后一根 close (跳过的日级预筛日也必须结转, 否则次日 pc 断链)。"""
    for code in frame.codes:
        lc = frame.last_close(code)
        if lc > 0:
            pc_map[code] = lc
    return pc_map


def _summary(trades):
    """标准报告 (对齐设计文档 §6.1 + 五高指标映射, 09-09 B-5)。

    字段: 笔数/胜率/均收/盈亏比/收益五分桶/20日峰值分布与均值/月均笔数(可操作性)/
         日均收益(单位时间收益比=均收益÷持有交易日)/前后两段分段稳定性。
    """
    if not trades:
        return {"n": 0}
    rets = [t["return_pct"] for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    buckets = {"≤-10": 0, "-10~-3": 0, "-3~+3": 0, "+3~+10": 0, ">+10": 0}
    for r in rets:
        k = "≤-10" if r <= -10 else "-10~-3" if r <= -3 else \
            "-3~+3" if r < 3 else "+3~+10" if r < 10 else ">+10"
        buckets[k] += 1
    peaks = [t["peak_return_pct"] for t in trades if t.get("peak_return_pct") is not None]
    months = sorted({str(t.get("entry_date", ""))[:7] for t in trades} - {""})
    n_h = len(trades) // 2
    seg = lambda ts: round(sum(1 for t in ts if t["return_pct"] > 0) / len(ts) * 100, 1) if ts else None
    hold_days_avg = sum(t.get("exit_day") or 0 for t in trades) / len(trades)
    return {
        "n": len(trades),
        "winrate": round(len(wins) / len(trades) * 100, 1),
        "avg_ret": round(sum(rets) / len(rets), 2),
        "pl_ratio": round((sum(wins) / len(wins)) / abs(sum(losses) / len(losses)), 2)
        if wins and losses else None,
        "ret_buckets": buckets,
        "peak": {"mean": round(sum(peaks) / len(peaks), 2) if peaks else None,
                 "lt10": sum(1 for p in peaks if p < 10),
                 "10_20": sum(1 for p in peaks if 10 <= p < 20),
                 "ge20": sum(1 for p in peaks if p >= 20)},
        "monthly_avg": round(len(trades) / len(months), 1) if months else None,
        "ret_per_day": round(sum(rets) / len(rets) / hold_days_avg, 3) if hold_days_avg else None,
        "winrate_1st_half": seg(trades[:n_h]),
        "winrate_2nd_half": seg(trades[n_h:]),
    }


if __name__ == "__main__":
    import argparse
    import json
    import os

    # CLI 直跑时需自行加载 .env (服务进程已由应用加载, 重复加载无害)
    try:
        from dotenv import load_dotenv
        for _p in [os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))))), ".env"),
                os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")]:
            if os.path.isfile(_p):
                load_dotenv(_p, override=False)
                break
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="框架内全市场回测流水线 (策略经注册表分发)")
    parser.add_argument("--strategy", default="dragon",
                        help="任意已注册策略 key (dragon/v1/break/...)")
    parser.add_argument("--days", type=int, default=300)
    parser.add_argument("--codes", default="", help="逗号分隔, 空则全市场")
    parser.add_argument("--start-date", default="", help="窗口起点 (盘中策略精确复现用)")
    parser.add_argument("--end-date", default="", help="窗口终点 (默认今天)")
    parser.add_argument("--out", default="", help="结果JSON输出路径 (对数用)")
    parser.add_argument("--probe", default="",
                        help="开启调试探针并存档 (值=tag; 数据落 tmp/probes/, 供 AI 离线分析)")
    args = parser.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()] or None
    probe = None
    if args.probe:
        from app.market_cn.auto.probe import Probe
        probe = Probe(args.strategy, tag=args.probe)
    res = run_all(strategy=args.strategy, days=args.days, codes=codes,
                  start_date=args.start_date or None, end_date=args.end_date or None,
                  probe=probe)
    if probe is not None:
        probe.close()
    print("统计:", res["stats"], "| codes_ok:", res["codes_ok"],
          "| 耗时:", res["elapsed"], "s")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(res["trades"], f, ensure_ascii=False)
        # meta sidecar (P2-2): git 版本+生效参数+窗口, 溯源用; --out 本体保持纯 trades 列表
        # (不破坏既有对账脚本对纯列表的假设)
        with open(args.out + ".meta.json", "w", encoding="utf-8") as f:
            json.dump(res.get("meta", {}), f, ensure_ascii=False, indent=2)
        print("已写出:", args.out, "| meta:", args.out + ".meta.json")
