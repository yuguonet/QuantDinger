"""P0-A 评测：尾部指标 + naive 四对照 + 分年度 + 生死线判定。

设计点：
- 读取 train_gbdt 落盘的逐窗预测，按 common.build_labels 统一构建标签（口径唯一）。
- 模型与每个 naive 用相同日期 / universe / K / 可交易口径，确保配对可比（附录 C）。
- 逐日 recall@K / precision@K / top-decile lift；配对区块 bootstrap 报 95%CI。
- 漏斗分层归因 + 分年度报告 + 生死线判定表 → tmp/p0a_report.md。

易错点：
- 测试窗只用于最终验收，不用于挑特征/阈值/模型版本。
- 全部指标按可交易口径重算（剔一字板）；base_rate 为当日可交易尾部事件占比。
- naive 的 copy_last 必须固定种子（common.SEED），不可拿股票顺序当基准。
"""

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from common import (
    SEED,
    build_labels,
    daily_metrics,
    funnel_attribution,
    naive_scores,
    survival_verdict,
)

K_LIST = (20, 50, 100)
NAIVE_METHODS = ("copy_last", "momentum_5d", "momentum_20d", "vol_rank")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pred_dir", default="tmp/preds")
    p.add_argument("--data_dir", default="data/cn_data")
    p.add_argument("--out", default="tmp/p0a_report.md")
    p.add_argument("--k", type=int, nargs="+", default=list(K_LIST))
    args = p.parse_args()

    pred_dir = Path(args.pred_dir)
    files = sorted(pred_dir.glob("pred_w*.parquet"))
    if not files:
        raise SystemExit(f"no prediction files in {pred_dir}")
    raw = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    for c in ("open", "high", "low", "close", "volume"):
        if c not in raw.columns:
            raw[c] = np.nan
    st_file = Path(args.data_dir) / "instruments" / "st.txt" if args.data_dir else None
    st_codes = None
    if st_file and st_file.exists():
        st_codes = set(st_file.read_text(encoding="utf-8").split())
        print(f"ST codes: {len(st_codes)}", flush=True)
    labeled = build_labels(raw, st_codes=st_codes)
    tradable = labeled[labeled["tradable"]].copy()
    print(f"labeled rows: {len(labeled)}  tradable: {len(tradable)}", flush=True)

    model_daily = daily_metrics(tradable, k_list=args.k, score_col="score")
    naive_dailies = {}
    for method in NAIVE_METHODS:
        ns = naive_scores(tradable, method)
        naive_dailies[method] = daily_metrics(ns, k_list=args.k, score_col="score")
        print(f"naive {method} done", flush=True)

    verdict = survival_verdict(model_daily, naive_dailies)
    funnel = funnel_attribution(tradable, pred_col="score", k=max(args.k))

    model_daily["year"] = pd.to_datetime(model_daily["datetime"]).dt.year
    yearly = (
        model_daily.groupby(["year", "K"])[["recall", "precision", "top_decile_lift"]]
        .mean()
        .round(4)
    )

    lines = [
        "# P0-A 尾部事件验证报告",
        "",
        f"生成时间：2026-11-22  随机种子：{SEED}  预测文件：{len(files)} 个",
        f"样本：{len(labeled)} 行，可交易 {len(tradable)} 行；K={args.k}",
        "",
        "## 1. 模型主指标（均值）",
        "",
        model_daily.groupby("K")[
            ["recall", "precision", "base_rate", "top_decile_lift"]
        ].mean().round(4).to_markdown(),
        "",
        "## 2. 配对区块 bootstrap（模型 - naive，按交易日重采样 2000 次）",
        "",
    ]
    drows = [
        {
            "baseline": d["baseline"],
            "metric": d["metric"],
            "diff": round(d["diff"], 4),
            "ci_low": round(d["ci_low"], 4),
            "ci_high": round(d["ci_high"], 4),
            "n_days": d["n_days"],
            "pass(low>0)": d["pass"],
        }
        for d in verdict["details"]
    ]
    lines.append(pd.DataFrame(drows).to_markdown(index=False))
    lines += [
        "",
        "## 3. 分年度（模型主指标）",
        "",
        yearly.to_markdown(),
        "",
        "## 4. 漏斗分层归因",
        "",
        funnel.round(4).to_markdown(index=False),
        "",
        "## 5. 生死线判定（§6.2）",
        "",
        f"- 两主指标对全部 naive 的 CI 下界均 >0：**{'是' if verdict['pass'] else '否'}**",
        f"- top-decile lift 均值：{verdict['mean_lift']:.2f}×（可用线 3×）",
        f"- 结论：**{'可用' if verdict['usable'] else ('及格' if verdict['pass'] else '不过/存疑')}**",
        f"- 下一步：{'进入 P0-B（KiT 增量评估）' if verdict['pass'] else '止损预测路线，转风控/异常检测（§7）'}",
        "",
    ]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")

    csv = out.parent / "p0a_metrics_all.csv"
    model_daily.assign(model="gbdt").to_csv(csv, index=False)
    print(f"REPORT -> {out}   METRICS -> {csv}", flush=True)


if __name__ == "__main__":
    main()
