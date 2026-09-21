#!/usr/bin/env python3
"""auto/tools/debug.py — 单候选判定追踪器 (2026-09-21, 用户裁定形态 1)

用途: 回答"这个票这一天为什么没出信号 / 出了什么信号" —— 调试**新策略与存量策略**的
     **单一候选 × 单一日** 视图。

为什么需要它 (现有工具的盲区):
  - `replay`      单股但只看**整窗逐笔** (日线策略看不到"某一天为什么不出");
  - `rule_audit`  门级但只看**全市场聚合** (拦截表/判别力, 没有"这只票现在各门值多少");
  - `gate_dbg`(M6) 只回调**布尔向量**, 且只被 explain 聚合消费, 无单候选值追踪。
本工具补的就是这一格: 逐门打印 `门id / 名称 / 角色 / 表达式 / 计算值(含叶子函数值) / 通过否`,
再给"入场/出场"结论。

三种引擎 (自动择一):

  A 门表引擎 — strategies/<key>.yaml 存在, 且编排为受支持形态
      (enumeration=limit_up / day(v1|relay3|break)):
      门值由 **core.runtime.expr.evaluate** 给出 (as-of 安全, 唯一求值器);
      最终"是否成信号"由 **core.runtime.evaluate.run_backtest** 判定 —— 因为去重±4 /
      U1~U4 预筛 / 入场腿依赖前序窗口, 只能在整窗引擎里成立; 本工具不复制这些层。

  B 插件引擎 — 无 YAML (dragon_v2/break_v2/triple_resonance) 或 YAML 需特化
      (g56 横截面 ext):
      走策略**自带的 TRACE 探针** —— `backtest_stock(bars, code, probe=内存探针)` 收集
      目标日的 sample (stage + rule_trace + 特征/标签) 与窗口内逐笔。零新增打点。

  C 盘中引擎 — scan_spec.kind == "intraday_window" (tail_oversold/knife_catch):
      复用回测同款"快照帧 + 触发槽位"通道, 对目标日逐触发调
      `intraday_shortlist` / `scan_signals(probe=)`, 打印预筛与门级落点。

as-of 纪律: 本工具只做**取数 + 排版**, 判定一律经 core 的求值器/引擎; 不引入第二份规则。

用法:
  python -m app.market_cn.auto.tools.debug --strategy dragon_callback --code 000859
  python -m app.market_cn.auto.tools.debug --strategy v1 --code 000021 --date 2026-09-18
  python -m app.market_cn.auto.tools.debug --strategy break --code 000032 --days 400 --json
  python -m app.market_cn.auto.tools.debug --strategy dragon --code 000859   # 旧别名: dragon→dragon_callback

易错点:
  - `--date` 缺省 = 窗口内最后一根 bar; 该日必须是该票的交易日, 否则报错并列出可用区间。
  - 门表路径的"计算值"里叶子函数值只是**辅助可读** (如 chg(-1)=-0.85); 判定真值以
    evaluate(整条 expr) 为准 —— 叶子失败不影响门结论 (门本身照样求值)。
  - 同一天可能有多笔候选 (limit_up 遍历多个 lu); 默认只详展前 `--max-lu` 个, 可用
    `--lu YYYY-MM-DD` 锁定某个涨停日, 或 `--max-lu 0` 展开全部。
  - 别名仅本工具/replay/strategy_cli 认 (dragon→dragon_callback 等); 直接跑 core 不认。
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys

# 旧 CLI 别名 (与 tools/replay.py、strategy_cli.py 保持一致)
_ALIAS = {"dragon": "dragon_callback", "dragon2": "dragon_v2", "break_buy": "break"}

# 门表引擎支持的 day 编排 (其余走插件引擎; 见文件头 A/B 分工)
_GATE_DAY_FLOWS = {"v1", "relay3", "break"}


# ================================================================
# 内存探针 (实现 probe.py 的 trace/sample/shell 面)
# ================================================================
class MemProbe:
    """内存探针 —— 供 *.py 策略自带 TRACE 打点收集 (不落盘)。

    与 probe.Probe 同接口 (trace/sample/shell/close), 但只收在内存里给本工具展开。
    策略侧 `probe=None → 零开销` 的契约不受影响: 这里恒非 None 只在调试时使用。
    """

    def __init__(self):
        self.traces: list = []
        self.samples: list = []
        self.shells: list = []

    def trace(self, stage, **kw):
        self.traces.append({"stage": stage, **kw})

    def sample(self, **kw):
        self.samples.append(kw)

    def shell(self, name, **kw):
        self.shells.append({"name": name, **kw})

    def close(self):
        pass


# ================================================================
# 排版小件
# ================================================================
def _fmt_val(v) -> str:
    """门计算值 / 叶子值 → 短串 (bool 用 True/False, float 保 4 位有效)。"""
    if isinstance(v, bool):
        return "True" if v else "False"
    if v is None:
        return "None"
    if isinstance(v, float):
        return f"{v:.4g}"
    return str(v)


def _leaf_values(expr: str, params: dict, funcs: dict) -> list:
    """表达式里所有 `Call(Name)` 子树的求值结果 → [(源码片段, 值/错误), ...]。

    仅**辅助可读** (让 chg(-1)=-0.85 这类中间量看得见); 求值失败不影响门结论。
    复用 core 的 `_ev` (同一白名单求值器), 不引第二份语义。
    """
    from app.market_cn.auto.core.runtime import expr as _ex
    try:
        tree = _ex.parse(expr)
    except Exception:
        return []
    out, seen = [], set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            try:
                src = ast.unparse(n)
            except Exception:
                continue
            if src in seen:
                continue
            seen.add(src)
            try:
                out.append((src, _ex._ev(n, params, funcs)))
            except Exception as e:            # 叶子失败(未来偏移/未知量) → 记错误文本
                out.append((src, f"<{type(e).__name__}>"))
    out.sort(key=lambda kv: len(kv[0]))
    return out


def _gate_row(g, params: dict, funcs: dict) -> dict:
    """单门求值 → {id,name,role,expr,value,passed,leaves,error}。"""
    from app.market_cn.auto.core.runtime.expr import ExprError, evaluate
    row = {"id": g.id, "name": g.name, "role": g.role, "expr": g.expr,
           "value": None, "passed": False, "leaves": [], "error": ""}
    try:
        v = evaluate(g.expr, params, funcs)
        row["value"] = v
        row["passed"] = bool(v)
    except ExprError as e:
        row["error"] = str(e)                 # 求值异常视作门未过 (与引擎同语义)
    except Exception as e:                    # noqa: BLE001 - 诊断工具, 不吞掉类型
        row["error"] = f"{type(e).__name__}: {e}"
    row["leaves"] = _leaf_values(g.expr, params, funcs)
    return row


def _print_gate_rows(title: str, rows: list) -> None:
    if not rows:
        return
    print(f"  {title}")
    for r in rows:
        mark = "✓" if r["passed"] else "✗"
        print(f"    {mark} {r['id']:<18} {r['expr']}")
        bits = []
        if r["error"]:
            bits.append(f"求值异常 {r['error']}")
        else:
            bits.append(f"值 {_fmt_val(r['value'])}")
        if r["leaves"]:
            bits.append(", ".join(f"{s}={_fmt_val(v)}" for s, v in r["leaves"]))
        print(f"        ↳ " + " | ".join(bits))


# ================================================================
# 数据准备 (镜像引擎的 ctx 构造; 判定语义仍由 core 唯一提供)
# ================================================================
def _load_env() -> None:
    try:
        from app.market_cn.auto.core._paths import load_env_first_found
        load_env_first_found(os.path.join(os.getcwd(), ".env"))
    except Exception:
        pass


def _bars_and_info(code: str, days: int):
    from app.market_cn.auto.core.data.hub import daily, stock_info
    try:
        info = stock_info().get(code)
    except Exception:
        info = None
    return daily(code, days), info


def _find_i(bars: list, date: str | None):
    """目标日 → bar 索引; date=None → 末根。找不到返回 (-1, 可用区间串)。"""
    if not bars:
        return -1, ""
    lo, hi = str(bars[0]["time"])[:10], str(bars[-1]["time"])[:10]
    if not date:
        return len(bars) - 1, f"{lo} ~ {hi}"
    for k, b in enumerate(bars):
        if str(b["time"])[:10] == date:
            return k, f"{lo} ~ {hi}"
    return -1, f"{lo} ~ {hi}"


def _ctx(spec, bars, i, lu_idx, code, stock_info):
    from app.market_cn.auto.core.market import get_board_type
    from app.market_cn.auto.core.runtime.functions import Ctx
    return Ctx(bars, i, lu_idx=lu_idx, params=spec.params,
               board_type=get_board_type(code, spec.market_spec), code=code,
               stock_info=stock_info, market=spec.market_spec)


def _funcs(spec, ctx):
    from app.market_cn.auto.core.runtime.functions import build_funcs
    return build_funcs(ctx, spec.func_names)


# ================================================================
# 引擎 A: 门表追踪
# ================================================================
def report_gate_table(key: str, spec, bars: list, code: str, stock_info, i: int,
                      max_lu: int = 5, pin_lu: str | None = None) -> dict:
    """门表策略单候选追踪: 逐门值 + 引擎结论 (不复制去重/预筛/入场层)。"""
    from app.market_cn.auto.core.market import find_limit_ups
    from app.market_cn.auto.core.runtime.evaluate import run_backtest
    from app.market_cn.auto.core.market import get_board_type

    date = str(bars[i]["time"])[:10]
    enum = str(spec.meta.get("enumeration", "limit_up")).lower()
    out: dict = {"mode": "gate", "strategy": key, "code": code, "date": date,
                 "enumeration": enum, "phases": [], "conclusion": "", "trades": []}

    if enum == "day":
        ctx = _ctx(spec, bars, i, 0, code, stock_info)
        rows = [_gate_row(g, spec.params, _funcs(spec, ctx))
                for g in spec.enabled_gates]
        _print_gate_rows(f"全门 (all) @ 决策日 {date}", rows)
        out["phases"].append({"phase": "all", "gates": rows})
    else:
        bt = get_board_type(code, spec.market_spec)
        lu_all = find_limit_ups(bars, bt, spec.market_spec)
        pd_min = int(spec.params.get("min_pullback_days", 1))
        pd_max = int(spec.params.get("max_pullback_days", 10))
        # 廉价预筛 (引擎的数学必要条件; 此处只作消息, 真判定仍归引擎)
        pre_ok = any(pd_min + 1 <= i - j <= pd_max + 1 for j in lu_all)
        print(f"  预筛 (引擎廉价必要条件): 回调窗口内涨停日 "
              f"{'存在 ✓' if pre_ok else '不存在 ✗'} "
              f"(回调 {pd_min}~{pd_max} 日, 历史涨停 {len(lu_all)} 个)")
        out["prescreen_ok"] = pre_ok

        ctx_q = _ctx(spec, bars, i, 0, code, stock_info)
        qrows = [_gate_row(g, spec.params, _funcs(spec, ctx_q))
                 for g in spec.prefilter_gates()]
        _print_gate_rows("资格门 (qualify)", qrows)
        out["phases"].append({"phase": "qualify", "gates": qrows})

        cands = [j for j in lu_all if j < i]
        if pin_lu:
            cands = [j for j in cands if str(bars[j]["time"])[:10] == pin_lu]
        else:
            cands = sorted(cands, key=lambda j: -(i - j))   # 近端优先更实用
        shown = cands if max_lu <= 0 else cands[:max_lu]
        for lu in shown:
            ctx_d = _ctx(spec, bars, i, lu, code, stock_info)
            rows = [_gate_row(g, spec.params, _funcs(spec, ctx_d))
                    for g in spec.decision_gates()]
            lu_date = str(bars[lu]["time"])[:10]
            in_win = pd_min + 1 <= i - lu <= pd_max + 1
            _print_gate_rows(
                f"判定门 (decision) @ 涨停日 {lu_date} (回调 {(i - 1) - lu} 日"
                f"{'' if in_win else ', 窗外'})", rows)
            out["phases"].append({"phase": "decision", "lu_date": lu_date, "gates": rows})
        if max_lu > 0 and len(cands) > max_lu:
            print(f"    ... 另有 {len(cands) - max_lu} 个涨停候选 (--max-lu 0 全展 / --lu 锁定)")

    # ---- 引擎结论 (去重±4 / U1~U4 / 入场腿只能在整窗引擎里成立) ----
    trades = run_backtest(bars, code, spec, stock_info=stock_info,
                          use_prefilter=True) or []
    out["trades"] = trades
    sig_key = ("signal_date", "d0_date", "lu_date")
    mine = None
    for t in trades:
        s = next((str(t.get(k))[:10] for k in sig_key if t.get(k)), "")
        if s == date:
            mine = t
            break
    out["signal"] = mine
    return out


def _conclude_gate(out: dict) -> str:
    """由门表追踪 + 引擎结论拼结论句。"""
    date = out["date"]
    if out.get("signal"):
        t = out["signal"]
        ed = str(t.get("exit_date") or "")[:10]
        if not ed and t.get("exit_day") is not None:
            ed = f"{t.get('exit_day')}日"          # limit_up/day 两族 trade dict 键不同
        return (f"✅ {date} 出信号 → 买入 {str(t.get('entry_date'))[:10]}"
                f"@{t.get('entry_price')} → 出场 {ed} "
                f"{t.get('exit_reason') or ''} @{t.get('exit_price')} "
                f"| 收益 {t.get('return_pct')}%")
    # 无信号: 归因到第一处拦截
    for ph in out["phases"]:
        bad = next((g for g in ph["gates"] if not g["passed"]), None)
        if bad:
            where = ph.get("lu_date") and f" (涨停日 {ph['lu_date']})" or ""
            return (f"✗ {date} 无信号 — 拦截于 [{bad['id']}]{where}: "
                    f"`{bad['expr']}` → {_fmt_val(bad['value'])}")
    if out.get("prescreen_ok") is False:
        return f"✗ {date} 无信号 — 引擎廉价预筛未过 (回调窗口内无涨停日)"
    return (f"✗ {date} 无信号 — 门全过但未成笔: 去重±4 / U1~U4 预筛 / 入场腿拦截 "
            f"(用 `qd run <策略> --codes {out['code']}` 看整窗)")


# ================================================================
# 引擎 B: 插件 (策略自带 TRACE)
# ================================================================
def report_plugin(key: str, strat, bars: list, code: str, stock_info,
                  date: str, days: int) -> dict:
    """*.py 策略单候选追踪: backtest_stock(probe=内存探针) → 目标日 sample/rule_trace。"""
    probe = MemProbe()
    try:
        trades = strat.backtest_stock(bars, code, stock_info=stock_info,
                                     probe=probe) or []
    except TypeError:
        # 极少数策略未接 probe 形参 → 退化为无探针逐笔
        trades = strat.backtest_stock(bars, code, stock_info=stock_info) or []

    day_samples = [s for s in probe.samples
                   if str(s.get("d0_date") or "")[:10] == date]
    out = {"mode": "plugin", "strategy": key, "code": code, "date": date,
           "samples": day_samples, "traces": probe.traces, "trades": trades,
           "conclusion": ""}

    if day_samples:
        for s in day_samples:
            print(f"  决策日追踪 (策略自带 TRACE): stage={s.get('stage')}")
            for t in (s.get("rule_trace") or []):
                kw = {k: v for k, v in t.items() if k != "stage"}
                print(f"    · {t.get('stage'):<14} {kw}")
            feats = s.get("features") or {}
            if feats:
                show = {k: feats.get(k) for k in
                        ("d0_pct_chg", "vol_r", "turnover_d0", "rsi6", "board_type")
                        if k in feats}
                print(f"    特征: {show}")
            if s.get("sig"):
                print(f"    信号: {s['sig']}")
    else:
        print(f"  {date} 无判定样本 (该日未到达完整判定 / 廉价预筛跳过 — "
              f"策略对'预筛跳过日'不采样)")

    sig_key = ("signal_date", "d0_date")
    mine = next((t for t in trades
                 if next((str(t.get(k))[:10] for k in sig_key if t.get(k)), "") == date),
                None)
    out["signal"] = mine
    return out


def _conclude_plugin(out: dict) -> str:
    if out.get("signal"):
        t = out["signal"]
        return (f"✅ {out['date']} 出信号 → 买入 @{t.get('entry_price')} → "
                f"出场 {t.get('exit_reason') or ''} @{t.get('exit_price')} "
                f"| 收益 {t.get('return_pct')}%")
    if out.get("samples"):
        st = out["samples"][0].get("stage")
        return f"✗ {out['date']} 无信号 — 最深判定步 stage={st} (见上方 TRACE)"
    return f"✗ {out['date']} 无信号 — 该日未到达完整判定 (预筛跳过; 用法见 `qd gates` 看聚合)"


# ================================================================
# 引擎 C: 盘中 (快照帧逐触发)
# ================================================================
def report_intraday(key: str, strat, code: str, date: str, days: int) -> dict:
    """intraday_window 策略单日追踪: 逐触发槽位 → shortlist/scan_signals(probe=)。"""
    from app.market_cn.auto.core.backtest import _exec_trigger_mis
    from app.market_cn.auto.core.data import frames as fr
    from app.market_cn.auto.core.data.hub import daily

    mis = _exec_trigger_mis(strat.scan_spec)
    out = {"mode": "intraday", "strategy": key, "code": code, "date": date,
           "events": [], "conclusion": ""}
    if not mis:
        out["conclusion"] = "该策略 scan_spec 未声明触发时刻, 无法复现"
        print("  " + out["conclusion"])
        return out

    first_1m = fr.first_1m_date()
    if first_1m and date < first_1m:
        out["conclusion"] = f"1m 数据自 {first_1m} 起; {date} 不可复现"
        print("  " + out["conclusion"])
        return out

    from datetime import datetime, timedelta
    prev_date = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    pc_map = fr.prev_closes(date)
    frame = fr.build_frame(date)
    if len(frame) == 0:
        out["conclusion"] = f"{date} 无分钟帧数据 (非交易日 / 未回填)"
        print("  " + out["conclusion"])
        return out

    print(f"  触发时刻 {[fr.MI_HHMM[m] for m in mis]} | 前收种子 {len(pc_map)} 只 "
          f"| 当日帧 {len(frame)} 只")
    for mi in mis:
        snap = frame.snap(code, mi, pc_map.get(code))
        hhmm = fr.MI_HHMM[mi]
        if snap is None:
            print(f"    {hhmm} 无该票快照 (停牌/无 1m bar)")
            out["events"].append({"trigger": hhmm, "snap": None})
            continue
        mkt = frame.mkt_gain(mi, pc_map)
        short = strat.intraday_shortlist({code: snap}, mkt)
        probe = MemProbe()
        sigs = []
        if short:
            bars = daily(code, 300, as_of=prev_date)
            sigs = strat.scan_signals(
                bars, code,
                ctx={"latest": snap, "series": frame.series(code, mi), "mkt_gain": mkt},
                probe=probe) or []
        ev = {"trigger": hhmm, "last": snap["last"], "mkt_gain": round(mkt, 2),
              "prev_close": snap.get("previousClose"),
              "shortlist": bool(short),
              "signals": [{"score": s.score, "label": s.label, **(s.extra or {})}
                          for s in sigs],
              "traces": probe.traces}
        chg = ((snap["last"] / snap["previousClose"] - 1) * 100
               if snap.get("previousClose") else None)
        print(f"    {hhmm} last={snap['last']} 涨幅={_fmt_val(chg)}% "
              f"mkt={ev['mkt_gain']}% | 预筛{'过' if short else '否'} | "
              f"信号 {len(sigs) if sigs else 0}")
        for t in probe.traces:
            kw = {k: v for k, v in t.items() if k != "stage"}
            print(f"        · {t.get('stage'):<14} {kw}")
        for s in sigs:
            print(f"        → {s.label} score={s.score} {(s.extra or {})}")
        out["events"].append(ev)

    hits = [e for e in out["events"] if e.get("signals")]
    out["conclusion"] = (f"✅ {date} 触发信号 {len(hits)} 次 (共评估 "
                         f"{len(out['events'])} 个槽位)" if hits
                         else f"✗ {date} 全槽位无信号 (评估 {len(out['events'])} 个槽位)")
    return out


# ================================================================
# 入口
# ================================================================
def main() -> int:
    ap = argparse.ArgumentParser(
        prog="qd debug",
        description="单候选判定追踪器 (门表逐门值 / 插件 TRACE / 盘中逐触发)")
    ap.add_argument("--strategy", required=True,
                    help="策略 key (别名 dragon→dragon_callback / dragon2→dragon_v2 / break_buy→break)")
    ap.add_argument("--code", required=True, help="单只股票代码")
    ap.add_argument("--date", default="", help="目标决策日 YYYY-MM-DD (默认窗口末根)")
    ap.add_argument("--days", type=int, default=300, help="回看窗口自然日 (默认 300)")
    ap.add_argument("--lu", default="", help="门表 limit_up: 锁定某个涨停日 YYYY-MM-DD")
    ap.add_argument("--max-lu", type=int, default=5,
                    help="门表 limit_up: 详展的涨停候选数 (0=全部; 默认 5)")
    ap.add_argument("--json", action="store_true", help="输出原始 JSON (供 AI 统计)")
    args = ap.parse_args()

    _load_env()
    key = _ALIAS.get(args.strategy, args.strategy)
    date = args.date or None

    from app.market_cn.auto import strategies as reg
    reg.autodiscover()
    strat = reg.get_strategy(key)
    if strat is None:
        from app.market_cn.auto.registry import strategy_keys
        print(f"策略 {key} 未注册。可用: {', '.join(sorted(strategy_keys()))}")
        return 2

    spec_kind = getattr(getattr(strat, "scan_spec", None), "kind", "")
    yaml_path = _yaml_path(key)

    # ---- 引擎 C: 盘中 ----
    if spec_kind == "intraday_window":
        if not date:
            from datetime import datetime
            date = datetime.now().strftime("%Y-%m-%d")
        print(f"策略 {key} {getattr(strat, 'name', '')} | 形态 盘中窗口 | 目标日 {date}")
        out = report_intraday(key, strat, args.code, date, args.days)
        return _emit(out, args)

    # ---- 取日线 ----
    bars, info = _bars_and_info(args.code, args.days)
    if not bars:
        print(f"{args.code}: 无日线数据 (代码错误? 或 DB 不可用)")
        return 1
    i, span = _find_i(bars, date)
    if i < 0:
        print(f"{args.code} 无 {date} 的 bar。可用区间: {span}")
        return 1
    date = str(bars[i]["time"])[:10]
    board = _board_name(args.code)
    print(f"策略 {key} {getattr(strat, 'name', '')} | 形态 {spec_kind} | "
          f"{args.code} {board}")
    print(f"bars {len(bars)} 根 ({span}) | 决策日 {date} @ 收盘 {bars[i]['close']}")
    print("-" * 72)

    # ---- 引擎 A: 门表 ----
    use_gate = bool(yaml_path)
    if use_gate:
        from app.market_cn.auto.core.runtime.evaluate import load_strategy
        spec = load_strategy(key)
        enum = str(spec.meta.get("enumeration", "limit_up")).lower()
        flow = str(spec.meta.get("day_flow", "v1")).lower()
        if enum == "day" and flow not in _GATE_DAY_FLOWS:
            use_gate = False               # g56 等需横截面 ext → 走插件引擎
            print(f"  [门表编排 day_flow={flow} 需特化上下文 → 转插件引擎 (策略自带 TRACE)]")

    if use_gate:
        out = report_gate_table(key, spec, bars, args.code, info, i,
                                max_lu=args.max_lu, pin_lu=args.lu or None)
        out["conclusion"] = _conclude_gate(out)
    else:
        out = report_plugin(key, strat, bars, args.code, info, date, args.days)
        out["conclusion"] = _conclude_plugin(out)

    return _emit(out, args)


def _emit(out: dict, args) -> int:
    print("-" * 72)
    print("结论: " + out["conclusion"])
    if args.json:
        def _clean(o):
            if isinstance(o, dict):
                return {k: _clean(v) for k, v in o.items()}
            if isinstance(o, list):
                return [_clean(v) for v in o]
            return o
        print(json.dumps(_clean(out), ensure_ascii=False, indent=1, default=str))
    return 0


def _board_name(code: str) -> str:
    try:
        from app.market_cn.auto.core.market import get_board_name
        return get_board_name(code)
    except Exception:
        return ""


def _yaml_path(key: str) -> str:
    from app.market_cn.auto.core._paths import STRATEGY_DIR
    p = os.path.join(STRATEGY_DIR, f"{key}.yaml")
    return p if os.path.isfile(p) else ""


if __name__ == "__main__":
    sys.exit(main())
