"""
导出 WF 通过的策略参数为可直接用于实盘的配置文件

用法:
    python -m optimizer.export_passed_params \
        --passed optimizer/optimizer_output/wf_direct_passed.json \
        --output-dir optimizer/optimizer_output \
        --output optimizer/optimizer_output/passed_strategies.json

输出:
    passed_strategies.json — 包含每只股票的模板、最优参数、WF 验证指标
"""
import argparse
import json
import os
import sys
from datetime import datetime


def main():
    parser = argparse.ArgumentParser(description="导出 WF 通过策略的最优参数")
    parser.add_argument("--passed", required=True, help="wf_direct_passed.json 路径")
    parser.add_argument("--output-dir", required=True, help="optimizer_output 目录（含各股票的详细 JSON）")
    parser.add_argument("--output", default=None, help="输出文件路径（默认 output-dir/passed_strategies.json）")
    args = parser.parse_args()

    output = args.output or os.path.join(args.output_dir, "passed_strategies.json")

    # 1. 加载通过列表
    with open(args.passed, "r", encoding="utf-8") as f:
        passed = json.load(f)
    print(f"  📄 通过列表: {len(passed)} 条")

    # 2. 逐条提取参数
    results = []
    missing = 0
    for item in passed:
        symbol_raw = item["symbol"]
        template = item["template"]

        # 解析路径: CNStock:002174.SZ → CNStock/daily/002174.SZ_xxx.json
        if ":" in symbol_raw:
            market, symbol = symbol_raw.split(":", 1)
        else:
            market, symbol = "CNStock", symbol_raw

        detail_path = os.path.join(
            args.output_dir, market, "daily",
            f"{symbol.replace('/', '_')}_{template}.json"
        )

        best_params = {}
        backtest_score = None
        backtest_metrics = None

        if os.path.isfile(detail_path):
            try:
                with open(detail_path, "r", encoding="utf-8") as f:
                    detail = json.load(f)
                best = detail.get("best", {})
                best_params = best.get("params", {})
                backtest_score = best.get("score")
                backtest_metrics = best.get("metrics", {})
            except Exception as e:
                print(f"  ⚠️ {symbol_raw} 读取失败: {e}")
                missing += 1
                continue
        else:
            print(f"  ⚠️ {symbol_raw} 参数文件不存在: {detail_path}")
            missing += 1
            continue

        results.append({
            "symbol": symbol_raw,
            "market": market,
            "template": template,
            "params": best_params,
            "backtest": {
                "score": backtest_score,
                "metrics": backtest_metrics,
            },
            "wf": {
                "test_score": item["wf_test_score"],
                "overfitting_ratio": item["overfitting_ratio"],
                "consistency": item["consistency"],
            },
        })

    # 3. 按 WF test_score 降序排列
    results.sort(key=lambda r: -r["wf"]["test_score"])

    # 4. 输出
    export = {
        "generated_at": datetime.now().isoformat(),
        "total": len(results),
        "missing_params": missing,
        "criteria": {
            "overfitting_ratio_max": 0.3,
            "test_score_min": 0,
            "consistency_min": 0.5,
        },
        "strategies": results,
    }

    with open(output, "w", encoding="utf-8") as f:
        json.dump(export, f, ensure_ascii=False, indent=2)

    print(f"\n  ✅ 导出完成: {output}")
    print(f"  📊 共 {len(results)} 只股票（{missing} 只因缺少参数被跳过）")

    # 打印摘要
    from collections import Counter
    tmpl_cnt = Counter(r["template"] for r in results)
    print(f"\n  模板分布:")
    for t, c in tmpl_cnt.most_common():
        print(f"    {t}: {c}")

    # Top 10 预览
    print(f"\n  Top 10 预览:")
    print(f"  {'#':<3} {'股票':<18} {'模板':<28} {'WF_Test':>8} {'Overfit':>8} {'Consist':>8} {'Params'}")
    print(f"  {'-'*120}")
    for i, r in enumerate(results[:10], 1):
        params_str = ", ".join(f"{k}={v}" for k, v in list(r["params"].items())[:4]) + "..."
        print(f"  {i:<3} {r['symbol']:<18} {r['template']:<28} "
              f"{r['wf']['test_score']:>8.3f} {r['wf']['overfitting_ratio']:>8.3f} "
              f"{r['wf']['consistency']:>8.3f} {params_str}")


if __name__ == "__main__":
    main()
