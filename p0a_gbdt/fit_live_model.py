"""P0-A 粗筛常驻模型训练落盘：DB(kline_1D) + 前复权 + Alpha158 + 尾部二分类。

作用：把已验收的训练口径（tmp/train_alpha158_cls.py）固化成可反复加载的常驻模型，
供 infer_daily.py 每日选 200 使用。walk-forward 脚本只产出评测预测、不保存模型，
本脚本是实盘推理链路唯一的模型生产者。

设计点：
- 标签与因子口径不重写：tail_label_binary 来自 common.py，Alpha158 来自 alpha158_db.py，
  DB 读取/前复权来自 train_gbdt.py；避免与验收链路漂移。
- 默认滚动训练：截至 as_of（默认 DB 最新交易日）取最近 train_years 个交易日训练，
  紧邻其后的 valid_months 个交易日早停；与 windowize 的 train/valid 结构一致。
- 显式区间模式（--train_start 等）用于复现某个 walk-forward 窗口做对拍验收。
- 模型存 LightGBM 原生 txt（booster.save_model），meta.json 记特征列/参数/区间/best_iteration；
  推理必须按 meta 的 best_iteration 预测，否则与 sklearn 早停后的口径不一致。

易错点：
- since 只控制因子预热起点，不是训练起点；Alpha158 最长 60 日 rolling，若 since 距训练
  起点不足 60 个交易日，窗口开头因子为 NaN，与验收链路不一致（对拍时 since 用 2021-01-01）。
- 段内 shift(-1) 使每段最后一日标签为 NaN，训练/验证前由 np.isfinite(y) 过滤，
  该日不进训练集；否则会把无次日行情的样本当伪负样本。
- 前复权因子文件是全局最新快照，历史 bar 会随新除权日重算；这是已知边界，不是前视。
"""

import argparse
import json
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from alpha158_db import ALPHA158_COLS, compute_alpha158
from common import SEED, tail_label_binary
from train_gbdt import apply_qfq, load_db_ohlcv

HERE = Path(__file__).resolve().parent


def prepare_panel(since):
    """读 DB → 前复权 → 清洗 → 全量 Alpha158 → 截断到 since 之后的长表面板。"""
    raw = load_db_ohlcv()
    raw = apply_qfq(raw)
    raw = raw[np.isfinite(raw["close"]) & (raw["close"] > 0) & (raw["volume"] > 0)].copy()
    n_dup = int(raw.duplicated(["datetime", "instrument"]).sum())
    if n_dup:
        print(f"dropping {n_dup} duplicate (datetime, instrument) rows", flush=True)
        raw = raw.drop_duplicates(["datetime", "instrument"], keep="last")
    print(f"DB rows after clean: {len(raw)}", flush=True)

    print("computing full Alpha158 (158 factors)...", flush=True)
    feat_wide = compute_alpha158(raw)
    ohlc = raw.set_index(["datetime", "instrument"])[["open", "high", "low", "close", "volume"]]
    keep = ohlc.index.get_level_values("datetime") >= pd.Timestamp(since)
    ohlc, feat_wide = ohlc[keep], feat_wide.reindex(ohlc.index)
    # 不要对 163 列宽表再 .copy()：pandas 3.0 会 consolidate 出 8.35GiB 连续块并 OOM。
    panel = ohlc.join(feat_wide).reset_index()
    print(f"panel since {since}: {panel.shape}", flush=True)
    return panel


def resolve_bounds(panel, args):
    """显式区间优先；否则按 as_of 往前取 t_n 训练 + v_n 验证（均按交易日数）。"""
    cal = np.array(sorted(panel["datetime"].unique()))
    if args.as_of:
        cal = cal[cal <= np.datetime64(pd.Timestamp(args.as_of))]
    if len(cal) == 0:
        raise ValueError(f"no trading day <= as_of={args.as_of}")
    if args.train_start:
        bounds = {
            "train": (pd.Timestamp(args.train_start), pd.Timestamp(args.train_end)),
            "valid": (pd.Timestamp(args.valid_start), pd.Timestamp(args.valid_end)),
        }
    else:
        t_n = int(252 * args.train_years)
        v_n = int(252 * args.valid_months / 12)
        if len(cal) < t_n + v_n:
            raise ValueError(f"need >= {t_n + v_n} trading days, got {len(cal)}")
        bounds = {
            "train": (pd.Timestamp(cal[-t_n - v_n]), pd.Timestamp(cal[-v_n - 1])),
            "valid": (pd.Timestamp(cal[-v_n]), pd.Timestamp(cal[-1])),
        }
    # data_last 必须是模型实际见到的最后一个交易日（验证段末），不是 DB 全局最新日；
    # 否则显式区间训练的模型会把自己没见过的最近数据误报为已覆盖，放过去就成回测偷看。
    return bounds, bounds["valid"][1]


def fit(panel, bounds, args):
    def seg(b):
        return panel[(panel["datetime"] >= b[0]) & (panel["datetime"] <= b[1])]

    tr, va = seg(bounds["train"]), seg(bounds["valid"])
    ytr, yva = tail_label_binary(tr), tail_label_binary(va)
    ok_tr = np.isfinite(tr["close"]) & np.isfinite(ytr)
    ok_va = np.isfinite(va["close"]) & np.isfinite(yva)
    m = lgb.LGBMClassifier(
        n_estimators=args.num_boost_round,
        learning_rate=0.05,
        num_leaves=31,
        min_child_samples=100,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=SEED,
        n_jobs=args.n_jobs,
        verbosity=-1,
    )
    m.fit(
        tr.loc[ok_tr, ALPHA158_COLS], ytr[ok_tr],
        eval_set=[(va.loc[ok_va, ALPHA158_COLS], yva[ok_va])],
        callbacks=[lgb.early_stopping(50, verbose=False)],
    )
    return m, int(ok_tr.sum()), int(ok_va.sum())


def run(args):
    panel = prepare_panel(args.since)
    bounds, data_last = resolve_bounds(panel, args)
    print(f"train={bounds['train'][0].date()}..{bounds['train'][1].date()} "
          f"valid={bounds['valid'][0].date()}..{bounds['valid'][1].date()} "
          f"data_last={data_last.date()}", flush=True)
    m, n_tr, n_va = fit(panel, bounds, args)

    outdir = (HERE / args.model_dir).resolve() if not Path(args.model_dir).is_absolute() else Path(args.model_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    model_path = outdir / f"tail_cls_{args.tag}.txt"
    meta_path = outdir / f"tail_cls_{args.tag}.meta.json"
    m.booster_.save_model(str(model_path))
    meta = {
        "tag": args.tag,
        "model": model_path.name,
        "features": list(ALPHA158_COLS),
        "n_features": len(ALPHA158_COLS),
        "params": {
            "n_estimators": args.num_boost_round,
            "learning_rate": 0.05,
            "num_leaves": 31,
            "min_child_samples": 100,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "random_state": SEED,
        },
        "best_iteration": int(m.best_iteration_),
        "train": [str(bounds["train"][0]), str(bounds["train"][1])],
        "valid": [str(bounds["valid"][0]), str(bounds["valid"][1])],
        "n_train": n_tr,
        "n_valid": n_va,
        "data_last": str(data_last),
        "factor_since": args.since,
        "label_rule": "next_close/close-1 > 0.07 OR >= board_limit (tail binary)",
        "feature_source": "alpha158_db.py (DB qfq, VWAP0=typical price proxy)",
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"MODEL_SAVED {model_path}  best_iteration={meta['best_iteration']}  meta={meta_path}", flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tag", default="live")
    p.add_argument("--model_dir", default="models")
    p.add_argument("--since", default="2021-01-01", help="factor warmup start, not training start")
    p.add_argument("--as_of", default="", help="training data upper bound (default: DB latest day)")
    p.add_argument("--train_years", type=int, default=3)
    p.add_argument("--valid_months", type=int, default=6)
    p.add_argument("--train_start", default="")
    p.add_argument("--train_end", default="")
    p.add_argument("--valid_start", default="")
    p.add_argument("--valid_end", default="")
    p.add_argument("--num_boost_round", type=int, default=2000)
    p.add_argument("--n_jobs", type=int, default=8)
    args = p.parse_args()
    if bool(args.train_start) != bool(args.train_end) or bool(args.valid_start) != bool(args.valid_end):
        raise SystemExit("--train_start/--train_end and --valid_start/--valid_end must be given together")
    if args.train_start and not args.valid_start:
        raise SystemExit("explicit mode needs --valid_start/--valid_end as well")
    run(args)


if __name__ == "__main__":
    main()
