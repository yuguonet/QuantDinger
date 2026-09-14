#!/usr/bin/env python3
"""auto/tools/param_scan.py — 参数网格扫描+敏感性报告器 (2026-09-13)

用途: 09-12 定案"参数微调=代码网格扫描"的工具载体。给定策略+参数网格, 逐组合
     全市场回测, 一份报告输出: 全组合表+两段稳定性+单维敏感性(OFAT)+最优点邻域
     (平台/尖峰判定)+whatif 逐笔 diff (最优点 vs 基准: 保留/新增/消失/改判)。
     分工纪律 (09-12): 策略组合=人凭经验, 参数微调=本工具; 报告呈现响应面,
     最终取舍由人裁定。导出的最优组合交易清单可直接喂 pool_check 复验。

用法:
  # 缺省 = 自动网格: 对 default_params 全部数值参数做单维切片 (int±1 /
  # float±15% 取 1-2-5 步长 / bool 开关消融), days 默认 300:
  python -m app.market_cn.auto.tools.param_scan --strategy break
  # 自动网格只扫指定参数:
  python -m app.market_cn.auto.tools.param_scan --strategy dragon_callback \
      --params min_streak,lu_gain20_min
  # 显式网格 (笛卡尔积):
  python -m app.market_cn.auto.tools.param_scan --strategy break --days 600 \
      --grid '{"min_streak":[2,3,4],"turnover_min":[null,10,21.7]}'
  python -m app.market_cn.auto.tools.param_scan --strategy dragon_callback \
      --days 600 --grid-file tmp/grid_dragon.json
  # 快速冒烟 (抽样股, 数字不可与全市场基线对照):
  python -m app.market_cn.auto.tools.param_scan --strategy v1 \
      --sample-codes 250 --grid '{"score_min":[7.0,7.5,8.0]}'

设计点:
  - 自动网格 (缺省 --grid): 单维切片而非笛卡尔积 — 每个数值参数独立 ±1 档 (int)
    / ±15% (float, 1-2-5 取整) / bool 消融, 恰构成 OFAT 敏感性扫描; 每维 ±两档
    +基准补跑 = 3 点, 邻域平台/尖峰判定天然可用; 基准点不入切片 (由补跑出);
  - 覆写机制: 实例级 strat.default_params = {**基准, **网格覆写} — 回测钩子内部
    merged_params(None) 只读 default_params; config params 仅经实盘扫描路径的
    params_override 注入, 不进回测路径, 故实例覆写即权威且互不干扰 (跑完即还原);
  - 零落库: 只调 run_all/run_all_intraday (内存 trades), 不写 store/不改 config;
  - 数据复用: hub daily memo 跨组合复用, 第 2 组合起零重载 (865s→71s 红利);
  - 两段口径: 按 entry_date 排序前后半 (⚠️ backtest._summary 的 1st/2nd half
    对 daily 类是按代码聚合序非时间序, 本工具自算时间序, 判"两段全正"以此为准);
  - 布尔/None 网格值 = 规则开关消融 (前提: 策略参数真实消费该开关, 如 turnover_min);
  - 报告落 D:/QuantDinger/tmp 带日期前缀; tmp 非权威 — 结论须与代码现状比对。

易错点:
  - 自动网格跳过 None/str/list/dict/0 值参数 (无法相对基准偏移), 打印 skipped
    清单; bool 参数切片后仅基准 1 个邻居, 邻域报"无法判定"属预期 (开关本就二元);
  - intraday_window 策略 (tail/knife) 走时间线引擎, 1m 历史仅 ~2026-04-20 后,
    days 给大无增量且慢; 其回测基准=代码默认值 (config params 不进回测路径,
    报告头部已注明; 如需以实盘参数为基准用 --base-params 注入);
  - 显式网格键不在 default_params 时会警告 (可能是拼写错, 或策略不消费该参数);
  - 单组合 0~1 笔时两段为 None (展示为 —), 全网格零交易直接退出;
  - markdown 单元格内不得出现 "|" (劈裂列), _seg_md 分隔符用 "·";
  - --max-combos 防组合爆炸; 大网格 (50+) 建议交用户终端跑 (沙箱内存上限);
  - --sample-codes 抽样只用于管道自测, 其数字与全市场基线不可比。
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import time
from datetime import datetime


# ================================================================
# 小工具
# ================================================================

def _fmt(v):
    """网格值 → 展示串。"""
    if v is None:
        return "None"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (list, tuple)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def _pct(v):
    """可空数值 → +x.xx 展示。"""
    return f"{v:+.2f}" if v is not None else "—"


def _bucket_stats(trades):
    """一组交易的摘要串 (n/胜率/均收); 空组返回 —。"""
    if not trades:
        return "—"
    rets = [float(t["return_pct"]) for t in trades]
    wr = sum(1 for r in rets if r > 0) / len(rets) * 100
    return f"n={len(rets)} 胜率{wr:.1f}% 均收{sum(rets)/len(rets):+.2f}%"


def _seg_stats(trades):
    """两段稳定性: 按 entry_date 排序前后半 → ((段1胜率,段1均收),(段2胜率,段2均收))。

    不足 2 笔 (h=0) 返回 ((None,None),(None,None)) — 保持二元组结构, 解包不炸。
    """
    ts = sorted(trades, key=lambda t: str(t.get("entry_date") or ""))
    h = len(ts) // 2
    if h == 0:
        return (None, None), (None, None)

    def _st(part):
        rets = [float(t["return_pct"]) for t in part]
        wr = sum(1 for r in rets if r > 0) / len(rets) * 100
        return (round(wr, 1), round(sum(rets) / len(rets), 2))

    return _st(ts[:h]), _st(ts[h:])


def _seg_md(r):
    """行对象的两段展示串 "wr1/wr2 · avg1/avg2" (seg 缺失→—)。

    ⚠️ 分隔符不得用 "|": 该串嵌入 markdown 表格单元格, 竖线会劈裂列。
    """
    if r.get("seg1", {}).get("avg") is None:
        return "—"
    return (f"{r['seg1']['wr']}/{r['seg2']['wr']} · "
            f"{r['seg1']['avg']:+.2f}/{r['seg2']['avg']:+.2f}")


def _trade_key(t):
    return (str(t.get("code") or ""), str(t.get("entry_date") or ""))


def _diff_trades(base_trades, cand_trades, tol=0.005):
    """whatif 逐笔 diff: 键=(code, entry_date)。

    返回 dict: kept(同键同果) / changed(同键改判: 收益或出场原因变) /
    added(新增) / removed(消失), 各为 trade 列表。
    """
    b = {_trade_key(t): t for t in base_trades}
    c = {_trade_key(t): t for t in cand_trades}
    kept, changed, added = [], [], []
    for k, t in c.items():
        if k not in b:
            added.append(t)
        elif (abs(float(t["return_pct"]) - float(b[k]["return_pct"])) > tol
              or t.get("exit_reason") != b[k].get("exit_reason")):
            changed.append(t)
        else:
            kept.append(t)
    removed = [t for k, t in b.items() if k not in c]
    return {"kept": kept, "changed": changed, "added": added, "removed": removed}


def _md_table(header, rows):
    """极简 markdown 表格。"""
    out = ["| " + " | ".join(header) + " |",
           "|" + "|".join(["---"] * len(header)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(out)


def _dedupe(seq):
    """保持顺序去重 (自动网格值点列用)。"""
    out, seen = [], set()
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _nice_step(x):
    """x>0 → 1-2-5×10^k 中覆盖 x 的最小"好看"步长 (容差 5% 防浮尘)。

    例: 0.105→0.1, 0.42→0.5, 0.9→1.0, 9→10。float 自动网格步长=_nice_step(|v|*0.15)。
    """
    e = math.floor(math.log10(x))
    m = x / (10 ** e)
    for c in (1.0, 2.0, 5.0, 10.0):
        if c >= m / 1.05:
            return c * (10 ** e)
    return 10.0 * (10 ** e)


def _auto_grid(params, only=None):
    """缺省 --grid 时: 从参数表自动生成单维切片网格。

    规则: bool→[false,true] (开关消融); int≥1→[v-1,v,v+1]; float≠0→v±_nice_step
    (≈±15%, 1-2-5 取整); None/str/list/dict/0 无法相对基准偏移 → 跳过进 skipped。
    返回 (grid, skipped)。
    """
    grid, skipped = {}, []
    for k, v in params.items():
        if only is not None and k not in only:
            continue
        if isinstance(v, bool):
            grid[k] = [False, True]
        elif isinstance(v, int) and v >= 1:
            grid[k] = _dedupe([v - 1, v, v + 1])
        elif isinstance(v, float) and v != 0.0:
            st = _nice_step(abs(v) * 0.15)
            grid[k] = _dedupe([round(v - st, 10), v, round(v + st, 10)])
        else:
            skipped.append(f"{k}={_fmt(v)}")
    return grid, skipped


# ================================================================
# 主流程
# ================================================================

def main():
    ap = argparse.ArgumentParser(description="参数网格扫描+敏感性报告 (零落库)")
    ap.add_argument("--strategy", required=True, help="已注册策略 key")
    ap.add_argument("--days", type=int, default=300)
    ap.add_argument("--grid", default="",
                    help='显式网格 JSON (缺省=自动网格: 全部数值参数单维切片)')
    ap.add_argument("--grid-file", default="", help="网格 JSON 文件 (优先于 --grid)")
    ap.add_argument("--params", default="",
                    help="逗号分隔参数名 (自动网格只扫这些; 缺省=全部可偏移参数)")
    ap.add_argument("--base-params", default="",
                    help='基准参数补丁 JSON (应用于所有组合, 如实盘 config params)')
    ap.add_argument("--codes", default="", help="逗号分隔股票池 (默认全市场)")
    ap.add_argument("--sample-codes", type=int, default=0, help="随机抽样股数 (仅冒烟用)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--max-combos", type=int, default=48, help="组合数上限 (防爆炸)")
    ap.add_argument("--min-n", type=int, default=10, help="选最优点时的最低笔数")
    ap.add_argument("--top", type=int, default=15, help="报告展示前 N 组合")
    ap.add_argument("--start-date", default="")
    ap.add_argument("--end-date", default="")
    ap.add_argument("--out", default="", help="报告 md 路径 (默认 tmp/param_scan_*.md)")
    args = ap.parse_args()

    # .env (DB 连接; tools 目录上溯 4 级=backend_api_python, 5 级=项目根)
    try:
        from dotenv import load_dotenv
        d = os.path.dirname(os.path.abspath(__file__))
        for _p in [os.path.normpath(os.path.join(d, "..", "..", "..", "..", ".env")),
                   os.path.normpath(os.path.join(d, "..", "..", "..", "..", "..", ".env"))]:
            if os.path.isfile(_p):
                load_dotenv(_p, override=False)
                break
    except Exception:
        pass

    # ---- 网格解析 (显式 --grid/--grid-file 优先; 缺省=自动网格, 需先加载策略) ----
    explicit_grid = None
    try:
        if args.grid_file:
            with open(args.grid_file, encoding="utf-8") as f:
                explicit_grid = json.load(f)
        elif args.grid.strip():
            explicit_grid = json.loads(args.grid)
    except json.JSONDecodeError as e:
        raise SystemExit(f"网格 JSON 解析失败: {e}")
    auto_mode = explicit_grid is None
    only = ([s.strip() for s in args.params.split(",") if s.strip()]
            if args.params.strip() else None)
    base_patch = json.loads(args.base_params) if args.base_params.strip() else {}
    if not auto_mode:
        if not explicit_grid or not all(isinstance(v, list) and v
                                        for v in explicit_grid.values()):
            raise SystemExit(
                '网格须为 {参数名: [值,...]} 且每个值列表非空; None/bool 值=开关消融。'
                '示例: --grid \'{"min_streak":[2,3,4],"turnover_min":[null,10]}\' '
                '(或省略 --grid, 自动对全部数值参数做单维切片)')

    # ---- 策略与基准 ----
    from app.market_cn.auto import strategies as strat_reg
    strat_reg.autodiscover()
    strat = strat_reg.get_strategy(args.strategy)
    if strat is None:
        raise SystemExit(f"策略 {args.strategy} 未注册 "
                         f"(可用: {sorted(strat_reg.all_strategies())})")
    kind = strat.scan_spec.kind
    base_eff = strat.merged_params(None)          # 代码默认值 (回测路径基准)
    base_eff.update(base_patch)                   # 用户注入的基准补丁
    cfg_params = strat_reg.params_override(args.strategy)
    if cfg_params and not base_patch:
        print(f"⚠️ 该策略 config.json 有 params 覆盖 {sorted(cfg_params)} "
              f"(仅实盘扫描路径生效); 回测基准=代码默认值, 如需以实盘参数为基准 "
              f"用 --base-params '{json.dumps(cfg_params, ensure_ascii=False)}'")

    # ---- 网格与组合构造 (auto 须在加载策略后从 default_params 生成) ----
    if auto_mode:
        grid, skipped = _auto_grid(base_eff, only)
        if not grid:
            raise SystemExit(
                "自动网格为空: 该策略参数中无可偏移的数值参数 (bool/int≥1/float≠0)。"
                "None/str/list/dict/0 值参数被跳过; 请用显式 --grid 指定。")
        combos = [{k: v} for k, vs in grid.items() for v in vs
                  if v != base_eff.get(k)]     # 单维切片; 基准点不入切片 (补跑出)
        print("自动网格 (单维切片, 基准=代码默认值):")
        for k in grid:
            print(f"  {k}: [{', '.join(_fmt(v) for v in grid[k])}] "
                  f"(基准 {_fmt(base_eff.get(k))})")
        if skipped:
            print(f"  跳过 (无法自动偏移): {', '.join(skipped)}")
    else:
        grid = explicit_grid
        if only:
            print("⚠️ --params 仅自动网格模式生效 (显式 --grid 已指定扫描集, 忽略)")
        combos = [dict(zip(list(grid), c))
                  for c in itertools.product(*[grid[k] for k in grid])]
    keys = list(grid)
    if not auto_mode:
        unknown = [k for k in keys if k not in base_eff]
        if unknown:
            print(f"⚠️ 网格键不在策略参数中 (将作为新增参数传入, 策略可能不消费): {unknown}")
    if len(combos) > args.max_combos:
        raise SystemExit(f"组合数 {len(combos)} > 上限 {args.max_combos} "
                         f"(--max-combos 可调; 大网格建议用户终端跑)")

    # ---- 股票池 ----
    from app.market_cn.auto.data.hub import all_codes
    pool_mode = "全市场"
    codes = None
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
        pool_mode = f"指定 {len(codes)} 股"
    elif args.sample_codes > 0:
        import random
        allc = sorted(all_codes())
        random.seed(args.seed)
        codes = random.sample(allc, min(args.sample_codes, len(allc)))
        pool_mode = (f"抽样 {len(codes)}/{len(allc)} (seed={args.seed}, "
                     f"冒烟口径, 不可与全市场基线对照)")

    # ---- 回测执行件 ----
    if kind == "intraday_window":
        from app.market_cn.auto.backtest import run_all_intraday
    else:
        from app.market_cn.auto.backtest import run_all
    start_date = args.start_date or None
    end_date = args.end_date or None

    def _full(rp):                                   # 缺省键补基准值后对齐
        return {k: rp.get(k, base_eff.get(k)) for k in keys}

    def _run_one():
        """按策略 kind 分发一次全市场回测 (progress 静音, 本工具自打进度)。"""
        if kind == "intraday_window":
            return run_all_intraday(strat, days=args.days, codes=codes,
                                    start_date=start_date, end_date=end_date,
                                    progress_every=0)
        return run_all(strategy=args.strategy, days=args.days, codes=codes,
                       start_date=start_date, end_date=end_date, progress_every=0)

    def _mk_row(override, res, t0):
        trades = res.get("trades") or []
        stats = res.get("stats") or {}
        (wr1, avg1), (wr2, avg2) = _seg_stats(trades)
        seg = {"wr": wr1, "avg": avg1}, {"wr": wr2, "avg": avg2}
        worst = (min(x for x in (avg1, avg2) if x is not None)
                 if avg1 is not None and avg2 is not None else None)
        return {"params": override, "n": stats.get("n", 0),
                "winrate": stats.get("winrate"), "avg_ret": stats.get("avg_ret"),
                "pl_ratio": stats.get("pl_ratio"),
                "seg1": seg[0], "seg2": seg[1], "worst_seg": worst,
                "elapsed": round(time.time() - t0, 1), "trades": trades}

    rows = []
    print(f"param_scan {args.strategy} kind={kind} days={args.days} "
          f"| {pool_mode} | {'自动网格·单维切片' if auto_mode else '显式网格·笛卡尔积'} "
          f"| 组合 {len(combos)} 个", flush=True)
    t_all = time.time()
    for i, override in enumerate(combos, 1):
        eff = {**base_eff, **override}
        strat.default_params = eff                   # 实例级覆写 (finally 还原)
        t0 = time.time()
        try:
            row = _mk_row(override, _run_one(), t0)
            print(f"  [{i}/{len(combos)}] {json.dumps(override, ensure_ascii=False)} "
                  f"→ n={row['n']} {row['winrate']}%/ {_pct(row['avg_ret'])}% "
                  f"两段 {_seg_md(row)} ({row['elapsed']}s)", flush=True)
        except Exception as e:                       # 单组合失败不拖垮整轮
            row = {"params": override, "error": f"{type(e).__name__}: {e}",
                   "n": 0, "elapsed": round(time.time() - t0, 1)}
            print(f"  [{i}/{len(combos)}] {json.dumps(override, ensure_ascii=False)} "
                  f"→ ERROR {row['error']}", flush=True)
        finally:
            strat.__dict__.pop("default_params", None)   # 还原实例覆写
        rows.append(row)

    ok_rows = [r for r in rows if "error" not in r]
    if not ok_rows:
        raise SystemExit("全部组合失败, 详见上方 ERROR 行")
    total_s = round(time.time() - t_all, 1)

    # ---- 基准行 (网格已覆盖基准点 → 复用该组合; 否则单独补跑一次) ----
    base_row = next((r for r in rows if not r["params"]), None)
    if base_row is None:
        base_row = next((r for r in ok_rows
                         if all(r["params"].get(k, base_eff.get(k)) == base_eff.get(k)
                                for k in keys)), None)
    if base_row is None:
        strat.default_params = dict(base_eff)
        t0 = time.time()
        try:
            base_row = _mk_row({}, _run_one(), t0)
            print(f"  [基准补跑] {{}} → n={base_row['n']} "
                  f"{base_row['winrate']}%/ {_pct(base_row['avg_ret'])}% "
                  f"两段 {_seg_md(base_row)}", flush=True)
        except Exception as e:
            base_row = {"params": {}, "error": f"{type(e).__name__}: {e}", "n": 0}
        finally:
            strat.__dict__.pop("default_params", None)

    # ---- 最优点 (n≥min-n 中优先两段全正, 取均收最高; 最终取舍归人) ----
    cand = [r for r in ok_rows if r["n"] >= args.min_n] or ok_rows
    seg_ok = [r for r in cand
              if r.get("seg1", {}).get("avg") is not None
              and r["seg1"]["avg"] > 0 and r["seg2"]["avg"] > 0]
    best = max(seg_ok or cand,
               key=lambda r: r["avg_ret"] if r["avg_ret"] is not None else -999.0)
    if not best.get("n") or best.get("avg_ret") is None:
        raise SystemExit("所有组合均无交易 (检查参数/窗口/股票池), 无从比较")
    if not seg_ok:
        print("⚠️ 无组合两段全正 — 最优点只是全段均收最高, 单段有效=过拟合嫌疑")

    # ---- 邻域分析 (与 best 恰差一维的所有已跑组合, 基准行也参与) ----
    best_full = _full(best["params"])
    neighbor_pool = list(ok_rows)
    if base_row and "error" not in base_row:
        neighbor_pool.append(base_row)
    neighbors = []
    for r in neighbor_pool:
        if r is best:
            continue
        diff = [k for k in keys if _full(r["params"])[k] != best_full[k]]
        if len(diff) == 1:
            neighbors.append((diff[0], r))
    verdict = "邻域信息不足 (网格太稀)"
    if best["avg_ret"] > 0 and neighbors:
        nb_valid = [r for _, r in neighbors if r["avg_ret"] is not None]
        nb_best = max(r["avg_ret"] for r in nb_valid) if nb_valid else None
        nb_mean = (sum(r["avg_ret"] for r in nb_valid) / len(nb_valid)
                   if nb_valid else None)
        if len(nb_valid) < 2:
            verdict = (f"有效邻域点仅 {len(nb_valid)}/{len(neighbors)} 个, "
                       f"无法判定平台/尖峰 — 建议每维至少 3 个值")
        elif best["avg_ret"] > 1.5 * max(nb_best, 1e-9):
            verdict = (f"⚠️ 尖峰嫌疑: best {best['avg_ret']:+.2f} 远超邻域最大 "
                       f"{nb_best:+.2f}/均值 {nb_mean:+.2f} — 换相邻阈值即失效, 勿采纳")
        elif nb_best >= 0.8 * best["avg_ret"]:
            verdict = (f"平台: 邻域最大 {nb_best:+.2f}/均值 {nb_mean:+.2f} "
                       f"与 best {best['avg_ret']:+.2f} 同量级 — 稳健性好")
        else:
            verdict = f"中间态: 邻域最大 {nb_best:+.2f}/均值 {nb_mean:+.2f}"

    # ---- whatif diff (best vs 基准) ----
    diff = None
    if base_row and "trades" in base_row and base_row["trades"]:
        diff = _diff_trades(base_row["trades"], best["trades"])

    # ---- 落盘 (tools 上溯 5 级 = D:\QuantDinger; tmp 为项目级约定目录) ----
    d = os.path.dirname(os.path.abspath(__file__))
    root = os.path.normpath(os.path.join(d, "..", "..", "..", "..", ".."))
    tmp_dir = os.path.join(root, "tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    stem = os.path.join(tmp_dir, f"param_scan_{args.strategy}_{ts}")
    out_md = args.out or stem + ".md"

    def _row_cells(r, dims):
        if "error" in r:
            # 单元格总数须=len(dims)+7 与表头对齐; 报文占首格, 末格(耗时)省略
            return ["ERROR: " + r["error"]] + ["—"] * (len(dims) + 6)
        return ([_fmt(r["params"][d]) if d in r["params"] else _fmt(base_eff.get(d))
                 for d in dims]
                + [r["n"], r["winrate"], _pct(r["avg_ret"]),
                   r["pl_ratio"] if r["pl_ratio"] is not None else "—",
                   _seg_md(r),
                   _pct(r["worst_seg"]),
                   f"{r['elapsed']}s"])

    hdr = keys + ["n", "胜率%", "均收%", "PL", "两段(胜率·均收)", "最差段", "耗时s"]
    ranked = sorted(ok_rows, key=lambda r: (r["avg_ret"] if r["avg_ret"] is not None
                                            else -999), reverse=True)

    md = [f"# param_scan 报告 — {args.strategy}",
          "",
          f"- 生成 {datetime.now().strftime('%Y-%m-%d %H:%M')} | kind={kind} "
          f"| days={args.days} | 窗口 {start_date or '∞'}~{end_date or '今'} "
          f"| {pool_mode} | 组合 {len(combos)} | 总耗时 {total_s}s",
          f"- 扫描网格 ({'自动·单维切片' if auto_mode else '显式·笛卡尔积'}): "
          + "; ".join(f"{k}∈[{'/'.join(_fmt(v) for v in grid[k])}]" for k in keys),
          f"- 基准参数 (代码默认{'+补丁' if base_patch else ''}): "
          f"{json.dumps({k: base_eff[k] for k in keys if k in base_eff}, ensure_ascii=False)}",
          f"- 两段=按入场日排序前后半; **单段有效=过拟合; 只认长窗口 "
          f"(150d/300d/600d 可差 10pp)**; 本报告落 tmp/ 非权威, 采纳前须与代码现状比对",
          "",
          "## 全组合表 (按均收降序)",
          _md_table(hdr, [_row_cells(r, keys) for r in ranked[:max(args.top, 1)]]),
          "",
          "## 基准行",
          _md_table(hdr, [_row_cells(base_row, keys)]) if base_row else "(缺失)",
          "",
          "## 最优点",
          f"- 参数: {json.dumps(best['params'], ensure_ascii=False)} "
          f"(改动维: {[k for k in keys if best['params'].get(k, base_eff.get(k)) != base_eff.get(k)]})",
          f"- n={best['n']} 胜率 {best['winrate']}% 均收 {_pct(best['avg_ret'])}% "
          f"PL {best['pl_ratio']} | 两段(胜率·均收) {_seg_md(best)}",
          f"- 邻域判定: {verdict}",
          ""]

    # 策略规则清单 (2026-09-13 用户反馈①: 报告须自解释 — 每个参数维属于哪条规则)
    rule_defs = list(getattr(strat, "RULE_DEFS", None) or [])
    if rule_defs:
        md += ["## 策略规则清单 (RULE_DEFS, 序 = 判定短路序)", ""]
        md += [f"{k}. **{g}** — {desc}" for k, (g, desc) in enumerate(rule_defs, 1)]
        md += ["", f"> 门级拦截比例 / 通过组 vs 被拦组统计 / 换序稳定性: "
               f"`python -m app.market_cn.auto.tools.rule_audit "
               f"--strategy {args.strategy} --days {args.days}`", ""]
    else:
        md += ["## 策略规则清单", "",
               f"(策略 {args.strategy} 未声明 RULE_DEFS — 门级归因不可用; "
               "声明方法参照 strategies/triple_resonance.py)", ""]

    # 单维敏感性 (OFAT: 其余维=基准值; 候选池含基准行 — auto 单维切片的基准点即此)
    md += ["## 单维敏感性 (OFAT, 其余维=基准)", ""]
    ofat_pool = (list(ok_rows)
                 + ([base_row] if base_row and "error" not in base_row else []))
    for d in keys:
        if d not in base_eff:
            continue          # unknown 键无基准锚点 (上文已警告), 跳过防 KeyError
        if base_eff[d] not in grid[d]:
            md += [f"### {d} — ⚠️ 基准值 {_fmt(base_eff[d])} 不在网格 "
                   f"{[_fmt(v) for v in grid[d]]}, 无基准锚点, 请全网格表自行对照", ""]
            continue
        rows_d = []
        for v in grid[d]:
            match = next((r for r in ofat_pool
                          if _full(r["params"]).get(d) == v
                          and all(_full(r["params"]).get(k) == base_eff.get(k)
                                  for k in keys if k != d)), None)
            rows_d.append([_fmt(v)] + (
                [match["n"], match["winrate"], _pct(match["avg_ret"]),
                 match["pl_ratio"] if match["pl_ratio"] is not None else "—",
                 _seg_md(match)]
                if match else ["(该切片未跑出)"] * 5))
        md += [f"### {d} (基准 {_fmt(base_eff[d])})",
               _md_table(["值", "n", "胜率%", "均收%", "PL", "两段(胜率·均收)"],
                         rows_d), ""]

    # 邻域明细
    md += ["## 最优点邻域 (恰差一维的组合)",
           _md_table(["维度", "n", "胜率%", "均收%", "两段(胜率·均收)"],
                     [[f"{dim}={_fmt(r['params'].get(dim, base_eff.get(dim)))}", r["n"],
                       r["winrate"], _pct(r["avg_ret"]), _seg_md(r)]
                      for dim, r in neighbors]) if neighbors else "(无)", ""]

    # whatif diff
    md += ["## whatif 逐笔 diff (最优点 vs 基准)"]
    if diff:
        md += [f"- 保留 {len(diff['kept'])} | 改判 {len(diff['changed'])} | "
               f"新增 {len(diff['added'])} | 消失 {len(diff['removed'])}",
               f"- 保留组 {_bucket_stats(diff['kept'])}",
               f"- 改判组 {_bucket_stats(diff['changed'])}",
               f"- 新增组 {_bucket_stats(diff['added'])} "
               f"(基准没有、新参数带来的交易)",
               f"- 消失组 {_bucket_stats(diff['removed'])} "
               f"(被新参数杀掉的旧交易 — 看'消失组均收'判断杀对了没)", ""]

        def _lines(ts_, cap=12):
            return [f"  - {t.get('code')} {t.get('entry_date')} "
                    f"{t['return_pct']:+.2f}% {t.get('exit_reason', '')}"
                    for t in ts_[:cap]] + ([f"  - ...共 {len(ts_)} 笔"]
                                           if len(ts_) > cap else [])

        for name in ("changed", "added", "removed"):
            if diff[name]:
                md += [f"### {name} 明细 (前 12)"] + _lines(diff[name]) + [""]
    else:
        md += ["(基准无交易或缺失, 无法 diff)", ""]

    md += ["## 判读提示 (纪律内置)",
           "- 采纳前: ①两段全正 ②邻域平台 (非尖峰) ③换窗口复扫验证 "
           "(150d/300d/600d 结论一致才可信) ④最优清单过 pool_check 信号级复验",
           f"- 最优组合交易清单已导出: `{stem}_best_trades.json` "
           f"(pool_check 兼容: python -m app.market_cn.auto.tools.pool_check "
           f"--trades 该文件 --gate \"...\")",
           ""]

    with open(out_md, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    with open(stem + ".json", "w", encoding="utf-8") as f:
        json.dump({"meta": {"strategy": args.strategy, "kind": kind, "days": args.days,
                            "auto": auto_mode, "grid": grid, "base_eff": base_eff,
                            "pool": pool_mode, "start_date": start_date,
                            "end_date": end_date, "generated": ts},
                   "rows": [{k: v for k, v in r.items() if k != "trades"}
                            for r in rows]}, f, ensure_ascii=False, indent=1)
    with open(stem + "_best_trades.json", "w", encoding="utf-8") as f:
        json.dump(best.get("trades") or [], f, ensure_ascii=False)

    print(f"\n最优: {json.dumps(best['params'], ensure_ascii=False)} → "
          f"n={best['n']} {best['winrate']}%/ {_pct(best['avg_ret'])}% "
          f"两段(胜率·均收) {_seg_md(best)}")
    print(f"邻域: {verdict}")
    if diff:
        print(f"diff vs 基准: 保留{len(diff['kept'])} 改判{len(diff['changed'])} "
              f"新增{len(diff['added'])} 消失{len(diff['removed'])} "
              f"(消失组 {_bucket_stats(diff['removed'])})")
    print(f"报告: {out_md}\nJSON: {stem}.json\n最优清单: {stem}_best_trades.json")


if __name__ == "__main__":
    main()
