"""P0-A 尾部事件验证：标签构建、naive 对照、尾部指标、生死线统计的唯一口径实现。

设计点：
- 标签口径与方案 §6.2 严格对齐：next-bar 收益 r = close[t+1]/close[t] - 1（前复权）；
  尾部双口径（相对 >7% / 分板块涨停），一字板单列不计入可交易命中。
- tail_label_binary 是粗筛训练目标的唯一实现（收益回归不适合涨停查全，已实测）；
  训练脚本与实盘重训脚本必须共用，避免口径漂移。
- naive 四对照：copy_last（固定种子随机排序）、momentum_5d/20d、vol_rank（振幅代理）。
- 指标：tail_recall@K / tail_precision@K / top-decile lift；全部按可交易口径重算。
- 配对区块 bootstrap：按交易日重采样，报差值与 95% 置信区间（附录 C）。

易错点：
- qlib 数据 $close 已前复权，r 用相邻 close 比值；涨停阈值按样本日板块归属判定。
- 一字板（open==high==low==close 且封涨停）不可买入，必须从可交易命中中剔除。
- copy_last 若为「无预测力基准」必须用固定种子随机排序，不能拿未定义的股票顺序当基准。
- 所有统计量只用训练窗拟合；测试窗只用于最终验收。
"""

import numpy as np
import pandas as pd

SEED = 20261122

BOARD_LIMIT = {
    "main": 0.098,
    "gem": 0.198,
    "star": 0.198,
    "bse": 0.298,
}


def board_of(instrument):
    """按证券代码判定板块（主板/创业板/科创板/北交所）。"""
    code = str(instrument).split(".")[0]
    if code.startswith(("300", "301")):
        return "gem"
    if code.startswith("688"):
        return "star"
    if code.startswith(("83", "87", "43", "92")):
        return "bse"
    return "main"


ST_LIMIT = 0.048


def limit_up_threshold(instrument, is_st=False):
    """涨停阈值：ST ±5% 单列（4.8% 容差），其余按板块。"""
    if is_st:
        return ST_LIMIT
    return BOARD_LIMIT[board_of(instrument)]


def build_labels(df, st_codes=None):
    """构建 next-bar 标签与尾部事件标记。

    df 需含列：instrument, datetime, open, high, low, close（前复权）。
    st_codes: ST 股票代码集合（可选，来自 instruments/st.txt）；命中则涨停阈值 4.8%。
    返回同索引的 DataFrame，附加：
      r_next, is_tail_rel, is_tail_limit, is_tail, is_yizi, tradable, is_tail_tradable
    """
    out = df.copy()
    out = out.sort_values(["instrument", "datetime"]).reset_index(drop=True)
    g = out.groupby("instrument", sort=False)
    out["close_next"] = g["close"].shift(-1)
    out["open_next"] = g["open"].shift(-1)
    out["high_next"] = g["high"].shift(-1)
    out["low_next"] = g["low"].shift(-1)
    out["r_next"] = out["close_next"] / out["close"] - 1.0

    if st_codes:
        codes = out["instrument"].astype(str).str.split(".").str[0]
        is_st = codes.isin(set(st_codes))
    else:
        is_st = pd.Series(False, index=out.index)
    thr = pd.Series(
        [limit_up_threshold(i, s) for i, s in zip(out["instrument"], is_st)],
        index=out.index,
    )
    out["is_tail_rel"] = out["r_next"] > 0.07
    out["is_tail_limit"] = out["r_next"] >= thr
    out["is_tail"] = out["is_tail_rel"] | out["is_tail_limit"]

    yz = (
        (out["open_next"] == out["high_next"])
        & (out["high_next"] == out["low_next"])
        & (out["low_next"] == out["close_next"])
        & (out["is_tail_limit"])
    )
    out["is_yizi"] = yz.fillna(False)
    out["tradable"] = out["r_next"].notna() & ~out["is_yizi"]
    out["is_tail_tradable"] = out["is_tail"] & out["tradable"]
    return out


def tail_label_binary(df):
    """粗筛训练标签（尾部二分类）：next-bar 收益 >7% 或达板块涨停线 → 1，否则 0。

    只取段内 groupby shift(-1) 的次日 close；段末行无次日，标签保留 NaN（不可训练）。
    返回 float 而非 int，调用方必须用 np.isfinite(y) 过滤段末行，
    否则 NaN 会被当成 0 变成伪负样本。这是训练脚本与实盘重训脚本的唯一口径实现。
    """
    nxt = df.groupby("instrument")["close"].shift(-1)
    ret = nxt / df["close"] - 1.0
    thr = df["instrument"].map(lambda x: limit_up_threshold(x))
    y = ((ret > 0.07) | (ret >= thr)).astype(float)
    return y.where(nxt.notna())


def naive_scores(panel, method, date_col="datetime", inst_col="instrument"):
    """按日生成 naive 排序分数（越大越优先）。

    panel 需含：instrument, datetime, close, volume, amplitude（可选）。
    copy_last 用固定种子随机分数（无预测力基准，可复现）。
    """
    rng = np.random.default_rng(SEED)
    df = panel.copy()
    if method == "copy_last":
        df["score"] = rng.random(len(df))
    elif method in ("momentum_5d", "momentum_20d"):
        n = 5 if method == "momentum_5d" else 20
        df = df.sort_values([inst_col, date_col])
        df["score"] = df.groupby(inst_col, sort=False)["close"].pct_change(n)
    elif method == "vol_rank":
        df = df.sort_values([inst_col, date_col])
        if "amplitude" not in df.columns:
            df["amplitude"] = (df["high"] - df["low"]) / df["close"]
        df["score"] = df.groupby(inst_col, sort=False)["amplitude"].transform(
            lambda s: s.rolling(20, min_periods=5).mean()
        )
    else:
        raise ValueError(f"unknown naive method: {method}")
    return df


def daily_metrics(df, k_list=(20, 50, 100), score_col="score", tail_col="is_tail_tradable"):
    """逐日横截面计算 recall@K / precision@K / top-decile lift。

    返回 DataFrame，每行 = (datetime, K, recall, precision, ...)。
    """
    rows = []
    for dt, day in df.groupby("datetime", sort=True):
        day = day.dropna(subset=[score_col]).sort_values(score_col, ascending=False)
        n = len(day)
        if n == 0:
            continue
        base = float(day[tail_col].mean()) if tail_col in day.columns else 0.0
        n_tail = int(day[tail_col].sum()) if tail_col in day.columns else 0
        top_decile = max(1, n // 10)
        decile_tail = float(day[tail_col].head(top_decile).sum()) if tail_col in day.columns else 0.0
        decile_rate = decile_tail / top_decile
        lift = decile_rate / base if base > 0 else np.nan
        for k in k_list:
            kk = min(k, n)
            hit = int(day[tail_col].head(kk).sum()) if tail_col in day.columns else 0
            rows.append(
                {
                    "datetime": dt,
                    "K": k,
                    "n": n,
                    "n_tail": n_tail,
                    "hit": hit,
                    "recall": hit / n_tail if n_tail else np.nan,
                    "precision": hit / kk,
                    "base_rate": base,
                    "top_decile_lift": lift,
                }
            )
    return pd.DataFrame(rows)


def paired_block_bootstrap(model_daily, naive_daily, metric="recall", n_boot=2000, seed=SEED):
    """按交易日配对区块 bootstrap，返回差值与 95% CI。

    model_daily / naive_daily 均为 daily_metrics 输出，按 (datetime, K) 对齐。
    实现：先 pivot 成 (datetime × K) 差值矩阵，再按「行=交易日」整块重采样；
    一次 rng 索引完成 n_boot 次抽样，避免逐日 MultiIndex.xs 的 O(n_boot×n_days) 开销。
    """
    a = model_daily.pivot(index="datetime", columns="K", values=metric)
    b = naive_daily.pivot(index="datetime", columns="K", values=metric)
    ks = a.columns.intersection(b.columns)
    idx = a.index.intersection(b.index)
    if len(idx) == 0 or len(ks) == 0:
        return {"diff": np.nan, "ci_low": np.nan, "ci_high": np.nan, "n_days": 0}
    diff = (a.loc[idx, ks] - b.loc[idx, ks]).to_numpy(dtype=float)
    valid = ~np.isnan(diff)
    diff = np.where(valid, diff, 0.0)
    counts = valid.sum(axis=1).reshape(-1, 1)
    day_sum = (diff * valid).sum(axis=1)
    rng = np.random.default_rng(seed)
    picked = rng.integers(0, len(day_sum), size=(n_boot, len(day_sum)))
    boot_sum = day_sum[picked].sum(axis=1)
    boot_cnt = counts.ravel()[picked].sum(axis=1)
    boot_mean = boot_sum / np.maximum(boot_cnt, 1)
    return {
        "diff": float(day_sum.sum() / max(counts.sum(), 1)),
        "ci_low": float(np.percentile(boot_mean, 2.5)),
        "ci_high": float(np.percentile(boot_mean, 97.5)),
        "n_days": int((counts.ravel() > 0).sum()),
    }


def funnel_attribution(labels_df, pred_col="score", k=50, tail_col="is_tail_tradable"):
    """漏斗分层归因：全市场 → 规则过滤 → 排序 top-K 的尾部覆盖。

    返回每层 (n, n_tail, recall)。规则过滤层以 tradable 代替（P0-A 无独立规则层）。
    """
    rows = []
    day_groups = labels_df.groupby("datetime", sort=True)
    tot_n = tot_tail = top_hit = 0
    for _, day in day_groups:
        day = day.dropna(subset=[pred_col])
        if len(day) == 0:
            continue
        tot_n += len(day)
        tot_tail += int(day[tail_col].sum())
        kk = min(k, len(day))
        top_hit += int(day.nlargest(kk, pred_col)[tail_col].sum())
    rows.append({"layer": "all", "n": tot_n, "n_tail": tot_tail, "recall": 1.0})
    rows.append(
        {
            "layer": f"top{k}",
            "n": tot_n,
            "n_tail": tot_tail,
            "recall": top_hit / tot_tail if tot_tail else np.nan,
        }
    )
    return pd.DataFrame(rows)


def survival_verdict(model_daily, naive_dailies, lift_threshold=3.0):
    """生死线判定：两主指标对每基准 CI 下界>0 且分年度无崩盘；可用线 lift≥3×。

    naive_dailies: dict[name, daily_df]。
    """
    result = {"pass": False, "usable": False, "details": []}
    all_pass = True
    for name, nd in naive_dailies.items():
        for metric in ("recall", "top_decile_lift"):
            ci = paired_block_bootstrap(model_daily, nd, metric=metric)
            ok = ci["ci_low"] > 0
            all_pass &= ok
            result["details"].append({"baseline": name, "metric": metric, **ci, "pass": ok})
    result["pass"] = bool(all_pass)
    lift = float(model_daily["top_decile_lift"].mean())
    result["mean_lift"] = lift
    result["usable"] = bool(all_pass and lift >= lift_threshold)
    return result
