#!/usr/bin/env python3
"""auto/tools/m6_check.py — M6「LLM 分析闭环」验收自检 (2026-09-20)

四项验收 (退出码 = 失败项数):
  1. explain 同口径: explain 的 JSONL 经 rule_audit/rule_stats 纯函数复算 → 门漏斗数字一致
     (证明 emit 的 gates/labels 经 JSON 往返仍faithful);
  2. proposal 往返: proposal → validate → apply(备份) → revert → YAML 逐位还原 (sha 相等);
  3. 红线三闸: 4 类违规 proposal 全部被 validate 拒绝
     (改 .py / 删 qualify 合规门 / 缺稳定性证据 / 引未来函数);
  4. 诊断零足迹: `run_backtest(gate_dbg=None)` 与 `gate_dbg=collector` 的 trades **逐笔相等**
     (证明 M6-1 的 core 增量是纯观测, 不改变判定)。

用法:
  python -m app.market_cn.auto.tools.m6_check                 # 默认 dragon_callback 抽样
  python -m app.market_cn.auto.tools.m6_check --strategy v1 --sample-codes 150 --days 120

易错点:
  - 抽样口径数字为冒烟用, 不与全市场基线对照; 验收只查"一致性/往返/红线/零足迹", 不查绝对收益;
  - 全程不触碰真实 strategies/*.yaml (往返测试在 tmp/_m6_check_strat/ 的副本上做);
  - 需 DB (取日线); 无 DB 时提前报错退出。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import time

from app.market_cn.auto.core._paths import PROJECT_ROOT

_REPO_ROOT = os.path.dirname(PROJECT_ROOT)
_TMP = os.path.join(_REPO_ROOT, "tmp")


def _t1_explain_same_metric(strategy, days, sample_codes, seed):
    """验收①: explain 的 JSONL 经 rule_audit/rule_stats 复算 == explain 自带漏斗。"""
    from app.market_cn.auto.core.runtime.evaluate import load_strategy
    from app.market_cn.auto.tools.explain import run_explain_backtest, build_funnel, LABEL_KEYS

    spec = load_strategy(strategy)
    rows, trades, _meta = run_explain_backtest(spec, days, None,
                                               sample_codes=sample_codes, seed=seed)
    if not rows:
        return [f"① {strategy}: 无门向量样本 (检查窗口/股票池)"], {}
    order = [g.id for g in spec.enabled_gates]
    key_ret, key_peak = LABEL_KEYS["d5"], "peak5"
    funnel_A, metaA = build_funnel(rows, order, key_ret, key_peak)

    # 落临时 JSONL, 经 rule_stats._load_rows 读回 (模拟 rule_audit --probe-file 通道)
    tmp_jsonl = os.path.join(_TMP, f"_m6_t1_{strategy}.jsonl")
    os.makedirs(_TMP, exist_ok=True)
    with open(tmp_jsonl, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({k: v for k, v in r.items() if k != "_g"},
                               ensure_ascii=False, default=str) + "\n")
    from app.market_cn.auto.tools.rule_stats import _load_rows, _metrics
    from app.market_cn.auto.tools.rule_audit import _order_funnel
    rows2 = _load_rows(tmp_jsonl)
    for r in rows2:
        r["_g"] = r["gates"]
    funnel_B = [(g, _metrics(re, key_ret, key_peak, "mae5"),
                 _metrics(pa, key_ret, key_peak, "mae5"),
                 _metrics(rj, key_ret, key_peak, "mae5"))
                for (g, re, pa, rj) in _order_funnel(rows2, order)]

    errs = []
    if len(funnel_A) != len(funnel_B):
        errs.append(f"① 漏斗门数不一致 A={len(funnel_A)} B={len(funnel_B)}")
    else:
        for (g, miA, mpA, mrA), (g2, miB, mpB, mrB) in zip(funnel_A, funnel_B):
            for tag, mA, mB in (("reach", miA, miB), ("pass", mpA, mpB), ("reject", mrA, mrB)):
                if mA.get("n") != mB.get("n") or mA.get("avg_ret") != mB.get("avg_ret"):
                    errs.append(f"① 门 {g} {tag}: A(n={mA.get('n')},avg={mA.get('avg_ret')}) "
                                f"≠ B(n={mB.get('n')},avg={mB.get('avg_ret')})")
    # 信号行 ⊇ 实际成行 trades (按 code+signal_date/d0_date)
    sig = {(r["code"], r["d0_date"]) for r in rows if r.get("stage") == "signal"}
    miss = [(t.get("code"), str(t.get("signal_date") or t.get("entry_date") or "")[:10])
            for t in trades
            if (t.get("code"), str(t.get("signal_date") or t.get("entry_date") or "")[:10]) not in sig]
    if miss:
        errs.append(f"① 有 {len(miss)} 笔 trades 无对应 signal 行 (向量不 faithful)")
    info = {"rows": len(rows), "trades": len(trades),
            "final_signal_rows": sum(1 for r in rows if r.get("stage") == "signal"),
            "gates": order}
    return errs, info


def _t2_roundtrip(strategy):
    """验收②: proposal → validate → apply(备份) → revert → YAML 逐位还原。"""
    from app.market_cn.auto.tools import proposal as P
    import hashlib
    src = os.path.join(P._DEFAULT_STRATEGY_DIR, f"{strategy}.yaml")
    if not os.path.isfile(src):
        return [f"② 源策略不存在: {src}"], {}
    tmp_dir = os.path.join(_TMP, "_m6_check_strat")
    if os.path.isdir(tmp_dir):
        shutil.rmtree(tmp_dir)
    os.makedirs(tmp_dir, exist_ok=True)
    dst = os.path.join(tmp_dir, f"{strategy}.yaml")
    shutil.copy2(src, dst)

    def _sha(p):
        return hashlib.sha256(open(p, "rb").read()).hexdigest()

    sha0 = _sha(dst)
    yaml_text = open(dst, encoding="utf-8").read()

    # 取一个真实 param 做编辑
    doc, _gates = P._parse_gates(yaml_text)
    params = doc.get("params") or {}
    if not params:
        return [f"② {strategy} 无 params 可编辑"], {}
    pk = next(iter(params))
    before = params[pk]
    after = (before + 1) if isinstance(before, int) else round(float(before) + 0.1, 4)
    prop = {"strategy": strategy, "base_sha": P._sha256(yaml_text),
            "created": time.strftime("%Y-%m-%dT%H:%M"), "rationale": "roundtrip test",
            "edits": [{"kind": "param", "target": pk, "before": before, "after": after,
                       "rationale": "t", "evidence": {"stability": {"both_positive": True},
                                                      "pool_check": {"verdict": "MIGRATE"}}}]}
    prop_path = os.path.join(tmp_dir, "prop.json")
    json.dump(prop, open(prop_path, "w", encoding="utf-8"), ensure_ascii=False)

    errs = []
    # validate
    verrs = P.validate(prop, strategy, yaml_text)
    if verrs:
        errs.append(f"② validate 意外失败: {verrs}")
        return errs, {}
    # apply (真写盘 + 备份)
    args = argparse.Namespace(proposal=prop_path, strategy=strategy, strategy_dir=tmp_dir,
                              apply=True, allow_stale=False)
    rc = P.cmd_apply(args)
    if rc != 0:
        errs.append(f"② apply 返回 {rc}")
    sha1 = _sha(dst)
    if sha1 == sha0:
        errs.append("② apply 后 YAML 未变化 (文本替换未生效)")
    hist = os.path.join(tmp_dir, ".history")
    backups = os.listdir(hist) if os.path.isdir(hist) else []
    if not backups:
        errs.append("② apply 未生成备份")
    # revert
    rargs = argparse.Namespace(strategy=strategy, to="", strategy_dir=tmp_dir)
    rc = P.cmd_revert(rargs)
    if rc != 0:
        errs.append(f"② revert 返回 {rc}")
    sha2 = _sha(dst)
    if sha2 != sha0:
        errs.append(f"② revert 未逐位还原 (sha {sha0[:8]} → {sha2[:8]})")
    return errs, {"param": pk, "before": before, "after": after,
                  "sha_before": sha0[:8], "sha_applied": sha1[:8], "sha_reverted": sha2[:8],
                  "backups": len(backups)}


def _t3_redlines(strategy):
    """验收③: 4 类违规 proposal 全部被 validate 拒绝。"""
    from app.market_cn.auto.tools import proposal as P
    src = os.path.join(P._DEFAULT_STRATEGY_DIR, f"{strategy}.yaml")
    yaml_text = open(src, encoding="utf-8").read()
    doc, gates = P._parse_gates(yaml_text)
    qualify_gid = next((str(g["id"]) for g in gates if str(g.get("role")) == "qualify"), None)
    ok_ev = {"stability": {"both_positive": True}, "pool_check": {"verdict": "MIGRATE"}}

    cases = {
        "改 .py (越权路径)": {"strategy": strategy, "edits": [
            {"kind": "param", "target": "../../core/exec.py", "before": 1, "after": 2,
             "evidence": ok_ev}]},
        "删 qualify 合规门": {"strategy": strategy, "edits": [
            {"kind": "gate_del", "target": qualify_gid or "g1", "evidence": ok_ev}]},
        "缺稳定性证据": {"strategy": strategy, "edits": [
            {"kind": "param", "target": next(iter(doc.get("params") or {"x": 1})),
             "before": next(iter((doc.get("params") or {"x": 1}).values())), "after": 999,
             "evidence": {}}]},
        "引未来函数": {"strategy": strategy, "edits": [
            {"kind": "gate_expr", "target": str(gates[-1]["id"]) if gates else "g4",
             "before": str(gates[-1].get("expr", "")) if gates else "",
             "after": "chg(1) > 0", "evidence": ok_ev}]},
    }
    errs, detail = [], {}
    for name, prop in cases.items():
        v = P.validate(prop, strategy, yaml_text)
        detail[name] = len(v)
        if not v:
            errs.append(f"③ 违规用例『{name}』未被拒绝 (应失败却通过)")
    return errs, detail


def _t4_zero_footprint(strategy, days, sample_codes, seed):
    """验收④: gate_dbg=None 与 gate_dbg=collector 的 trades 逐笔相等 (诊断零足迹)。"""
    import random
    from app.market_cn.auto.core.data.hub import all_codes, daily
    from app.market_cn.auto.core.data.hub import stock_info as _hub_stock_info
    from app.market_cn.auto.core.runtime.evaluate import load_strategy, run_backtest
    from app.market_cn.auto.tools.explain import GateCollector

    spec = load_strategy(strategy)
    allc = sorted(all_codes())
    random.seed(seed)
    codes = (random.sample(allc, min(sample_codes, len(allc)))
             if sample_codes > 0 else allc)
    try:
        si_map = _hub_stock_info() or {}
    except Exception:
        si_map = {}
    errs = []
    n_cmp = 0
    for code in codes:
        bars = daily(code, days)
        if not bars or len(bars) < 5:
            continue
        si = si_map.get(code)
        t_off = run_backtest(bars, code, spec, stock_info=si, use_prefilter=True, gate_dbg=None)
        t_on = run_backtest(bars, code, spec, stock_info=si, use_prefilter=True,
                            gate_dbg=GateCollector())
        if json.dumps(t_off, sort_keys=True, default=str) != json.dumps(t_on, sort_keys=True,
                                                                       default=str):
            errs.append(f"④ {code}: gate_dbg 改变了 trades (非零足迹)")
            if len(errs) >= 3:
                break
        n_cmp += 1
    return errs, {"codes_compared": n_cmp}


def main():
    ap = argparse.ArgumentParser(description="M6 验收自检 (explain/往返/红线/零足迹)")
    ap.add_argument("--strategy", default="dragon_callback")
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--sample-codes", type=int, default=150)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    try:
        from dotenv import load_dotenv
        for p in (os.path.join(PROJECT_ROOT, ".env"),):
            if os.path.isfile(p):
                load_dotenv(p, override=False)
                break
    except Exception:
        pass

    all_errs = []
    print(f"=== M6 验收自检: strategy={args.strategy} days={args.days} "
          f"sample={args.sample_codes} ===", flush=True)

    t0 = time.time()
    e1, i1 = _t1_explain_same_metric(args.strategy, args.days, args.sample_codes, args.seed)
    all_errs += e1
    print(f"[1/4] explain 同口径: {'PASS' if not e1 else 'FAIL'} {i1}", flush=True)

    e2, i2 = _t2_roundtrip(args.strategy)
    all_errs += e2
    print(f"[2/4] proposal 往返: {'PASS' if not e2 else 'FAIL'} {i2}", flush=True)

    e3, i3 = _t3_redlines(args.strategy)
    all_errs += e3
    print(f"[3/4] 红线三闸: {'PASS' if not e3 else 'FAIL'} {i3}", flush=True)

    e4, i4 = _t4_zero_footprint(args.strategy, args.days, args.sample_codes, args.seed)
    all_errs += e4
    print(f"[4/4] 诊断零足迹: {'PASS' if not e4 else 'FAIL'} {i4}", flush=True)

    print(f"--- 用时 {time.time() - t0:.0f}s ---")
    if all_errs:
        print(f"\n❌ M6 验收失败 ({len(all_errs)} 项):")
        for e in all_errs:
            print(f"  - {e}")
        return len(all_errs)
    print("\n✅ M6 验收全过 (4/4)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
