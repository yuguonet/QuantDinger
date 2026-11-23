"""P0-A 训练：DB(kline_1D) 直读 + 前复权 + 核心因子 + LightGBM 滚动训练。

设计点（2026-11-22 改版，数据源从 qlib 二进制仓切到 DB）：
- 数据源：CNStock_db.kline_1D_YYYY（不复权原始价 OHLCV）+ data/adjustment_factors.json
  （新浪 qfq 因子），前复权口径严格复刻 provider/adjustment.py::unadj_to_qfq：
  factor = <= bar_date 的最近除权日因子；最新除权日之后 factor=1；价 / factor。
- 核心因子（约20个，动量/反转/波动/量价/位置），全部只用 t 日及之前信息，无未来泄漏；
  滚动窗口内标准化（z-score）只用训练窗拟合。
- walk-forward：2年训练 → 6月验证 → 6月测试，逐窗前推；pred+close+open 落盘 parquet。

易错点：
- kline_1D.time 是 15:00 的时间戳，取 YYYY-MM-DD 作为交易日。
- 前复权因子是"分段常数 + 前向填充"，不是线性插值；volume 不复权（与生产一致）。
- 停牌/缺行不前向填充价格（会把停牌当平盘）；因子允许 NaN，LightGBM 原生处理。
- 标签在 common.build_labels 统一构建（口径唯一），这里只落预测分数与原始 OHLC。
- 随机种子固定（common.SEED）。
"""

import argparse
import json
import os
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
import psycopg2

from common import SEED

np.random.seed(SEED)

DB_URL = os.environ.get("QD_P0A_DB_URL", "postgresql://postgres:yuguo@localhost:5432/CNStock_db")
FACTOR_FILE = os.environ.get(
    "QD_P0A_FACTOR_FILE",
    r"D:\QuantDinger\backend_api_python\data\adjustment_factors.json",
)
START_YEAR = int(os.environ.get("QD_P0A_START_YEAR", "2021"))
END_YEAR = int(os.environ.get("QD_P0A_END_YEAR", "2026"))


def load_db_ohlcv(start_year=START_YEAR, end_year=END_YEAR):
    """读 kline_1D_YYYY 全表，返回 long 格式 DataFrame（不复权）。"""
    conn = psycopg2.connect(DB_URL, connect_timeout=10)
    frames = []
    for y in range(start_year, end_year + 1):
        q = f'SELECT symbol, time, open, high, low, close, volume FROM "kline_1D_{y}"'
        df = pd.read_sql(q, conn)
        frames.append(df)
        print(f"  kline_1D_{y}: {len(df)} rows", flush=True)
    conn.close()
    out = pd.concat(frames, ignore_index=True)
    out["datetime"] = pd.to_datetime(out["time"]).dt.normalize()
    out["instrument"] = out["symbol"]
    return out[["instrument", "datetime", "open", "high", "low", "close", "volume"]]


def _sina_code(instrument):
    code = str(instrument).split(".")[0]
    if code.startswith(("6", "9")):
        return "sh" + code
    if code.startswith(("8", "4", "92")):
        return "bj" + code
    return "sz" + code


def apply_qfq(df, factor_path=FACTOR_FILE):
    """对 long DataFrame 就地前复权（向量化复刻 unadj_to_qfq 口径）。

    factor 查找：<= bar_date 的最近除权日 factor；最新除权日之后 = 1；价 / factor。
    volume 不复权。无因子记录的票 factor=1（不处理）。
    """
    raw = json.loads(Path(factor_path).read_text(encoding="utf-8"))
    fac_map = {}
    for k, v in raw.items():
        if k.startswith("_") or not isinstance(v, list):
            continue
        entries = [(d, float(f)) for d, f in v if isinstance(f, (int, float))]
        if entries:
            fac_map[k] = sorted(entries)

    dates = df["datetime"].dt.strftime("%Y-%m-%d").to_numpy()
    fvals = np.ones(len(df), dtype=float)
    codes = df["instrument"].map(_sina_code).to_numpy()
    for i, (code, d) in enumerate(zip(codes, dates)):
        entries = fac_map.get(code)
        if not entries:
            continue
        latest_ex = entries[-1][0]
        if d >= latest_ex:
            continue
        f = 1.0
        for ed, ef in entries:
            if ed <= d:
                f = ef
            else:
                break
        fvals[i] = f
    for c in ("open", "high", "low", "close"):
        df[c] = df[c] / fvals
    return df


def build_features(df):
    """构造核心因子（约20个），只用 t 及之前信息。"""
    out = df.sort_values(["instrument", "datetime"]).reset_index(drop=True)
    g = out.groupby("instrument", sort=False)
    c = out["close"]
    cg = g["close"]
    v = out["volume"]
    h, l, o = out["high"], out["low"], out["open"]

    out["ret_1"] = cg.pct_change(1)
    out["ret_5"] = cg.pct_change(5)
    out["ret_10"] = cg.pct_change(10)
    out["ret_20"] = cg.pct_change(20)
    out["ret_60"] = cg.pct_change(60)
    out["ret_120"] = cg.pct_change(120)
    out["log_vol_20"] = np.log1p(v).groupby(out["instrument"]).transform(
        lambda s: s.rolling(20, min_periods=5).mean()
    )
    out["vol_ratio_5_20"] = out["log_vol_20"] / (
        np.log1p(v).groupby(out["instrument"]).transform(lambda s: s.rolling(60, min_periods=10).mean()) + 1e-9
    )
    out["amp_1"] = (h - l) / c
    amp_mean = out["amp_1"].groupby(out["instrument"]).transform(
        lambda s: s.rolling(20, min_periods=5).mean()
    )
    out["amp_ratio"] = out["amp_1"] / (amp_mean + 1e-9)
    out["std_20"] = out["ret_1"].groupby(out["instrument"]).transform(
        lambda s: s.rolling(20, min_periods=5).std()
    )
    out["std_60"] = out["ret_1"].groupby(out["instrument"]).transform(
        lambda s: s.rolling(60, min_periods=10).std()
    )
    out["vol_of_vol"] = out["std_20"] / (out["std_60"] + 1e-9)
    for n in (5, 10, 20, 60):
        out[f"pos_{n}"] = (c - g["low"].transform(lambda s: s.rolling(n, min_periods=2).min())) / (
            g["high"].transform(lambda s: s.rolling(n, min_periods=2).max())
            - g["low"].transform(lambda s: s.rolling(n, min_periods=2).min())
            + 1e-9
        )
    out["gap"] = o / cg.shift(1) - 1
    out["upper_shadow"] = (h - np.maximum(o, c)) / c
    out["lower_shadow"] = (np.minimum(o, c) - l) / c
    out["body"] = (c - o) / c
    out["close_to_vwap_proxy"] = c / ((h + l + c) / 3) - 1
    out["turn_proxy"] = np.log1p(v) - out["log_vol_20"]
    return out


FEATURE_COLS = [
    "ret_1", "ret_5", "ret_10", "ret_20", "ret_60", "ret_120",
    "log_vol_20", "vol_ratio_5_20", "amp_1", "amp_ratio",
    "std_20", "std_60", "vol_of_vol",
    "pos_5", "pos_10", "pos_20", "pos_60",
    "gap", "upper_shadow", "lower_shadow", "body",
    "close_to_vwap_proxy", "turn_proxy",
]


def windowize(feat, train_years=2, valid_months=6, test_months=6, step_months=6):
    """按交易日切 (train, valid, test) 窗口，均由 calendar 索引定位。"""
    cal = np.array(sorted(feat["datetime"].unique()))
    t_n = int(252 * train_years)
    v_n = int(252 * valid_months / 12)
    s_n = int(252 * test_months / 12)
    step = int(252 * step_months / 12)
    windows = []
    i = t_n
    while i + v_n + s_n <= len(cal):
        windows.append(
            {
                "train": (cal[i - t_n], cal[i - 1]),
                "valid": (cal[i], cal[i + v_n - 1]),
                "test": (cal[i + v_n], cal[i + v_n + s_n - 1]),
            }
        )
        i += step
    return windows, cal


def run(args):
    print("loading DB...", flush=True)
    raw = load_db_ohlcv()
    print(f"DB rows: {len(raw)}", flush=True)
    raw = apply_qfq(raw)
    print("qfq applied", flush=True)
    feat = build_features(raw)
    feat = feat[np.isfinite(feat["close"]) & (feat["close"] > 0)].copy()
    print(f"features: {feat.shape}", flush=True)

    windows, _ = windowize(feat, args.train_years, args.valid_months, args.test_months, args.step_months)
    print(f"windows: {len(windows)}  first={windows[0]}", flush=True)

    outdir = Path(args.out_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    meta = []
    for wi, w in enumerate(windows):
        tr = feat[(feat["datetime"] >= w["train"][0]) & (feat["datetime"] <= w["train"][1])]
        va = feat[(feat["datetime"] >= w["valid"][0]) & (feat["datetime"] <= w["valid"][1])]
        te = feat[(feat["datetime"] >= w["test"][0]) & (feat["datetime"] <= w["test"][1])]
        Xtr, Xva = tr[FEATURE_COLS], va[FEATURE_COLS]
        ytr = tr.groupby("instrument")["close"].shift(-1) / tr["close"] - 1
        yva = va.groupby("instrument")["close"].shift(-1) / va["close"] - 1
        ok_tr, ok_va = ytr.notna() & np.isfinite(ytr), yva.notna() & np.isfinite(yva)
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
            Xtr[ok_tr], ytr[ok_tr],
            eval_set=[(Xva[ok_va], yva[ok_va])],
            callbacks=[lgb.early_stopping(50, verbose=False)],
        )
        pred = m.predict(te[FEATURE_COLS])
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
    p.add_argument("--out_dir", default="tmp/preds")
    p.add_argument("--num_boost_round", type=int, default=600)
    p.add_argument("--train_years", type=int, default=2)
    p.add_argument("--valid_months", type=int, default=6)
    p.add_argument("--test_months", type=int, default=6)
    p.add_argument("--step_months", type=int, default=6)
    p.add_argument("--n_jobs", type=int, default=8)
    args = p.parse_args()
    run(args)


if __name__ == "__main__":
    main()
