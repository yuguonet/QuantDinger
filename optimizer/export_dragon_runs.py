"""
连板股扫描 + OHLCV 数据导出

扫描全市场 db_market，找出 ≥2板连板股（允许 1-2 天洗盘日），
向后搜索 5 日最高点确定终止日，第一板前一日为起始日，
起始日再向前推 10 日为数据起点，导出完整 OHLCV 到 CSV。

用法:
    python export_dragon_runs.py                  # 全量扫描
    python export_dragon_runs.py --quick           # 抽样 500 只
    python export_dragon_runs.py --min-streak=3    # ≥3 板
    python export_dragon_runs.py --max-gap=2       # 允许 2 天洗盘（默认）
    python export_dragon_runs.py --start=2024-01-01 --end=2026-05-21
"""
from __future__ import annotations

import os
import sys
import argparse
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

import pandas as pd
import numpy as np

# 确保路径
_optimizer_dir = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.dirname(_optimizer_dir)
_backend_root = os.path.join(_project_root, "backend_api_python")
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def _load_env():
    try:
        from dotenv import load_dotenv
        for env_path in [
            os.path.join(_backend_root, '.env'),
            os.path.join(_project_root, '.env'),
        ]:
            if os.path.isfile(env_path):
                load_dotenv(env_path, override=False)
                break
    except Exception:
        pass


def _get_writer():
    _load_env()
    from app.utils.db_market import get_market_kline_writer
    return get_market_kline_writer()


def _get_mgr():
    _load_env()
    from app.utils.db_market import get_market_db_manager
    return get_market_db_manager()


# ================================================================
# 板块 & 涨停阈值
# ================================================================

def get_board(code: str) -> str:
    c = code[:3] if len(code) >= 3 else code
    if c.startswith("68"):
        return "科创板"
    elif c.startswith("30"):
        return "创业板"
    elif c.startswith(("8", "4")):
        return "北交所"
    elif c.startswith("6"):
        return "沪主板"
    elif c.startswith(("0", "2")):
        return "深主板"
    return "未知"


def limit_up_threshold(code: str) -> float:
    """创业板/科创板 20%，主板/北交所 10%"""
    board = get_board(code)
    if board in ("创业板", "科创板"):
        return 0.198
    return 0.098


# ================================================================
# 数据加载
# ================================================================

def load_daily(code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
    """从 db_market 加载日线数据"""
    writer = _get_writer()
    data = writer.query("CNStock", code, "1D",
                        start_time=start_date, end_time=end_date, limit=0)
    if not data:
        return None
    df = pd.DataFrame(data)
    if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"])
        df = df.set_index("time")
    df = df.sort_index()
    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def get_all_codes() -> list:
    writer = _get_writer()
    stats = writer.stats("CNStock")
    if not stats.get("exists"):
        print("❌ CNStock_db 不存在")
        sys.exit(1)
    return stats.get("symbol_list", [])


# ================================================================
# 连板检测核心
# ================================================================

def detect_limit_up_runs(
    df: pd.DataFrame,
    code: str,
    min_streak: int = 2,
    max_gap: int = 2,
) -> List[Dict[str, Any]]:
    """
    检测连板段。

    规则:
      - 连续涨停 >= min_streak 天
      - 允许中间有 max_gap 天非涨停（洗盘日）
      - 8天7板、10天8板等都算

    Returns:
      [{
        "first_limit_idx": int,      # 第一个涨停日的 index 位置
        "last_limit_idx": int,       # 最后一个涨停日的 index 位置
        "n_limit_ups": int,          # 涨停天数
        "n_total_days": int,         # 总跨度天数（含洗盘日）
        "max_consecutive": int,      # 最长连续涨停数
        "first_limit_date": Timestamp,
        "last_limit_date": Timestamp,
      }, ...]
    """
    threshold = limit_up_threshold(code)
    returns = df["close"].pct_change()
    is_lu = returns >= threshold

    # 数据断层检查：排除相邻交易日间隔>5天的假信号
    dates = df.index
    for _gap_idx in range(1, len(dates)):
        if hasattr(dates[_gap_idx], "value") and hasattr(dates[_gap_idx-1], "value"):
            if (dates[_gap_idx] - dates[_gap_idx-1]).days > 5:
                is_lu.iloc[_gap_idx] = False

    runs = []
    i = 0
    while i < len(is_lu):
        if not is_lu.iloc[i]:
            i += 1
            continue

        # 开始扫描一个连板段
        streak_start = i
        lu_count = 0
        consecutive = 0
        max_consecutive = 0
        j = i
        gap_count = 0

        while j < len(is_lu):
            if is_lu.iloc[j]:
                lu_count += 1
                consecutive += 1
                max_consecutive = max(max_consecutive, consecutive)
                gap_count = 0  # 重置洗盘计数
                j += 1
            else:
                # 非涨停
                gap_count += 1
                if gap_count <= max_gap and j + 1 < len(is_lu) and is_lu.iloc[j + 1]:
                    # 还在容忍范围内，且下一个是涨停 → 算洗盘日
                    j += 1
                    consecutive = 0
                else:
                    break

        total_days = j - streak_start
        if lu_count >= min_streak:
            runs.append({
                "first_limit_idx": streak_start,
                "last_limit_idx": j - 1,
                "n_limit_ups": lu_count,
                "n_total_days": total_days,
                "max_consecutive": max_consecutive,
                "first_limit_date": df.index[streak_start],
                "last_limit_date": df.index[j - 1],
            })

        i = j

    return runs


def find_peak_after_run(
    df: pd.DataFrame,
    run_end_idx: int,
    lookahead: int = 5,
) -> Dict[str, Any]:
    """
    向后搜索最高点。

    如果最高点在 lookahead 窗口末尾，继续扩展（最多 3 轮）。

    Returns:
      {
        "peak_date": Timestamp,
        "peak_price": float,
        "peak_idx": int,           # 在 df 中的位置
        "days_to_peak": int,
      }
    """
    start = run_end_idx + 1
    peak_price = -1.0
    peak_idx = run_end_idx
    extended = 0

    while extended < 3:
        end = min(start + lookahead, len(df))
        if start >= len(df):
            break
        window = df.iloc[start:end]
        if len(window) == 0:
            break

        local_peak_idx = window["high"].idxmax()
        local_peak_price = window["high"].loc[local_peak_idx]

        if local_peak_price > peak_price:
            peak_price = local_peak_price
            peak_idx = df.index.get_loc(local_peak_idx)

        # 如果最高点在窗口最后一根 K 线，继续扩展
        if local_peak_idx == window.index[-1] and end < len(df):
            start = end
            extended += 1
        else:
            break

    if peak_price < 0:
        peak_price = float(df["high"].iloc[run_end_idx])
        peak_idx = run_end_idx

    return {
        "peak_date": df.index[peak_idx],
        "peak_price": peak_price,
        "peak_idx": peak_idx,
        "days_to_peak": peak_idx - run_end_idx,
    }


# ================================================================
# 主扫描流程
# ================================================================

def scan_and_export(
    codes: List[str],
    min_streak: int = 2,
    max_gap: int = 2,
    start_date: str = "2023-01-01",
    end_date: str = "2026-05-21",
    pre_days: int = 10,
    output_path: str = "analysis_output/dragon_ohlcv.csv",
    full_history: bool = False,
):
    """
    扫描全市场连板股，导出 OHLCV 到 CSV（流式写入，低内存）。

    两种模式:
      --full-history  导出每只股票的完整历史（正式回测用，无断层）
      默认            仅导出涨停前后窗口（快速分析用，文件小）
    """
    import csv as _csv

    print(f"\n{'='*70}")
    print(f"  连板股扫描 + OHLCV 导出（流式写入）")
    print(f"  ≥{min_streak}板, 允许{max_gap}天洗盘, 区间 {start_date} ~ {end_date}")
    print(f"  数据起点: 起始日前{pre_days}个交易日, 终止日: 最高点")
    print(f"{'='*70}")

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    # 需要额外向前加载 pre_days+5 个交易日的数据（用于起点）
    # 和向后 lookahead*3+5 的数据（用于找最高点）
    # 所以实际查询范围比 start_date~end_date 宽
    buffer_days = max(pre_days + 10, 30)
    query_start = (datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=buffer_days * 2)).strftime("%Y-%m-%d")
    query_end = (datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=40)).strftime("%Y-%m-%d")

    # 流式写入：只在内存中保留去重集合（code, time）和摘要
    seen_keys: set = set()
    run_summaries = []
    total = len(codes)
    stocks_with_runs = 0
    error_count = 0
    row_count = 0

    CSV_FIELDS = [
        "code", "board", "run_n_limit_ups", "run_max_consecutive",
        "run_first_limit_date", "run_last_limit_date",
        "peak_date", "peak_price", "start_date", "end_date",
        "time", "open", "high", "low", "close", "volume",
    ]

    def _write_row(writer, row_dict):
        """写一行并去重"""
        nonlocal row_count
        key = (row_dict["code"], row_dict["time"])
        if key in seen_keys:
            return
        seen_keys.add(key)
        writer.writerow([row_dict[f] for f in CSV_FIELDS])
        row_count += 1

    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = _csv.writer(f)
        writer.writerow(CSV_FIELDS)

        if full_history:
            # ── 模式2: 完整历史导出（正式回测用）──
            # 第一遍：扫描哪些股票有连板段
            stock_runs = {}
            for i, code in enumerate(codes):
                if (i + 1) % 500 == 0 or i == 0:
                    print(f"\r   扫描中: {i+1}/{total}", end="", flush=True)
                try:
                    df = load_daily(code, query_start, query_end)
                    if df is None or len(df) < 60:
                        continue
                    sd = pd.Timestamp(start_date)
                    ed = pd.Timestamp(end_date)
                    df_scan = df[(df.index >= sd) & (df.index <= ed)]
                    if len(df_scan) < 30:
                        continue
                    runs = detect_limit_up_runs(df_scan, code, min_streak=min_streak, max_gap=max_gap)
                    if not runs:
                        continue
                    stocks_with_runs += 1
                    board = get_board(code)
                    stock_data = []
                    for run in runs:
                        run_first_idx = df_scan.index.get_loc(run["first_limit_date"])
                        run_last_idx = df_scan.index.get_loc(run["last_limit_date"])
                        if run_first_idx < 1:
                            continue
                        start_date_val = df_scan.index[run_first_idx - 1]
                        peak = find_peak_after_run(df_scan, run_last_idx, lookahead=5)
                        stock_data.append((run, peak, start_date_val, peak["peak_date"]))
                        run_summaries.append({
                            "code": code, "board": board,
                            "n_limit_ups": run["n_limit_ups"],
                            "max_consecutive": run["max_consecutive"],
                            "first_limit": run["first_limit_date"].strftime("%Y-%m-%d"),
                            "last_limit": run["last_limit_date"].strftime("%Y-%m-%d"),
                            "start_date": start_date_val.strftime("%Y-%m-%d"),
                            "peak_date": peak["peak_date"].strftime("%Y-%m-%d"),
                            "peak_price": round(peak["peak_price"], 3),
                            "days_to_peak": peak["days_to_peak"],
                        })
                    if stock_data:
                        stock_runs[code] = stock_data
                except Exception:
                    error_count += 1
            print(f"\r   扫描完成: {stocks_with_runs} 只股票有连板段, {len(run_summaries)} 个连板段")

            # 第二遍：导出完整历史（流式写入）
            print(f"   导出数据...")
            for idx, (code, run_list) in enumerate(stock_runs.items()):
                if (idx + 1) % 200 == 0:
                    print(f"\r   导出: {idx+1}/{len(stock_runs)}  {row_count:,} 行", end="", flush=True)
                try:
                    df = load_daily(code, query_start, query_end)
                    if df is None:
                        continue
                    board = get_board(code)
                    for ts, row in df.iterrows():
                        _write_row(writer, {
                            "code": code, "board": board,
                            "run_n_limit_ups": run_list[0][0]["n_limit_ups"],
                            "run_max_consecutive": run_list[0][0]["max_consecutive"],
                            "run_first_limit_date": run_list[0][0]["first_limit_date"].strftime("%Y-%m-%d"),
                            "run_last_limit_date": run_list[0][0]["last_limit_date"].strftime("%Y-%m-%d"),
                            "peak_date": run_list[0][1]["peak_date"].strftime("%Y-%m-%d"),
                            "peak_price": round(run_list[0][1]["peak_price"], 3),
                            "start_date": run_list[0][2].strftime("%Y-%m-%d"),
                            "end_date": run_list[0][3].strftime("%Y-%m-%d"),
                            "time": ts.strftime("%Y-%m-%d"),
                            "open": round(float(row["open"]), 3),
                            "high": round(float(row["high"]), 3),
                            "low": round(float(row["low"]), 3),
                            "close": round(float(row["close"]), 3),
                            "volume": int(row["volume"]),
                        })
                except Exception:
                    error_count += 1
        else:
            # ── 模式1: 窗口导出（快速分析用，默认）──
            for i, code in enumerate(codes):
                if (i + 1) % 200 == 0 or i == 0:
                    print(f"\r   扫描中: {i+1}/{total}  已导出 {row_count:,} 行  "
                          f"连板段 {len(run_summaries)} 个", end="", flush=True)
                try:
                    df = load_daily(code, query_start, query_end)
                    if df is None or len(df) < 60:
                        continue
                    sd = pd.Timestamp(start_date)
                    ed = pd.Timestamp(end_date)
                    df_scan = df[(df.index >= sd) & (df.index <= ed)]
                    if len(df_scan) < 30:
                        continue
                    runs = detect_limit_up_runs(df_scan, code, min_streak=min_streak, max_gap=max_gap)
                    if not runs:
                        continue
                    stocks_with_runs += 1
                    board = get_board(code)
                    for run in runs:
                        run_first_idx = df_scan.index.get_loc(run["first_limit_date"])
                        run_last_idx = df_scan.index.get_loc(run["last_limit_date"])
                        if run_first_idx < 1:
                            continue
                        start_date_val = df_scan.index[run_first_idx - 1]
                        peak = find_peak_after_run(df_scan, run_last_idx, lookahead=5)
                        end_date_val = peak["peak_date"]
                        start_loc_in_full = df.index.get_loc(start_date_val) if start_date_val in df.index else None
                        end_loc_in_full = df.index.get_loc(end_date_val) if end_date_val in df.index else None
                        if start_loc_in_full is None or end_loc_in_full is None:
                            continue
                        data_start_loc = max(0, start_loc_in_full - pre_days)
                        post_days = 5
                        data_end_loc = min(len(df) - 1, end_loc_in_full + post_days)
                        segment = df.iloc[data_start_loc:data_end_loc + 1].copy()
                        if len(segment) == 0:
                            continue
                        # 跳过停牌日（volume=0 或 OHLC 全相同）
                        for ts, row in segment.iterrows():
                            if float(row.get("volume", 0)) == 0:
                                continue
                            _write_row(writer, {
                                "code": code, "board": board,
                                "run_n_limit_ups": run["n_limit_ups"],
                                "run_max_consecutive": run["max_consecutive"],
                                "run_first_limit_date": run["first_limit_date"].strftime("%Y-%m-%d"),
                                "run_last_limit_date": run["last_limit_date"].strftime("%Y-%m-%d"),
                                "peak_date": peak["peak_date"].strftime("%Y-%m-%d"),
                                "peak_price": round(peak["peak_price"], 3),
                                "start_date": start_date_val.strftime("%Y-%m-%d"),
                                "end_date": end_date_val.strftime("%Y-%m-%d"),
                                "time": ts.strftime("%Y-%m-%d"),
                                "open": round(float(row["open"]), 3),
                                "high": round(float(row["high"]), 3),
                                "low": round(float(row["low"]), 3),
                                "close": round(float(row["close"]), 3),
                                "volume": int(row["volume"]),
                            })
                        run_summaries.append({
                            "code": code, "board": board,
                            "n_limit_ups": run["n_limit_ups"],
                            "max_consecutive": run["max_consecutive"],
                            "first_limit": run["first_limit_date"].strftime("%Y-%m-%d"),
                            "last_limit": run["last_limit_date"].strftime("%Y-%m-%d"),
                            "start_date": start_date_val.strftime("%Y-%m-%d"),
                            "peak_date": peak["peak_date"].strftime("%Y-%m-%d"),
                            "peak_price": round(peak["peak_price"], 3),
                            "days_to_peak": peak["days_to_peak"],
                        })
                except Exception:
                    error_count += 1

    # ── 汇总 ──
    if row_count > 0:
        print(f"\n\n✅ 导出完成: {output_path}")
        print(f"   总行数: {row_count:,}（已去重）")
        print(f"   连板段: {len(run_summaries)} 个")
        print(f"   涉及股票: {stocks_with_runs} 只")
        print(f"   错误: {error_count} 个")

        # 摘要统计（从 CSV 读取做统计，避免内存中保留全量数据）
        if run_summaries:
            df_sum = pd.DataFrame(run_summaries)
            print(f"\n📊 连板段分布:")
            for n in sorted(df_sum["n_limit_ups"].unique()):
                cnt = (df_sum["n_limit_ups"] == n).sum()
                print(f"   {n}板: {cnt} 个")
            print(f"\n📊 板块分布:")
            for board, cnt in df_sum["board"].value_counts().items():
                print(f"   {board}: {cnt} 个")

            # 保存摘要
            summary_path = output_path.replace(".csv", "_summary.csv")
            df_sum.to_csv(summary_path, index=False, encoding="utf-8-sig")
            print(f"\n💾 摘要: {summary_path}")
    else:
        print(f"\n\n❌ 未找到任何连板段")

    return row_count


# ================================================================
# CLI
# ================================================================

def main():
    parser = argparse.ArgumentParser(description="连板股扫描 + OHLCV 导出")
    parser.add_argument("--min-streak", type=int, default=2, help="最少涨停天数 (默认 2)")
    parser.add_argument("--max-gap", type=int, default=2, help="最大洗盘天数 (默认 2)")
    parser.add_argument("--start", type=str, default="2023-01-01", help="扫描起始日期")
    parser.add_argument("--end", type=str, default="2026-05-21", help="扫描结束日期")
    parser.add_argument("--pre-days", type=int, default=10, help="起始日前取多少个交易日 (默认 10)")
    parser.add_argument("--quick", action="store_true", help="抽样 500 只")
    parser.add_argument("--sample", type=int, default=0, help="抽样 N 只 (0=全量)")
    parser.add_argument("--output", type=str, default="analysis_output/dragon_ohlcv.csv", help="输出 CSV 路径")
    parser.add_argument("--full-history", action="store_true", help="导出完整历史（正式回测用，默认: 仅窗口导出）")

    args = parser.parse_args()

    print("🚀 连板股扫描 + OHLCV 导出")

    all_codes = get_all_codes()
    print(f"   全市场: {len(all_codes)} 只股票")

    if args.quick:
        codes = all_codes[:500]
        print(f"   抽样模式: {len(codes)} 只")
    elif args.sample > 0:
        codes = all_codes[:args.sample]
        print(f"   抽样模式: {len(codes)} 只")
    else:
        codes = all_codes

    scan_and_export(
        codes=codes,
        min_streak=args.min_streak,
        max_gap=args.max_gap,
        start_date=args.start,
        end_date=args.end,
        pre_days=args.pre_days,
        output_path=args.output,
        full_history=args.full_history,
    )


if __name__ == "__main__":
    main()
