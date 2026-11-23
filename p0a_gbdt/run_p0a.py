"""P0-A 一键编排：训练（A3→A4）→ 评测 → 报告。

设计点：串起 train_gbdt.py 与 evaluate.py，固定随机种子与数据口径。
易错点：data_dir 指向 qlib provider 根目录（含 calendars/ features/ instruments/）；
产物一律进 tmp/，不污染数据目录。
"""

import argparse
import subprocess
import sys
from pathlib import Path


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data_dir", default="data/cn_data")
    p.add_argument("--work_dir", default=".")
    p.add_argument("--num_boost_round", type=int, default=600)
    args = p.parse_args()

    py = sys.executable
    pred_dir = str(Path(args.work_dir) / "tmp" / "preds")
    cmd_train = [
        py, str(Path(args.work_dir) / "train_gbdt.py"),
        "--out_dir", pred_dir,
        "--num_boost_round", str(args.num_boost_round),
    ]
    cmd_eval = [
        py, str(Path(args.work_dir) / "evaluate.py"),
        "--pred_dir", pred_dir,
        "--data_dir", str(Path(args.work_dir) / args.data_dir),
        "--out", str(Path(args.work_dir) / "tmp" / "p0a_report.md"),
    ]
    print("TRAIN:", " ".join(cmd_train), flush=True)
    subprocess.run(cmd_train, check=True)
    print("EVAL:", " ".join(cmd_eval), flush=True)
    subprocess.run(cmd_eval, check=True)


if __name__ == "__main__":
    main()
