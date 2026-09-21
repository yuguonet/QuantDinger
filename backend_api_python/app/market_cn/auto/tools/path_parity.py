#!/usr/bin/env python3
"""path_parity.py — 三路径同源自检 (M2 验收件, 2026-09-20)

用途: 把"三条执行路径判定口径必须同源"这条纪律变成可执行闸门。
    回测≠实盘会买什么, 是本框架最贵的一类 bug (2026-09-20 已实修一次:
    run_all_intraday 缺 U1~U4 → 盘中回测信号集比实盘发散)。

三条路径 (docs/自动策略框架设计.md §3):
  实盘       scan.run_scan (daily_close) / scan.run_scan_knife (intraday_window)
  盘中回测   backtest.run_all_intraday (时间线引擎)
  日线回测   strategy.backtest_stock (base 默认 or 策略覆盖)

两层检查:
  A. 静态同源 (唯一实现 + 引用一致 + 无内联副本):
     A1 共享原语在 auto/ 树中**顶层定义唯一** (防复制粘贴出第二份);
     A2 三条路径模块必须 import 引用该原语 (防各自重写);
     A3 路径/引擎文件禁出现执行约束原语的**内联等价式** (如 * (1 - 0.10) 内联跌停价)。
  B. 运行时同源 (行为对账):
     B1 日线族: 回测 d0_date 集合 ⊆ 实盘口径 kept 集合 (回测只能判实盘会判的日子);
     B2 盘中族: run_all_intraday 的 (code,date) 集 == IDE 门表通道同集 (两套独立实现同一语义)。

易错点:
  - 本工具只判定"是否同源", 不做调试 (调试=probe); 分叉定位靠逐笔等价脚本;
  - B2 依赖 1m 快照帧缓存; 无缓存时该段 SKIP 而非 FAIL;
  - 静态 A2 只看 import 事实, 不看调用点 (调用点漏用由 A3/B 兜)。

用法:
  python -m app.market_cn.auto.tools.path_parity                # 全量
  python -m app.market_cn.auto.tools.path_parity --static-only  # 只跑静态段 (零 IO)
  python -m app.market_cn.auto.tools.path_parity --days 60 --limit 400
"""
from __future__ import annotations

import argparse
import ast
import os
import sys

# CLI 直跑时加载 .env (运行时段取数需要)
try:
    from dotenv import load_dotenv
    for _p in [os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))), ".env"),
            os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))))), ".env"),
            os.path.join(os.getcwd(), ".env")]:
        if os.path.isfile(_p):
            load_dotenv(_p, override=False)
            break
except Exception:
    pass

_AUTO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # .../auto

# ================================================================
# A. 共享原语清单: (人类名, 模块路径, 函数名)
#    这些是"判定口径/成交语义"的原子实现, 全框架必须只有一份。
# ================================================================
SHARED = [
    ("U1~U4 锚点",   "app.market_cn.auto.scan",                "_anchor_idx"),
    ("U1~U4 统一过滤", "app.market_cn.auto.core.filters",     "unified_prefilter"),
    ("跌停价",       "app.market_cn.auto.core.market",   "limit_dn_price"),
    ("一字跌停",     "app.market_cn.auto.core.exec",       "is_one_word_limit_dn"),
    ("跳空成交",     "app.market_cn.auto.core.exec",       "fill_on_gap"),
    ("跌停卖出阻",   "app.market_cn.auto.core.exec",       "fill_blocked_by_limit_dn"),
]

# A2: 判定口径原语 → 必须 import 引用它的路径 (直接内联即 FAIL)。
#     成交语义原语 (core.exec.*) 由 A3 内联副本检测兜底 —— 它们只被出场引擎消费,
#     若强行要求三条判定路径 import 会误报 (路径只判入场, 不做出场成交)。
REQUIRED_IMPORTS = [
    ("unified_prefilter", ("实盘", "盘中回测", "日线回测")),   # U1~U4 三条路径都施
    ("_anchor_idx",       ("盘中回测",)),                     # 盘中回测必须与实盘同锚点
]

# 三条路径模块 (相对 auto/): 实盘 / 盘中回测 / 日线回测
# ⚠️ 路径必须跟着 §10 目录重组走：M5 把 `common/` `ide/` `data/` 与 `backtest.py`
# 收进 `core/`（如 `backtest.py` → `core/backtest.py`）。此处漏改会退化成
# "缺文件 → 报 FAIL"，把**检查器自身过期**误报成生产分叉。
PATH_FILES = {
    "实盘":       "scan.py",
    "盘中回测":   "core/backtest.py",
    "日线回测":   "strategies/base.py",
}

# 参与 A3 内联副本检测的目录 (路径 + 引擎 + IDE 实现)
_ENGINE_DIRS = ["strategies", "core"]
_ENGINE_FILES = ["scan.py", "core/backtest.py"]

# A3: 执行约束原语的"内联等价式"特征 — 出现即视为未走 core.exec 原语。
# 只收高信号模式 (误报会让人关掉闸门, 宁缺勿滥):
#   跌停价内联: x * (1 - 0.10) / x * (1 - 0.20) / x * 0.9 / x * 0.8
_INLINE_LIMIT_DN = {0.10, 0.20, 0.9, 0.8}


def _iter_py(root):
    for dirpath, _dirs, files in os.walk(root):
        if os.sep + "tools" + os.sep in dirpath + os.sep:
            continue                      # tools/ 是检查器自身, 不算路径/引擎
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(dirpath, f)


def _parse(path):
    with open(path, "r", encoding="utf-8") as fh:
        return ast.parse(fh.read(), filename=path)


def _toplevel_defs(tree):
    return {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _imported_names(tree):
    """本模块从别处 import 进来的名字集合 (含 as 别名)。"""
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            for a in n.names:
                names.add(a.asname or a.name)
        elif isinstance(n, ast.Import):
            for a in n.names:
                names.add((a.asname or a.name).split(".")[0])
    return names


def _inline_limit_dn_hits(tree):
    """A3: 检出 x * (1 - k) / x * k (k ∈ 跌停价内联特征) 形态。"""
    hits = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.BinOp) or not isinstance(n.op, ast.Mult):
            continue
        for side in (n.left, n.right):
            v = None
            if isinstance(side, ast.Constant) and isinstance(side.value, (int, float)):
                v = float(side.value)
            elif (isinstance(side, ast.BinOp) and isinstance(side.op, ast.Sub)
                  and isinstance(side.left, ast.Constant) and side.left.value == 1
                  and isinstance(side.right, ast.Constant)):
                v = 1.0 - float(side.right.value)     # (1 - 0.10) → 0.9
            if v is not None and round(v, 4) in {round(k, 4) for k in _INLINE_LIMIT_DN}:
                hits.append((n.lineno, round(v, 3)))
    return hits


def static_audit(verbose=True):
    """A 段: 返回 (n_fail, report_lines)。"""
    report, n_fail = [], 0

    # ---- 收集 auto/ 树所有文件的顶层 def + import ----
    files = list(_iter_py(_AUTO))
    defs_of = {}          # {function_name: [(path, ...)]}
    imports_of = {}       # {path: set(names)}
    trees = {}
    for p in files:
        try:
            t = _parse(p)
        except SyntaxError as e:
            report.append(f"[FAIL] 语法错误 {p}: {e}")
            n_fail += 1
            continue
        trees[p] = t
        imports_of[p] = _imported_names(t)
        for name in _toplevel_defs(t):
            defs_of.setdefault(name, []).append(p)

    # ---- A1 唯一实现 ----
    for label, mod, fn in SHARED:
        defs = defs_of.get(fn, [])
        # 允许 _anchor_idx 在 scan.py 内定义; 其余必须恰一处
        if len(defs) == 1:
            report.append(f"[PASS] A1 唯一实现 {label:12s} {fn}  ← {os.path.relpath(defs[0], _AUTO)}")
        elif len(defs) == 0:
            report.append(f"[FAIL] A1 唯一实现 {label:12s} {fn}  未找到顶层定义 (模块/名字漂移?)")
            n_fail += 1
        else:
            rels = [os.path.relpath(d, _AUTO) for d in defs]
            report.append(f"[FAIL] A1 唯一实现 {label:12s} {fn}  出现 {len(defs)} 份: {rels}")
            n_fail += 1

    # ---- A2 判定口径必须 import 引用 (而非内联/重定义) ----
    for fn, pathnames in REQUIRED_IMPORTS:
        for pname in pathnames:
            rel = PATH_FILES[pname]
            path = os.path.normpath(os.path.join(_AUTO, rel))
            if path not in imports_of:
                report.append(f"[FAIL] A2 引用  {fn:18s} {pname} 缺文件 {rel}")
                n_fail += 1
                continue
            self_def = path in defs_of.get(fn, [])       # scan._anchor_idx @ 实盘 = 定义处
            if self_def:
                report.append(f"[PASS] A2 引用  {fn:18s} {pname}  本模块为定义处")
                continue
            ok = fn in imports_of[path]
            report.append(f"[{'PASS' if ok else 'FAIL'}] A2 引用  {fn:18s} {pname}  "
                          f"{'import' if ok else '未 import ' + fn}")
            n_fail += int(not ok)

    # ---- A3 内联副本检测 (路径 + 引擎 + IDE) ----
    # normpath 必须加: `trees` 的键来自 os.walk (全反斜杠)，而这里拼的是
    # "core/backtest.py" 这类带正斜杠的相对串 —— 不归一化就对不上键, 该文件会被静默跳过。
    scan_paths = [os.path.normpath(os.path.join(_AUTO, f)) for f in _ENGINE_FILES]
    for d in _ENGINE_DIRS:
        scan_paths += [os.path.join(dp, f) for dp, _dd, fs in os.walk(os.path.join(_AUTO, d))
                       for f in fs if f.endswith(".py")]
    a3_fail = 0
    scanned = 0
    for path in sorted(set(scan_paths)):
        if path not in trees:
            continue
        scanned += 1
        hits = _inline_limit_dn_hits(trees[path])
        if hits:
            rel = os.path.relpath(path, _AUTO)
            report.append(f"[FAIL] A3 内联跌停价副本 {rel}: "
                          f"{[f'L{l}*{v}' for l, v in hits[:4]]} — 应走 core/exec")
            a3_fail += 1
    n_fail += a3_fail
    if a3_fail == 0:
        report.append(f"[PASS] A3 内联副本检测: 扫描 {scanned} 个引擎/路径文件, 0 副本")
    return n_fail, report


# ================================================================
# B. 运行时同源
# ================================================================

def _daily_scan_kept(strat, bars, code, si, params, only_days=None):
    """实盘口径 (镜像 scan.run_scan 单股判定): scan_signals + U1~U4 → kept 信号日集合。

    关键 (三条, 都是踩过的坑):
      1. 判定日 k 必须用**截断到该日的 bars** 取锚点与跑 U1~U4 (run_scan 里 bars 已截到
         target 日; _anchor_idx('signal') 取 bars 末根 = 判定日)。若误传全 bars,
         'signal' 锚点会落到最后一根 → 索引错位 (假"越界");
      2. 起点取 2 而非 25: 各策略 backtest_stock 枚举起点不同 (base=25, break=4),
         检查器必须取**真超集**, 否则早期索引信号漏判 → 假"越界";
      3. only_days 给定则**只在这些天求值** —— 断言是 `回测 d0_date ⊆ kept`, 只需查
         回测成交日是否落在 kept; 全枚举对 g56 这类 scan_signals 内部 O(n) 重建特征
         的策略会退化成 O(n²) (实测全市场 37min 跑不完)。
    """
    from app.market_cn.auto.scan import _anchor_idx
    from app.market_cn.auto.core.filters import unified_prefilter
    day_to_idx = {str(b["time"])[:10]: i for i, b in enumerate(bars)}
    if only_days is not None:
        days = [d for d in sorted(only_days) if d in day_to_idx]
    else:
        days = [str(b["time"])[:10] for b in bars[2:-1]]
    kept = set()
    for d in days:
        k = day_to_idx.get(d)
        if k is None or k < 2:
            continue
        sub = bars[:k + 1]
        try:
            sigs = strat.scan_signals(sub, code, **params) or []
        except Exception:
            continue
        if not sigs:
            continue
        if not getattr(strat, "use_unified_prefilter", True):
            kept.add(d)
            continue
        for s in sigs:
            idx = _anchor_idx(sub, s, strat)
            if idx is None:
                continue
            ok, _f = unified_prefilter(sub, idx, code, si)
            if ok:
                kept.add(d)
                break
    return kept


def _d0_of(tr):
    return str(tr.get("d0_date") or tr.get("signal_date") or "")[:10]


def runtime_daily(strats, codes, days, si, progress_every=0):
    """B1: 日线族 — 回测 d0_date ⊆ 实盘口径 kept 集合。"""
    from app.market_cn.auto.core.data.kline import fetch_kline_db
    from app.market_cn.auto import strategies as strat_reg
    report, n_fail = [], 0
    for key, strat in strats.items():
        params = strat_reg.params_override(key)
        n_bt = n_kept = n_over = 0
        bad = []
        for ci, code in enumerate(codes):
            if progress_every and ci and ci % progress_every == 0:
                print(f"  [{key}] {ci}/{len(codes)} 回测d0={n_bt} 越界={n_over}", flush=True)
            bars = fetch_kline_db(code, days)
            if not bars or len(bars) < 70:
                continue
            sinfo = si(code) if callable(si) else si
            try:
                trades = strat.backtest_stock(bars, code, stock_info=sinfo,
                                              use_prefilter=True) or []
            except TypeError:
                trades = strat.backtest_stock(bars, code, stock_info=sinfo) or []
            if not trades:
                continue
            bt_days = {_d0_of(t) for t in trades if _d0_of(t)}
            # only_days: 只在回测成交日查实盘口径 (断言 bt_days ⊆ kept 只需这些点;
            # 全枚举对 g56 等退化 O(n²), 见 _daily_scan_kept 注释)
            kept = _daily_scan_kept(strat, bars, code, sinfo, params, only_days=bt_days)
            n_bt += len(bt_days)
            n_kept += len(kept)
            over = bt_days - kept                        # 回测判了实盘不会判的日子 = 分叉
            n_over += len(over)
            if over:
                bad.append((code, sorted(over)[:3]))
        verdict = "PASS" if n_over == 0 else "FAIL"
        report.append(f"[{verdict}] B1 {key:16s} 回测d0={n_bt} 实盘口径通过={n_kept} 越界={n_over}"
                      + (f"  样例 {bad[:3]}" if bad else ""))
        n_fail += int(n_over > 0)
    return n_fail, report


def runtime_intraday(strats, days, limit=None):
    """B2: 盘中族 — 生产 run_all_intraday 的 (code,date) 集 == IDE 门表盘中通道同集。

    仅对 IDE 侧确有 `intraday` 枚举通道 (meta.enumeration=='intraday') 的策略对账。
    少数 intraday_window 策略的 IDE 通道落在日线枚举 (如 dragon_callback 用
    meta.enumeration 缺省 = limit_up, 因其实盘判定走 14:56 合成 D0 后按日线口径),
    此时盘中口径由各自的逐笔等价脚本独立覆盖, 这里显式 SKIP 并注明 (非 FAIL)。
    """
    report, n_fail = [], 0
    try:
        from app.market_cn.auto.core.data import frames as fr
        if not fr.first_1m_date():
            return 0, ["[SKIP] B2 盘中族: 无 1m 快照帧缓存, 跳过 (非 FAIL)"]
    except Exception as e:
        return 0, [f"[SKIP] B2 盘中族: frames 不可用 ({e}), 跳过"]

    from app.market_cn.auto.core.backtest import run_all_intraday
    from app.market_cn.auto.core.runtime.evaluate import load_strategy
    from app.market_cn.auto.core.present.intraday import run_all_intraday_ide
    for key, strat in strats.items():
        if strat.scan_spec.kind != "intraday_window":
            continue
        spec = load_strategy(key)
        enum = str(spec.meta.get("enumeration", "limit_up")).lower()
        if enum != "intraday" or not spec.meta.get("intraday"):
            report.append(f"[SKIP] B2 {key:16s} IDE 通道为 '{enum}' 枚举 (无 meta.intraday), "
                          f"盘中口径由逐笔等价脚本单独覆盖 — 非 FAIL")
            continue
        codes = None
        if limit:
            from app.market_cn.auto.core.data.hub import all_codes
            codes = all_codes()[:limit]
        ref = run_all_intraday(strat, days=days, codes=codes, progress_every=0)
        ref_set = {(t.get("code"), str(t.get("entry_date") or t.get("signal_date"))[:10])
                   for t in ref.get("trades", []) if t.get("code")}
        try:
            ide = run_all_intraday_ide(spec, days=days, codes=codes, progress_every=0)
            ide_set = {(t.get("code"), str(t.get("entry_date") or t.get("signal_date"))[:10])
                       for t in (ide or {}).get("trades", []) if t.get("code")}
        except Exception as e:
            report.append(f"[FAIL] B2 {key:16s} IDE 通道调用失败 ({type(e).__name__}: {e})")
            n_fail += 1
            continue
        only_ref, only_ide = ref_set - ide_set, ide_set - ref_set
        verdict = "PASS" if not only_ref and not only_ide else "FAIL"
        report.append(f"[{verdict}] B2 {key:16s} 盘中回测={len(ref_set)} IDE={len(ide_set)} "
                      f"仅回测={len(only_ref)} 仅IDE={len(only_ide)}"
                      + (f"  样例 ref-only={sorted(only_ref)[:2]} ide-only={sorted(only_ide)[:2]}"
                         if (only_ref or only_ide) else ""))
        n_fail += int(bool(only_ref or only_ide))
    return n_fail, report


def run(static_only=False, days=60, limit=None, only=None,
        skip_b1=False, skip_b2=False, progress_every=0):
    from app.market_cn.auto import strategies as strat_reg
    from app.market_cn.auto.core.data.hub import all_codes, stock_info as hub_si

    total_fail = 0
    print("=" * 64)
    print("A. 静态同源审计 (唯一实现 / 引用一致 / 无内联副本)")
    print("=" * 64)
    nf, rep = static_audit()
    print("\n".join(rep))
    print(f"--> A 段: {'PASS' if nf == 0 else f'{nf} FAIL'}")
    total_fail += nf

    if static_only:
        print(f"\npath_parity: {'ALL_PASS' if total_fail == 0 else f'{total_fail} FAIL'}")
        return total_fail

    strat_reg.autodiscover()
    enabled = {k: s for k, s in strat_reg.all_strategies().items() if strat_reg.is_enabled(k)}
    if only:
        enabled = {k: v for k, v in enabled.items() if k in only}
    daily = {k: s for k, s in enabled.items() if s.scan_spec.kind == "daily_close"}
    intra = {k: s for k, s in enabled.items() if s.scan_spec.kind == "intraday_window"}

    codes = all_codes()
    if limit:
        codes = codes[:limit]
    try:
        _si_map = hub_si()
    except Exception:
        _si_map = {}

    if not skip_b1:
        print("\n" + "=" * 64)
        print(f"B1. 日线族运行时同源 (回测 d0_date ⊆ 实盘口径; {len(codes)} 股 / {days}日)")
        print("=" * 64)
        nf, rep = runtime_daily(daily, codes, days, lambda c: _si_map.get(c),
                                progress_every=progress_every)
        print("\n".join(rep))
        print(f"--> B1 段: {'PASS' if nf == 0 else f'{nf} FAIL'}")
        total_fail += nf

    if not skip_b2:
        print("\n" + "=" * 64)
        print(f"B2. 盘中族运行时同源 (盘中回测 == IDE 门表通道; {days}日)")
        print("=" * 64)
        nf, rep = runtime_intraday(intra, days, limit=limit)
        print("\n".join(rep))
        print(f"--> B2 段: {'PASS' if nf == 0 else f'{nf} FAIL'}")
        total_fail += nf

    print("\n" + "=" * 64)
    print(f"path_parity: {'ALL_PASS ✓ (三路径同源)' if total_fail == 0 else f'{total_fail} FAIL ✗'}")
    return total_fail


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="三路径同源自检 (实盘/盘中回测/日线回测)")
    ap.add_argument("--static-only", action="store_true", help="只跑静态段 (零 IO)")
    ap.add_argument("--days", type=int, default=60, help="运行时取样天数 (默认 60)")
    ap.add_argument("--limit", type=int, default=None, help="取样股数上限 (默认全市场)")
    ap.add_argument("--only", default="", help="只测指定策略 key (逗号分隔)")
    ap.add_argument("--skip-b1", action="store_true", help="跳过 B1 (日线族运行时)")
    ap.add_argument("--skip-b2", action="store_true", help="跳过 B2 (盘中族运行时)")
    ap.add_argument("--progress", type=int, default=0, help="B1 每 N 股打一行进度 (0=关)")
    a = ap.parse_args()
    sys.exit(1 if run(static_only=a.static_only, days=a.days, limit=a.limit,
                      only=[s.strip() for s in a.only.split(",") if s.strip()] or None,
                      skip_b1=a.skip_b1, skip_b2=a.skip_b2,
                      progress_every=a.progress) else 0)
