# -*- coding: utf-8 -*-
"""
train_tailcls_exp.py — P0-A 尾部二分类实验训练器（改进文档 §1/§2 的实验臂）

作用:
  在同一 walk-forward 框架里对比 2×3 实验矩阵:
    特征集 ∈ {Alpha158, Alpha158+尾部特征(--tail_features)}
    训练目标 ∈ {logloss, weighted(scale_pos_weight), focal}
  产出与 train_alpha158.py 同 schema 的逐窗预测 parquet（score=尾部概率）,
  直接喂 evaluate.py 出 tail_recall@K / top-decile lift / bootstrap CI 判定。

  口径复用（不重写）: load_db_ohlcv/apply_qfq/windowize(train_gbdt),
  compute_alpha158/ALPHA158_COLS(alpha158_db), tail_label_binary(common, J1 修复后)。

用法:
  # 三臂基线（无新特征）
  python train_tailcls_exp.py --objective logloss  --out_dir tmp/exp/logloss
  python train_tailcls_exp.py --objective weighted --scale_pos_weight 10 --out_dir tmp/exp/w10
  python train_tailcls_exp.py --objective focal --gamma 2.0 --focal_alpha 0.5 --out_dir tmp/exp/focal
  # + 尾部特征（p0a_tail_features/extract_tail_features.py 的产物）
  python train_tailcls_exp.py --objective focal --tail_features tmp/tail_features.parquet \
      --out_dir tmp/exp/focal_tailfeat

易错点:
  1. focal 是自定义目标函数, 预测分数必须手动 sigmoid: 统一走 raw_score=True +
     手算 sigmoid, 保证三臂分数口径一致（标准目标下与 predict_proba 等价）。
  2. 早停指标三臂统一用 valid AUC（--early_stop_metric）, 组内可比;
     与历史报告（logloss 早停）数值不直接可比, 判定只在本实验内部+对 naive 做。
  3. --tail_features 的 join 键是 (交易日, 6 位代码): 两侧代码取 6 位前缀归一
     （DB symbol 与 ticai ts_code 后缀格式不同）。join 后 NaN 保留不填
     （LightGBM 原生处理; 填 0 会把"覆盖期外"当真实信号）。
  4. 标签段末 NaN 由 tail_label_binary 保留、np.isfinite 过滤（J1 修复）, 勿改成
     astype(int), 会复活伪负样本。
  5. 随机种子固定 common.SEED; 结果落盘 tmp/（产物区）, 报告引用窗口表 windows.csv。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from alpha158_db import ALPHA158_COLS, compute_alpha158
from common import SEED, tail_label_binary
from train_gbdt import apply_qfq, load_db_ohlcv, windowize

np.random.seed(SEED)


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -40.0, 40.0)))


def _norm6(s: pd.Series) -> pd.Series:
    return s.astype(str).str.split(".").str[0].str.zfill(6)


def join_tail_features(panel: pd.DataFrame, feat_path: str) -> pd.DataFrame:
    """左连接尾部特征表 (trade_date, ts_code) → (datetime, instrument)。"""
    tf = pd.read_parquet(feat_path)
    tf["key6"] = _norm6(tf["ts_code"])
    tf["datetime"] = pd.to_datetime(tf["trade_date"])
    panel = panel.copy()
    panel["key6"] = _norm6(panel["instrument"])
    tail_cols = [c for c in tf.columns
                 if c not in ("trade_date", "ts_code", "key6", "datetime", "is_st_last")]
    tf = tf[["datetime", "key6"] + tail_cols].drop_duplicates(["datetime", "key6"])
    merged = panel.merge(tf, on=["datetime", "key6"], how="left").drop(columns=["key6"])
    n_hit = int(merged[tail_cols[0]].notna().sum())
    print(f"tail features joined: cols={len(tail_cols)} hit={n_hit}/{len(merged)} "
          f"({n_hit / max(len(merged), 1):.1%})", flush=True)
    return merged, tail_cols


def build_model(args):
    if args.objective == "logloss":
        obj = "binary"
    elif args.objective == "weighted":
        obj = "binary"
    elif args.objective == "focal":
        from focal_loss import focal_loss_objective
        obj = focal_loss_objective(gamma=args.gamma, alpha=args.focal_alpha)
    else:
        raise SystemExit(f"unknown objective: {args.objective}")
    kw = {}
    if args.objective == "weighted":
        kw["scale_pos_weight"] = args.scale_pos_weight
    return lgb.LGBMClassifier(
        n_estimators=args.num_boost_round,
        learning_rate=0.05,
        num_leaves=31,
        min_child_samples=100,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=SEED,
        n_jobs=args.n_jobs,
        verbosity=-1,
        objective=obj,
        eval_metric=args.early_stop_metric,
        **kw,
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--objective", choices=["logloss", "weighted", "focal"], required=True)
    p.add_argument("--tail_features", default="", help="extract_tail_features.py 产物 parquet")
    p.add_argument("--out_dir", required=True)
    p.add_argument("--scale_pos_weight", type=float, default=10.0)
    p.add_argument("--gamma", type=float, default=2.0)
    p.add_argument("--focal_alpha", type=float, default=0.5)
    p.add_argument("--early_stop_metric", default="auc")
    p.add_argument("--num_boost_round", type=int, default=2000)
    p.add_argument("--train_years", type=int, default=3)
    p.add_argument("--valid_months", type=int, default=6)
    p.add_argument("--test_months", type=int, default=6)
    p.add_argument("--step_months", type=int, default=6)
    p.add_argument("--n_jobs", type=int, default=8)
    args = p.parse_args()

    print("loading DB...", flush=True)
    raw = load_db_ohlcv()
    raw = apply_qfq(raw)
    raw = raw[np.isfinite(raw["close"]) & (raw["close"] > 0) & (raw["volume"] > 0)].copy()
    raw = raw.drop_duplicates(["datetime", "instrument"], keep="last")

    print("computing Alpha158...", flush=True)
    feat_wide = compute_alpha158(raw)
    ohlc = raw.set_index(["datetime", "instrument"])[["open", "high", "low", "close", "volume"]]
    feat_wide = feat_wide.reindex(ohlc.index)
    panel = ohlc.join(feat_wide).reset_index()

    feat_cols = list(ALPHA158_COLS)
    if args.tail_features:
        panel, tail_cols = join_tail_features(panel, args.tail_features)
        feat_cols = feat_cols + tail_cols

    windows, _ = windowize(panel, args.train_years, args.valid_months,
                           args.test_months, args.step_months)
    outdir = Path(args.out_dir)
    outdir.mkdir(parents=True, exist_ok=True)

    meta = []
    for wi, w in enumerate(windows):
        def seg(b):
            return panel[(panel["datetime"] >= b[0]) & (panel["datetime"] <= b[1])]

        tr, va, te = seg(w["train"]), seg(w["valid"]), seg(w["test"])
        ytr, yva = tail_label_binary(tr), tail_label_binary(va)
        ok_tr = np.isfinite(tr["close"]) & np.isfinite(ytr).to_numpy()
        ok_va = np.isfinite(va["close"]) & np.isfinite(yva).to_numpy()

        m = build_model(args)
        m.fit(
            tr.loc[ok_tr, feat_cols], ytr[ok_tr],
            eval_set=[(va.loc[ok_va, feat_cols], yva[ok_va])],
            callbacks=[lgb.early_stopping(50, verbose=False)],
        )
        # 统一分数口径: raw margin → sigmoid（易错点1）
        raw_te = m.booster_.predict(te[feat_cols].to_numpy(dtype=float), raw_score=True)
        out = te[["instrument", "datetime", "open", "high", "low", "close", "volume"]].copy()
        out["score"] = _sigmoid(np.asarray(raw_te, dtype=float))
        out["window"] = wi
        f = outdir / f"pred_w{wi:02d}.parquet"
        out.to_parquet(f, index=False)

        yte = tail_label_binary(te)
        ok_te = np.isfinite(yte).to_numpy()
        pos_rate = float(np.nanmean(yte[ok_te])) if ok_te.any() else float("nan")
        meta.append({
            "window": wi, "train_s": str(w["train"][0]), "train_e": str(w["train"][1]),
            "valid_s": str(w["valid"][0]), "valid_e": str(w["valid"][1]),
            "test_s": str(w["test"][0]), "test_e": str(w["test"][1]),
            "n_train": int(ok_tr.sum()), "n_test": int(ok_te.sum()),
            "best_iteration": int(m.best_iteration_), "test_pos_rate": pos_rate,
        })
        print(f"window {wi}: train={int(ok_tr.sum())} test={int(ok_te.sum())} "
              f"pos_rate={pos_rate:.3f} best_iter={m.best_iteration_} -> {f.name}", flush=True)

    pd.DataFrame(meta).to_csv(outdir / "windows.csv", index=False)
    (outdir / "exp_meta.json").write_text(json.dumps({
        "objective": args.objective, "tail_features": args.tail_features,
        "scale_pos_weight": args.scale_pos_weight if args.objective == "weighted" else None,
        "gamma": args.gamma if args.objective == "focal" else None,
        "focal_alpha": args.focal_alpha if args.objective == "focal" else None,
        "early_stop_metric": args.early_stop_metric,
        "feat_cols": feat_cols, "seed": SEED,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print("TRAIN_DONE", flush=True)


if __name__ == "__main__":
    main()
