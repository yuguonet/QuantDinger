"""
回测结果分析器
读取 optimizer_output/ 下所有 JSON，汇总成策略表现矩阵。

用法:
    python analyze_results.py [optimizer_output_dir]

输出:
    1. 模板×股票 表现矩阵（终端）
    2. CSV 文件: _analysis_matrix.csv
    3. 模式摘要: _patterns.txt（供 LLM 使用）
"""
import json
import os
import sys
import csv
from collections import defaultdict
from typing import Dict, List, Any


def load_results(output_dir: str) -> List[Dict[str, Any]]:
    """加载所有回测结果 JSON"""
    results = []
    for fname in os.listdir(output_dir):
        if not fname.endswith(".json") or fname.startswith("_"):
            continue
        filepath = os.path.join(output_dir, fname)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "template" in data and "best" in data:
                results.append(data)
        except Exception as e:
            print(f"  ⚠️ 跳过 {fname}: {e}")
    return results


def build_matrix(results: List[Dict]) -> Dict[str, Dict[str, Dict]]:
    """
    构建 模板→股票→指标 的矩阵
    matrix[template][symbol] = {sharpe, totalReturn, winRate, maxDrawdown, ...}
    """
    matrix = defaultdict(dict)
    for r in results:
        tpl = r["template"]
        sym = r.get("symbol", "unknown")
        best = r.get("best", {})
        metrics = best.get("metrics", {})
        matrix[tpl][sym] = {
            "sharpe": metrics.get("sharpeRatio", 0),
            "total_return": metrics.get("totalReturn", 0),
            "win_rate": metrics.get("winRate", 0),
            "max_drawdown": metrics.get("maxDrawdown", 0),
            "profit_factor": metrics.get("profitFactor", 0),
            "total_trades": metrics.get("totalTrades", 0),
            "score": best.get("score", 0),
            "params": best.get("params", {}),
        }
    return matrix


def print_summary(matrix: Dict[str, Dict[str, Dict]]):
    """打印模板级别的汇总统计"""
    print(f"\n{'='*80}")
    print(f"  模板表现汇总（跨所有股票）")
    print(f"{'='*80}")

    tpl_stats = []
    for tpl, stocks in matrix.items():
        sharpes = [v["sharpe"] for v in stocks.values()]
        returns = [v["total_return"] for v in stocks.values()]
        drawdowns = [v["max_drawdown"] for v in stocks.values()]
        win_rates = [v["win_rate"] for v in stocks.values()]

        n = len(sharpes)
        avg_sharpe = sum(sharpes) / n if n else 0
        avg_return = sum(returns) / n if n else 0
        avg_dd = sum(drawdowns) / n if n else 0
        avg_wr = sum(win_rates) / n if n else 0
        positive = sum(1 for s in sharpes if s > 0)

        tpl_stats.append({
            "template": tpl,
            "n_stocks": n,
            "avg_sharpe": avg_sharpe,
            "avg_return": avg_return,
            "avg_drawdown": avg_dd,
            "avg_win_rate": avg_wr,
            "positive_ratio": positive / n if n else 0,
        })

    # 按平均 Sharpe 排序
    tpl_stats.sort(key=lambda x: x["avg_sharpe"], reverse=True)

    print(f"\n  {'模板':<25} {'股票数':<8} {'Avg Sharpe':<12} {'Avg Return%':<13} {'Avg DD%':<10} {'Avg WR%':<10} {'正Sharpe%':<10}")
    print(f"  {'-'*98}")
    for s in tpl_stats:
        print(f"  {s['template']:<25} {s['n_stocks']:<8} "
              f"{s['avg_sharpe']:<12.2f} {s['avg_return']:<13.1f} "
              f"{s['avg_drawdown']:<10.1f} {s['avg_win_rate']:<10.1f} "
              f"{s['positive_ratio']*100:<10.0f}")

    return tpl_stats


def print_top_combos(matrix: Dict[str, Dict[str, Dict]], top_n: int = 30):
    """打印最佳 模板×股票 组合"""
    combos = []
    for tpl, stocks in matrix.items():
        for sym, m in stocks.items():
            combos.append({"template": tpl, "symbol": sym, **m})

    combos.sort(key=lambda x: x["sharpe"], reverse=True)

    print(f"\n{'='*80}")
    print(f"  Top {top_n} 最佳组合（按 Sharpe 排序）")
    print(f"{'='*80}")

    print(f"\n  {'#':<4} {'模板':<25} {'股票':<15} {'Sharpe':<10} {'Return%':<10} {'DD%':<10} {'WR%':<10} {'交易数':<8}")
    print(f"  {'-'*92}")
    for i, c in enumerate(combos[:top_n], 1):
        print(f"  {i:<4} {c['template']:<25} {c['symbol']:<15} "
              f"{c['sharpe']:<10.2f} {c['total_return']:<10.1f} "
              f"{c['max_drawdown']:<10.1f} {c['win_rate']:<10.1f} "
              f"{c['total_trades']:<8}")

    return combos


def print_worst_combos(matrix: Dict[str, Dict[str, Dict]], bottom_n: int = 10):
    """打印最差组合"""
    combos = []
    for tpl, stocks in matrix.items():
        for sym, m in stocks.items():
            combos.append({"template": tpl, "symbol": sym, **m})

    combos.sort(key=lambda x: x["sharpe"])

    print(f"\n{'='*80}")
    print(f"  Bottom {bottom_n} 最差组合（需淘汰或改进）")
    print(f"{'='*80}")

    print(f"\n  {'#':<4} {'模板':<25} {'股票':<15} {'Sharpe':<10} {'Return%':<10} {'DD%':<10}")
    print(f"  {'-'*74}")
    for i, c in enumerate(combos[:bottom_n], 1):
        print(f"  {i:<4} {c['template']:<25} {c['symbol']:<15} "
              f"{c['sharpe']:<10.2f} {c['total_return']:<10.1f} "
              f"{c['max_drawdown']:<10.1f}")


def generate_patterns(matrix: Dict[str, Dict[str, Dict]], tpl_stats: List[Dict]) -> str:
    """生成供 LLM 使用的模式摘要文本"""
    lines = []
    lines.append("# 策略回测表现摘要（中证1000 日线）")
    lines.append("")

    # 模板排名
    lines.append("## 模板整体排名（按平均 Sharpe）")
    for i, s in enumerate(tpl_stats, 1):
        lines.append(f"{i}. {s['template']}: Avg Sharpe={s['avg_sharpe']:.2f}, "
                     f"Avg Return={s['avg_return']:.1f}%, "
                     f"正Sharpe比例={s['positive_ratio']*100:.0f}%")
    lines.append("")

    # 找出最佳模板在哪些股票上表现好
    lines.append("## 最佳模板的股票偏好")
    best_tpl = tpl_stats[0]["template"] if tpl_stats else "N/A"
    if best_tpl in matrix:
        stocks = matrix[best_tpl]
        sorted_stocks = sorted(stocks.items(), key=lambda x: x[1]["sharpe"], reverse=True)
        top5 = sorted_stocks[:5]
        bottom5 = sorted_stocks[-5:]
        lines.append(f"最佳模板 '{best_tpl}' 表现最好的股票:")
        for sym, m in top5:
            lines.append(f"  - {sym}: Sharpe={m['sharpe']:.2f}, Return={m['total_return']:.1f}%")
        lines.append(f"表现最差的股票:")
        for sym, m in bottom5:
            lines.append(f"  - {sym}: Sharpe={m['sharpe']:.2f}, Return={m['total_return']:.1f}%")
    lines.append("")

    # 按股票维度看哪个模板最好
    lines.append("## 每只股票的最佳模板")
    all_symbols = set()
    for stocks in matrix.values():
        all_symbols.update(stocks.keys())

    symbol_best = {}
    for sym in sorted(all_symbols):
        best_for_sym = None
        best_sharpe = -999
        for tpl, stocks in matrix.items():
            if sym in stocks and stocks[sym]["sharpe"] > best_sharpe:
                best_sharpe = stocks[sym]["sharpe"]
                best_for_sym = tpl
        if best_for_sym:
            symbol_best[sym] = (best_for_sym, best_sharpe)
            lines.append(f"  {sym}: {best_for_sym} (Sharpe={best_sharpe:.2f})")

    lines.append("")

    # 统计各模板赢了多少只股票
    lines.append("## 模板获胜次数（在多少只股票上 Sharpe 最高）")
    win_count = defaultdict(int)
    for sym, (tpl, _) in symbol_best.items():
        win_count[tpl] += 1
    for tpl, count in sorted(win_count.items(), key=lambda x: x[1], reverse=True):
        lines.append(f"  {tpl}: {count} 只股票")

    return "\n".join(lines)


def save_csv(matrix: Dict[str, Dict[str, Dict]], output_path: str):
    """保存矩阵为 CSV"""
    rows = []
    for tpl, stocks in matrix.items():
        for sym, m in stocks.items():
            rows.append({
                "template": tpl,
                "symbol": sym,
                "sharpe": m["sharpe"],
                "total_return": m["total_return"],
                "win_rate": m["win_rate"],
                "max_drawdown": m["max_drawdown"],
                "profit_factor": m["profit_factor"],
                "total_trades": m["total_trades"],
                "score": m["score"],
            })

    rows.sort(key=lambda x: x["sharpe"], reverse=True)

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"\n  📊 矩阵已保存: {output_path}")


def main():
    output_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "optimizer_output"
    )

    if not os.path.isdir(output_dir):
        print(f"❌ 目录不存在: {output_dir}")
        sys.exit(1)

    print(f"\n  📂 加载结果: {output_dir}")
    results = load_results(output_dir)
    print(f"  ✅ 加载 {len(results)} 个结果文件")

    if not results:
        print("  ❌ 没有找到有效的回测结果")
        sys.exit(1)

    matrix = build_matrix(results)

    # 统计
    all_symbols = set()
    for stocks in matrix.values():
        all_symbols.update(stocks.keys())
    print(f"  📈 模板数: {len(matrix)}")
    print(f"  📈 股票数: {len(all_symbols)}")
    print(f"  📈 总组合数: {sum(len(s) for s in matrix.values())}")

    # 分析
    tpl_stats = print_summary(matrix)
    print_top_combos(matrix)
    print_worst_combos(matrix)

    # 保存 CSV
    csv_path = os.path.join(output_dir, "_analysis_matrix.csv")
    save_csv(matrix, csv_path)

    # 生成模式摘要
    patterns = generate_patterns(matrix, tpl_stats)
    patterns_path = os.path.join(output_dir, "_patterns.txt")
    with open(patterns_path, "w", encoding="utf-8") as f:
        f.write(patterns)
    print(f"  📝 模式摘要已保存: {patterns_path}")

    print(f"\n{'='*80}")
    print(f"  分析完成！")
    print(f"{'='*80}")


if __name__ == "__main__":
    main()
