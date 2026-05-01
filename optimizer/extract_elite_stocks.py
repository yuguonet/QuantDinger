"""
从 optimizer_output 逐股 JSON 中提取双优股票清单。
条件：best_score > 3 且 wf_test_score > 0

用法（在项目根目录执行）：
    python -m optimizer.extract_elite_stocks

输出：optimizer/elite_strategies_adaptive_vol.json
"""

import json
import os
import glob


def extract_elite(output_dir=None, score_threshold=3.0, wf_threshold=0.0):
    if output_dir is None:
        output_dir = os.path.join(os.path.dirname(__file__), "optimizer_output")

    # 扫描所有逐股 JSON
    pattern = os.path.join(output_dir, "**", "*.json")
    files = glob.glob(pattern, recursive=True)
    # 排除 _summary.json
    files = [f for f in files if not os.path.basename(f).startswith("_")]

    # 如果文件太多，优先筛选目标模板的文件（提速）
    target_files = [f for f in files if "adaptive_volatility" in os.path.basename(f)]
    if target_files:
        print(f"共 {len(files)} 个文件，其中 adaptive_volatility 相关 {len(target_files)} 个，优先扫描")
        files = target_files
    else:
        print(f"扫描到 {len(files)} 个结果文件")

    elite = {}
    all_count = 0

    for i, fpath in enumerate(files):
        if (i + 1) % 5000 == 0:
            print(f"  已扫描 {i+1}/{len(files)} ...")
        with open(fpath, "r", encoding="utf-8") as f:
            data = json.load(f)

        # 跳过非目标格式（列表、缺少 best 字段等）
        if not isinstance(data, dict):
            continue
        best = data.get("best")
        if not best or not isinstance(best, dict):
            continue

        all_count += 1
        score = best.get("score", -999)
        if not isinstance(score, (int, float)):
            continue
        validation = data.get("validation") or {}
        wf_score = validation.get("avg_test_score", -999)
        if not isinstance(wf_score, (int, float)):
            wf_score = -999

        if score > score_threshold and wf_score > wf_threshold:
            sym = data.get("symbol", os.path.basename(fpath).split("_")[0])
            # 提取纯股票代码（去掉 CNStock: 前缀）
            if ":" in sym:
                sym = sym.split(":")[-1]

            elite[sym] = {
                "template": data.get("template"),
                "template_name": data.get("template_name"),
                "best_score": round(score, 4),
                "best_params": best.get("params", {}),
                "metrics": best.get("metrics", {}),
                "wf_test_score": round(wf_score, 4),
                "wf_validation": {
                    "avg_train_score": validation.get("avg_train_score"),
                    "overfitting_ratio": validation.get("overfitting_ratio"),
                    "consistency": validation.get("consistency"),
                    "verdict": validation.get("verdict"),
                },
                "source_file": os.path.relpath(fpath, output_dir),
            }

    # 按 wf_test_score 降序排列
    elite_sorted = dict(
        sorted(elite.items(), key=lambda x: x[1]["wf_test_score"], reverse=True)
    )

    result = {
        "description": "双优股票清单：回测得分>3 且 WF验证得分>0",
        "criteria": {
            "min_best_score": score_threshold,
            "min_wf_score": wf_threshold,
        },
        "extracted_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total_scanned": all_count,
        "elite_count": len(elite_sorted),
        "stocks": elite_sorted,
    }

    # 保存
    out_path = os.path.join(os.path.dirname(__file__), "elite_strategies_adaptive_vol.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"扫描总数: {all_count}")
    print(f"双优股票: {len(elite_sorted)}")
    print(f"已保存:   {out_path}")

    # 打印 Top 10
    print(f"\n{'='*80}")
    print(f"  Top 10 双优股票（按 WF 得分排序）")
    print(f"{'='*80}")
    print(f"{'排名':>4s}  {'股票':>12s}  {'得分':>6s}  {'WF':>8s}  {'Sharpe':>7s}  {'收益':>8s}  {'胜率':>6s}")
    for i, (sym, v) in enumerate(elite_sorted.items()):
        if i >= 10:
            break
        m = v["metrics"]
        print(
            f"{i+1:>4d}  {sym:>12s}  {v['best_score']:>6.2f}  "
            f"{v['wf_test_score']:>+8.3f}  {m.get('sharpeRatio',0):>+7.2f}  "
            f"{m.get('totalReturn',0):>+7.1f}%  {m.get('winRate',0):>5.1f}%"
        )

    return result


if __name__ == "__main__":
    extract_elite()
