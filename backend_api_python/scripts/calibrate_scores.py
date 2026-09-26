# -*- coding: utf-8 -*-
"""评分校准回填脚本（A1b，2026-09-26）。

用法：
    python scripts/calibrate_scores.py [--skills technical_analysis,bull_bear_research]

从 qd_traces 取 (score, direction, correct) → IsotonicRegression 拟合 →
存 qd_agent_weights(layer='calibration')。冷启动时样本不足会自动跳过并报告。

幂等：每次先 DELETE 该 skill 的旧 calibration 记录再 INSERT。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 让脚本可独立运行（backend_api_python 作为根）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.utils.calibration import fit_score_map  # noqa: E402

DEFAULT_SKILLS = ["technical_analysis", "bull_bear_research"]


def main() -> None:
    parser = argparse.ArgumentParser(description="评分校准回填")
    parser.add_argument("--skills", default=",".join(DEFAULT_SKILLS),
                        help="逗号分隔的 skill 名列表")
    args = parser.parse_args()

    skills = [s.strip() for s in args.skills.split(",") if s.strip()]
    results = []
    for skill in skills:
        print(f"[calibrate] fitting {skill} ...")
        r = fit_score_map(skill)
        results.append(r)
        print(f"  -> {r}")

    print("\n=== 汇总 ===")
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
