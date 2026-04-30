"""
Walk-Forward 批量验证脚本
基于已有 _summary.json 中的最优参数，对 Top N 股票跑 WF 验证

用法（在项目根目录运行）:
    # 对 Top 50 股票跑 WF 验证
    python -m optimizer.wf_validate --summary optimizer/optimizer_output/_summary.json --top 50

    # 对全部股票跑（并行 20 进程）
    python -m optimizer.wf_validate --summary optimizer/optimizer_output/_summary.json --all -j 20

    # 自定义 WF 参数
    python -m optimizer.wf_validate --summary optimizer/optimizer_output/_summary.json --top 100 --splits 5 --train-ratio 0.7

    # 只跑特定模板
    python -m optimizer.wf_validate --summary optimizer/optimizer_output/_summary.json --top 50 -t rsi_volume_divergence

输出:
    wf_results.json — 每只股票的 WF 验证详情
    wf_summary.txt  — 可读的汇总报告
"""
import argparse
import json
import multiprocessing
import os
import sys
import time
from datetime import datetime
from typing import Dict, Any, List, Optional

# 路径设置
_optimizer_dir = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.dirname(_optimizer_dir)
_backend_root = os.path.join(_project_root, "backend_api_python")
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# Monkey-patch 本地仓库
from optimizer.runner import _patch_datasource_warehouse
_patch_datasource_warehouse()

from optimizer.walk_forward import WalkForwardValidator
from optimizer.runner import BacktestObjective, get_template_unified, ALL_TEMPLATES, _is_ashare_market


def parse_market_symbol(raw: str):
    """解析 'CNStock:000001.SZ' 或 '000001.SZ' 格式"""
    if ":" in raw:
        parts = raw.split(":", 1)
        return parts[0], parts[1]
    return "CNStock", raw


def run_wf_for_stock(
    symbol_raw: str,
    template_key: str,
    best_params: dict,
    timeframe: str,
    start_str: str,
    end_str: str,
    score_fn: str,
    n_splits: int,
    train_ratio: float,
) -> Dict[str, Any]:
    """对单只股票跑 Walk-Forward 验证"""
    market, symbol = parse_market_symbol(symbol_raw)
    start_date = datetime.strptime(start_str, "%Y-%m-%d")
    end_date = datetime.strptime(end_str, "%Y-%m-%d").replace(hour=23, minute=59, second=59)

    try:
        # 构建回测目标函数
        objective = BacktestObjective(
            template_key=template_key,
            symbol=symbol,
            market=market,
            timeframe=timeframe,
            start_date=start_date,
            end_date=end_date,
        )

        # Walk-Forward 验证
        validator = WalkForwardValidator(
            n_splits=n_splits,
            train_ratio=train_ratio,
        )
        result = validator.validate(
            objective_fn=objective,
            best_params=best_params,
            start_date=start_date,
            end_date=end_date,
            score_fn=score_fn,
        )

        return {
            "symbol": symbol_raw,
            "template": template_key,
            "wf_result": result,
            "status": "ok",
        }

    except Exception as e:
        return {
            "symbol": symbol_raw,
            "template": template_key,
            "wf_result": None,
            "status": f"error: {str(e)}",
        }


def _worker_run(args_tuple):
    """多进程 worker"""
    (symbol_raw, template_key, best_params,
     timeframe, start_str, end_str, score_fn,
     n_splits, train_ratio) = args_tuple

    return run_wf_for_stock(
        symbol_raw=symbol_raw,
        template_key=template_key,
        best_params=best_params,
        timeframe=timeframe,
        start_str=start_str,
        end_str=end_str,
        score_fn=score_fn,
        n_splits=n_splits,
        train_ratio=train_ratio,
    )


def main():
    parser = argparse.ArgumentParser(description="Walk-Forward 批量验证")
    parser.add_argument("--summary", "-s", required=True, help="_summary.json 路径")
    parser.add_argument("--top", type=int, default=50, help="取 Top N 只股票（默认 50）")
    parser.add_argument("--all", action="store_true", help="验证全部股票")
    parser.add_argument("--template", "-t", type=str, default=None, help="只验证特定模板")
    parser.add_argument("--timeframe", "-tf", type=str, default=None, help="时间框架（默认从 summary 读取）")
    parser.add_argument("--start", type=str, default=None, help="开始日期（默认从 summary 读取）")
    parser.add_argument("--end", type=str, default=None, help="结束日期（默认从 summary 读取）")
    parser.add_argument("--score", type=str, default="composite",
                        choices=["sharpe", "return_dd_ratio", "composite"])
    parser.add_argument("--splits", type=int, default=5, help="WF 分割数（默认 5）")
    parser.add_argument("--train-ratio", type=float, default=0.7, help="训练集比例（默认 0.7）")
    parser.add_argument("--jobs", "-j", type=int, default=1, help="并行进程数")
    parser.add_argument("--output", "-o", type=str, default=None, help="输出目录（默认同 summary 目录）")
    parser.add_argument("--params-file", type=str, default=None,
                        help="自定义参数文件（JSON，格式: {symbol: {template, params}}）")

    args = parser.parse_args()

    # 加载 summary
    with open(args.summary, "r", encoding="utf-8") as f:
        summary = json.load(f)

    timeframe = args.timeframe or summary.get("timeframe", "1D")
    period = summary.get("period", "2024-01-01 ~ 2025-12-31")
    start_str = args.start or period.split("~")[0].strip()
    end_str = args.end or period.split("~")[1].strip()
    templates_in_summary = summary.get("templates", [])

    # 加载自定义参数文件（如果有）
    custom_params = {}
    if args.params_file and os.path.isfile(args.params_file):
        with open(args.params_file, "r", encoding="utf-8") as f:
            custom_params = json.load(f)
        print(f"  📄 加载自定义参数: {args.params_file} ({len(custom_params)} 条)")

    # 构建任务列表: 从 all_ranked 中取 top N
    all_ranked = summary.get("all_ranked", [])
    stock_best = summary.get("stock_best", {})

    # 筛选
    if args.template:
        ranked = [r for r in all_ranked if r["template"] == args.template]
    else:
        ranked = all_ranked

    # 过滤掉零交易的
    ranked = [r for r in ranked if r["metrics"].get("totalTrades", 0) > 0]

    if not args.all:
        ranked = ranked[:args.top]

    print(f"\n{'='*60}")
    print(f"  Walk-Forward 批量验证")
    print(f"  股票数: {len(ranked)}")
    print(f"  模板: {args.template or '全部'}")
    print(f"  时间框架: {timeframe}")
    print(f"  回测区间: {start_str} ~ {end_str}")
    print(f"  WF 分割: {args.splits} folds, 训练比例 {args.train_ratio}")
    print(f"  评分函数: {args.score}")
    print(f"  并行进程: {args.jobs}")
    print(f"{'='*60}")

    # 需要加载最优参数 — 从各股票的详细结果文件中读取
    # 如果没有详细文件，用 params-file 或空 dict
    output_dir = args.output or os.path.dirname(os.path.abspath(args.summary))

    # 构建任务
    tasks = []
    for r in ranked:
        symbol_raw = r["symbol"]
        template_key = r["template"]

        # 优先从 custom_params 取
        if symbol_raw in custom_params:
            best_params = custom_params[symbol_raw].get("params", custom_params[symbol_raw])
        elif template_key in custom_params.get(symbol_raw, {}):
            best_params = custom_params[symbol_raw][template_key]
        else:
            # 尝试从单独的回测结果文件读取
            market, symbol = parse_market_symbol(symbol_raw)
            tf_dir_map = {"1D": "daily", "1H": "hourly", "4H": "4h", "1W": "weekly",
                          "5m": "5min", "15m": "15min", "30m": "30min", "1m": "1min"}
            tf_dir = tf_dir_map.get(timeframe, timeframe.lower())
            detail_path = os.path.join(
                output_dir, market, tf_dir,
                f"{symbol.replace('/', '_').replace(':', '_')}_{template_key}.json"
            )
            if os.path.isfile(detail_path):
                with open(detail_path, "r", encoding="utf-8") as f:
                    detail = json.load(f)
                best_params = detail.get("best", {}).get("params", {})
            else:
                # 没有参数文件，跳过
                print(f"  ⚠️ {symbol_raw} 无最优参数文件，跳过 (需要 --params-file 或详细结果)")
                continue

        if not best_params:
            print(f"  ⚠️ {symbol_raw} 参数为空，跳过")
            continue

        tasks.append((
            symbol_raw, template_key, best_params,
            timeframe, start_str, end_str, args.score,
            args.splits, args.train_ratio,
        ))

    if not tasks:
        print(f"\n  ❌ 没有可用任务。请提供 --params-file 或确保详细结果文件存在。")
        print(f"     也可以用 runner.py 重新跑并开启 WF 验证（去掉 --no-validate）")
        sys.exit(1)

    print(f"\n  📋 有效任务数: {len(tasks)}")

    # 执行
    t0 = time.time()
    results = []

    if args.jobs <= 1:
        for i, task in enumerate(tasks):
            symbol_raw = task[0]
            print(f"  [{i+1}/{len(tasks)}] {symbol_raw} ...", end=" ", flush=True)
            result = _worker_run(task)
            results.append(result)
            if result["status"] == "ok":
                wf = result["wf_result"]
                print(f"✅ test_score={wf['avg_test_score']:.3f} overfit={wf['overfitting_ratio']:.3f} {wf['verdict']}")
            else:
                print(f"❌ {result['status']}")
    else:
        os.environ["PYTHONUNBUFFERED"] = "1"
        with multiprocessing.Pool(processes=args.jobs) as pool:
            results = pool.map(_worker_run, tasks)

    elapsed = time.time() - t0

    # ── 汇总 ──
    ok_results = [r for r in results if r["status"] == "ok"]
    err_results = [r for r in results if r["status"] != "ok"]

    print(f"\n{'='*60}")
    print(f"  📊 Walk-Forward 验证结果汇总")
    print(f"  总耗时: {elapsed:.1f}s")
    print(f"  成功: {len(ok_results)}, 失败: {len(err_results)}")
    print(f"{'='*60}")

    if not ok_results:
        print(f"\n  ❌ 没有成功的结果")
        # 保存结果
        _save_results(output_dir, results, args)
        return

    # 按 WF 测试得分排序
    ok_results.sort(key=lambda r: r["wf_result"]["avg_test_score"], reverse=True)

    # 统计
    test_scores = [r["wf_result"]["avg_test_score"] for r in ok_results]
    overfit_ratios = [r["wf_result"]["overfitting_ratio"] for r in ok_results]
    consistencies = [r["wf_result"]["consistency"] for r in ok_results]
    verdicts = [r["wf_result"]["verdict"] for r in ok_results]

    import statistics as st

    print(f"\n  测试集得分: 均值={st.mean(test_scores):.3f}, 中位={st.median(test_scores):.3f}")
    print(f"  过拟合比率: 均值={st.mean(overfit_ratios):.3f}, 中位={st.median(overfit_ratios):.3f}")
    print(f"  一致性:     均值={st.mean(consistencies):.3f}, 中位={st.median(consistencies):.3f}")

    # 判定分布
    from collections import Counter
    verdict_counts = Counter(verdicts)
    print(f"\n  判定分布:")
    for v, cnt in verdict_counts.most_common():
        pct = cnt / len(ok_results) * 100
        print(f"    {v}: {cnt} ({pct:.1f}%)")

    # 通过率
    passed = sum(1 for r in ok_results
                 if r["wf_result"]["overfitting_ratio"] < 0.5
                 and r["wf_result"]["avg_test_score"] > 0)
    print(f"\n  ✅ 通过 (overfit<0.5 & test>0): {passed}/{len(ok_results)} ({passed/len(ok_results)*100:.1f}%)")

    strict_passed = sum(1 for r in ok_results
                        if r["wf_result"]["overfitting_ratio"] < 0.3
                        and r["wf_result"]["avg_test_score"] > 0
                        and r["wf_result"]["consistency"] > 0.6)
    print(f"  ✅ 严格通过 (overfit<0.3 & test>0 & consistency>0.6): {strict_passed}/{len(ok_results)} ({strict_passed/len(ok_results)*100:.1f}%)")

    # Top 20
    print(f"\n  Top 20 (按 WF 测试得分):")
    print(f"  {'Rank':<5} {'股票':<18} {'模板':<25} {'Train':<8} {'Test':<8} {'Overfit':<8} {'Consist':<8} {'判定'}")
    print(f"  {'-'*110}")
    for i, r in enumerate(ok_results[:20], 1):
        wf = r["wf_result"]
        print(f"  {i:<5} {r['symbol']:<18} {r['template']:<25} "
              f"{wf['avg_train_score']:<8.3f} {wf['avg_test_score']:<8.3f} "
              f"{wf['overfitting_ratio']:<8.3f} {wf['consistency']:<8.3f} "
              f"{wf['verdict']}")

    # Bottom 10
    if len(ok_results) > 20:
        print(f"\n  Bottom 10:")
        print(f"  {'Rank':<5} {'股票':<18} {'模板':<25} {'Train':<8} {'Test':<8} {'Overfit':<8} {'Consist':<8} {'判定'}")
        print(f"  {'-'*110}")
        for i, r in enumerate(ok_results[-10:], len(ok_results) - 9):
            wf = r["wf_result"]
            print(f"  {i:<5} {r['symbol']:<18} {r['template']:<25} "
                  f"{wf['avg_train_score']:<8.3f} {wf['avg_test_score']:<8.3f} "
                  f"{wf['overfitting_ratio']:<8.3f} {wf['consistency']:<8.3f} "
                  f"{wf['verdict']}")

    # 保存结果
    _save_results(output_dir, results, args)

    # 保存通过的股票列表
    passed_list = [r for r in ok_results
                   if r["wf_result"]["overfitting_ratio"] < 0.5
                   and r["wf_result"]["avg_test_score"] > 0]
    if passed_list:
        passed_path = os.path.join(output_dir, "wf_passed_stocks.json")
        with open(passed_path, "w", encoding="utf-8") as f:
            json.dump([{
                "symbol": r["symbol"],
                "template": r["template"],
                "wf_test_score": r["wf_result"]["avg_test_score"],
                "overfitting_ratio": r["wf_result"]["overfitting_ratio"],
                "consistency": r["wf_result"]["consistency"],
            } for r in passed_list], f, ensure_ascii=False, indent=2)
        print(f"\n  📄 通过列表: {passed_path}")


def _save_results(output_dir, results, args):
    """保存完整结果"""
    # JSON
    wf_json_path = os.path.join(output_dir, "wf_results.json")
    with open(wf_json_path, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": datetime.now().isoformat(),
            "config": {
                "splits": args.splits,
                "train_ratio": args.train_ratio,
                "score_fn": args.score,
                "top": args.top,
                "template_filter": args.template,
            },
            "total": len(results),
            "results": results,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n  📄 完整结果: {wf_json_path}")

    # 可读报告
    report_path = os.path.join(output_dir, "wf_summary.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("Walk-Forward 验证报告\n")
        f.write(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"配置: {args.splits} folds, train_ratio={args.train_ratio}, score={args.score}\n\n")

        ok = [r for r in results if r["status"] == "ok"]
        err = [r for r in results if r["status"] != "ok"]
        f.write(f"成功: {len(ok)}, 失败: {len(err)}\n\n")

        if ok:
            ok.sort(key=lambda r: r["wf_result"]["avg_test_score"], reverse=True)
            f.write(f"{'Rank':<5} {'股票':<18} {'模板':<25} {'Train':<8} {'Test':<8} {'Overfit':<8} {'Consist':<8} {'判定'}\n")
            f.write("-" * 110 + "\n")
            for i, r in enumerate(ok, 1):
                wf = r["wf_result"]
                f.write(f"{i:<5} {r['symbol']:<18} {r['template']:<25} "
                        f"{wf['avg_train_score']:<8.3f} {wf['avg_test_score']:<8.3f} "
                        f"{wf['overfitting_ratio']:<8.3f} {wf['consistency']:<8.3f} "
                        f"{wf['verdict']}\n")

        if err:
            f.write(f"\n失败列表:\n")
            for r in err:
                f.write(f"  {r['symbol']} / {r['template']}: {r['status']}\n")

    print(f"  📄 可读报告: {report_path}")


if __name__ == "__main__":
    main()
