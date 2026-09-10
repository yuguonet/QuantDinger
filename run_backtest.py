#!/usr/bin/env python3
"""run_backtest.py — 自动策略组回测一键入口 (根目录, 2026-09-10)

用途: 在项目根目录直接跑框架内全市场回测 (backend_api_python/app/market_cn/auto/backtest.py)。
     判定/出场/过滤全部 import 系统侧权威实现 (strategies/ 插件 + backtest 出场引擎),
     本脚本只是根目录的薄壳: 补 sys.path + .env → 调 run_all → 打人话报告。
     与 `python -m app.market_cn.auto.backtest` 完全同一份代码与口径 (已对数 117/139/94)。

用法 (在 D:/QuantDinger 根目录):
  python run_backtest.py                          # 默认: dragon, 300交易日, 全市场 (~50s)
  python run_backtest.py --strategy v1            # V1 (全市场 ~40s)
  python run_backtest.py --strategy break --days 120
  python run_backtest.py --strategy all           # 全部可回测策略连跑 (~2分钟)
  python run_backtest.py --strategy tail_oversold --days 120   # 盘中策略 (时间线引擎, 1m覆盖内)
  python run_backtest.py --codes 000859,002081    # 指定股票秒级调试
  python run_backtest.py --out tmp/my.json        # 逐笔明细落盘 (对数用)
  python run_backtest.py --top 10                 # 报告附 Top/Bottom 各10笔明细 (默认5)
  python run_backtest.py --list                   # 查看策略说明

参数:
  --strategy   任意已注册策略 key, 或 all (默认 dragon)
  --days N     回看窗口 (默认 300; 盘中策略受 1m 数据覆盖限制, 实际回测到覆盖起点)
  --start-date / --end-date   显式窗口 (盘中策略精确复现用)
  --codes      逗号分隔股票代码, 空则全市场
  --out PATH   逐笔交易 JSON 落盘路径 (不给则不落盘, 只打印报告)
  --top N      报告尾部展示盈亏两端各 N 笔 (默认 5, 0 关闭)
  --list       打印策略说明后退出

阅读提示 (现实化口径, 2026-09-09 出场引擎修正 T+1/跳空/跌停后):
  dragon  ≈50% 胜率 / +0.2% 均收  (慢接力, 峰值均 ~8%)
  v1      ≈73% 胜率 / +3.5% 均收  (D1弱动量 D2 开盘清仓)
  break   ≈71% 胜率 / +4.3% 均收  (断板确认, 收盘价口径出场)
  规则本体在 strategies/ 插件 + backtest.py 出场引擎; 改规则 → 跑本脚本 → 与基线对数:
  tmp/_compare_hub_vs_baseline.py (基线 tmp/test_dragon_callback_result_all.json)。

易错点:
  - dragon 全市场 ~50s (2026-09-10 起带必要条件预筛, 原 7 分钟); v1/break 各 ~40s;
  - 盘中策略 (tail/knife) 首次回测需逐日构建快照帧 (~15s/交易日, 一次性, 落盘缓存);
  - 需 backend_api_python/.env 的 DB 连接; DB 未起会静默返回 0 笔 (data 层吞错);
  - 输出 trades 字段与基线 JSON 对齐 (entry_date/entry_price/return_pct/exit_day...),
    可直接喂 _compare_hub_vs_baseline.py 对数。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

# ---- 根目录薄壳自举: backend 包入 path + .env (与 daily_screener.py 同款) ----
_backend_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend_api_python")
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)

try:
    from dotenv import load_dotenv
    for _p in [os.path.join(_backend_root, ".env"),
               os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")]:
        if os.path.isfile(_p):
            load_dotenv(_p, override=False)
            break
except Exception:
    pass

STRATEGY_INFO = {
    "dragon_callback": ("龙回头", "回调后慢接力, D1开盘买; 分段追踪+峰值逃顶, 7天到期"),
    "v1": ("V1追板", "D0四因子判定, D1开盘买; D1弱动量 D2 开盘清仓"),
    "break": ("断板", "连板首断确认, D1开盘买; 收盘价口径出场"),
}


def _load_registry():
    """读策略注册表 (autodiscover), 返回 {key: (实例, 可回测)}。"""
    from app.market_cn.auto import strategies as strat_reg
    from app.market_cn.auto.strategies.base import StrategyBase
    strat_reg.autodiscover()
    out = {}
    for key, s in strat_reg.all_strategies().items():
        has_hook = type(s).backtest_stock is not StrategyBase.backtest_stock
        is_intraday = getattr(getattr(s, "scan_spec", None), "kind", "") == "intraday_window"
        out[key] = (s, has_hook or is_intraday)   # 可回测 = 日线钩子 或 盘中时间线引擎
    return out


ALIAS = {"dragon": "dragon_callback"}   # 旧 CLI 名 → 注册表 key


def _print_report(strategy, res, top_n, strat_inst=None):
    """把 run_all 结果打成可读报告 (不 dump 全部字段, 只给人看)。"""
    if strat_inst is not None:
        label = getattr(strat_inst, "name", strategy)
        desc = STRATEGY_INFO.get(strategy, ("", strategy))[1]
    else:
        label, desc = STRATEGY_INFO.get(strategy, (strategy, ""))
    st = res["stats"]
    line = "=" * 68
    print(line)
    print(f" {label} ({strategy}) — {desc}")
    print(line)
    if st.get("n", 0) == 0:
        print(" 0 笔交易 (检查 DB 连接/数据是否就绪, 或窗口内无信号)")
        return
    print(f" 笔数 {st['n']} | 胜率 {st['winrate']}% | 均收 {st['avg_ret']:+.2f}%"
          f" | 盈亏比 {st['pl_ratio']} | 日均收益 {st['ret_per_day']}%")
    b = st["ret_buckets"]
    print(f" 收益分布: ≤-10%: {b['≤-10']} | -10~-3%: {b['-10~-3']} | -3~+3%: {b['-3~+3']}"
          f" | +3~+10%: {b['+3~+10']} | >+10%: {b['>+10']}")
    p = st["peak"]
    peak_mean = f"{p['mean']:.2f}%" if p["mean"] is not None else "-"
    print(f" 持仓期峰值: 均值 {peak_mean} (<10%: {p['lt10']} | 10~20%: {p['10_20']} | ≥20%: {p['ge20']})")
    print(f" 月均 {st['monthly_avg']} 笔 | 前半段胜率 {st['winrate_1st_half']}%"
          f" / 后半段 {st['winrate_2nd_half']}% (两段差异大=强环境依赖, 警惕)")
    print(f" 耗时 {res['elapsed']}s | 有效股票 {res['codes_ok']}")

    if top_n > 0:
        trades = sorted(res["trades"], key=lambda t: t["return_pct"])
        for title, rows in (("盈利 Top", trades[-top_n:][::-1]), ("亏损 Top", trades[:top_n])):
            print(f"\n {title}{top_n}:")
            print(f"   {'代码':<8}{'入场日':<12}{'入场价':>9}{'收益%':>8}{'峰值%':>8}"
                  f"{'持有日':>6}  出场理由")
            for t in rows:
                print(f"   {t['code']:<8}{str(t['entry_date']):<12}{t['entry_price']:>9}"
                      f"{t['return_pct']:>+8.2f}{t.get('peak_return_pct', 0):>8.2f}"
                      f"{t.get('exit_day', 0):>6}  {t.get('exit_reason', '') or '-'}")
    print(line)


def main():
    ap = argparse.ArgumentParser(description="自动策略组回测一键入口 (框架内, 与系统同口径)")
    ap.add_argument("--strategy", default="dragon",
                    help="任意已注册策略 key, 或 all (全部带回测钩子的策略)")
    ap.add_argument("--days", type=int, default=300)
    ap.add_argument("--start-date", default="", help="窗口起点 (盘中策略精确复现)")
    ap.add_argument("--end-date", default="", help="窗口终点 (默认今天)")
    ap.add_argument("--codes", default="", help="逗号分隔, 空则全市场")
    ap.add_argument("--out", default="", help="逐笔明细JSON输出路径 (对数用)")
    ap.add_argument("--top", type=int, default=5, help="报告展示盈亏两端各N笔 (0关闭)")
    ap.add_argument("--probe", default="",
                    help="开启调试探针并存档 (值=tag; 数据落 tmp/probes/, 供 AI 离线分析)")
    ap.add_argument("--list", action="store_true", help="打印可用策略后退出")
    args = ap.parse_args()

    reg = _load_registry()
    if args.list:
        print("可用策略 (--strategy; ✓=日线枚举回测, ◉=盘中时间线引擎):")
        for key in sorted(reg):
            s, runnable = reg[key]
            label = getattr(s, "name", key)
            kind = getattr(getattr(s, "scan_spec", None), "kind", "")
            mark = "✓" if kind == "daily_close" else ("◉" if runnable else "✗")
            print(f"  {mark} {key:<16} {label:<8} {kind}")
        return

    args.strategy = ALIAS.get(args.strategy, args.strategy)
    if args.strategy == "all":
        strategies = sorted(k for k, (_, runnable) in reg.items() if runnable)
    else:
        if args.strategy not in reg:
            print(f"策略 {args.strategy} 未注册 (可用: {', '.join(sorted(reg))}; 详情 --list)")
            return
        if not reg[args.strategy][1]:
            print(f"策略 {args.strategy} 暂无回测路径 (未实现 backtest_stock 且非盘中窗口策略)")
            return
        strategies = [args.strategy]

    from app.market_cn.auto.backtest import run_all
    codes = [c.strip() for c in args.codes.split(",") if c.strip()] or None
    probe = None
    if args.probe:
        from app.market_cn.auto.probe import Probe
        probe = Probe(strategies[0] if len(strategies) == 1 else "multi", tag=args.probe)

    t0 = time.time()
    for st in strategies:
        res = run_all(strategy=st, days=args.days, codes=codes,
                      start_date=args.start_date or None,
                      end_date=args.end_date or None, probe=probe)
        _print_report(st, res, args.top, strat_inst=reg[st][0])
        if args.out:
            out = args.out if len(strategies) == 1 else args.out.replace(
                ".json", f"_{st}.json")
            os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
            with open(out, "w", encoding="utf-8") as f:
                json.dump(res["trades"], f, ensure_ascii=False)
            print(f"逐笔明细已写出: {out} ({len(res['trades'])}笔)")
    if probe is not None:
        probe.close()
    print(f"\n总耗时 {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
