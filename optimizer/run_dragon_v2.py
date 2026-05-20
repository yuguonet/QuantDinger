#!/usr/bin/env python3
"""
龙回头 V2 优化版批量测试
基于 WF 验证结果，用 WF>0 的股票跑 v2 模板，多时间框架对比

用法:
  python optimizer/run_dragon_v2.py                    # 1D 基线
  python optimizer/run_dragon_v2.py --tf 1H            # 1 小时
  python optimizer/run_dragon_v2.py --tf 30m           # 30 分钟
  python optimizer/run_dragon_v2.py --all-tf           # 全部时间框架
  python optimizer/run_dragon_v2.py --trials 200       # 增加试验次数
  python optimizer/run_dragon_v2.py -j 4               # 4 进程并行
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

_OPTIMIZER_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_OPTIMIZER_DIR)
_SUMMARY_PATH = os.path.join(_OPTIMIZER_DIR, "optimizer_output", "_summary_dragon_v1.json")

# WF>0 的股票（从 v1 结果中提取）
_WF_POSITIVE_STOCKS = None


def get_wf_positive_stocks(summary_path: str = None) -> list:
    """从 v1 结果中提取 WF>0 的股票列表"""
    global _WF_POSITIVE_STOCKS
    if _WF_POSITIVE_STOCKS is not None:
        return _WF_POSITIVE_STOCKS

    path = summary_path or _SUMMARY_PATH
    if not os.path.isfile(path):
        # 尝试当前目录
        alt = os.path.join(os.getcwd(), "_summary_dragon.json")
        if os.path.isfile(alt):
            path = alt
        else:
            print(f"❌ 未找到 v1 结果文件: {path}")
            print(f"   请将 _summary.json 放到 {os.path.dirname(path)}/ 或当前目录")
            sys.exit(1)

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    ranked = data.get("all_ranked", [])
    wf_positive = []
    for r in ranked:
        wf = r.get("wf_test_score")
        if wf is not None and wf > 0:
            # 去掉 CNStock: 前缀
            sym = r["symbol"].replace("CNStock:", "").replace("Crypto:", "")
            wf_positive.append(sym)

    # 也接受 stock_best 的格式
    if not wf_positive:
        stock_best = data.get("stock_best", {})
        for sym, info in stock_best.items():
            wf = info.get("wf_test_score")
            if wf is not None and wf > 0:
                wf_positive.append(sym)

    _WF_POSITIVE_STOCKS = sorted(set(wf_positive))
    print(f"  📋 从 v1 结果中提取 WF>0 股票: {len(_WF_POSITIVE_STOCKS)} 只")
    return _WF_POSITIVE_STOCKS


def run_optimization(
    template: str = "dragon_pullback",
    symbols: list = None,
    timeframe: str = "1D",
    start: str = "2021-01-01",
    end: str = "2025-12-31",
    trials: int = 100,
    jobs: int = 1,
    output_dir: str = None,
    resume: bool = False,
    score: str = "composite",
    validate: bool = True,
):
    """运行单个时间框架的优化"""
    if output_dir is None:
        output_dir = os.path.join(_OPTIMIZER_DIR, "optimizer_output")

    if symbols is None:
        symbols = get_wf_positive_stocks()

    if not symbols:
        print("❌ 没有可用的股票")
        return None

    # 构建 runner 命令
    cmd = [
        sys.executable, "-m", "optimizer.runner",
        "-t", template,
        "-s", ",".join(symbols),
        "-tf", timeframe,
        "--start", start,
        "--end", end,
        "-n", str(trials),
        "--score", score,
        "-j", str(jobs),
        "-o", output_dir,
    ]
    if not validate:
        cmd.append("--no-validate")
    if resume:
        cmd.append("--resume")

    print(f"\n{'='*60}")
    print(f"  🚀 龙回头 V2 优化 — {timeframe}")
    print(f"  模板: {template}")
    print(f"  股票: {len(symbols)} 只")
    print(f"  时间框架: {timeframe}")
    print(f"  试验次数: {trials}")
    print(f"  并行进程: {jobs}")
    print(f"  评分函数: {score}")
    print(f"{'='*60}\n")

    t0 = datetime.now()
    result = subprocess.run(cmd, cwd=_PROJECT_ROOT)
    elapsed = (datetime.now() - t0).total_seconds()

    if result.returncode == 0:
        print(f"\n  ✅ {timeframe} 完成 ({elapsed:.0f}s)")
    else:
        print(f"\n  ❌ {timeframe} 失败 (returncode={result.returncode})")

    return result.returncode


def run_all_timeframes(args):
    """运行多个时间框架"""
    timeframes = []
    if args.all_tf:
        timeframes = ["1D", "1H", "30m", "15m"]
    else:
        timeframes = [args.tf]

    symbols = get_wf_positive_stocks(args.summary)
    if args.max_stocks and len(symbols) > args.max_stocks:
        import random
        if args.seed:
            random.seed(args.seed)
        symbols = random.sample(symbols, args.max_stocks)
        print(f"  🎲 随机抽样 {args.max_stocks} 只 (seed={args.seed})")

    results = {}
    for tf in timeframes:
        output_dir = os.path.join(_OPTIMIZER_DIR, "optimizer_output", f"dragon_v2_{tf}")
        rc = run_optimization(
            template=args.template,
            symbols=symbols,
            timeframe=tf,
            start=args.start,
            end=args.end,
            trials=args.trials,
            jobs=args.jobs,
            output_dir=output_dir,
            resume=args.resume,
            score=args.score,
            validate=not args.no_validate,
        )
        results[tf] = rc

    # 汇总
    print(f"\n{'='*60}")
    print(f"  📊 全部时间框架测试完成")
    print(f"{'='*60}")
    for tf, rc in results.items():
        status = "✅" if rc == 0 else "❌"
        print(f"  {status} {tf}")


def main():
    parser = argparse.ArgumentParser(description="龙回头 V2 优化版批量测试")
    parser.add_argument("--template", "-t", default="dragon_pullback",
                        help="策略模板 (默认 dragon_pullback)")
    parser.add_argument("--tf", default="1D", help="时间框架 (默认 1D)")
    parser.add_argument("--all-tf", action="store_true", help="测试全部时间框架 (1D/1H/30m/15m)")
    parser.add_argument("--start", default="2021-01-01", help="回测开始日期")
    parser.add_argument("--end", default="2025-12-31", help="回测结束日期")
    parser.add_argument("--trials", "-n", type=int, default=100, help="试验次数")
    parser.add_argument("--jobs", "-j", type=int, default=1, help="并行进程数")
    parser.add_argument("--resume", action="store_true", help="断点续跑")
    parser.add_argument("--score", default="composite", choices=["sharpe", "return_dd_ratio", "composite"])
    parser.add_argument("--no-validate", action="store_true", help="跳过 WF 验证")
    parser.add_argument("--summary", default=None, help="v1 结果文件路径")
    parser.add_argument("--max-stocks", type=int, default=None, help="最大股票数（随机抽样）")
    parser.add_argument("--seed", type=int, default=None, help="随机种子")
    parser.add_argument("--list-stocks", action="store_true", help="仅列出 WF>0 股票")

    args = parser.parse_args()

    if args.list_stocks:
        stocks = get_wf_positive_stocks(args.summary)
        print(f"\nWF>0 股票 ({len(stocks)} 只):")
        for s in stocks:
            print(f"  {s}")
        return

    run_all_timeframes(args)


if __name__ == "__main__":
    main()
