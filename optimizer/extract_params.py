"""
从已有的回测结果文件中提取最优参数，生成 params-file 供 wf_validate.py 使用

用法（在项目根目录运行）:
    # 从 optimizer_output 目录提取所有最优参数
    python -m optimizer.extract_params --input optimizer/optimizer_output --output best_params.json

    # 只提取特定模板
    python -m optimizer.extract_params --input optimizer/optimizer_output -t rsi_volume_divergence

    # 只提取 Top 50（基于 _summary.json 排序）
    python -m optimizer.extract_params --input optimizer/optimizer_output --summary optimizer/optimizer_output/_summary.json --top 50
"""
import argparse
import json
import os
import sys


def extract_from_dir(input_dir: str, template_filter: str = None) -> dict:
    """遍历目录，从每个回测结果文件中提取最优参数"""
    params_map = {}
    count = 0

    for root, dirs, files in os.walk(input_dir):
        for fname in files:
            if not fname.endswith(".json") or fname.startswith("_"):
                continue
            if template_filter and f"_{template_filter}.json" not in fname:
                continue

            fpath = os.path.join(root, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    data = json.load(f)

                if "best" not in data or "params" not in data["best"]:
                    continue

                symbol = data.get("symbol", "")
                market = data.get("market", "CNStock")
                template = data.get("template", "")
                best_params = data["best"]["params"]
                score = data["best"].get("score", 0)

                # 统一格式: "CNStock:000001.SZ"
                if ":" not in symbol:
                    symbol_raw = f"{market}:{symbol}"
                else:
                    symbol_raw = symbol

                key = symbol_raw
                if key not in params_map or score > params_map[key].get("_score", 0):
                    params_map[key] = {
                        "template": template,
                        "params": best_params,
                        "score": score,
                        "_score": score,
                        "_source": fpath,
                    }
                    count += 1

            except (json.JSONDecodeError, KeyError, TypeError):
                continue

    return params_map


def main():
    parser = argparse.ArgumentParser(description="提取最优参数")
    parser.add_argument("--input", "-i", required=True, help="回测结果目录")
    parser.add_argument("--output", "-o", default="best_params.json", help="输出文件")
    parser.add_argument("--template", "-t", default=None, help="只提取特定模板")
    parser.add_argument("--summary", "-s", default=None, help="_summary.json 路径（用于排序）")
    parser.add_argument("--top", type=int, default=0, help="只保留 Top N")

    args = parser.parse_args()

    print(f"  📂 扫描目录: {args.input}")
    params_map = extract_from_dir(args.input, args.template)
    print(f"  📊 提取到 {len(params_map)} 只股票的最优参数")

    if not params_map:
        print(f"  ❌ 没有找到有效的回测结果文件")
        sys.exit(1)

    # 如果有 summary，按 summary 排序取 top N
    if args.summary and args.top > 0:
        with open(args.summary, "r", encoding="utf-8") as f:
            summary = json.load(f)
        ranked = summary.get("all_ranked", [])
        if args.template:
            ranked = [r for r in ranked if r["template"] == args.template]
        top_symbols = set(r["symbol"] for r in ranked[:args.top])
        params_map = {k: v for k, v in params_map.items() if k in top_symbols}
        print(f"  🔝 Top {args.top} 筛选后: {len(params_map)} 只")

    # 清理内部字段
    output = {}
    for k, v in params_map.items():
        output[k] = {
            "template": v["template"],
            "params": v["params"],
            "score": v["score"],
        }

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"  ✅ 保存到: {args.output}")

    # 模板分布
    from collections import Counter
    tpl_counts = Counter(v["template"] for v in output.values())
    print(f"\n  模板分布:")
    for tpl, cnt in tpl_counts.most_common():
        print(f"    {tpl}: {cnt}")


if __name__ == "__main__":
    main()
