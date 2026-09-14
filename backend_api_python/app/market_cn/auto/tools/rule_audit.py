#!/usr/bin/env python3
"""auto/tools/rule_audit.py — 门级规则审计: 拦截表 + 判别力 + 换序稳定性 (2026-09-13)

定位 (param_scan 管"参数", 本工具管"规则" — 回答用户三点不直观批评):
  1. 策略到底用了哪些规则 → 段1 RULE_DEFS 清单 (声明序 = 判定短路序);
  2. 每条规则拦了多少笔、拦得对不对 → 段2 逐门拦截表: 到达 n → 通过/拦截 n/拦截%,
     通过组与被拦组各自的 胜率/均收/盈亏比/均峰值/均MAE/大肉率 + Δ (Δ>0=规则有效,
     ≤0=反指标候选 — 与 rule_stats 判别力口径一致);
     峰值口径 (2026-09-13 用户裁定, 让"大肉被拦了多少"可见): 知道出场位置 →
     peak_exit (出场模拟为准, v1 引擎, D1 当日 high 起追踪, 默认); 不知道 →
     peak60/120/180 (D1 入场日起固定窗口最高, 短线60/中线120/长线180 粗估上限);
  3. 无先后依赖的 AND 门换序后结果是否不变 → 段3 换序稳定性: 最终信号集跨序恒等
     (顺序无关性/数据完整性验证) + 门×序 拦截数矩阵 (拦截 n 随序变 = "功劳归属"是
     序的函数; 被拦组统计跨序恒等 = "门质量"与序无关)。

数据源: sample 行 extra.gates 门向量 (策略 _signal_core_dbg 产出, backtest_stock
  **fail 日也采样**)。与 rule_stats.py 平级: 本工具消费 gates 向量口径;
  dragon_callback 等 stage 口径策略请用 rule_stats.py。
调试/生产边界 (2026-09-13 用户裁定): 本工具 + 策略探针采样 (_signal_core_dbg /
  _probe_day_light / peak_maps 预计算) 全部只在 debug 链路 — 生产 probe=None 走
  短路主路径零开销、无统计输出; triple_resonance 无 config 段不进实盘 schedule。
  两层口径说明: 段2/3 纯门视角 (gates 向量, 标签=假设 D1 开盘入场的前瞻收益);
  段4 落选分布/段5 最终池用 stage 字段 (含 prefilter/engine_skip 层)。

用法:
  python -m app.market_cn.auto.tools.rule_audit --strategy triple_resonance --days 300
  python -m app.market_cn.auto.tools.rule_audit --probe-file tmp/probes/x.jsonl
  可选: --codes 600519,000001 / --label d1|d5|d10 / --peak exit|5|60|120|180 / --orders 3

易错点:
  - 行须有 extra.gates 门向量, 无此字段的行 (未适配策略/旧存档) 会被剔除并计数;
  - off 门 (开关关闭) 不参与拦截: 漏斗跳过, 清单标注 off;
  - 标签为前瞻假设收益 (labels), 只能离线分析, 绝不能回流判定/实盘路径;
  - 同 D0 去重 (used) 发生在门判定之后 — 段5 最终池按 stage=="signal" 口径另计;
  - 本工具只读 + 落 tmp/ 报告, 不改任何判定代码/参数。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time

from app.market_cn.auto.tools.rule_stats import _load_rows, _metrics

# 标签口径: ret_d1c=D1收盘卖; ret_d5o/ret_d10o=持有N日到开盘卖 (D1开盘入场基准);
# 峰值 (2026-09-13 裁定): peak_exit=出场模拟为准 (v1 引擎, D1 当日 high 起追踪);
# peak5/60/120/180=D1 入场日起固定窗口最高 (短线60/中线120/长线180 粗估上限,
# D1 起算含当日冲高 — 旧 D2 起口径低估大肉); mae5=D1 起 5 日窗口最低。
# 视野不足 (末 N 日) 不记 = censored: --days 300 时 peak120/180 大量截断, 看中线/长线请 --days 550+。
LABEL_KEYS = {"d1": "ret_d1c", "d5": "ret_d5o", "d10": "ret_d10o"}
PEAK_KEYS = {"exit": "peak_exit", "5": "peak5", "60": "peak60",
             "120": "peak120", "180": "peak180"}


def _gate_rows(rows):
    """筛出带 gates 门向量的行; 附 r["_g"]=gates。返回 (rows, 剔除数)。

    gates 位于行顶层 (_probe_day 的 extra 通道是 rec.update(extra) 展开,
    非 extra.gates 嵌套); extra.gates 作回落兼容。
    """
    out, dropped = [], 0
    for r in rows:
        g = r.get("gates")
        if not (isinstance(g, dict) and g):
            g = (r.get("extra") or {}).get("gates")
        if isinstance(g, dict) and g:
            r["_g"] = g
            out.append(r)
        else:
            dropped += 1
    return out, dropped


def _attr(gates, order):
    """归因门: order 中第一个 False 门; 全过 → 'signal'。"""
    for name in order:
        if gates.get(name) is False:
            return name
    return "signal"


def _off_gates(rows, order):
    """配置级 off 门 (所有行同值 'off') — 漏斗跳过; 混杂值=数据异常, 进警告。"""
    off, warn = set(), []
    for name in order:
        vals = {r["_g"].get(name) for r in rows}
        if vals <= {"off"}:
            off.add(name)
        elif "off" in vals:
            warn.append(f"{name} 门值混杂 {vals} (off 应为配置级, 检查 dbg 版实现)")
    return off, warn


def _order_funnel(rows, order):
    """给定序的漏斗: 每门 → (到达池, 通过池, 被拦池)。

    到达 g = 归因门不在 g 之前的门中 (前面全过); 通过 = 到达且该门非 False。
    """
    idx = {name: k for k, name in enumerate(order)}
    for r in rows:
        r["_attr"] = _attr(r["_g"], order)
    out = []
    for gname in order:
        pre = set(order[:idx[gname]])
        reach, passed, rejected = [], [], []
        for r in rows:
            if r["_attr"] in pre:
                continue
            reach.append(r)
            (rejected if r["_g"].get(gname) is False else passed).append(r)
        out.append((gname, reach, passed, rejected))
    return out


def _short(m, keys=("winrate", "avg_ret", "pl_ratio", "peak_mean", "mae_mean")):
    """_metrics 结果 → 表格片段 (缺失记 —)。"""
    return [("—" if m.get(k) is None else m.get(k)) for k in keys]


def analyze(rows, order, key_ret, key_peak):
    """基准序主分析 → (漏斗, 汇总 meta)。漏斗元素 (gname, m_in, m_pass, m_rej)。"""
    off, warns = _off_gates(rows, order)
    funnel = []
    for gname, reach, passed, rejected in _order_funnel(rows, order):
        if gname in off:
            continue          # off 门不参与拦截, 不进漏斗 (清单里标注即可)
        funnel.append((gname,
                       _metrics(reach, key_ret, key_peak, "mae5"),
                       _metrics(passed, key_ret, key_peak, "mae5"),
                       _metrics(rejected, key_ret, key_peak, "mae5")))
    final_stage = [r for r in rows if r.get("stage") == "signal"]
    stage_counts = {}
    for r in rows:
        stage_counts[r.get("stage") or "?"] = stage_counts.get(r.get("stage") or "?", 0) + 1
    meta = {"n_rows": len(rows), "final_stage": _metrics(final_stage, key_ret,
                                                         key_peak, "mae5"),
            "stage_counts": dict(sorted(stage_counts.items(),
                                        key=lambda kv: -kv[1])),
            "off": sorted(off), "warns": warns}
    return funnel, meta


def render_md(strategy, order, funnel, meta, swap, args, dropped, label_desc):
    """报告: 1 清单 / 2 拦截表 / 3 判别力 / 4 大肉分档 / 5 换序 / 6 落选 / 7 最终池。"""
    rule_defs = dict(getattr(args, "_rule_defs", None) or [])
    n_rows = meta["n_rows"]
    L = [f"# 门级规则审计 — {strategy}",
         "",
         f"- 生成 {time.strftime('%Y-%m-%d %H:%M')} | 样本 {n_rows} 决策日 "
         f"(fail 日含; 无 gates 向量剔除 {dropped} 行) | 标签口径: {label_desc}",
         f"- 分母口径: 段2/3/4 只统计标签有效样本 (censored=视野不足已排除); "
         f"段5 矩阵/段6 分布用全部 {n_rows} 行 — 两处拦截数会差一个 censored 量",
         f"- 两层口径: 段2~5=门向量视角 (前瞻假设入场); 段6/7=stage 视角 "
         f"(含 prefilter/engine_skip 层, 最终池已实际成行)",
         f"- off 门 (不参与拦截): {meta['off'] or '无'}",
         ""]
    if meta["warns"]:
        L += [f"- ⚠️ 数据警告: {'; '.join(meta['warns'])}", ""]

    # ---- 段1 规则清单 ----
    L += ["## 1. 策略规则清单 (RULE_DEFS, 序 = 判定短路序 = 换序基准)", "",
          "| # | 门 | 说明 |", "|---|---|---|"]
    for k, gname in enumerate(order, 1):
        desc = rule_defs.get(gname, "—")
        if gname in meta["off"]:
            desc += " **[off: 开关关闭]**"
        L.append(f"| {k} | {gname} | {desc} |")
    L.append("")

    # ---- 段2 逐门拦截表 + 判别力 ----
    L += ["## 2. 逐门拦截表 (基准序漏斗: 到达 → 通过/被拦)", "",
          "| 门 | 到达 n | 通过 n | 拦截 n | 拦截% |", "|---|---|---|---|---|"]
    for gname, m_in, m_p, m_r in funnel:
        reach = m_in["n"]
        rej = m_r["n"]
        pct = round(rej / reach * 100, 1) if reach else "—"
        L.append(f"| {gname} | {reach} | {m_p['n']} | {rej} | {pct} |")
    pk_name = getattr(args, "_peak_name", "peak_exit")
    L += ["", "## 3. 单门判别力 (通过组 vs 被拦组, Δ>0 = 规则有效; ≤0 = 反指标候选)", "",
          f"(均峰/大肉率按峰值口径 [{pk_name}] — 大肉率=窗口内峰值≥20% 的样本占比)",
          "",
          "| 门 | 过:n | 过:胜率 | 过:均收 | 过:PL | 过:均峰 | 过:大肉% "
          "| 拦:n | 拦:胜率 | 拦:均收 | 拦:PL | 拦:均峰 | 拦:大肉% "
          "| Δ胜率 | Δ均收 | Δ均峰 | Δ大肉% |",
          "|---|" + "---|" * 16]
    for gname, _m_in, m_p, m_r in funnel:
        if not m_p["n"] and not m_r["n"]:
            continue
        d = lambda a, b: (round(a - b, 2) if a is not None and b is not None else "—")
        wr_p, wr_r = m_p.get("winrate"), m_r.get("winrate")
        ar_p, ar_r = m_p.get("avg_ret"), m_r.get("avg_ret")
        pk_p, pk_r = m_p.get("peak_mean"), m_r.get("peak_mean")
        meat_p = (m_p.get("peak") or {}).get("≥20")
        meat_r = (m_r.get("peak") or {}).get("≥20")
        L.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} "
                 "| {} | {} | {} | {} |".format(
                     gname, m_p["n"], wr_p, ar_p,
                     "—" if m_p.get("pl_ratio") is None else m_p["pl_ratio"],
                     "—" if pk_p is None else pk_p,
                     "—" if meat_p is None else meat_p,
                     m_r["n"], wr_r, ar_r,
                     "—" if m_r.get("pl_ratio") is None else m_r["pl_ratio"],
                     "—" if pk_r is None else pk_r,
                     "—" if meat_r is None else meat_r,
                     d(wr_p, wr_r), d(ar_p, ar_r), d(pk_p, pk_r),
                     d(meat_p, meat_r)))
    L.append("")

    # ---- 段4 大肉分档 (2026-09-13 用户裁定: 3-5 档占比, 调试看各档大肉被拦多少) ----
    L += ["## 4. 大肉分档 (峰值档占比%, 过/拦两组对比)", "",
          f"(峰值口径 [{pk_name}] 同表3; 分档与 rule_stats PEAK_BUCKETS 同口径)", "",
          "| 门 | 组 | n | <5 | 5~10 | 10~20 | ≥20 |",
          "|---|---|---|---|---|---|---|"]
    for gname, _mi, m_p, m_r in funnel:
        for tag, m in (("过", m_p), ("拦", m_r)):
            if not m["n"]:
                continue
            pk = m.get("peak") or {}
            L.append("| {} | {} | {} | {} | {} | {} | {} |".format(
                gname, tag, m["n"], pk.get("<5", "—"), pk.get("5~10", "—"),
                pk.get("10~20", "—"), pk.get("≥20", "—")))
    L += ["", "> 读法: 通过组高占比档=该门放进来的肉; 被拦组高占比档=该门拦掉的肉 — "
          "被拦组 ≥20% 占比显著高于通过组 = 门在拦大肉 (反指标实锤)。", ""]

    # ---- 段5 换序稳定性 ----
    L += ["## 5. 换序稳定性 (AND 门顺序无关性验证)",
          "",
          f"- 完整性: {n_rows} 行门向量键集与声明门集 "
          f"{'一致 ✓' if swap['intact'] else '存在缺键 ✗ (缺键行 ' + str(swap['missing_rows']) + ')'}"
          f"; 换 {len(swap['orders'])} 个序, 每序最终通过 n = "
          f"{sorted(set(swap['final_ns']))} "
          f"({'跨序恒等 ✓ (AND 门独立, 顺序只影响归因归属)' if len(set(swap['final_ns'])) == 1 else '✗ 不恒等 — 检查 dbg 版判定'})",
          "",
          "门×序 拦截数矩阵 (值 = 该序下此门作为归因门拦下的行数):",
          ""]
    hdr = ["门"] + [nm for nm, _o in swap["orders"]] + ["被拦组均收 (跨序恒等)"]
    L += ["| " + " | ".join(hdr) + " |",
          "|---|" + "---|" * (len(swap["orders"]) + 1)]
    for gname in order:
        if gname in meta["off"]:
            continue
        m_r = next(m_r for g, _mi, _mp, m_r in funnel if g == gname)
        cells = [str(swap["matrix"].get((gname, nm), 0))
                 for nm, _o in swap["orders"]]
        L.append(f"| {gname} | " + " | ".join(cells) + f" | {m_r.get('avg_ret', '—')} |")
    L += ["",
          "> 读法: 拦截数随序变化 = 归因功劳归属变化 (序决定把样本判给哪个门); "
          "被拦组均收恒等 = 门自身判别力与序无关 (组定义只依赖门本身)。"
          "若最终通过 n 跨序不恒等 → 门判定存在顺序耦合 bug, 须回查策略。", ""]

    # ---- 段5 落选分布 (stage 口径) ----
    L += ["## 6. 落选分布 (stage 口径, 含门后 prefilter/engine_skip 层)", "",
          "| stage | 行数 | 占比 |", "|---|---|---|"]
    for s, n in meta["stage_counts"].items():
        L.append(f"| {s} | {n} | {round(n / n_rows * 100, 1) if n_rows else '—'}% |")
    L.append("")

    # ---- 段6 最终信号池 ----
    fm = meta["final_stage"]
    L += ["## 7. 最终信号池 (gates 全过且实际成行 stage==signal)",
          "",
          f"- {fm['n']} 行 (censored={fm.get('censored')}; 同 D0 去重后实际交易"
          f"更少 — 去重发生在门判定之后): 胜率 {fm.get('winrate')}% / "
          f"均收 {fm.get('avg_ret')}% / 盈亏比 {fm.get('pl_ratio')} / "
          f"均峰值 {fm.get('peak_mean')}% / 均MAE {fm.get('mae_mean')}% / "
          f"大肉率(峰≥20%) {(fm.get('peak') or {}).get('≥20', '—')}%",
          f"- 两段胜率 前 {fm.get('wr_1st')}% / 后 {fm.get('wr_2nd')}% "
          f"(差异大 = 环境依赖)", "",
          "> 判读纪律: Δ≤0 的门 = 无效/反指标候选 (放宽或删除前先换窗口复验); "
          "**规则改动必须重跑 tmp/_dbg_equiv.py 等价性单测 + 两段稳定性验证。**", ""]
    return "\n".join(L)


def run_swap(rows, base_order, n_rand, seed):
    """换序实验: 返回 {intact, missing_rows, final_ns, orders, matrix}。

    matrix[(gname, 序名)] = 该序下归因到 gname 的行数。
    """
    decl = set(base_order)
    missing_rows = sum(1 for r in rows if set(r["_g"]) != decl)
    orders = [("基准序", list(base_order)),
              ("反转序", list(reversed(base_order)))]
    rng = random.Random(seed)
    for k in range(n_rand):
        o = list(base_order)
        rng.shuffle(o)
        orders.append((f"随机{k + 1}", o))
    matrix, final_ns = {}, []
    final_sets = []
    for nm, o in orders:
        fin_n = 0
        fin_keys = set()
        for r in rows:
            a = _attr(r["_g"], o)
            if a == "signal":
                fin_n += 1
                fin_keys.add((r.get("code"), r.get("d0_date")))
            else:
                matrix[(a, nm)] = matrix.get((a, nm), 0) + 1
        final_ns.append(fin_n)
        final_sets.append(fin_keys)
    intact = all(s == final_sets[0] for s in final_sets[1:])
    return {"intact": intact and missing_rows == 0, "missing_rows": missing_rows,
            "final_ns": final_ns, "orders": orders, "matrix": matrix}


def main():
    ap = argparse.ArgumentParser(description="门级规则审计 (拦截表+判别力+换序稳定性)")
    ap.add_argument("--strategy", default="triple_resonance")
    ap.add_argument("--days", type=int, default=300, help="回测窗口 (自然日)")
    ap.add_argument("--codes", default="", help="逗号分隔; 空=全市场 (慢)")
    ap.add_argument("--label", choices=sorted(LABEL_KEYS), default="d5",
                    help="收益标签口径: d1=D1收盘卖 / d5=D5开盘卖 / d10=D10开盘卖")
    ap.add_argument("--peak", choices=sorted(PEAK_KEYS), default="exit",
                    help="峰值口径: exit=出场模拟为准(默认, 知道出场位置) / "
                         "5|60|120|180=D1起固定窗口最高 (不知道出场位置时粗估, "
                         "短线60/中线120/长线180)")
    ap.add_argument("--orders", type=int, default=3,
                    help="换序实验的随机序数量 (另有基准序+反转序)")
    ap.add_argument("--seed", type=int, default=42, help="随机序种子 (可复现)")
    ap.add_argument("--probe-file", default="", help="复用已存档探针 JSONL (跳过回测)")
    ap.add_argument("--out", default="", help="markdown 报告路径 (默认 tmp/规则审计_<策略>_<ts>.md)")
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
    rule_defs = list(getattr(strat, "RULE_DEFS", None) or [])
    if not rule_defs:
        print(f"{args.strategy} 未声明 RULE_DEFS (未接门级归因), "
              f"请用 rule_stats.py (stage 口径) 或先给策略适配", file=sys.stderr)
        return 1
    args._rule_defs = rule_defs
    base_order = [g for g, _d in rule_defs]
    key_ret = LABEL_KEYS[args.label]
    key_peak = PEAK_KEYS[args.peak]
    args._peak_name = key_peak
    peak_desc = {"exit": "peak_exit (出场模拟为准: stop/trail/hold, D1 当日 high 起追踪)",
                 "5": "peak5 (D1 起 5 日最高)", "60": "peak60 (D1 起 60 日最高, 短线粗估)",
                 "120": "peak120 (D1 起 120 日最高, 中线粗估)",
                 "180": "peak180 (D1 起 180 日最高, 长线粗估)"}[args.peak]
    label_desc = {"d1": "ret_d1c (D1 收盘卖)", "d5": "ret_d5o (持有5日到D5开盘卖)",
                  "d10": "ret_d10o (持有10日到D10开盘卖)"}[args.label] + \
        f"; 峰值={peak_desc}; MAE=mae5 (D1 起 5 日最低)"

    if args.probe_file:
        raw = _load_rows(args.probe_file)
        print(f"复用探针存档: {args.probe_file} → {len(raw)} 行 sample")
    else:
        from app.market_cn.auto.backtest import run_all
        from app.market_cn.auto.probe import Probe
        codes = [c.strip() for c in args.codes.split(",") if c.strip()] or None
        t0 = time.time()
        with Probe(args.strategy, tag=f"audit{args.days}") as pr:
            res = run_all(strategy=args.strategy, days=args.days, codes=codes,
                          probe=pr)
        raw = _load_rows(pr.path)
        print(f"回测完成: {res['stats'].get('n')} 笔信号 / {len(raw)} 行 sample "
              f"({time.time() - t0:.0f}s)")

    rows, dropped = _gate_rows(raw)
    if not rows:
        print("无带 gates 门向量的 sample 行 (旧存档或策略未适配), 无法审计",
              file=sys.stderr)
        return 1
    funnel, meta = analyze(rows, base_order, key_ret, key_peak)
    swap = run_swap(rows, base_order, args.orders, args.seed)
    md = render_md(args.strategy, base_order, funnel, meta, swap, args,
                   dropped, label_desc)

    d = os.path.dirname(os.path.abspath(__file__))
    root = os.path.normpath(os.path.join(d, "..", "..", "..", "..", ".."))
    out = args.out or os.path.join(root, "tmp",
                                   f"规则审计_{args.strategy}_{time.strftime('%Y%m%d_%H%M')}.md")
    out = os.path.normpath(out)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(md)
    print("报告:", os.path.abspath(out))
    print("\n" + md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
