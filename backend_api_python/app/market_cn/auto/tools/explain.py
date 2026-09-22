#!/usr/bin/env python3
"""auto/tools/explain.py — 门表策略「机器可读报告」器 (M6, 2026-09-20)

定位 (架构 §8「LLM 协作」+ §12-M6「LLM 分析闭环」):
  产出一份**同源双形态**报告 —— JSON(给 LLM 读) + MD(给人看) —— 让 LLM 的职责收敛为
  "读报告 → 建议加/删/改哪道门、调哪个参数", 产出物只是 `<key>.yaml` 里的一行改动
  (天然可审计、可回滚, 见 tools/proposal.py)。

与既有工具的分工 (不重复造轮子):
  - 门漏斗/判别力: **口径照抄 tools/rule_audit.py** (`_order_funnel` + `rule_stats._metrics`)，
    保证与旧报告数字可比；
  - 参数敏感性: **复用 tools/param_scan.py** 的 `_auto_grid` / `_seg_stats`（可选段）；
  - 信号级复验: 不在此工具内跑, 由 proposal.validate 走 tools/pool_check.py (红线②)。
  - 与 rule_audit/param_scan 不同点: 本工具走 **门表路径**(core/runtime/evaluate.run_backtest),
    而非插件 + RULE_DEFS 路径 —— 这正是 M5 后新架构的主路径。

数据来源 (M6-1 新增诊断钩子):
  `run_backtest(..., gate_dbg=cb)` 在 **gate_dbg=None 时零开销**; 非 None 时每次门求值额外
  回调全门布尔向量 `cb(phase, code, i, lu_idx, {gate_id: bool})`。本工具按 (股, 决策日) 聚合。

命令:
  python -m app.market_cn.auto.tools.explain --strategy dragon_callback --days 300
  python -m app.market_cn.auto.tools.explain --strategy v1 --days 300 --label d5 --peak 5
  # 冒烟 (抽样股, 数字不可与全市场对照):
  python -m app.market_cn.auto.tools.explain --strategy break --sample-codes 250
  # 附参数敏感性 (多跑若干单维切片, 更慢):
  python -m app.market_cn.auto.tools.explain --strategy v1 --with-sensitivity

易错点:
  - 标签口径: `sample_feats` 提供 ret_d1c/ret_d5o/ret_d10o + peak5 + mae5 —— 均为
    **自包含**(不依赖策略出场引擎), 故 peak 只支持 5(默认); peak_exit 需出场模拟,
    不在门表门向量口径内 (要 peak_exit 请用 rule_audit 的出场口径)。
  - limit_up 类按 (决策日, lu_idx) 多次求值: 本工具按 (股, 日) 聚合, decision 相取**最后一次**
    (= 决定该日结论的那次尝试; 命中即 break, 未命中即最深拒绝)。
  - 探针 JSONL 与 rule_audit 兼容 (行含 `gates` 字段): 可直接
    `rule_audit --probe-file <本工具产出> --label d5 --peak 5` 复算比对。
  - **只读**: 不改任何判定代码/参数; 输出全部落 tmp/ 非权威。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time

from app.market_cn.auto.core._paths import PROJECT_ROOT

# 项目根 = backend_api_python 的上一级 (D:\QuantDinger); tmp/ 与 probe.py 的落点同级。
_REPO_ROOT = os.path.dirname(PROJECT_ROOT)
_TMP_DIR = os.path.join(_REPO_ROOT, "tmp")

# 标签口径映射 (与 rule_audit 同名 keys, 但仅用 sample_feats 自包含的键)
LABEL_KEYS = {"d1": "ret_d1c", "d5": "ret_d5o", "d10": "ret_d10o"}
PEAK_KEYS = {"5": "peak5"}
# 门表门向量可覆盖的阶段: qualify(资格) / decision(判定) / all(一次性)
_PHASES = ("qualify", "decision", "all")


# ================================================================
# 门向量采集 (gate_dbg 回调)
# ================================================================

class GateCollector:
    """按 (code, 决策日索引) 聚合门向量。

    limit_up 类: 同一天会先 fire qualify (一次) 再 fire decision (每个 lu 候选一次, 命中即 break)。
    day 类: 每天只 fire all (一次)。
    聚合规则: all 直接取; 否则 qualify 打底 + decision 覆盖 (decision 保留最后一次 = 决定该日
    结论的那次尝试)。
    """

    def __init__(self):
        self.by_key = {}      # (code, i) -> {phase: vec}

    def __call__(self, phase, code, i, lu_idx, vec):
        d = self.by_key.setdefault((code, i), {})
        d[phase] = dict(vec)

    def merged(self):
        """→ {(code, i): {gate_id: bool}} (仅当日至少 fire 过一次的键)。"""
        out = {}
        for key, d in self.by_key.items():
            if "all" in d:
                out[key] = dict(d["all"])
                continue
            v = {}
            if "qualify" in d:
                v.update(d["qualify"])
            if "decision" in d:
                v.update(d["decision"])
            out[key] = v
        return out


def _first_false(gate_order, vec):
    """归因门: order 中第一个 False → 该门 id; 全过 → 'signal' (与 rule_audit._attr 同义)。"""
    for gid in gate_order:
        if vec.get(gid) is False:
            return gid
    return "signal"


# ================================================================
# 跑门表回测 + 采门向量
# ================================================================

def run_explain_backtest(spec, days, codes, sample_codes=0, seed=42, progress_every=0):
    """按门表路径跑回测, 采集 (rows, trades, pool_mode)。

    rows: 每行 = 一个 (股, 决策日) 的门向量 + 标签 (供漏斗/rule_audit 消费)。
    trades: 门表回测实际成行的交易 (供两段稳定性/最终池)。
    """
    from app.market_cn.auto.core.data.hub import all_codes
    from app.market_cn.auto.core.data.kline import fetch_klines_batch
    from app.market_cn.auto.core.data.hub import stock_info as _hub_stock_info
    from app.market_cn.auto.core.runtime.evaluate import run_backtest
    from app.market_cn.auto.probe import sample_feats

    gate_order = [g.id for g in spec.enabled_gates]
    if codes is None:
        allc = sorted(all_codes())
        if sample_codes > 0:
            random.seed(seed)
            codes = random.sample(allc, min(sample_codes, len(allc)))
            pool_mode = f"抽样 {len(codes)}/{len(allc)} (seed={seed}, 冒烟口径)"
        else:
            codes = allc
            pool_mode = f"全市场 {len(allc)} 股"
    else:
        pool_mode = f"指定 {len(codes)} 股"
    try:
        si_map = _hub_stock_info() or {}
    except Exception:
        si_map = {}

    rows, trades = [], []
    t0 = time.time()
    n_ok = 0
    # 批量取数: 一次往返取全部票日线 (与逐票 hub.daily 行内容/窗口/复权完全一致,
    # 消除 O(N) DB 往返瓶颈; 缺失票不在 dict 中, 与 daily 返回 [] 等价)。
    bars_by_code = fetch_klines_batch(codes, days)
    for k, code in enumerate(codes, 1):
        bars = bars_by_code.get(code)
        if not bars or len(bars) < 5:
            continue
        si = si_map.get(code) if si_map else None
        coll = GateCollector()
        trades.extend(run_backtest(bars, code, spec, stock_info=si,
                                   use_prefilter=True, gate_dbg=coll) or [])
        merged = coll.merged()
        for (c, i), vec in merged.items():
            if i < 0 or i >= len(bars):
                continue
            d0 = str(bars[i]["time"])[:10]
            sf = sample_feats(bars, i, c, si)
            rows.append({"kind": "sample", "strategy": spec.key, "code": c,
                         "d0_date": d0, "gates": vec,
                         "stage": _first_false(gate_order, vec),
                         "labels": sf.get("labels") or {},
                         "features": sf.get("features") or {}})
        n_ok += 1
        if progress_every and k % progress_every == 0:
            print(f"[{k}/{len(codes)}] rows={len(rows)} trades={len(trades)} "
                  f"({time.time() - t0:.0f}s)", flush=True)
    return rows, trades, {"pool_mode": pool_mode, "codes_ok": n_ok,
                          "elapsed": round(time.time() - t0, 1)}


# ================================================================
# 门漏斗 (口径 = rule_audit)
# ================================================================

def build_funnel(rows, gate_order, key_ret, key_peak):
    """→ (funnel, meta)。funnel 元素 (gid, m_in, m_pass, m_rej); 与 rule_audit 同口径。"""
    from app.market_cn.auto.tools.rule_audit import _order_funnel
    from app.market_cn.auto.tools.rule_stats import _metrics

    for r in rows:
        r["_g"] = r["gates"]
    funnel = []
    for gname, reach, passed, rejected in _order_funnel(rows, gate_order):
        funnel.append((gname,
                       _metrics(reach, key_ret, key_peak, "mae5"),
                       _metrics(passed, key_ret, key_peak, "mae5"),
                       _metrics(rejected, key_ret, key_peak, "mae5")))
    final = [r for r in rows if r.get("stage") == "signal"]
    stage_counts = {}
    for r in rows:
        s = r.get("stage") or "?"
        stage_counts[s] = stage_counts.get(s, 0) + 1
    meta = {"n_rows": len(rows),
            "final": _metrics(final, key_ret, key_peak, "mae5"),
            "stage_counts": dict(sorted(stage_counts.items(), key=lambda kv: -kv[1]))}
    return funnel, meta


def two_segment(trades):
    """两段稳定性 (按入场日排序前后半) — 复用 param_scan._seg_stats。"""
    from app.market_cn.auto.tools.param_scan import _seg_stats
    (wr1, avg1), (wr2, avg2) = _seg_stats(trades)
    return {"seg1": {"wr": wr1, "avg": avg1}, "seg2": {"wr": wr2, "avg": avg2},
            "both_positive": bool(avg1 is not None and avg2 is not None
                                  and avg1 > 0 and avg2 > 0)}


# ================================================================
# 参数敏感性 (可选段; 复用 param_scan 的网格/两段工具)
# ================================================================

def build_sensitivity(spec, days, codes, sample_codes, seed, only, max_combos):
    """单维 OFAT 敏感性: 每个数值参数取 [v-1档, v, v+1档] 各跑一次全市场回测。

    → {param: [{"value":..., "n":..., "winrate":..., "avg_ret":..., "two_seg":...}, ...]}。
    复用 param_scan._auto_grid 生成切片 (与 param_scan 同规则)。组合超 max_combos → 截断。
    """
    from app.market_cn.auto.core.data.hub import all_codes, daily
    from app.market_cn.auto.core.data.hub import stock_info as _hub_stock_info
    from app.market_cn.auto.core.runtime.evaluate import run_backtest
    from app.market_cn.auto.tools.param_scan import _auto_grid, _seg_stats

    grid, skipped = _auto_grid(spec.params, only=only)
    if not grid:
        return {}, skipped
    base = dict(spec.params)
    if codes is None:
        allc = sorted(all_codes())
        codes = (random.sample(allc, min(sample_codes, len(allc)))
                 if sample_codes > 0 else allc)
    try:
        si_map = _hub_stock_info() or {}
    except Exception:
        si_map = {}

    def _run_one():
        trs = []
        for code in codes:
            bars = daily(code, days)
            if not bars or len(bars) < 5:
                continue
            trs.extend(run_backtest(bars, code, spec, stock_info=si_map.get(code)) or [])
        return trs

    out = {}
    n_run = 0
    for kdim, vals in grid.items():
        out[kdim] = []
        for v in vals:
            if n_run >= max_combos:
                break
            spec.params = {**base, kdim: v}
            try:
                trs = _run_one()
                (wr1, avg1), (wr2, avg2) = _seg_stats(trs)
                rets = [float(t["return_pct"]) for t in trs]
                out[kdim].append({
                    "value": v, "n": len(rets),
                    "winrate": (round(sum(1 for r in rets if r > 0) / len(rets) * 100, 1)
                                if rets else None),
                    "avg_ret": round(sum(rets) / len(rets), 2) if rets else None,
                    "seg1_avg": avg1, "seg2_avg": avg2})
            finally:
                spec.params = dict(base)
            n_run += 1
    return out, skipped


# ================================================================
# 渲染
# ================================================================

def render_md(spec, key_ret, peak_name, funnel, meta, seg, sens, skipped,
              run_meta, with_sens):
    L = [f"# explain 报告 — {spec.key} ({spec.meta.get('name', spec.key)})",
         "",
         f"- 生成 {time.strftime('%Y-%m-%d %H:%M')} | 路径: 门表 (core/runtime/evaluate) "
         f"| market={spec.market_key}",
         f"- 窗口 {run_meta['days']}d | {run_meta['pool_mode']} | "
         f"决策日样本 {meta['n_rows']} 行 | 回测耗时 {run_meta['elapsed']}s",
         f"- 标签口径: {key_ret} (自包含, 不依赖策略出场引擎); 峰值 {peak_name}; MAE=mae5",
         f"- 门序 (声明序 = 判定短路序): {[g for g, _a, _b, _c in funnel]}",
         ""]

    L += ["## 1. 门漏斗 (到达 → 通过 / 被拦)", "",
          "| 门 | 到达 n | 通过 n | 拦截 n | 拦截% |", "|---|---|---|---|---|"]
    for gname, m_in, m_p, m_r in funnel:
        reach = m_in["n"]
        pct = round(m_r["n"] / reach * 100, 1) if reach else "—"
        L.append(f"| {gname} | {reach} | {m_p['n']} | {m_r['n']} | {pct} |")
    L.append("")

    L += ["## 2. 单门判别力 (通过组 vs 被拦组, Δ>0 = 规则有效; ≤0 = 反指标候选)", "",
          "| 门 | 过n | 过胜率 | 过均收 | 过PL | 过均峰 | 拦n | 拦胜率 | 拦均收 | 拦PL "
          "| 拦均峰 | Δ胜率 | Δ均收 | Δ均峰 |", "|---|" + "---|" * 13]
    for gname, _mi, m_p, m_r in funnel:
        if not m_p["n"] and not m_r["n"]:
            continue
        d = lambda a, b: (round(a - b, 2) if a is not None and b is not None else "—")
        L.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
            gname, m_p["n"], m_p.get("winrate", "—"), m_p.get("avg_ret", "—"),
            m_p.get("pl_ratio", "—"), m_p.get("peak_mean", "—"),
            m_r["n"], m_r.get("winrate", "—"), m_r.get("avg_ret", "—"),
            m_r.get("pl_ratio", "—"), m_r.get("peak_mean", "—"),
            d(m_p.get("winrate"), m_r.get("winrate")),
            d(m_p.get("avg_ret"), m_r.get("avg_ret")),
            d(m_p.get("peak_mean"), m_r.get("peak_mean"))))
    L.append("")

    L += ["## 3. 失败样本归因 (落选阶段分布)", "",
          "| stage (归因门) | 行数 | 占比 |", "|---|---|---|"]
    n_rows = meta["n_rows"]
    for s, n in meta["stage_counts"].items():
        L.append(f"| {s} | {n} | {round(n / n_rows * 100, 1) if n_rows else '—'}% |")
    L.append("")

    fm = meta["final"]
    L += ["## 4. 最终信号池 (门表全过)", "",
          f"- 行数 {fm['n']} (censored={fm.get('censored')}): 胜率 {fm.get('winrate')}% / "
          f"均收 {fm.get('avg_ret')}% / 盈亏比 {fm.get('pl_ratio')} / 均峰 {fm.get('peak_mean')}% "
          f"/ 均MAE {fm.get('mae_mean')}%",
          f"- 两段胜率 前 {fm.get('wr_1st')}% / 后 {fm.get('wr_2nd')}%",
          ""]

    L += ["## 5. 两段稳定性 (按入场日前后半, 门表实际成行 trades)", "",
          f"- 前段 胜率 {seg['seg1']['wr']}% / 均收 {seg['seg1']['avg']}% ; "
          f"后段 胜率 {seg['seg2']['wr']}% / 均收 {seg['seg2']['avg']}%",
          f"- **两段全正: {'是 ✓' if seg['both_positive'] else '否 ✗ (单段有效=过拟合预警, 改动不得采纳)'}**", ""]

    if with_sens:
        L += ["## 6. 参数敏感性 (OFAT 单维切片)", ""]
        if not sens:
            L += [f"(无可切片参数; 跳过: {', '.join(skipped) or '无'})", ""]
        else:
            L += ["| 参数 | 值 | n | 胜率% | 均收% | 段1均收 | 段2均收 |",
                  "|---|---|---|---|---|---|---|"]
            for kdim, pts in sens.items():
                for p in pts:
                    L.append(f"| {kdim} | {p['value']} | {p['n']} | {p['winrate']} | "
                             f"{p['avg_ret']} | {p['seg1_avg']} | {p['seg2_avg']} |")
            if skipped:
                L += ["", f"- 跳过 (无法自动偏移): {', '.join(skipped)}"]
            L.append("")
    else:
        L += ["## 6. 参数敏感性",
              f"- 未跑 (加 `--with-sensitivity` 开启); 或直接跑 "
              f"`python -m app.market_cn.auto.tools.param_scan --strategy {spec.key} "
              f"--days {run_meta['days']}`", ""]

    L += ["> 判读纪律: Δ≤0 的门 = 无效/反指标候选 (放宽或删除前先换窗口复验); "
          "任何门/参数改动必须带两段稳定性 + pool_check 信号级复验证据, "
          "见 `tools/proposal.py validate`。", ""]
    return "\n".join(L)


# ================================================================
# main
# ================================================================

def main():
    ap = argparse.ArgumentParser(description="门表策略 explain 报告 (机器可读 JSON + MD)")
    ap.add_argument("--strategy", required=True, help="门表策略 key (strategies/<key>.yaml)")
    ap.add_argument("--days", type=int, default=300)
    ap.add_argument("--codes", default="", help="逗号分隔股票池 (默认全市场)")
    ap.add_argument("--sample-codes", type=int, default=0, help="随机抽样股数 (仅冒烟)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--label", choices=sorted(LABEL_KEYS), default="d5",
                    help="收益标签: d1=D1收盘卖 / d5=D5开盘卖 / d10=D10开盘卖")
    ap.add_argument("--peak", choices=sorted(PEAK_KEYS), default="5",
                    help="峰值口径 (门向量自包含口径仅支持 5=peak5; peak_exit 请用 rule_audit)")
    ap.add_argument("--with-sensitivity", action="store_true", help="附参数 OFAT 敏感性 (更慢)")
    ap.add_argument("--params", default="", help="敏感性只扫这些参数 (逗号分隔; 空=全部)")
    ap.add_argument("--max-combos", type=int, default=24, help="敏感性组合上限")
    ap.add_argument("--progress-every", type=int, default=0)
    ap.add_argument("--out", default="", help="MD 路径 (默认 tmp/explain_<key>_<ts>.md)")
    ap.add_argument("--json-out", default="", help="JSON 路径 (默认 tmp/explain_<key>_<ts>.json)")
    ap.add_argument("--probe-out", default="",
                    help="探针 JSONL 路径 (默认 tmp/probes/explain_<key>_<ts>.jsonl)")
    args = ap.parse_args()

    # .env (DB 连接; tools 目录上溯 5 级 = 项目根)
    try:
        from dotenv import load_dotenv
        for _p in (os.path.join(PROJECT_ROOT, ".env"),
                   os.path.normpath(os.path.join(PROJECT_ROOT, "..", ".env"))):
            if os.path.isfile(_p):
                load_dotenv(_p, override=False)
                break
    except Exception:
        pass

    from app.market_cn.auto.core.runtime.evaluate import load_strategy
    spec = load_strategy(args.strategy)

    codes = [c.strip() for c in args.codes.split(",") if c.strip()] or None
    key_ret = LABEL_KEYS[args.label]
    peak_name = PEAK_KEYS[args.peak]
    only = ([s.strip() for s in args.params.split(",") if s.strip()]
            if args.params.strip() else None)

    print(f"explain {args.strategy} days={args.days} label={args.label} peak={args.peak} "
          f"| market={spec.market_key} | 门 {len(spec.enabled_gates)} 道", flush=True)
    rows, trades, run_meta = run_explain_backtest(
        spec, args.days, codes, sample_codes=args.sample_codes, seed=args.seed,
        progress_every=args.progress_every)
    run_meta["days"] = args.days
    if not rows:
        print("无门向量样本 (检查策略/窗口/股票池)", file=sys.stderr)
        return 1

    gate_order = [g.id for g in spec.enabled_gates]
    funnel, meta = build_funnel(rows, gate_order, key_ret, peak_name)
    seg = two_segment(trades)
    sens, skipped = ({}, [])
    if args.with_sensitivity:
        sens, skipped = build_sensitivity(spec, args.days, codes, args.sample_codes,
                                          args.seed, only, args.max_combos)

    md = render_md(spec, key_ret, peak_name, funnel, meta, seg, sens, skipped,
                   run_meta, args.with_sensitivity)

    stem = os.path.join(_TMP_DIR,
                        f"explain_{args.strategy}_{time.strftime('%Y%m%d_%H%M')}")
    out_md = args.out or stem + ".md"
    out_json = args.json_out or stem + ".json"
    out_probe = args.probe_out or os.path.join(_TMP_DIR, "probes",
                                               f"explain_{args.strategy}_"
                                               f"{time.strftime('%Y%m%d_%H%M')}.jsonl")
    for p in (out_md, out_json, out_probe):
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)

    payload = {
        "meta": {"strategy": args.strategy, "name": spec.meta.get("name", args.strategy),
                 "market": spec.market_key, "days": args.days,
                 "label": key_ret, "peak": peak_name,
                 "gate_order": gate_order, "generated": time.strftime("%Y%m%d_%H%M"),
                 "pool_mode": run_meta["pool_mode"], "elapsed": run_meta["elapsed"]},
        "funnel": [{"gate": g, "reach": m_in["n"], "pass": m_p["n"], "reject": m_r["n"],
                    "pass_stats": m_p, "reject_stats": m_r}
                   for g, m_in, m_p, m_r in funnel],
        "final": meta["final"], "stage_counts": meta["stage_counts"],
        "two_segment": seg, "sensitivity": sens, "sensitivity_skipped": skipped,
        "n_rows": meta["n_rows"], "n_trades": len(trades),
    }
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(md)
    with open(out_probe, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({k: v for k, v in r.items() if k != "_g"},
                               ensure_ascii=False, default=str) + "\n")

    print(f"决策日样本 {meta['n_rows']} 行 | 成行 trades {len(trades)} 笔 | "
          f"两段全正 {'是' if seg['both_positive'] else '否'} | "
          f"最终池 n={meta['final']['n']}")
    print(f"报告 MD  : {os.path.abspath(out_md)}")
    print(f"报告 JSON: {os.path.abspath(out_json)}")
    print(f"探针 JSONL (rule_audit 可复算): {os.path.abspath(out_probe)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
