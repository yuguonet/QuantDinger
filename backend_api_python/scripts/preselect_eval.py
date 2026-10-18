# -*- coding: utf-8 -*-
"""preselect_eval —— 工具预选质量批跑闭环（F23 / O-7 + D-16）。

定位：把两条离线数据源合成一份可判定报告——
  1. `pairs` JSONL（在线攒的「任务→实际调用工具」）→ held-out 双口径召回
     （`preselect_cooc.eval_heldout`，与周级脚本同一口径）；
  2. `stats` 快照 JSONL（O-7 落盘的观测计数器）→ 跨重启聚合
     `empty_rate` / `gate_miss_rate`（`preselect_stats.aggregate_snapshots`）。

用法：
    python scripts/preselect_eval.py [--pairs PATH | --db] [--stats PATH]
                                     [--holdout 20] [--k 8] [--min-pairs 0]

退出码：0=召回达标(pass95)，1=低于阈值，2=无评估样本（先攒数据）。
输出合并 JSON 到 stdout，便于 CI / 人工判读。

注意：held-out 召回是**代理指标**（面 = 词典/共现构造，不是 LLM 真实点名面），
用于校验数据与逻辑通不通；最终达标要以真实预选面跑批复核。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 让脚本可独立运行（backend_api_python 作为根），与 calibrate_scores.py 同款
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.preselect import (  # noqa: E402
    aggregate_snapshots, build_weights, eval_heldout, load_pairs,
    load_pairs_from_db, load_snapshots, stats_path,
)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="工具预选质量批跑（held-out 召回 + 观测聚合）")
    ap.add_argument("--db", action="store_true",
                    help="从 qd_agent_traces 读配对（默认读 JSONL）")
    ap.add_argument("--pairs", default="", help="配对 JSONL 路径（默认走通道）")
    ap.add_argument("--stats", default="", help="观测快照 JSONL 路径（默认走通道）")
    ap.add_argument("--holdout", type=int, default=20,
                    help="时间序切最后一段做 held-out（默认 20 对）")
    ap.add_argument("--k", type=int, default=8,
                    help="预选面大小（默认 8 = QD_PRESELECT_MAX 默认值）")
    ap.add_argument("--min-pairs", type=int, default=0,
                    help="覆盖 QD_PRESELECT_MIN_PAIRS（默认 0=用全局）")
    args = ap.parse_args(list(argv) if argv is not None else None)

    # 独立跑批时没人替我们加载 .env（与 preselect_cooc.main 同款兜底，仅 CLI 路径）
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    except Exception:
        pass

    if args.db:
        pairs = load_pairs_from_db()
        source = "db"
    elif args.pairs:
        pairs = load_pairs(Path(args.pairs))
        source = args.pairs
    else:
        pairs = load_pairs()
        source = "default"

    # 时间序切尾做 held-out，且最多占 20%——样本本来就少时优先保训练段
    n_hold = max(0, min(args.holdout, len(pairs) // 5))
    train = pairs[:-n_hold] if n_hold else pairs
    test = pairs[-n_hold:] if n_hold else []
    weights = build_weights(train, min_pairs=args.min_pairs or None)

    rec = eval_heldout(test, k=args.k, weights=weights)

    stats_p = Path(args.stats) if args.stats else stats_path()
    stats = aggregate_snapshots(load_snapshots(stats_p))
    stats["file"] = str(stats_p)

    gate = "no_data"
    if rec["n"] > 0:
        best = rec.get("weighted") or rec.get("heuristic") or {}
        gate = "pass95" if best.get("pass_95") else "below95"

    report = {
        "pairs_total": len(pairs),
        "train": len(train),
        "heldout": len(test),
        "weights_built": bool(weights),
        "source": source,
        "recall": rec,
        "stats": stats,
        "gate": gate,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if gate == "pass95" else (2 if gate == "no_data" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
