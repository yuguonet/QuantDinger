#!/usr/bin/env python3
"""auto/tools/rule_stats.py — 入场规则归因统计 (2026-09-10, 龙回头低胜率/低盈亏比排查)

用途: 把"入场规则链"拆成两级统计, 用**同一条固定出场规则**衡量每个规则的贡献,
     让规则改进不再靠猜 (dragon 预筛同款方法论: 统计先行, 规则后改)。

  A. 漏斗表  逐级池: 到达规则 g 的样本 → 通过 g 后的样本, 各池的收益/峰值分布。
  B. 判别力表  规则 g 的 通过池 vs 被拒池: Δ胜率/Δ均收/Δ盈亏比/Δ均峰值 —
     正数=规则有效; **负数或接近 0 = 该规则是反指标/无效** (历史上"放量过滤"即如此)。

出场口径: 标签由**策略文件内**的调试段生成 (2026-09-10 裁定: 调策略只改策略文件,
     框架层 probe.py 零改动)。龙回头在 strategies/dragon_callback.py 顶部:
     DEBUG_HOLD_DAYS(固定持有N日) / DEBUG_TRAILS(峰值回撤多档) / DEBUG_WAVE_DAYS(波次窗口)。
     本工具只负责**按键名读取并统计** (--exit/--trail 选择分析哪一档), 不注入口径;
     改口径去改策略文件的 DEBUG_* 常量后重跑。

数据来源: 策略探针 sample 行 (stage=当日最深判定阶段 + 策略调试标签)。
     用法一 (跑新鲜数据): --strategy dragon_callback --days 600
     用法二 (复用存档):   --probe-file tmp/probes/dragon_callback_xxx.jsonl
     策略无关: 门名与顺序读自策略类属性 PROBE_STAGE_RANK (框架不知道策略的门)。

用法:
  python -m app.market_cn.auto.tools.rule_stats --strategy dragon_callback --days 600
  python -m app.market_cn.auto.tools.rule_stats --probe-file tmp/probes/x.jsonl --out tmp/rule_stats.md

易错点:
  - stage 是"当日最深阶段" (取该日所有候选 lu 中 rank 最大者), 故"通过前 k 门"
    = 至少有一个候选走到第 k 门, 与实盘"多候选重锚定"语义一致;
  - 视野不足 (末 days 日) 的样本 ret 记 None = censored, 统计时排除并在表内标注;
  - 本工具只读 + 落 tmp/ 报告, 不改任何判定代码/参数。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

# peak 区间分布桶 (单位 %): 与"是否有逃顶空间"直接相关
PEAK_BUCKETS = (("<5", lambda p: p < 5), ("5~10", lambda p: 5 <= p < 10),
                ("10~20", lambda p: 10 <= p < 20), ("≥20", lambda p: p >= 20))


def _load_rows(probe_file):
    rows = []
    with open(probe_file, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if rec.get("kind") == "sample":
                rows.append(rec)
    return rows


def _eff_rank(stage, rank_map, signal_rank):
    """stage → 有效名次: 被拒=该门 rank; 通过全部=signal_rank; engine_skip 视同通过。"""
    if stage in ("signal", "engine_skip"):
        return signal_rank
    return rank_map.get(stage, 0)


def _metrics(rows, key_ret="ret_d7c", key_peak="peak7", key_mae="mae7",
             key_cap=None, key_day=None, key_rsn=None):
    """一组样本的出场口径统计 (胜率/均收/盈亏比/峰值分布/均峰/均MAE + 两段胜率)。

    key_cap/key_day/key_rsn: 仅峰值回撤口径有 (捕获率/持有日/出场原因)。
    """
    rets, peaks, maes, dates = [], [], [], []
    caps, days, rsns = [], [], []
    censored = late = 0
    for r in rows:
        lb = r.get("labels") or {}
        v = lb.get(key_ret)
        if v is None:
            # late = 入场日已超出波次窗口 (信号被多轮筛选推后太久) → 自然淘汰, 非视野截断
            if key_rsn and lb.get(key_rsn) == "late":
                late += 1
            else:
                censored += 1
            continue
        rets.append(float(v))
        dates.append(str(r.get("d0_date") or ""))
        if lb.get(key_peak) is not None:
            peaks.append(float(lb[key_peak]))
        if lb.get(key_mae) is not None:
            maes.append(float(lb[key_mae]))
        if key_cap and lb.get(key_cap) is not None:
            caps.append(float(lb[key_cap]))
        if key_day and lb.get(key_day) is not None:
            days.append(float(lb[key_day]))
        if key_rsn:
            rsns.append(str(lb.get(key_rsn) or "?"))
    n = len(rets)
    out = {"n": n, "censored": censored, "late": late}
    if not n:
        return out
    wins = [x for x in rets if x > 0]
    losses = [x for x in rets if x <= 0]
    out["winrate"] = round(len(wins) / n * 100, 1)
    out["avg_ret"] = round(sum(rets) / n, 2)
    out["pl_ratio"] = (round((sum(wins) / len(wins)) / abs(sum(losses) / len(losses)), 2)
                       if wins and losses else None)
    if peaks:
        out["peak_mean"] = round(sum(peaks) / len(peaks), 2)
        out["peak"] = {}
        for name, fn in PEAK_BUCKETS:
            out["peak"][name] = round(sum(1 for p in peaks if fn(p)) / len(peaks) * 100, 1)
        out["peak_ge10"] = round(sum(1 for p in peaks if p >= 10) / len(peaks) * 100, 1)
    if maes:
        out["mae_mean"] = round(sum(maes) / len(maes), 2)
    if caps:
        out["cap_mean"] = round(sum(caps) / len(caps), 2)
    if days:
        out["day_mean"] = round(sum(days) / len(days), 1)
    if rsns:
        c = {}
        for x in rsns:
            c[x] = c.get(x, 0) + 1
        out["rsn"] = {k: round(v / len(rsns) * 100, 1) for k, v in
                      sorted(c.items(), key=lambda kv: -kv[1])}
    # 两段稳定性 (按 d0_date 排序取前后半; 每段胜率, 差异大=环境依赖)
    pairs = sorted(zip(dates, rets))
    h = len(pairs) // 2
    if h:
        seg = lambda ps: round(sum(1 for _, v in ps if v > 0) / len(ps) * 100, 1) if ps else None
        out["wr_1st"] = seg(pairs[:h])
        out["wr_2nd"] = seg(pairs[-h:])
    return out


def _fmt(m, keys=("n", "winrate", "avg_ret", "pl_ratio", "peak_mean", "peak_ge10", "mae_mean")):
    g = lambda k: "—" if m.get(k) is None else m.get(k)
    return " | ".join(str(g(k)) for k in keys)


def analyze(rows, rank_map, days=7, label=None, exit_mode="hold",
            trail=12.0, max_days=10):
    """主分析: 返回 (漏斗表, 判别力表, 汇总 dict)。

    exit_mode: hold=固定持有 days 日收盘卖 (衡量"第N日点位");
               trail=峰值回撤 trail% 出场 (衡量"这笔行情给了多少可捕获空间", 可操作)。
    """
    if exit_mode == "trail":
        sf = f"{trail:g}"
        key_ret, key_peak = f"ret_tr{sf}", f"peak_tr{sf}"
        key_mae, key_cap = f"mae_tr{sf}", f"cap_tr{sf}"
        key_day, key_rsn = f"day_tr{sf}", f"rsn_tr{sf}"
    else:
        key_ret, key_peak = f"ret_d{days}c", f"peak{days}"
        key_mae, key_cap, key_day, key_rsn = f"mae{days}", None, None, None
    kw = dict(key_cap=key_cap, key_day=key_day, key_rsn=key_rsn)
    if rank_map:
        signal_rank = rank_map.get("signal", max(rank_map.values()) + 1)
    else:
        signal_rank = 1
    gates = sorted(((s, r) for s, r in rank_map.items()
                    if s not in ("signal", "engine_skip") and r < signal_rank),
                   key=lambda x: x[1])
    for r in rows:
        r["_eff"] = _eff_rank(r.get("stage"), rank_map, signal_rank)

    funnel, power = [], []
    for i, (gname, grank) in enumerate(gates):
        pool_in = [r for r in rows if r["_eff"] >= grank]
        rej = [r for r in rows if r.get("stage") == gname]
        pool_out = [r for r in rows if r["_eff"] > grank]
        m_in, m_out, m_rej = (_metrics(pool_in, key_ret, key_peak, key_mae, **kw),
                              _metrics(pool_out, key_ret, key_peak, key_mae, **kw),
                              _metrics(rej, key_ret, key_peak, key_mae, **kw))
        funnel.append({"rule": gname, "rank": grank, "pool_in": m_in, "pool_out": m_out})
        delta = {k: (round(m_out[k] - m_rej[k], 2) if m_out.get(k) is not None
                     and m_rej.get(k) is not None else None)
                 for k in ("winrate", "avg_ret", "pl_ratio", "peak_mean")}
        power.append({"rule": gname, "rank": grank, "n_pass": m_out["n"],
                      "n_reject": m_rej["n"], "pass": m_out, "reject": m_rej,
                      "delta": delta})
    final = [r for r in rows if r["_eff"] >= signal_rank]
    final_m = _metrics(final, key_ret, key_peak, key_mae, **kw)
    return funnel, power, {"final": final_m, "n_rows": len(rows),
                           "days": days, "label": label,
                           "exit_mode": exit_mode, "trail": trail,
                           "max_days": max_days,
                           "stage_counts": _stage_counts(rows)}


def _stage_counts(rows):
    c = {}
    for r in rows:
        s = r.get("stage") or "?"
        c[s] = c.get(s, 0) + 1
    return dict(sorted(c.items(), key=lambda x: -x[1]))


def render_md(strategy, funnel, power, meta):
    d = meta["days"]
    tr = meta.get("trail")
    is_tr = meta.get("exit_mode") == "trail"
    if is_tr:
        wd = meta.get("wave_days") or 20
        exit_desc = (f"**峰值回撤 {tr:g}% 出场** (入场=D+1 开盘; 逐日 peak=max(high), "
                     f"收盘跌破 peak×(1-{tr:g}%) 即出场)。"
                     f"**波次窗口 = 从第一条规则(找龙)通过日起 {wd} 个交易日**, 峰值"
                     f"**仅从入场日**起追踪 (入场前涨幅买不到, 不计入可捕获空间) — "
                     f"买入日被多轮筛选推后越久 → 剩余窗口越短、入场价越高 → 自然淘汰")
    else:
        exit_desc = (f"**固定持有 {d} 个交易日** (入场=D+1 开盘, 第 {d} 日收盘无条件卖出), "
                     f"排除出场引擎差异")
    L = [f"# 入场规则归因统计 — {strategy}",
         "",
         f"- 出场口径: {exit_desc}",
         f"- 样本: {meta['n_rows']} 个决策日 (探针 sample 行, 廉价预筛已跳过噪声日)",
         f"- 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
         "",
         "## 1. 落选阶段分布 (样本在哪一步被拒)",
         "",
         "| stage | 样本数 |", "|---|---|"]
    for s, n in meta["stage_counts"].items():
        L.append(f"| {s} | {n} |")
    L += ["", "## 2. 漏斗表 (逐级池: 到达该门 → 通过该门)", "",
          "| 规则 | 到达池 n | 胜率 | 均收 | 盈亏比 | 均峰值 | 峰值≥10% | 均MAE "
          + ("| 均捕获 | 持有日 " if is_tr else "")
          + "| → 通过池 n | 胜率 | 均收 | 盈亏比 | 均峰值 |",
          "|---|---|---|---|---|---|---|" + ("---|---" if is_tr else "") + "|---|---|---|---|---|"]
    for f in funnel:
        a, b = f["pool_in"], f["pool_out"]
        row = "| {} | {} | {} | {} | {} | {} | {} | {} |".format(
            f["rule"], a["n"], a.get("winrate", "—"), a.get("avg_ret", "—"),
            a.get("pl_ratio", "—"), a.get("peak_mean", "—"), a.get("peak_ge10", "—"),
            a.get("mae_mean", "—"))
        if is_tr:
            row += " {} | {} |".format(a.get("cap_mean", "—"), a.get("day_mean", "—"))
        row += " → {} | {} | {} | {} | {} |".format(
            b["n"], b.get("winrate", "—"), b.get("avg_ret", "—"),
            b.get("pl_ratio", "—"), b.get("peak_mean", "—"))
        L.append(row)
    L += ["", "## 3. 单规则判别力 (通过池 vs 被拒池, Δ>0 = 规则有效)", "",
          "| 规则 | 通过 n | 被拒 n | 通过胜率 | 被拒胜率 | Δ胜率 | 通过均收 | 被拒均收 | Δ均收 | 通过盈亏比 | Δ盈亏比 | 通过均峰值 | Δ均峰值 |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for p in power:
        a, b, dd = p["pass"], p["reject"], p["delta"]
        L.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
            p["rule"], p["n_pass"], p["n_reject"], a.get("winrate", "—"),
            b.get("winrate", "—"), dd.get("winrate"), a.get("avg_ret", "—"),
            b.get("avg_ret", "—"), dd.get("avg_ret"), a.get("pl_ratio", "—"),
            dd.get("pl_ratio"), a.get("peak_mean", "—"), dd.get("peak_mean")))
    L += ["", "## 4. 各池峰值区间分布 (%)", "",
          "| 规则池 | 样本 | <5 | 5~10 | 10~20 | ≥20 | 两段胜率(前/后) |",
          "|---|---|---|---|---|---|---|"]
    for f in funnel:
        pk = f["pool_in"].get("peak") or {}
        L.append("| 到达 {} | {} | {} | {} | {} | {} | {}/{} |".format(
            f["rule"], f["pool_in"]["n"], pk.get("<5", "—"), pk.get("5~10", "—"),
            pk.get("10~20", "—"), pk.get("≥20", "—"),
            f["pool_in"].get("wr_1st", "—"), f["pool_in"].get("wr_2nd", "—")))
    fm = meta["final"]
    fpk = fm.get("peak") or {}
    L.append("| **最终信号池** | {} | {} | {} | {} | {} | {}/{} |".format(
        fm["n"], fpk.get("<5", "—"), fpk.get("5~10", "—"), fpk.get("10~20", "—"),
        fpk.get("≥20", "—"), fm.get("wr_1st", "—"), fm.get("wr_2nd", "—")))
    L += ["", "## 5. 最终信号池 (通过全部规则)",
          "",
          f"- 样本 {fm['n']} 笔 (censored={fm.get('censored')}, "
          f"late={fm.get('late')}=入场超出波次窗口被淘汰): "
          f"胜率 {fm.get('winrate')}% / 均收 {fm.get('avg_ret')}% / 盈亏比 {fm.get('pl_ratio')} "
          f"/ 均峰值 {fm.get('peak_mean')}% / 均MAE {fm.get('mae_mean')}%",
          f"- 两段胜率 前 {fm.get('wr_1st')}% / 后 {fm.get('wr_2nd')}% "
          f"(差异大 = 环境依赖, 换窗口就塌)"]
    if is_tr:
        rsn = fm.get("rsn") or {}
        L.append(f"- 均捕获率 {fm.get('cap_mean')} (= 实得收益/期间峰值, 越高越贴近逃顶) "
                 f"/ 平均持有 {fm.get('day_mean')} 日")
        if rsn:
            L.append("- 出场原因: " + ", ".join(f"{k} {v}%" for k, v in rsn.items()))
    L += ["",
          "> 读法: Δ胜率/Δ均收接近 0 或为负的规则 = 无效或反指标候选 (可放宽/删除); "
          "被拒池明显好于通过池的规则需优先复核。**规则改动后必须回归 + 两段稳定性验证。**",
          ""]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="入场规则归因统计 (固定持有口径)")
    ap.add_argument("--strategy", default="dragon_callback")
    ap.add_argument("--days", type=int, default=600, help="回测窗口 (自然日)")
    ap.add_argument("--hold", type=int, default=7, help="固定持有交易日数 (hold 口径出场)")
    ap.add_argument("--exit", dest="exit_mode", choices=("hold", "trail"), default="hold",
                    help="hold=固定持有N日收盘卖 (衡量第N日点位); "
                         "trail=峰值回撤X%%出场 (衡量行情给出多少可捕获空间)")
    ap.add_argument("--trail", default="12",
                    help="峰值回撤阈值%%; 逗号分隔可多档并存 (一次回测扫描多档, 主口径取首个)")
    ap.add_argument("--max-hold", type=int, default=10,
                    help="(仅报告描述) trail 口径最大持有交易日; 实际由策略内 "
                         "DEBUG_MAX_HOLD 决定")
    ap.add_argument("--wave-days", type=int, default=20,
                    help="(仅报告描述/兜底) 波次窗口: 从第一条规则(找龙)通过日起 N 个交易日; "
                         "实际由策略内 DEBUG_WAVE_DAYS 决定")
    ap.add_argument("--codes", default="", help="逗号分隔; 空=全市场")
    ap.add_argument("--probe-file", default="", help="复用已存档探针 JSONL (跳过回测)")
    ap.add_argument("--out", default="", help="markdown 报告输出路径 (默认 tmp/规则归因_<策略>_<ts>.md)")
    ap.add_argument("--json-out", default="", help="原始统计 JSON 输出路径")
    ap.add_argument("--full", action="store_true",
                    help="(已废弃, 保留兼容) 探针始终保留完整特征窗口")
    args = ap.parse_args()

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
    strat = strat_reg.get_strategy(args.strategy)
    if strat is None:
        print(f"strategy={args.strategy} 未注册", file=sys.stderr)
        return 1
    rank_map = dict(getattr(strat, "PROBE_STAGE_RANK", {}) or {})
    if not rank_map:
        print(f"{args.strategy} 无 PROBE_STAGE_RANK (未接探针?), 无法归因", file=sys.stderr)
        return 1
    # 调试标签口径 (策略文件内常量; 框架只读不注入)
    dbg_trails = tuple(float(x) for x in (getattr(strat, "DEBUG_TRAILS", ()) or ()))
    dbg_hold = int(getattr(strat, "DEBUG_HOLD_DAYS", 0) or 0)
    dbg_wave = int(getattr(strat, "DEBUG_WAVE_DAYS", args.wave_days) or 0)
    if dbg_hold and args.exit_mode == "hold" and dbg_hold != args.hold:
        print(f"[warn] 策略 DEBUG_HOLD_DAYS={dbg_hold} 与 --hold {args.hold} 不符 "
              f"(以策略文件为准)", file=sys.stderr)
        args.hold = dbg_hold
    print(f"策略调试口径: hold={dbg_hold}日 trails={dbg_trails or '(无)'} "
          f"wave={dbg_wave}日")

    trails = [float(x) for x in str(args.trail).split(",") if x.strip()]
    if args.probe_file:
        rows = _load_rows(args.probe_file)
        print(f"复用探针存档: {args.probe_file} → {len(rows)} 行 sample")
        if args.exit_mode == "trail":
            k = f"ret_tr{trails[0]:g}"
            if rows and not any((r.get("labels") or {}).get(k) is not None for r in rows):
                print(f"[warn] 存档无 {k} 标签 (口径不符; 请用 --trail 匹配或去掉 --probe-file 重跑)",
                      file=sys.stderr)
    else:
        from app.market_cn.auto.backtest import run_all
        from app.market_cn.auto.probe import Probe
        # 标签由策略文件内的 DEBUG_* 常量生成 (框架不注入口径 — 2026-09-10 裁定);
        # 这里只校验所选档位确实会被生成, 避免"分析了一个存档里没有的键"。
        if dbg_trails and trails and trails[0] not in dbg_trails:
            print(f"[warn] --trail {trails[0]:g} 不在策略 {args.strategy}.DEBUG_TRAILS="
                  f"{dbg_trails} 中 (改口径请改策略文件)", file=sys.stderr)
        codes = [c.strip() for c in args.codes.split(",") if c.strip()] or None
        t0 = time.time()
        tag = (f"rule{args.hold}" if args.exit_mode == "hold"
               else f"w{dbg_wave}tr{trails[0]:g}")
        with Probe(args.strategy, tag=tag) as pr:
            res = run_all(strategy=args.strategy, days=args.days, codes=codes, probe=pr)
        rows = _load_rows(pr.path)
        print(f"回测完成: {res['stats'].get('n')} 笔信号 / {len(rows)} 行 sample "
              f"({time.time() - t0:.0f}s)")

    funnel, power, meta = analyze(rows, rank_map, days=args.hold,
                                  label=args.probe_file or f"{args.strategy}@{args.days}d",
                                  exit_mode=args.exit_mode, trail=trails[0],
                                  max_days=args.max_hold)
    meta["wave_days"] = dbg_wave
    md = render_md(args.strategy, funnel, power, meta)
    ts = time.strftime("%Y%m%d_%H%M")
    out = args.out or os.path.join("..", "..", "..", "..", "tmp",
                                   f"规则归因_{args.strategy}_{ts}.md")
    out = os.path.normpath(out)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(md)
    print("报告:", os.path.abspath(out))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump({"funnel": funnel, "power": power, "meta": meta},
                      f, ensure_ascii=False, indent=2)
        print("原始统计:", os.path.abspath(args.json_out))
    print("\n" + md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
