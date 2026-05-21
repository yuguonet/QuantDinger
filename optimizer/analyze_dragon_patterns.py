"""
连板股形态分析 — 读取 dragon_ohlcv.csv，横向分析两个关键窗口

窗口1: 起始日(第一板前一日) → 第2板 — 起涨阶段有什么共同特征
窗口2: 最高点前2天 → 最高点后1天 — 见顶阶段有什么共同特征

用法:
    python analyze_dragon_patterns.py                           # 默认路径
    python analyze_dragon_patterns.py --csv path/to/file.csv    # 自定义路径
    python analyze_dragon_patterns.py --min-streak 3            # 只看 ≥3 板
"""
from __future__ import annotations

import os
import sys
import argparse
from typing import Dict, List, Any, Optional, Tuple
from collections import defaultdict

import pandas as pd
import numpy as np


# ================================================================
# 数据加载 & 分组
# ================================================================

def load_and_group(csv_path: str) -> Dict[str, pd.DataFrame]:
    """
    读取 dragon_ohlcv.csv，按 (code, run_first_limit_date) 分组。

    Returns:
        {(code, first_limit_date): DataFrame, ...}
        DataFrame 按 time 排序，保留所有原始列
    """
    df = pd.read_csv(csv_path)
    df["time"] = pd.to_datetime(df["time"])
    df = df.sort_values(["code", "run_first_limit_date", "time"])

    groups = {}
    for (code, fld), g in df.groupby(["code", "run_first_limit_date"]):
        g = g.reset_index(drop=True)
        groups[(code, fld)] = g

    return groups


# ================================================================
# 特征提取
# ================================================================

def extract_window_features(
    segment: pd.DataFrame,
    label: str = "",
) -> Dict[str, Any]:
    """
    从一个时间窗口的 OHLCV 数据中提取特征。

    Args:
        segment: 该窗口的 DataFrame（至少 2 行）
        label: 标记用

    Returns:
        特征字典
    """
    if len(segment) < 2:
        return {}

    close = segment["close"].values
    high = segment["high"].values
    low = segment["low"].values
    volume = segment["volume"].values.astype(float)
    opens = segment["open"].values

    features = {}

    # ── 收益 ──
    total_return = (close[-1] / close[0] - 1) * 100
    features["total_return_pct"] = round(total_return, 2)

    # 逐日收益率
    daily_returns = np.diff(close) / close[:-1] * 100
    features["n_days"] = len(segment)
    features["avg_daily_return_pct"] = round(float(np.mean(daily_returns)), 2)
    features["max_daily_return_pct"] = round(float(np.max(daily_returns)), 2)
    features["min_daily_return_pct"] = round(float(np.min(daily_returns)), 2)
    features["daily_return_std"] = round(float(np.std(daily_returns)), 2)

    # ── 振幅 ──
    amplitudes = (high - low) / np.where(close > 0, close, 1) * 100
    features["avg_amplitude_pct"] = round(float(np.mean(amplitudes)), 2)
    features["max_amplitude_pct"] = round(float(np.max(amplitudes)), 2)

    # ── 成交量 ──
    if len(volume) >= 2:
        vol_first_half = np.mean(volume[:max(1, len(volume) // 2)])
        vol_second_half = np.mean(volume[max(1, len(volume) // 2):])
        features["vol_ratio_2nd_vs_1st"] = round(
            float(vol_second_half / vol_first_half), 2) if vol_first_half > 0 else 0

        # 量能趋势：volume 的线性斜率
        vol_x = np.arange(len(volume))
        vol_slope = np.polyfit(vol_x, volume, 1)[0]
        features["vol_trend_slope"] = round(float(vol_slope / np.mean(volume)), 4) if np.mean(volume) > 0 else 0

        # 最后一天 vs 第一天量比
        features["vol_last_vs_first"] = round(
            float(volume[-1] / volume[0]), 2) if volume[0] > 0 else 0

    # ── 缺口 ──
    if len(segment) >= 2:
        gaps = (opens[1:] - close[:-1]) / close[:-1] * 100
        features["n_up_gaps"] = int(np.sum(gaps > 1.0))
        features["n_down_gaps"] = int(np.sum(gaps < -1.0))
        features["avg_gap_pct"] = round(float(np.mean(gaps)), 2)
        features["max_gap_pct"] = round(float(np.max(gaps)), 2)

    # ── K 线形态 ──
    # 实体比（实体 / 全天振幅）
    bodies = np.abs(close - opens)
    full_ranges = high - low
    full_ranges = np.where(full_ranges > 0, full_ranges, 0.001)
    body_ratios = bodies / full_ranges
    features["avg_body_ratio"] = round(float(np.mean(body_ratios)), 3)

    # 阳线占比
    features["bullish_pct"] = round(float(np.mean(close > opens)) * 100, 1)

    # 上影线占比（(high - max(open,close)) / (high-low)）
    upper_shadows = (high - np.maximum(close, opens)) / full_ranges
    features["avg_upper_shadow_pct"] = round(float(np.mean(upper_shadows)) * 100, 1)

    # 下影线占比
    lower_shadows = (np.minimum(close, opens) - low) / full_ranges
    features["avg_lower_shadow_pct"] = round(float(np.mean(lower_shadows)) * 100, 1)

    return features


def extract_run_features(
    group: pd.DataFrame,
) -> Dict[str, Any]:
    """
    从一个连板组中提取两个窗口的特征。

    窗口1: 起始日 → 第2板（含）
    窗口2: 最高点前2天 → 最高点后1天（含）
    """
    result = {}

    # 元数据
    first_row = group.iloc[0]
    result["code"] = first_row["code"]
    result["board"] = first_row.get("board", "")
    result["run_n_limit_ups"] = int(first_row["run_n_limit_ups"])
    result["run_max_consecutive"] = int(first_row["run_max_consecutive"])
    result["first_limit_date"] = first_row["run_first_limit_date"]
    result["last_limit_date"] = first_row["run_last_limit_date"]
    result["start_date"] = first_row["start_date"]
    result["peak_date"] = first_row["peak_date"]
    result["peak_price"] = float(first_row["peak_price"])

    # ── 找起始日和第2板的位置 ──
    start_date = pd.Timestamp(first_row["start_date"])
    first_limit_date = pd.Timestamp(first_row["run_first_limit_date"])

    # 起始日在 group 中的位置
    start_mask = group["time"] == start_date
    first_limit_mask = group["time"] == first_limit_date

    if not start_mask.any():
        return result  # 起始日不在数据中

    start_idx = group.index[start_mask][0]
    first_limit_loc = group.index[first_limit_mask][0] if first_limit_mask.any() else None

    # 第2板 = 起始日后的第 2 个涨停日
    # 从 first_limit_date 往后找第二个涨停日
    threshold = 0.198 if str(first_row.get("board", "")) in ("创业板", "科创板") else 0.098
    returns = group["close"].pct_change()
    is_limit_up = returns >= threshold

    # 找所有涨停日
    limit_up_dates = group.loc[is_limit_up, "time"].tolist()

    if len(limit_up_dates) < 2:
        # 不到 2 板，跳过窗口 1
        pass
    else:
        second_limit_date = limit_up_dates[1]  # 第 2 个涨停日

        # 窗口 1: 起始日 → 第 2 板
        w1_mask = (group["time"] >= start_date) & (group["time"] <= second_limit_date)
        w1 = group.loc[w1_mask]
        if len(w1) >= 2:
            f1 = extract_window_features(w1, "起涨窗口")
            for k, v in f1.items():
                result[f"w1_{k}"] = v
            result["w1_start_date"] = start_date.strftime("%Y-%m-%d")
            result["w1_end_date"] = second_limit_date.strftime("%Y-%m-%d")
        else:
            result["w1_start_date"] = start_date.strftime("%Y-%m-%d")
            result["w1_end_date"] = second_limit_date.strftime("%Y-%m-%d")

    # ── 找最高点位置 ──
    peak_date = pd.Timestamp(first_row["peak_date"])
    peak_mask = group["time"] == peak_date

    if peak_mask.any():
        peak_loc = group.index[peak_mask][0]
        peak_pos = group.index.get_loc(peak_loc)

        # 窗口 2: 最高点前2天 → 最高点后1天
        w2_start = max(0, peak_pos - 2)
        w2_end = min(len(group) - 1, peak_pos + 1)
        w2 = group.iloc[w2_start:w2_end + 1]

        if len(w2) >= 2:
            f2 = extract_window_features(w2, "见顶窗口")
            for k, v in f2.items():
                result[f"w2_{k}"] = v
            result["w2_start_date"] = w2.iloc[0]["time"].strftime("%Y-%m-%d")
            result["w2_end_date"] = w2.iloc[-1]["time"].strftime("%Y-%m-%d")

            # 额外：最高点当天是否涨停
            peak_day = group.iloc[peak_pos]
            peak_return = returns.iloc[peak_pos] if peak_pos < len(returns) else 0
            result["w2_peak_day_return_pct"] = round(float(peak_return) * 100, 2) if not pd.isna(peak_return) else 0
            result["w2_peak_is_limit_up"] = bool(peak_return >= threshold) if not pd.isna(peak_return) else False

            # 最高点后一天
            if peak_pos + 1 < len(group):
                next_day = group.iloc[peak_pos + 1]
                next_return = returns.iloc[peak_pos + 1] if peak_pos + 1 < len(returns) else 0
                result["w2_next_day_return_pct"] = round(float(next_return) * 100, 2) if not pd.isna(next_return) else 0
                result["w2_next_day_is_limit_down"] = bool(next_return <= -threshold) if not pd.isna(next_return) else False
        else:
            result["w2_start_date"] = group.iloc[w2_start]["time"].strftime("%Y-%m-%d")
            result["w2_end_date"] = group.iloc[w2_end]["time"].strftime("%Y-%m-%d")

    return result


# ================================================================
# 横向汇总统计
# ================================================================

def summarize_window(
    df_features: pd.DataFrame,
    prefix: str,
    window_name: str,
):
    """打印某个窗口的横向统计"""
    cols = [c for c in df_features.columns if c.startswith(f"{prefix}_")]
    if not cols:
        print(f"\n  ❌ {window_name}: 无数据")
        return

    print(f"\n{'─'*60}")
    print(f"  {window_name} (样本: {len(df_features)} 个)")
    print(f"{'─'*60}")

    # 收益
    ret_col = f"{prefix}_total_return_pct"
    if ret_col in df_features.columns:
        vals = df_features[ret_col].dropna()
        print(f"\n  📈 总收益:")
        print(f"     均值: {vals.mean():+.1f}%  中位数: {vals.median():+.1f}%  "
              f"标准差: {vals.std():.1f}%")
        print(f"     >0%: {(vals > 0).mean()*100:.0f}%  >10%: {(vals > 10).mean()*100:.0f}%  "
              f">20%: {(vals > 20).mean()*100:.0f}%")

    # 日均收益
    avg_ret = f"{prefix}_avg_daily_return_pct"
    if avg_ret in df_features.columns:
        vals = df_features[avg_ret].dropna()
        print(f"\n  📊 日均收益: 均值={vals.mean():+.2f}%  中位数={vals.median():+.2f}%")

    # 天数
    ndays = f"{prefix}_n_days"
    if ndays in df_features.columns:
        vals = df_features[ndays].dropna()
        print(f"\n  📅 窗口天数: 均值={vals.mean():.1f}  中位数={vals.median():.0f}  "
              f"范围=[{vals.min():.0f}, {vals.max():.0f}]")

    # 振幅
    amp = f"{prefix}_avg_amplitude_pct"
    if amp in df_features.columns:
        vals = df_features[amp].dropna()
        print(f"\n  📐 日均振幅: 均值={vals.mean():.1f}%  中位数={vals.median():.1f}%")

    # 量能
    vr = f"{prefix}_vol_ratio_2nd_vs_1st"
    if vr in df_features.columns:
        vals = df_features[vr].dropna()
        print(f"\n  📦 量能(后半/前半): 均值={vals.mean():.2f}x  中位数={vals.median():.2f}x  "
              f"放量(>1.5x): {(vals > 1.5).mean()*100:.0f}%")

    vt = f"{prefix}_vol_trend_slope"
    if vt in df_features.columns:
        vals = df_features[vt].dropna()
        print(f"     量能趋势斜率: 均值={vals.mean():+.4f}  (正=放量, 负=缩量)")

    # 缺口
    ug = f"{prefix}_n_up_gaps"
    if ug in df_features.columns:
        vals = df_features[ug].dropna()
        print(f"\n  🔺 向上跳空: 均值={vals.mean():.1f}次  有跳空: {(vals > 0).mean()*100:.0f}%")

    # K 线形态
    bp = f"{prefix}_bullish_pct"
    if bp in df_features.columns:
        vals = df_features[bp].dropna()
        print(f"\n  🕯️ 阳线占比: 均值={vals.mean():.0f}%")

    br = f"{prefix}_avg_body_ratio"
    if br in df_features.columns:
        vals = df_features[br].dropna()
        print(f"     实体比: 均值={vals.mean():.3f}  (1.0=光头光脚)")

    us = f"{prefix}_avg_upper_shadow_pct"
    if us in df_features.columns:
        vals = df_features[us].dropna()
        print(f"     上影线占比: 均值={vals.mean():.1f}%")

    ls = f"{prefix}_avg_lower_shadow_pct"
    if ls in df_features.columns:
        vals = df_features[ls].dropna()
        print(f"     下影线占比: 均值={vals.mean():.1f}%")

    # 按连板数分组
    if "run_n_limit_ups" in df_features.columns:
        print(f"\n  📊 按连板数分组:")
        for n in sorted(df_features["run_n_limit_ups"].unique()):
            sub = df_features[df_features["run_n_limit_ups"] == n]
            ret_vals = sub[f"{prefix}_total_return_pct"].dropna() if f"{prefix}_total_return_pct" in sub.columns else pd.Series()
            if len(ret_vals) > 0:
                print(f"     {n}板 ({len(sub)}个): 收益均值={ret_vals.mean():+.1f}%  "
                      f"中位数={ret_vals.median():+.1f}%")


def summarize_peak_extras(df_features: pd.DataFrame):
    """打印见顶窗口的额外统计"""
    print(f"\n  🔍 最高点当天:")
    col = "w2_peak_day_return_pct"
    if col in df_features.columns:
        vals = df_features[col].dropna()
        print(f"     涨幅均值: {vals.mean():+.2f}%  中位数: {vals.median():+.2f}%")
        lu = "w2_peak_is_limit_up"
        if lu in df_features.columns:
            print(f"     涨停占比: {df_features[lu].mean()*100:.0f}%")

    col2 = "w2_next_day_return_pct"
    if col2 in df_features.columns:
        vals = df_features[col2].dropna()
        print(f"\n  🔍 最高点后一天:")
        print(f"     涨幅均值: {vals.mean():+.2f}%  中位数: {vals.median():+.2f}%")
        print(f"     下跌占比: {(vals < 0).mean()*100:.0f}%")
        ld = "w2_next_day_is_limit_down"
        if ld in df_features.columns:
            print(f"     跌停占比: {df_features[ld].mean()*100:.0f}%")


# ================================================================
# 主流程
# ================================================================

def analyze(csv_path: str, min_streak: int = 0):
    print(f"\n{'='*70}")
    print(f"  连板股形态分析")
    print(f"  文件: {csv_path}")
    print(f"{'='*70}")

    groups = load_and_group(csv_path)
    print(f"\n   加载 {len(groups)} 个连板组")

    if min_streak > 0:
        groups = {k: v for k, v in groups.items()
                  if int(v.iloc[0]["run_n_limit_ups"]) >= min_streak}
        print(f"   ≥{min_streak}板过滤后: {len(groups)} 个")

    # 提取所有组的特征
    all_features = []
    for key, group in groups.items():
        feat = extract_run_features(group)
        if feat:
            all_features.append(feat)

    if not all_features:
        print("\n❌ 无有效数据")
        return

    df_feat = pd.DataFrame(all_features)
    print(f"   有效样本: {len(df_feat)} 个")

    # ── 窗口 1: 起涨阶段 ──
    summarize_window(df_feat, "w1", "窗口1: 起始日 → 第2板（起涨阶段）")

    # ── 窗口 2: 见顶阶段 ──
    summarize_window(df_feat, "w2", "窗口2: 最高点前2天 → 最高点后1天（见顶阶段）")
    summarize_peak_extras(df_feat)

    # ── 对比 ──
    print(f"\n{'─'*60}")
    print(f"  对比: 起涨 vs 见顶")
    print(f"{'─'*60}")

    pairs = [
        ("总收益", "total_return_pct"),
        ("日均振幅", "avg_amplitude_pct"),
        ("量能比(后/前)", "vol_ratio_2nd_vs_1st"),
        ("阳线占比", "bullish_pct"),
        ("实体比", "avg_body_ratio"),
        ("上影线占比", "avg_upper_shadow_pct"),
    ]

    for name, suffix in pairs:
        w1_col = f"w1_{suffix}"
        w2_col = f"w2_{suffix}"
        if w1_col in df_feat.columns and w2_col in df_feat.columns:
            v1 = df_feat[w1_col].dropna()
            v2 = df_feat[w2_col].dropna()
            if len(v1) > 0 and len(v2) > 0:
                print(f"  {name:16s}  起涨={v1.mean():>8.2f}  见顶={v2.mean():>8.2f}")

    # ── 保存明细 ──
    out_dir = os.path.dirname(csv_path) or "."
    detail_path = os.path.join(out_dir, "dragon_pattern_features.csv")
    df_feat.to_csv(detail_path, index=False, encoding="utf-8-sig")
    print(f"\n💾 明细已保存: {detail_path}")

    return df_feat


# ================================================================
# CLI
# ================================================================

def main():
    parser = argparse.ArgumentParser(description="连板股形态分析")
    parser.add_argument("--csv", type=str,
                        default="analysis_output/dragon_ohlcv.csv",
                        help="dragon_ohlcv.csv 路径")
    parser.add_argument("--min-streak", type=int, default=0,
                        help="只分析 ≥N 板的组 (0=全部)")

    args = parser.parse_args()
    analyze(args.csv, min_streak=args.min_streak)


if __name__ == "__main__":
    main()
