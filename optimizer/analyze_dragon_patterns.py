"""
连板股形态分析 — 三个核心买/卖窗口

买点1: 第一板次日开盘 — 实际可买入位
买点2: 第一板前1-2日收盘 — 提前埋伏窗口
卖点:  最高点当天 — 目标出场位

用法:
    python analyze_dragon_patterns.py                           # 默认路径
    python analyze_dragon_patterns.py --csv path/to/file.csv    # 自定义路径
    python analyze_dragon_patterns.py --min-streak 3            # 只看 ≥3 板
    python analyze_dragon_patterns.py --post-days 3             # 只看 peak 后 3 天
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
    post_days: int = 5,
) -> Dict[str, Any]:
    """
    从一个连板组中提取三个核心窗口的特征。

    买点1 (w_b1): 第一板次日 — 实际可买入的第一个位置
    买点2 (w_b2): 第一板前1-2日 — 提前埋伏的信号窗口
    卖点  (w_sell): 最高点当天 — 目标出场位

    每个买点计算：从买入到最高点的收益、最大回撤、持仓天数
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
    result["end_date"] = first_row.get("end_date", "")

    # 涨停阈值
    board = str(first_row.get("board", ""))
    threshold = 0.198 if board in ("创业板", "科创板") else 0.098

    group = group.sort_values("time").reset_index(drop=True)
    close = group["close"].values.astype(float)
    opens = group["open"].values.astype(float)
    high = group["high"].values.astype(float)
    low = group["low"].values.astype(float)
    volume = group["volume"].values.astype(float)
    dates = group["time"].values
    n = len(group)

    # ── 定位关键日期 ──
    first_limit_date = pd.Timestamp(first_row["run_first_limit_date"])
    peak_date = pd.Timestamp(first_row["peak_date"])

    # 第一板在 group 中的位置
    fl_mask = group["time"] == first_limit_date
    if not fl_mask.any():
        return result
    fl_pos = int(group.index[fl_mask][0])

    # 最高点在 group 中的位置
    pk_mask = group["time"] == peak_date
    if not pk_mask.any():
        return result
    pk_pos = int(group.index[pk_mask][0])
    peak_close = close[pk_pos]

    # ================================================================
    # 买点1: 第一板次日（实际可买入位）
    # ================================================================
    b1_pos = fl_pos + 1  # 第一板次日
    if b1_pos < n and b1_pos <= pk_pos:
        b1_open = opens[b1_pos]
        b1_close = close[b1_pos]
        b1_high = high[b1_pos]
        b1_low = low[b1_pos]
        b1_vol = volume[b1_pos]

        # 以开盘价买入（假设集合竞价买入）
        b1_entry = b1_open
        # 到最高点的收益
        b1_to_peak_ret = (peak_close / b1_entry - 1) * 100 if b1_entry > 0 else 0
        # 到最高点的持仓天数
        b1_hold_days = pk_pos - b1_pos

        result["b1_date"] = pd.Timestamp(dates[b1_pos]).strftime("%Y-%m-%d")
        result["b1_entry_price"] = round(b1_open, 2)
        result["b1_close"] = round(b1_close, 2)
        result["b1_return_pct"] = round(b1_to_peak_ret, 2)
        result["b1_hold_days"] = b1_hold_days

        # 第一板次日当天表现
        result["b1_day_return_pct"] = round((b1_close / b1_open - 1) * 100, 2) if b1_open > 0 else 0
        result["b1_day_amplitude_pct"] = round((b1_high - b1_low) / b1_open * 100, 2) if b1_open > 0 else 0
        result["b1_is_limit_up"] = bool((b1_close / b1_open - 1) >= threshold) if b1_open > 0 else False
        result["b1_is_limit_down"] = bool((b1_close / b1_open - 1) <= -threshold) if b1_open > 0 else False
        # 高开幅度（vs 前一天收盘 = 第一板收盘）
        prev_close = close[fl_pos]
        result["b1_gap_pct"] = round((b1_open / prev_close - 1) * 100, 2) if prev_close > 0 else 0

        # 从买入到最高点之间的最大回撤
        if b1_pos < pk_pos:
            entry_to_peak_low = low[b1_pos:pk_pos + 1].min()
            b1_max_dd = (entry_to_peak_low / b1_entry - 1) * 100 if b1_entry > 0 else 0
            result["b1_max_drawdown_pct"] = round(b1_max_dd, 2)
        else:
            result["b1_max_drawdown_pct"] = 0

        # 买入后第2天、第3天表现（到peak前）
        for d in range(2, min(6, b1_hold_days + 1)):
            d_pos = b1_pos + d
            if d_pos <= pk_pos and d_pos < n:
                result[f"b1_d{d}_return_pct"] = round((close[d_pos] / b1_entry - 1) * 100, 2) if b1_entry > 0 else 0
            else:
                result[f"b1_d{d}_return_pct"] = None

    # ================================================================
    # 买点2: 第一板前1-2日（提前埋伏窗口）
    # ================================================================
    for offset, label in [(1, "b2a"), (2, "b2b")]:
        pos = fl_pos - offset
        if pos >= 0:
            entry_open = opens[pos]
            entry_close = close[pos]
            entry_high = high[pos]
            entry_low = low[pos]
            entry_vol = volume[pos]

            # 以收盘价买入（当日尾盘埋伏）
            entry_price = entry_close
            to_peak_ret = (peak_close / entry_price - 1) * 100 if entry_price > 0 else 0
            hold_days = pk_pos - pos

            result[f"{label}_date"] = pd.Timestamp(dates[pos]).strftime("%Y-%m-%d")
            result[f"{label}_entry_price"] = round(entry_close, 2)
            result[f"{label}_return_pct"] = round(to_peak_ret, 2)
            result[f"{label}_hold_days"] = hold_days

            # 当天K线形态
            day_range = entry_high - entry_low
            result[f"{label}_is_bullish"] = bool(entry_close > entry_open)
            result[f"{label}_body_ratio"] = round(abs(entry_close - entry_open) / day_range, 3) if day_range > 0 else 0
            result[f"{label}_amplitude_pct"] = round(day_range / entry_close * 100, 2) if entry_close > 0 else 0

            # 量比（vs 前5天均量）
            vol_window = volume[max(0, pos - 5):pos]
            avg_vol = vol_window.mean() if len(vol_window) > 0 else entry_vol
            result[f"{label}_vol_ratio"] = round(entry_vol / avg_vol, 2) if avg_vol > 0 else 0

            # 买入到最高点最大回撤
            if pos < pk_pos:
                low_slice = low[pos:pk_pos + 1]
                max_dd = (low_slice.min() / entry_price - 1) * 100 if entry_price > 0 else 0
                result[f"{label}_max_drawdown_pct"] = round(max_dd, 2)
            else:
                result[f"{label}_max_drawdown_pct"] = 0

            # 是否涨停前最后一天阴线
            result[f"{label}_is_red"] = bool(entry_close < entry_open)
        else:
            result[f"{label}_date"] = None
            result[f"{label}_return_pct"] = None

    # ================================================================
    # 卖点: 最高点当天
    # ================================================================
    pk_return = (close[pk_pos] / close[pk_pos - 1] - 1) * 100 if pk_pos > 0 else 0
    result["sell_date"] = peak_date.strftime("%Y-%m-%d")
    result["sell_price"] = round(peak_close, 2)
    result["sell_day_return_pct"] = round(float(pk_return), 2)
    result["sell_is_limit_up"] = bool(pk_return >= threshold) if not pd.isna(pk_return) else False
    result["sell_amplitude_pct"] = round((high[pk_pos] - low[pk_pos]) / peak_close * 100, 2) if peak_close > 0 else 0

    # 最高点当天上影线
    sell_range = high[pk_pos] - low[pk_pos]
    if sell_range > 0:
        upper_shadow = (high[pk_pos] - max(opens[pk_pos], close[pk_pos])) / sell_range
        result["sell_upper_shadow_pct"] = round(upper_shadow * 100, 1)
    else:
        result["sell_upper_shadow_pct"] = 0

    # 最高点后逐日走势（到 peak 后 post_days 天）
    for d in range(1, post_days + 1):
        d_pos = pk_pos + d
        if d_pos < n:
            day_ret = (close[d_pos] / peak_close - 1) * 100
            daily_ret = (close[d_pos] / close[d_pos - 1] - 1) * 100 if d_pos > 0 else 0
            result[f"sell_post_d{d}_return_pct"] = round(day_ret, 2)
            result[f"sell_post_d{d}_daily_pct"] = round(float(daily_ret), 2)
            result[f"sell_post_d{d}_is_limit_down"] = bool(daily_ret <= -threshold)
        else:
            result[f"sell_post_d{d}_return_pct"] = None
            result[f"sell_post_d{d}_daily_pct"] = None
            result[f"sell_post_d{d}_is_limit_down"] = None

    # peak 后最大回撤
    post_prices = close[pk_pos + 1:pk_pos + post_days + 1]
    if len(post_prices) > 0:
        min_post = float(post_prices.min())
        result["sell_post_max_drawdown_pct"] = round((min_post / peak_close - 1) * 100, 2)
        min_idx = int(post_prices.argmin()) + 1
        result["sell_post_min_day"] = min_idx
    else:
        result["sell_post_max_drawdown_pct"] = None
        result["sell_post_min_day"] = None

    # ================================================================
    # 从买点到卖点的完整收益链
    # ================================================================
    if b1_pos < n and b1_pos <= pk_pos:
        # 买点1（次日开盘）→ 卖点（最高点收盘）
        result["chain_b1_to_sell_pct"] = result["b1_return_pct"]
    if fl_pos - 1 >= 0:
        # 买点2a（前1日收盘）→ 卖点
        result["chain_b2a_to_sell_pct"] = result.get("b2a_return_pct")
    if fl_pos - 2 >= 0:
        # 买点2b（前2日收盘）→ 卖点
        result["chain_b2b_to_sell_pct"] = result.get("b2b_return_pct")

    return result


# ================================================================
# 横向汇总统计
# ================================================================

def summarize_buy1(df: pd.DataFrame):
    """买点1: 第一板次日"""
    print(f"\n{'─'*60}")
    n = df["b1_return_pct"].notna().sum() if "b1_return_pct" in df.columns else 0
    print(f"  买点1: 第一板次日开盘买入 → 最高点卖出 (n={n})")
    print(f"{'─'*60}")

    if n == 0:
        print("  ❌ 无数据")
        return

    # 核心收益
    ret = df["b1_return_pct"].dropna()
    print(f"\n  📈 收益（到最高点）:")
    print(f"     均值: {ret.mean():+.1f}%  中位数: {ret.median():+.1f}%  标准差: {ret.std():.1f}%")
    print(f"     >0%: {(ret > 0).mean()*100:.0f}%  >10%: {(ret > 10).mean()*100:.0f}%  "
          f">20%: {(ret > 20).mean()*100:.0f}%")

    # 持仓天数
    hd = df["b1_hold_days"].dropna()
    print(f"\n  📅 持仓天数: 均值={hd.mean():.1f}  中位数={hd.median():.0f}  "
          f"范围=[{hd.min():.0f}, {hd.max():.0f}]")

    # 最大回撤
    dd = df["b1_max_drawdown_pct"].dropna()
    print(f"\n  📉 买入后最大回撤:")
    print(f"     均值: {dd.mean():+.1f}%  中位数: {dd.median():+.1f}%")
    print(f"     <-5%: {(dd < -5).mean()*100:.0f}%  <-10%: {(dd < -10).mean()*100:.0f}%  "
          f"<-15%: {(dd < -15).mean()*100:.0f}%")

    # 第一板次日当天表现
    print(f"\n  🔍 第一板次日当天:")
    day_ret = df["b1_day_return_pct"].dropna()
    print(f"     涨幅: 均值={day_ret.mean():+.1f}%  中位数={day_ret.median():+.1f}%")

    gap = df["b1_gap_pct"].dropna()
    print(f"     高开幅度: 均值={gap.mean():+.1f}%  中位数={gap.median():+.1f}%")
    print(f"     高开>3%: {(gap > 3).mean()*100:.0f}%  高开>5%: {(gap > 5).mean()*100:.0f}%")

    if "b1_is_limit_up" in df.columns:
        print(f"     涨停: {df['b1_is_limit_up'].mean()*100:.0f}%")
    if "b1_is_limit_down" in df.columns:
        print(f"     跌停: {df['b1_is_limit_down'].mean()*100:.0f}%")

    # 按连板数分组
    if "run_n_limit_ups" in df.columns:
        print(f"\n  📊 按连板数分组:")
        for n_board in sorted(df["run_n_limit_ups"].unique()):
            sub = df[df["run_n_limit_ups"] == n_board]
            r = sub["b1_return_pct"].dropna()
            if len(r) > 0:
                dd_sub = sub["b1_max_drawdown_pct"].dropna()
                print(f"     {n_board}板 ({len(sub)}个): 收益={r.mean():+.1f}%  "
                      f"回撤={dd_sub.mean():+.1f}%  胜率={( r > 0).mean()*100:.0f}%")


def summarize_buy2(df: pd.DataFrame):
    """买点2: 第一板前1-2日"""
    print(f"\n{'─'*60}")
    print(f"  买点2: 第一板前埋伏 → 最高点卖出")
    print(f"{'─'*60}")

    for label, name in [("b2a", "前1日收盘买入"), ("b2b", "前2日收盘买入")]:
        ret_col = f"{label}_return_pct"
        if ret_col not in df.columns:
            continue
        ret = df[ret_col].dropna()
        if len(ret) == 0:
            continue

        print(f"\n  📍 {name} (n={len(ret)}):")
        print(f"     收益: 均值={ret.mean():+.1f}%  中位数={ret.median():+.1f}%  "
              f"胜率={(ret > 0).mean()*100:.0f}%")

        dd_col = f"{label}_max_drawdown_pct"
        if dd_col in df.columns:
            dd = df[dd_col].dropna()
            print(f"     回撤: 均值={dd.mean():+.1f}%  <-10%: {(dd < -10).mean()*100:.0f}%")

        hd_col = f"{label}_hold_days"
        if hd_col in df.columns:
            hd = df[hd_col].dropna()
            print(f"     持仓: 均值={hd.mean():.1f}天")

        # K线形态
        br_col = f"{label}_is_red"
        if br_col in df.columns:
            red = df[br_col].dropna()
            if len(red) > 0:
                red_ret = df.loc[red[red].index, ret_col].dropna()
                green_ret = df.loc[red[~red].index, ret_col].dropna() if (~red).any() else pd.Series()
                print(f"     阴线日买入: {red.mean()*100:.0f}%")
                if len(red_ret) > 0 and len(green_ret) > 0:
                    print(f"     阴线收益={red_ret.mean():+.1f}%  阳线收益={green_ret.mean():+.1f}%")

        vr_col = f"{label}_vol_ratio"
        if vr_col in df.columns:
            vr = df[vr_col].dropna()
            print(f"     量比(vs前5日): 均值={vr.mean():.2f}x  放量(>1.5x): {(vr > 1.5).mean()*100:.0f}%")

    # 对比前1日 vs 前2日
    if "b2a_return_pct" in df.columns and "b2b_return_pct" in df.columns:
        a = df["b2a_return_pct"].dropna()
        b = df["b2b_return_pct"].dropna()
        if len(a) > 0 and len(b) > 0:
            print(f"\n  📊 对比: 前1日={a.mean():+.1f}% vs 前2日={b.mean():+.1f}%")


def summarize_sell(df: pd.DataFrame):
    """卖点: 最高点当天"""
    print(f"\n{'─'*60}")
    n = df["sell_date"].notna().sum() if "sell_date" in df.columns else 0
    print(f"  卖点: 最高点当天 (n={n})")
    print(f"{'─'*60}")

    if n == 0:
        print("  ❌ 无数据")
        return

    # 当天表现
    print(f"\n  🔍 最高点当天:")
    day_ret = df["sell_day_return_pct"].dropna()
    print(f"     涨幅: 均值={day_ret.mean():+.1f}%  中位数={day_ret.median():+.1f}%")

    if "sell_is_limit_up" in df.columns:
        print(f"     涨停占比: {df['sell_is_limit_up'].mean()*100:.0f}%")

    amp = df["sell_amplitude_pct"].dropna()
    print(f"     振幅: 均值={amp.mean():.1f}%")

    us = df["sell_upper_shadow_pct"].dropna()
    print(f"     上影线: 均值={us.mean():.1f}%  长上影(>30%): {(us > 30).mean()*100:.0f}%")

    # 后续走势
    print(f"\n  🔍 最高点后逐日走势:")
    for d in range(1, 6):
        col = f"sell_post_d{d}_return_pct"
        if col not in df.columns:
            continue
        vals = df[col].dropna()
        if len(vals) == 0:
            continue
        ld_col = f"sell_post_d{d}_is_limit_down"
        ld_pct = df[ld_col].dropna().mean() * 100 if ld_col in df.columns else 0
        down_pct = (vals < 0).mean() * 100
        print(f"     D+{d}: 均值={vals.mean():+.2f}%  中位数={vals.median():+.2f}%  "
              f"下跌={down_pct:.0f}%  跌停={ld_pct:.0f}%  (n={len(vals)})")

    # 最大回撤
    dd = df["sell_post_max_drawdown_pct"].dropna()
    if len(dd) > 0:
        print(f"\n  📉 最高点后最大回撤（{5}日内）:")
        print(f"     均值: {dd.mean():+.2f}%  中位数: {dd.median():+.2f}%")
        print(f"     <-5%: {(dd < -5).mean()*100:.0f}%  <-10%: {(dd < -10).mean()*100:.0f}%  "
              f"<-15%: {(dd < -15).mean()*100:.0f}%")

    md = df["sell_post_min_day"].dropna()
    if len(md) > 0:
        print(f"     最低点出现: D+{md.mean():.1f} (中位数)")
        from collections import Counter
        dist = Counter(md.astype(int))
        parts = [f"D+{k}={v}" for k, v in sorted(dist.items())]
        print(f"     分布: {'  '.join(parts)}")


# ================================================================
# 主流程
# ================================================================

def analyze(csv_path: str, min_streak: int = 0, post_days: int = 5):
    print(f"\n{'='*70}")
    print(f"  连板股形态分析 — 买点 / 卖点")
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
        feat = extract_run_features(group, post_days=post_days)
        if feat:
            all_features.append(feat)

    if not all_features:
        print("\n❌ 无有效数据")
        return

    df_feat = pd.DataFrame(all_features)
    print(f"   有效样本: {len(df_feat)} 个")

    # ── 买点1: 第一板次日 ──
    summarize_buy1(df_feat)

    # ── 买点2: 第一板前1-2日 ──
    summarize_buy2(df_feat)

    # ── 卖点: 最高点 ──
    summarize_sell(df_feat)

    # ── 完整收益链汇总 ──
    print(f"\n{'─'*60}")
    print(f"  📊 收益链汇总（买入 → 最高点卖出）")
    print(f"{'─'*60}")

    chains = [
        ("chain_b1_to_sell_pct", "1板次日开盘→最高点"),
        ("chain_b2a_to_sell_pct", "1板前1日收盘→最高点"),
        ("chain_b2b_to_sell_pct", "1板前2日收盘→最高点"),
    ]
    for col, name in chains:
        if col in df_feat.columns:
            vals = df_feat[col].dropna()
            if len(vals) > 0:
                print(f"  {name:24s}  均值={vals.mean():+.1f}%  中位数={vals.median():+.1f}%  "
                      f"胜率={(vals > 0).mean()*100:.0f}%  (n={len(vals)})")

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
    parser.add_argument("--post-days", type=int, default=5,
                        help="最高点后分析天数 (默认5)")

    args = parser.parse_args()
    analyze(args.csv, min_streak=args.min_streak, post_days=args.post_days)


if __name__ == "__main__":
    main()
