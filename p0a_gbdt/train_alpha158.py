"""P0-A 粗筛训练：DB(kline_1D) + 前复权 + 完整 Alpha158(158 因子) + LightGBM 滚动训练。

设计点（2026-11-22）：
- 与 train_gbdt.py 的差异：因子从 23 个自算核心因子换成完整 Alpha158（口径真源 = qlib
  Alpha158DL，已与 qlib 官方特征对拍：152/158 相关系数>0.95，无反向；VWAP0 因 DB 无
  amount 用典型价近似、WVMA 因复权口径差异，属已知边界）。
- 数据源与前复权口径与 train_gbdt.py 完全一致（复用其 load_db_ohlcv/apply_qfq），不重复实现。
- 双口径标签：回归 next-bar 收益（训练目标，贴官方 benchmark）+ 尾部标记（评测用，
  由 common.build_labels 统一算）。粗筛主指标 = recall@200。
- walk-forward：3年训练 → 6月验证 → 6月测试（贴原版交付包 train_gbdt.py 的 WINDOWS
  结构），逐窗前推；pred+OHLC 落盘 parquet 供 evaluate.py 评测。

易错点：
- 宽表 pivot(date × instrument) 后 stack 产生 MultiIndex(datetime, instrument)，
  pandas 3.0 下 sort_index 对 datetime MultiIndex 有 bug，不要调用；stack 本身有序。
- 因子一次性全量算（约 8~10GB 内存峰值），再按窗切片；不要每窗重算。
- LightGBM 原生处理 NaN，不做 fillna（填 0 会把"窗口不足"当真实信号）。
- 随机种子固定（common.SEED）。
"""

import argparse
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from alpha158_db import ALPHA158_COLS, compute_alpha158
from common import SEED
from train_gbdt import apply_qfq, load_db_ohlcv, windowize

np.random.seed(SEED)


def run(args):
    print("loading DB...", flush=True)
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
    feat_wide = feat_wide.reindex(ohlc.index)
    print(f"Alpha158 shape: {feat_wide.shape}", flush=True)

    panel = ohlc.join(feat_wide).reset_index()
    print(f"panel shape: {panel.shape}", flush=True)

    windows, _ = windowize(
        panel, args.train_years, args.valid_months, args.test_months, args.step_months
    )
    print(f"windows: {len(windows)}  first={windows[0]}", flush=True)

    outdir = Path(args.out_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    meta = []
    for wi, w in enumerate(windows):
        def seg(bounds):
            return panel[(panel["datetime"] >= bounds[0]) & (panel["datetime"] <= bounds[1])]

        tr, va, te = seg(w["train"]), seg(w["valid"]), seg(w["test"])
        ytr = tr.groupby("instrument")["close"].shift(-1) / tr["close"] - 1
        yva = va.groupby("instrument")["close"].shift(-1) / va["close"] - 1
        ok_tr = ytr.notna() & np.isfinite(ytr)
        ok_va = yva.notna() & np.isfinite(yva)
        m = lgb.LGBMRegressor(
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
        pred = m.predict(te[ALPHA158_COLS])
        out = te[["instrument", "datetime", "open", "high", "low", "close", "volume"]].copy()
        out["score"] = pred
        out["window"] = wi
        f = outdir / f"pred_w{wi:02d}.parquet"
        out.to_parquet(f, index=False)
        meta.append({"window": wi, "train_s": str(w["train"][0]), "train_e": str(w["train"][1]),
                     "test_s": str(w["test"][0]), "test_e": str(w["test"][1]), "n": len(out)})
        print(f"window {wi}: train={ok_tr.sum()} test={len(out)} -> {f.name}", flush=True)
    pd.DataFrame(meta).to_csv(outdir / "windows.csv", index=False)
    print("TRAIN_DONE", flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out_dir", default="tmp/preds_a158")
    p.add_argument("--num_boost_round", type=int, default=600)
    p.add_argument("--train_years", type=int, default=3)
    p.add_argument("--valid_months", type=int, default=6)
    p.add_argument("--test_months", type=int, default=6)
    p.add_argument("--step_months", type=int, default=6)
    p.add_argument("--n_jobs", type=int, default=8)
    args = p.parse_args()
    run(args)


if __name__ == "__main__":
    main()
