# -*- coding: utf-8 -*-
"""
extract_tail_features.py — ticai-daban 事件库 → 逐票逐日尾部特征表（供 P0-A 尾部二分类 H1 retry）

作用:
  从 ticai-daban 的涨停事件库/题材日度快照/龙头因子日表派生"强信号源"特征,
  输出 (trade_date, ts_code) 粒度的 parquet, 供 alpha158 特征矩阵左连接。
  全部特征只使用 t 日收盘及以前的信息(盘后出信号、预测 t+1 口径)。

用法:
  python extract_tail_features.py --ticai_root /path/to/ticai-daban \
      --out_dir tmp --start 2021-01-01 --end 2026-11-20
  # 如需与既有训练宇宙逐行对齐, 传 --grid_parquet (列: trade_date, ts_code)

特征分组(对应 H1 retry 的 A/B 档):
  A 档(事件基因, 覆盖 2019-11 起全窗口):
    zt_cnt_20 / zt_cnt_60      近 20/60 交易日涨停次数
    yizi_cnt_20                近 20 日一字板次数
    open_times_mean_20         近 20 日事件平均开板次数(炸板代理)
    limit_times_last           最近一次涨停的连板数(从未涨停=0)
    days_since_zt              距最近涨停的交易日数(从未=999)
    first_min_mean_20          近 20 日平均首封时间(分钟, 9:25=0)
    fd_ratio_20                近 20 日封单额/成交额 均值
    t1_close_ret_mean_20       近 20 日"已成熟"事件的 T+1 收盘收益均值
    t1_close_ret_last          最近一次已成熟事件的 T+1 收盘收益
    t1_open_ret_last           同上, 开盘口径
  B 档(题材卡位, 主覆盖 2023-11 起, 前段填 0/NaN):
    theme_zt_cnt               归属题材当日独占涨停家数
    theme_rank_pct             该数在当日全部题材的分位(0~1)
    theme_max_height           题材内最高连板
    theme_age                  题材连续活跃天数
    theme_is_leader            是否该题材当日龙头(0/1)
  C 档(龙头因子):
    qscore / sscore            factor.longtou 原值(未做 ST 修正, 见下)

易错点(口径红线, 改代码必读):
  1. t1_* 一族是 T+1 才成熟的收益 → 特征日 t 只能用 trade_date <= t-1 的事件。
     实现为事件收益序列 shift(1 个交易日) 后再滚动, 切勿去掉 shift(前视泄漏)。
  2. 窗口是"交易日窗"(按网格位置滚动), 不是自然日。
  3. 股票→题材映射主链路用 theme.attribution(逐日独占归属, PIT 干净);
     从未上过榜的股票回退 theme.members 当前快照——该快照无日期维度,
     存在成分漂移, B 档实验结论需披露此口径。
  4. is_st 来自事件日 name 快照, 只覆盖上过榜的股票; 全市场 PIT ST 需
     tushare namechange 补历史(见设计方案 H3), 本表不做全市场 ST。
  5. factor.longtou 的涨跌停价近似"未做 ST 修正"(其源码自注), qscore/sscore
     对 ST 票有偏, 可在下游按 is_st_last 或外部 ST 表剔除。
  6. B 档覆盖起点: 同花顺投票段 2023-11 / 开盘啦直标段 2024-01; 更早的
     theme 特征为填充值, 消融时 A/B 档分开报, 不要混窗比较。

产物:
  {out_dir}/tail_features.parquet     特征表
  {out_dir}/tail_features_manifest.json  覆盖率/行数/日期范围(留档, 供报告引用)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# ticai-daban 的数据读取唯一出处是它的 datastore(禁止硬编码 data/ 路径)
FEATURE_COLUMNS = [
    # A 档
    "zt_cnt_20", "zt_cnt_60", "yizi_cnt_20", "open_times_mean_20",
    "limit_times_last", "days_since_zt", "first_min_mean_20", "fd_ratio_20",
    "t1_close_ret_mean_20", "t1_close_ret_last", "t1_open_ret_last",
    # B 档
    "theme_zt_cnt", "theme_rank_pct", "theme_max_height", "theme_age",
    "theme_is_leader",
    # C 档
    "qscore", "sscore",
]
NEVER_LIMITED_SENTINEL = 999.0


def _load_ticai(ticai_root: str):
    root = Path(ticai_root).resolve()
    if not (root / "datastore.py").exists():
        raise SystemExit(f"ticai_root 下没有 datastore.py: {root}")
    sys.path.insert(0, str(root))
    from datastore import load  # noqa: E402
    return load


def _require_columns(df: pd.DataFrame, cols: list[str], table: str) -> None:
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise SystemExit(f"{table} 缺列 {missing}; 实际列: {list(df.columns)}")


def _build_grid(load, start: str, end: str, grid_parquet: str) -> pd.DataFrame:
    """基础网格: trade_date × ts_code。默认全 A 面板; 可传外部网格与训练宇宙对齐。"""
    if grid_parquet:
        grid = pd.read_parquet(grid_parquet, columns=["trade_date", "ts_code"])
    else:
        grid = load("market.daily_panel", columns=["trade_date", "ts_code"])
    grid["trade_date"] = pd.to_datetime(grid["trade_date"])
    grid = grid.drop_duplicates().sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    lo = pd.Timestamp(start) if start else grid["trade_date"].min()
    hi = pd.Timestamp(end) if end else grid["trade_date"].max()
    grid = grid[(grid["trade_date"] >= lo) & (grid["trade_date"] <= hi)]
    return grid.reset_index(drop=True)


def _event_features(grid_stock: pd.DataFrame, ev: pd.DataFrame) -> pd.DataFrame:
    """单只股票的 A 档特征。grid_stock: 该票网格(按日升序); ev: 该票事件(升序)。"""
    out = pd.DataFrame({"trade_date": grid_stock["trade_date"].values,
                        "ts_code": grid_stock["ts_code"].values})
    dates = out["trade_date"].values
    n = len(out)

    def align(col_values, on_dates, fill=0.0):
        vals = np.asarray(col_values, dtype=float)   # 剥掉原索引, 防按标签重索引
        s = pd.Series(vals, index=pd.DatetimeIndex(on_dates))
        s = s[~s.index.duplicated(keep="last")].reindex(pd.DatetimeIndex(dates))
        s = s.fillna(fill)
        s.index = pd.RangeIndex(len(s))   # 与 out 的 RangeIndex 对齐, 防赋值错位
        return s

    is_zt = (ev["tag"] == "涨停").astype(int) if "tag" in ev else pd.Series(1, index=ev.index)
    zt = ev[is_zt.values == 1] if len(ev) else ev

    zt_flag = align(np.ones(len(zt)), zt["trade_date"]) if len(zt) \
        else pd.Series(0.0, index=range(n))
    out["zt_cnt_20"] = zt_flag.rolling(20, min_periods=1).sum()
    out["zt_cnt_60"] = zt_flag.rolling(60, min_periods=1).sum()

    yizi_flag = align(zt["is_yizi"].astype(float).values, zt["trade_date"]) if len(zt) \
        else pd.Series(0.0, index=range(n))
    out["yizi_cnt_20"] = yizi_flag.rolling(20, min_periods=1).sum()

    ot = align(zt["open_times"].astype(float).values, zt["trade_date"], fill=np.nan) if len(zt) \
        else pd.Series(np.nan, index=range(n))
    out["open_times_mean_20"] = ot.rolling(20, min_periods=1).mean()  # 只对事件日求均值

    # 最近一次涨停的连板数 / 距最近涨停交易日数(位置滚动)
    if len(zt):
        lt = align(zt["limit_times"].astype(float).values, zt["trade_date"], fill=np.nan)
        out["limit_times_last"] = lt.ffill().fillna(0.0).to_numpy()
        pos = pd.Series(np.arange(n), index=pd.DatetimeIndex(dates))
        zt_pos = pos.reindex(pd.DatetimeIndex(zt["trade_date"])).dropna().values
        last_idx = pd.Series(zt_pos, index=zt_pos)  # 占位, 下面用 searchsorted
        sp = np.searchsorted(zt_pos, np.arange(n), side="right") - 1
        out["days_since_zt"] = np.where(
            sp >= 0, np.arange(n) - zt_pos[np.maximum(sp, 0)], NEVER_LIMITED_SENTINEL)
    else:
        out["limit_times_last"] = 0.0
        out["days_since_zt"] = float(NEVER_LIMITED_SENTINEL)

    fm = align(zt["first_min"].astype(float).values, zt["trade_date"], fill=np.nan) if len(zt) \
        else pd.Series(np.nan, index=range(n))
    out["first_min_mean_20"] = fm.rolling(20, min_periods=1).mean()

    if len(zt):
        ratio = (zt["fd_amount"].astype(float) / zt["amount"].astype(float).replace(0, np.nan))
        fr = align(ratio.values, zt["trade_date"], fill=np.nan)
    else:
        fr = pd.Series(np.nan, index=range(n))
    out["fd_ratio_20"] = fr.rolling(20, min_periods=1).mean()

    # T+1 收益族: 只用 trade_date <= t-1 的事件 → 事件收益对齐网格后 shift(1) 再滚动
    if len(zt):
        t1c = align(zt["next_close_ret"].astype(float).values, zt["trade_date"], fill=np.nan)
        t1o = align(zt["next_open_ret"].astype(float).values, zt["trade_date"], fill=np.nan)
    else:
        t1c = pd.Series(np.nan, index=range(n))
        t1o = pd.Series(np.nan, index=range(n))
    t1c_known = t1c.shift(1)   # 事件次日收益, 次一交易日起才可见(易错点1)
    t1o_known = t1o.shift(1)
    out["t1_close_ret_mean_20"] = t1c_known.rolling(20, min_periods=1).mean()
    out["t1_close_ret_last"] = t1c_known.ffill()
    out["t1_open_ret_last"] = t1o_known.ffill()

    # ST 标记(覆盖有限, 易错点4): 事件日 name 快照 ffill
    if len(ev):
        st = align(ev["is_st"].astype(float).values, ev["trade_date"], fill=np.nan)
        out["is_st_last"] = st.ffill()
    else:
        out["is_st_last"] = np.nan
    return out


def _theme_features(grid: pd.DataFrame, load) -> pd.DataFrame:
    """B 档: 逐日归属(theme.attribution, PIT) 主链路, members 快照回退。"""
    day = load("theme.day")
    _require_columns(day, ["trade_date", "concept_code", "zt_cnt", "max_height",
                           "theme_age", "leader_code"], "theme.day")
    day["trade_date"] = pd.to_datetime(day["trade_date"])
    day["theme_rank_pct"] = day.groupby("trade_date")["zt_cnt"].rank(pct=True)

    att = load("theme.attribution", columns=["trade_date", "ts_code", "concept_code"])
    att["trade_date"] = pd.to_datetime(att["trade_date"])
    att = att.sort_values(["ts_code", "trade_date"])
    # 逐日归属 ffill: 每票每天用"最近一次事件的归属题材"
    concept_last = att.groupby("ts_code").apply(
        lambda g: g.set_index("trade_date")["concept_code"], include_groups=False)

    rows = []
    day_idx = day.set_index(["trade_date", "concept_code"])
    for code, grp in grid.groupby("ts_code", sort=False):
        g = grp.sort_values("trade_date")
        if code in concept_last.index:
            cmap = concept_last.loc[code]
            if isinstance(cmap, pd.Series):
                c = cmap.reindex(pd.DatetimeIndex(g["trade_date"])).ffill()
            else:  # 单行退化
                c = pd.Series(cmap, index=pd.DatetimeIndex(g["trade_date"]))
        else:
            c = pd.Series(None, index=pd.DatetimeIndex(g["trade_date"]))
        sub = day_idx.reindex(
            pd.MultiIndex.from_arrays([g["trade_date"].values, c.values]))
        rows.append(pd.DataFrame({
            "trade_date": g["trade_date"].values, "ts_code": code,
            "theme_zt_cnt": sub["zt_cnt"].values,
            "theme_rank_pct": sub["theme_rank_pct"].values,
            "theme_max_height": sub["max_height"].values,
            "theme_age": sub["theme_age"].values,
            "theme_is_leader": (sub["leader_code"].values == code),
        }))
    out = pd.concat(rows, ignore_index=True)
    for col in ["theme_zt_cnt", "theme_max_height", "theme_age"]:
        out[col] = out[col].fillna(0.0)
    out["theme_rank_pct"] = out["theme_rank_pct"].fillna(0.0)
    out["theme_is_leader"] = out["theme_is_leader"].astype(float)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="ticai-daban → 尾部特征表")
    ap.add_argument("--ticai_root", required=True, help="ticai-daban 仓库根目录")
    ap.add_argument("--out_dir", default="tmp", help="产物目录(默认 tmp/)")
    ap.add_argument("--start", default="", help="起始日 YYYY-MM-DD, 空=全量")
    ap.add_argument("--end", default="", help="截止日, 空=全量")
    ap.add_argument("--grid_parquet", default="",
                    help="外部网格 parquet(列 trade_date,ts_code), 空=用市场面板全 A")
    args = ap.parse_args()

    load = _load_ticai(args.ticai_root)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    grid = _build_grid(load, args.start, args.end, args.grid_parquet)
    print(f"网格: {grid['trade_date'].min().date()}~{grid['trade_date'].max().date()} "
          f"{grid['ts_code'].nunique()} 票 {len(grid)} 行")

    ev = load("limitup.events_enriched")
    _require_columns(ev, ["trade_date", "ts_code", "limit_times", "first_min",
                          "open_times", "is_yizi", "is_st", "fd_amount", "amount",
                          "next_close_ret", "next_open_ret"], "limitup.events_enriched")
    ev["trade_date"] = pd.to_datetime(ev["trade_date"])
    ev = ev[ev["trade_date"] <= grid["trade_date"].max()]

    parts = []
    ev_by_code = {c: g.sort_values("trade_date") for c, g in ev.groupby("ts_code")}
    for code, grp in grid.groupby("ts_code", sort=False):
        parts.append(_event_features(grp.sort_values("trade_date"),
                                     ev_by_code.get(code, ev.iloc[0:0])))
    feat = pd.concat(parts, ignore_index=True)

    feat = feat.merge(_theme_features(grid, load), on=["trade_date", "ts_code"], how="left")

    lt = load("factor.longtou", columns=["trade_date", "ts_code", "qscore", "sscore"])
    lt["trade_date"] = pd.to_datetime(lt["trade_date"])
    lt = lt.drop_duplicates(["trade_date", "ts_code"])
    feat = feat.merge(lt, on=["trade_date", "ts_code"], how="left")

    feat = feat.sort_values(["trade_date", "ts_code"]).reset_index(drop=True)
    fp = out_dir / "tail_features.parquet"
    feat.to_parquet(fp, index=False)

    manifest = {
        "rows": int(len(feat)),
        "stocks": int(feat["ts_code"].nunique()),
        "date_min": str(feat["trade_date"].min().date()),
        "date_max": str(feat["trade_date"].max().date()),
        "feature_columns": FEATURE_COLUMNS,
        "coverage_nonzero": {
            c: float((feat[c].fillna(0) != 0).mean()) for c in FEATURE_COLUMNS
        },
        "note": "is_st_last 为附带列(覆盖有限, 非特征); 口径红线见脚本头部注释",
    }
    (out_dir / "tail_features_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"产物: {fp}  覆盖率: "
          + " ".join(f"{c}={manifest['coverage_nonzero'][c]:.0%}"
                     for c in ("zt_cnt_20", "theme_zt_cnt", "qscore")))


if __name__ == "__main__":
    main()
