"""P0-A「每日选 200」实盘推理入口（盘后运行，选出的股票按次日开盘执行假设）。

作用：把常驻粗筛模型（fit_live_model.py 产出）作用到指定交易日全市场横截面，
输出按尾部概率降序的 top-K（默认 200）清单，供下游联合决策 / KiT 使用。
这是工程推理入口，不代表已证明实盘盈利（方案 §7 风险段：P0-A 通过≠可直接实盘）。

设计点：
- 只用 t 日及之前信息算因子：Alpha158 全宽表滚动后只取目标日截面；目标日必须真实存在于 DB，
  不存在就报错，绝不静默回退到前一交易日。
- 模型与特征列从 meta.json 读取并校验，避免训练/推理特征顺序漂移；预测用 meta 的
  best_iteration，与 sklearn 早停后口径一致。
- 默认 top_k=200（方案 §3 唯一硬门「5000→~200」），可通过参数改，不硬编码在逻辑里。
- 可被调用：select_top200() 返回 (DataFrame, run_meta)，CLI 只是薄封装。

易错点：
- 必须在收盘后运行：模型验证段最后一日会用到 as_of 当日 close（标签 shift(-1)），
  盘中调用等于用半截日线，口径不成立。
- 前复权因子来自全局最新快照，历史 bar 会被新除权日重算；这是已知边界，不是前视。
- 若推理日早于模型 data_last，说明模型见过目标日之后的数据（回测偷看），strict=True 直接拒绝。
- 一字板不在这里剔除（粗筛=查全、宁滥勿缺），由 200→3~8 的联合决策处理。
"""

import argparse
import json
from datetime import datetime as _datetime
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from alpha158_db import ALPHA158_COLS, compute_alpha158
from common import board_of, limit_up_threshold
from train_gbdt import apply_qfq, load_db_ohlcv

HERE = Path(__file__).resolve().parent


def _load_model(model_path):
    model_path = Path(model_path).resolve()
    meta_path = model_path.with_name(model_path.stem + ".meta.json")
    if not meta_path.exists():
        raise FileNotFoundError(f"model meta not found: {meta_path}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    feats = list(meta["features"])
    if feats != list(ALPHA158_COLS):
        raise ValueError(f"feature mismatch: meta has {len(feats)} cols, code has {len(ALPHA158_COLS)}")
    booster = lgb.Booster(model_file=str(model_path))
    return booster, meta


def select_top200(as_of=None, model_path=None, top_k=200, since=None, out_dir=None, strict=True):
    """返回 (top_df, run_meta)；不落盘。落盘由 CLI 的 save 步骤完成。"""
    if model_path is None:
        model_path = HERE / "models" / "tail_cls_live.txt"
    booster, meta = _load_model(model_path)

    print("loading DB...", flush=True)
    raw = load_db_ohlcv()
    if as_of is None:
        as_of = raw["datetime"].max()
    as_of = pd.Timestamp(as_of)
    dates = np.sort(raw["datetime"].unique())
    # 因子预热只需覆盖最长 60 日 rolling；400 自然日留足余量，避免每天全量算 158 因子。
    # 显式 since 用于口径对拍（必须覆盖训练起点之前的完整历史）。
    since = since or str((as_of - pd.Timedelta(days=400)).date())
    # 先按日期窗口截断再做前复权：qfq 是逐行按日期查因子，与窗口外行无关，
    # 但 apply_qfq 是 Python 逐行循环，全量 680 万行要 2 分钟，截到 400 天只要几秒。
    raw = raw[(raw["datetime"] >= pd.Timestamp(since)) & (raw["datetime"] <= as_of)].copy()
    raw = apply_qfq(raw)
    raw = raw[np.isfinite(raw["close"]) & (raw["close"] > 0) & (raw["volume"] > 0)]
    n_dup = int(raw.duplicated(["datetime", "instrument"]).sum())
    if n_dup:
        raw = raw.drop_duplicates(["datetime", "instrument"], keep="last")
    if as_of not in dates:
        raise ValueError(f"{as_of.date()} is not in DB; latest available = {pd.Timestamp(dates[-1]).date()}")
    data_last = pd.Timestamp(meta["data_last"])
    if strict and as_of < data_last:
        raise ValueError(f"inference day {as_of.date()} < model data_last {data_last.date()} (look-ahead)")

    print("computing Alpha158...", flush=True)
    feat_wide = compute_alpha158(raw)
    ohlc = raw.set_index(["datetime", "instrument"])[["open", "high", "low", "close", "volume"]]
    panel = ohlc.join(feat_wide.reindex(ohlc.index)).reset_index()
    day = panel[panel["datetime"] == as_of].copy()
    if len(day) < top_k:
        raise ValueError(f"only {len(day)} instruments on {as_of.date()}, less than top_k={top_k}")

    scores = booster.predict(day[list(meta["features"])].to_numpy(dtype=float),
                             num_iteration=int(meta["best_iteration"]))
    day["score"] = scores
    day = day.sort_values(["score", "instrument"], ascending=[False, True], kind="mergesort").head(top_k)
    out = pd.DataFrame({
        "rank": np.arange(1, len(day) + 1),
        "datetime": day["datetime"].dt.strftime("%Y-%m-%d").to_numpy(),
        "instrument": day["instrument"].to_numpy(),
        "score": day["score"].to_numpy(),
        "close": day["close"].to_numpy(),
        "open": day["open"].to_numpy(),
        "high": day["high"].to_numpy(),
        "low": day["low"].to_numpy(),
        "volume": day["volume"].to_numpy(),
    })
    out["board"] = out["instrument"].map(board_of)
    out["limit_pct"] = out["instrument"].map(lambda x: limit_up_threshold(x))
    run_meta = {
        "generated_at": _datetime.now().isoformat(timespec="seconds"),
        "as_of": str(as_of.date()),
        "top_k": int(top_k),
        "universe": int(len(panel[panel["datetime"] == as_of])),
        "model": Path(model_path).name,
        "model_tag": meta.get("tag"),
        "model_best_iteration": int(meta["best_iteration"]),
        "model_data_last": str(data_last.date()),
        "factor_since": str(since),
        "n_features": int(meta["n_features"]),
        "note": "post-close inference; next-day execution assumed; coarse screen only",
    }
    return out, run_meta


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--date", default="", help="target trading day YYYY-MM-DD (default: DB latest)")
    p.add_argument("--model", default=str(HERE / "models" / "tail_cls_live.txt"))
    p.add_argument("--top_k", type=int, default=200)
    p.add_argument("--since", default="")
    p.add_argument("--out_dir", default=str(HERE / "tmp" / "daily_top200"))
    p.add_argument("--no_strict", action="store_true", help="allow inference day before model data_last")
    args = p.parse_args()

    out, run_meta = select_top200(
        as_of=args.date or None,
        model_path=args.model,
        top_k=args.top_k,
        since=args.since or None,
        strict=not args.no_strict,
    )
    outdir = Path(args.out_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    stem = f"top{args.top_k}_{run_meta['as_of'].replace('-', '')}"
    csv_path, json_path = outdir / f"{stem}.csv", outdir / f"{stem}.json"
    out.to_csv(csv_path, index=False, encoding="utf-8-sig")
    json_path.write_text(
        json.dumps({"meta": run_meta, "rows": out.to_dict("records")}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"TOP200_SAVED rows={len(out)} universe={run_meta['universe']} "
          f"score_max={out['score'].max():.6f} score_min={out['score'].min():.6f}", flush=True)
    print(f"  {csv_path}\n  {json_path}", flush=True)


if __name__ == "__main__":
    main()
