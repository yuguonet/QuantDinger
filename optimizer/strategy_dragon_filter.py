#!/usr/bin/env python3
"""
连板猎手 v2 — 双分支横向过滤 + 机械出场

策略逻辑:
  ┌─────────────────────────────────────────────────────┐
  │ 信号（T日收盘前可观测）:                                │
  │   1. 当天涨停（非一字板，有实际交易）                     │
  │   2. 双分支过滤（参数见 BOARD_PARAMS）:                  │
  │      主板(10%): 涨幅≥9.8% 封板≤8% 波动≤3% 量比≤1      │
  │      创/科(20%): 涨幅≥19.8% 封板≤2.8% 上影2~8% 波动≤10%│
  │                                                       │
  │ 买入: T+1 开盘价                                       │
  │ 持有: 涨停就拿着                                        │
  │ 卖出:                                                  │
  │   - 止盈 take_profit% (默认 15%)                       │
  │   - 追踪止损: 盈利 ≥ activate% 后，回撤 ≥ callback%    │
  │   - 固定止损 stop_loss% (默认 10%)                     │
  │   - 开板卖出: 当天不涨停 → 收盘卖出                     │
  │   - 最大持仓 max_hold 天                               │
  └─────────────────────────────────────────────────────┘

双数据源:
    --source db     从 db_market 读取（本地 Windows，需 PostgreSQL）
    --source csv    从 dragon_ohlcv.csv 读取（远程 Linux，无需数据库）

用法:
    # 数据库模式（Windows 本地）
    python strategy_dragon_filter.py --source db
    python strategy_dragon_filter.py --source db --start 2024-01-01

    # CSV 模式（远程服务器 / CI）
    python strategy_dragon_filter.py --source csv --csv analysis_output/dragon_ohlcv.csv
    python strategy_dragon_filter.py --source csv --csv analysis_output/dragon_ohlcv.csv --start 2024-01-01
"""
from __future__ import annotations

import os
import sys
import csv
import argparse
from datetime import datetime, timedelta
from collections import defaultdict
from typing import List, Dict, Any, Optional

import pandas as pd
import numpy as np

# 路径
_optimizer_dir = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.dirname(_optimizer_dir)
_backend_root = os.path.join(_project_root, "backend_api_python")
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


# ================================================================
# 板块 & 阈值
# ================================================================

def get_board(code: str) -> str:
    c = code[:3]
    if c.startswith("68"): return "科创板"
    if c.startswith("30"): return "创业板"
    if c.startswith(("8","4")): return "北交所"
    if c.startswith("6"): return "沪主板"
    if c.startswith(("0","2")): return "深主板"
    return "未知"


def lim_thresh(code: str) -> float:
    return 0.198 if get_board(code) in ("创业板","科创板") else 0.098


def is_20pct_board(code: str) -> bool:
    return get_board(code) in ("创业板", "科创板")


# ── 分板块默认参数 ──
BOARD_PARAMS = {
    "10pct": {  # 沪主板、深主板
        "min_return": 9.8,
        "max_seal": 8.0,
        "min_upper": 0.0,
        "max_upper": 8.0,
        "max_volatility": 3.0,   # 前5天低波动 → 突破更有效
        "max_vol_ratio": 1.0,    # 关键！缩量涨停 >> 放量涨停
        "open_break_stop": -5.0,
    },
    "20pct": {  # 创业板、科创板
        "min_return": 19.8,
        "max_seal": 2.8,
        "min_upper": 2.0,
        "max_upper": 8.0,
        "max_volatility": 10.0,
    },
}


# ================================================================
# 数据加载 — 双接口
# ================================================================

def _load_env():
    try:
        from dotenv import load_dotenv
        for p in [os.path.join(_backend_root, '.env'), os.path.join(_project_root, '.env')]:
            if os.path.isfile(p):
                load_dotenv(p, override=False)
                break
    except Exception:
        pass


def _get_writer():
    _load_env()
    from app.utils.db_market import get_market_kline_writer
    return get_market_kline_writer()


def get_all_codes_db() -> list:
    writer = _get_writer()
    stats = writer.stats("CNStock")
    return stats.get("symbol_list", []) if stats.get("exists") else []


def load_daily_db(code: str, start: str, end: str) -> Optional[pd.DataFrame]:
    """从 db_market 加载日线"""
    writer = _get_writer()
    data = writer.query("CNStock", code, "1D", start_time=start, end_time=end, limit=0)
    if not data:
        return None
    df = pd.DataFrame(data)
    if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"])
        df = df.set_index("time")
    df = df.sort_index()
    for c in ["open","high","low","close","volume"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def load_from_csv(csv_path: str, start_date: str, end_date: str):
    """
    从 CSV 加载数据，按 (code, first_limit_date) 分组。
    返回: code_data[code] = {df, close, high, low, open, dates, threshold}
          run_groups = [(code, fld, board, run_n_limit_ups, df_segment), ...]
    """
    print(f"  📂 加载 CSV: {csv_path}")
    df_all = pd.read_csv(csv_path, dtype={"code": str})
    df_all["code"] = df_all["code"].astype(str).str.zfill(6)
    df_all["time"] = pd.to_datetime(df_all["time"])

    # 过滤日期范围
    sd = pd.Timestamp(start_date)
    ed = pd.Timestamp(end_date)

    code_data = {}
    run_groups = []

    # 按 (code, first_limit_date) 分组
    grouped = df_all.groupby(["code", "run_first_limit_date"])

    for (code, fld), gdf in grouped:
        fld_ts = pd.Timestamp(fld)
        if fld_ts < sd or fld_ts > ed:
            continue

        gdf = gdf.sort_values("time").reset_index(drop=True)
        if len(gdf) < 6:
            continue

        board = gdf["board"].iloc[0]
        run_n = int(gdf["run_n_limit_ups"].iloc[0])

        # 设置 index 为 time
        gdf_indexed = gdf.set_index("time")

        if code not in code_data:
            code_data[code] = {
                "df_full": None,  # CSV 模式不用全量
                "board": board,
                "threshold": lim_thresh(code),
            }

        run_groups.append({
            "code": code,
            "board": board,
            "fld": fld,
            "fld_ts": fld_ts,
            "run_n_limit_ups": run_n,
            "df": gdf_indexed,
            "close": gdf_indexed["close"].values.astype(float),
            "high": gdf_indexed["high"].values.astype(float),
            "low": gdf_indexed["low"].values.astype(float),
            "open": gdf_indexed["open"].values.astype(float),
            "volume": gdf_indexed["volume"].values.astype(float),
            "dates": gdf_indexed.index,
            "threshold": lim_thresh(code),
        })

    print(f"  ✅ {len(run_groups)} 个连板段, 涉及 {len(code_data)} 只股票")
    return code_data, run_groups


# ================================================================
# 特征提取
# ================================================================

def extract_limit_up_features(
    close: np.ndarray,
    open_: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    volume: np.ndarray,
    dates,
    code: str,
    i: int,
    lookback: int = 5,
) -> Optional[Dict[str, Any]]:
    """
    提取第 i 天涨停时的特征（只用 i 及之前数据）。
    i 是涨停日在数组中的位置。
    """
    if i < lookback + 1:
        return None

    fl_c = close[i]
    fl_o = open_[i]
    fl_h = high[i]
    fl_l = low[i]
    fl_v = volume[i]
    prev_c = close[i - 1]

    threshold = lim_thresh(code)

    # 一字板判定
    limit_up_price = prev_c * (1 + threshold)
    gap_to_limit = abs(fl_o - limit_up_price) / limit_up_price if limit_up_price > 0 else 999
    amp = (fl_h - fl_l) / prev_c if prev_c > 0 else 999
    is_yizi = gap_to_limit < 0.01 and amp < 0.01
    if is_yizi:
        return None

    fl_return = (fl_c / prev_c - 1) * 100
    fl_seal = (fl_c - fl_l) / fl_c * 100 if fl_c > 0 else 999
    fl_upper_shadow = (fl_h - fl_c) / prev_c * 100
    fl_amplitude = (fl_h - fl_l) / prev_c * 100
    bar_range = fl_h - fl_l
    fl_body_ratio = abs(fl_c - fl_o) / bar_range if bar_range > 0 else 1.0

    vol_window = volume[max(0, i - lookback):i]
    avg_vol = vol_window.mean() if len(vol_window) > 0 else fl_v
    fl_vol_ratio = fl_v / avg_vol if avg_vol > 0 else 0

    rets = []
    for j in range(i - lookback, i):
        if close[j - 1] > 0:
            rets.append(close[j] / close[j - 1] - 1)
    volatility = np.std(rets) * 100 if len(rets) >= 3 else 999

    if close[i - lookback - 1] > 0:
        prev5_return = (prev_c / close[i - lookback - 1] - 1) * 100
    else:
        prev5_return = 0

    return {
        "code": code,
        "board": get_board(code),
        "branch": "20pct" if is_20pct_board(code) else "10pct",
        "date": dates[i],
        "date_str": dates[i].strftime("%Y-%m-%d") if hasattr(dates[i], 'strftime') else str(dates[i]),
        "fl_return": fl_return,
        "fl_seal": fl_seal,
        "fl_upper_shadow": fl_upper_shadow,
        "fl_amplitude": fl_amplitude,
        "fl_body_ratio": fl_body_ratio,
        "fl_vol_ratio": fl_vol_ratio,
        "volatility": volatility,
        "prev5_return": prev5_return,
        "is_yizi": False,
        "threshold": threshold,
    }


def passes_filter(feat: Dict[str, Any]) -> bool:
    """检查是否通过过滤规则（自动选板块分支）"""
    branch = "20pct" if is_20pct_board(feat["code"]) else "10pct"
    p = BOARD_PARAMS[branch]
    if feat["fl_return"] < p["min_return"]:
        return False
    if feat["fl_seal"] > p["max_seal"]:
        return False
    if feat["fl_upper_shadow"] < p["min_upper"]:
        return False
    if feat["fl_upper_shadow"] > p["max_upper"]:
        return False
    if feat["volatility"] > p["max_volatility"]:
        return False
    if branch == "10pct" and "max_vol_ratio" in p:
        if feat["fl_vol_ratio"] > p["max_vol_ratio"]:
            return False
    return True


# ================================================================
# 逐日推进出场引擎
# ================================================================

def simulate_trade(
    close: list,
    high: list,
    low: list,
    open_: list,
    entry_idx: int,
    entry_price: float,
    threshold: float,
    take_profit: float = 15.0,
    trailing_activate: float = 5.0,
    trailing_callback: float = 8.0,
    stop_loss: float = 10.0,
    max_hold: int = 20,
    open_break_stop: float = 0.0,
) -> Dict[str, Any]:
    """
    逐日推进出场，不看未来数据。

    入场: entry_idx 的 open（已传入 entry_price）
    出场优先级:
      1. 固定止损（任何状态都生效）
      2. 开板判定（当天不涨停 → 收盘卖出）
      3. 止盈 / 追踪止损（开板后才生效，涨停封板期间不触发）
      4. 最大持仓天数
    """
    n = len(close)
    if n == 0 or entry_idx >= n:
        return {"exit_idx": 0, "pnl_pct": 0, "hold_days": 0, "exit_reason": "数据异常"}

    peak = entry_price
    hold = 0

    for pos in range(entry_idx + 1, n):
        hold += 1
        c = close[pos]
        h = high[pos]
        prev_c = close[pos - 1]

        if h > peak:
            peak = h

        ret = (c / entry_price - 1) * 100

        # 1. 固定止损
        if ret <= -stop_loss + 0.01:
            return {"exit_idx": pos, "pnl_pct": round(ret, 2),
                    "hold_days": hold, "exit_reason": f"止损{stop_loss}%"}

        # 2. 开板判定
        if prev_c > 0:
            limit_price = prev_c * (1 + threshold)
            gap_from_limit = (limit_price - c) / limit_price
            is_limit_up = gap_from_limit < 0.02
        else:
            is_limit_up = False

        if not is_limit_up:
            peak_ret = (peak / entry_price - 1) * 100

            # 开板日大跌
            if open_break_stop < 0 and ret <= open_break_stop:
                return {"exit_idx": pos, "pnl_pct": round(ret, 2),
                        "hold_days": hold, "exit_reason": f"开板止损{ret:+.1f}%"}

            # 止盈
            if ret >= take_profit:
                return {"exit_idx": pos, "pnl_pct": round(ret, 2),
                        "hold_days": hold, "exit_reason": f"止盈{take_profit}%"}

            # 追踪止损
            if peak_ret >= trailing_activate:
                dd = (peak - c) / entry_price * 100
                if dd >= trailing_callback:
                    return {"exit_idx": pos, "pnl_pct": round(ret, 2),
                            "hold_days": hold,
                            "exit_reason": f"追踪止损(峰+{peak_ret:.1f}%→回撤{dd:.1f}%)"}

            # 开板即卖
            return {"exit_idx": pos, "pnl_pct": round(ret, 2),
                    "hold_days": hold, "exit_reason": "开板卖出"}

        # 3. 涨停封板中：继续持有
        if hold >= max_hold:
            return {"exit_idx": pos, "pnl_pct": round(ret, 2),
                    "hold_days": hold, "exit_reason": f"持仓{max_hold}天"}

    # 数据结束
    ret = (close[-1] / entry_price - 1) * 100 if entry_price > 0 else 0
    return {"exit_idx": n - 1, "pnl_pct": round(ret, 2),
            "hold_days": hold, "exit_reason": "数据结束"}


# ================================================================
# 主策略 — DB 模式
# ================================================================

def run_strategy_db(
    codes: List[str],
    start_date: str,
    end_date: str,
    take_profit: float,
    trailing_activate: float,
    trailing_callback: float,
    stop_loss: float,
    max_hold: int,
) -> List[Dict[str, Any]]:
    """全市场逐日扫描，从 db_market 读取数据。"""
    print(f"\n{'='*70}")
    print(f"  连板猎手 v2 — DB 模式")
    print(f"{'='*70}")
    print(f"  区间: {start_date} ~ {end_date}")
    _print_params(take_profit, trailing_activate, trailing_callback, stop_loss, max_hold)
    print(f"  股票: {len(codes)} 只")

    buffer = max_hold + 15
    query_start = (datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=buffer * 2)).strftime("%Y-%m-%d")
    query_end = (datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=buffer)).strftime("%Y-%m-%d")

    print(f"\n  [1/2] 扫描涨停信号...")
    day_signals = defaultdict(list)
    code_data = {}
    total = len(codes)
    loaded = 0

    for idx, code in enumerate(codes):
        if (idx + 1) % 500 == 0 or idx == 0:
            print(f"\r   加载: {idx+1}/{total}  已加载: {loaded}", end="", flush=True)
        try:
            df = load_daily_db(code, query_start, query_end)
            if df is None or len(df) < 30:
                continue
            sd = pd.Timestamp(start_date)
            ed = pd.Timestamp(end_date)
            df_scan = df[(df.index >= sd) & (df.index <= ed)]
            if len(df_scan) < 10:
                continue
            loaded += 1
            threshold = lim_thresh(code)
            close_arr = df_scan["close"].values
            dates = df_scan.index

            for i in range(1, len(close_arr)):
                if hasattr(dates[i], 'value') and hasattr(dates[i-1], 'value'):
                    if (dates[i] - dates[i-1]).days > 5:
                        continue
                day_ret = close_arr[i] / close_arr[i - 1] - 1
                if day_ret < threshold * 0.98:
                    continue
                feat = extract_limit_up_features(
                    close_arr, df_scan["open"].values, df_scan["high"].values,
                    df_scan["low"].values, df_scan["volume"].values.astype(float),
                    dates, code, i,
                )
                if feat is None:
                    continue
                # 只要第一板
                if i >= 2:
                    prev_ret = close_arr[i - 1] / close_arr[i - 2] - 1
                    if prev_ret >= threshold * 0.98:
                        continue
                day_signals[feat["date_str"]].append(feat)

            code_data[code] = {
                "close": df_scan["close"].values,
                "high": df_scan["high"].values,
                "low": df_scan["low"].values,
                "open": df_scan["open"].values,
                "dates": df_scan.index,
                "threshold": threshold,
            }
        except Exception:
            continue

    print(f"\r   加载完成: {loaded}/{total} 只股票  涨停信号日: {len(day_signals)} 天")

    print(f"\n  [2/2] 过滤 + 回测...")
    all_trades = []
    signal_count = 0
    filtered_count = 0

    for date_str in sorted(day_signals):
        feats = day_signals[date_str]
        signal_count += len(feats)
        passed = [f for f in feats if passes_filter(f)]
        filtered_count += len(passed)

        for feat in passed:
            code = feat["code"]
            if code not in code_data:
                continue
            data = code_data[code]
            dates = data["dates"]
            signal_date = feat["date"]
            if signal_date not in dates:
                continue
            signal_idx = dates.get_loc(signal_date)
            buy_idx = signal_idx + 1
            if buy_idx >= len(data["close"]):
                continue
            entry_price = data["open"][buy_idx]
            if entry_price <= 0:
                continue

            branch = feat.get("branch", "10pct" if not is_20pct_board(code) else "20pct")
            obs = BOARD_PARAMS[branch].get("open_break_stop", 0.0)

            result = simulate_trade(
                data["close"], data["high"], data["low"], data["open"],
                buy_idx, entry_price, data["threshold"],
                take_profit, trailing_activate, trailing_callback,
                stop_loss, max_hold, open_break_stop=obs,
            )
            exit_date = data["dates"][result["exit_idx"]]
            all_trades.append(_trade_record(feat, code, data, buy_idx, entry_price, exit_date, result))

    print(f"   涨停信号: {signal_count}  通过过滤: {filtered_count}  交易: {len(all_trades)}")
    return all_trades


# ================================================================
# 主策略 — CSV 模式
# ================================================================

def run_strategy_csv(
    csv_path: str,
    start_date: str,
    end_date: str,
    take_profit: float,
    trailing_activate: float,
    trailing_callback: float,
    stop_loss: float,
    max_hold: int,
) -> List[Dict[str, Any]]:
    """从 CSV 读取数据，逐连板段回测。"""
    print(f"\n{'='*70}")
    print(f"  连板猎手 v2 — CSV 模式")
    print(f"{'='*70}")
    print(f"  区间: {start_date} ~ {end_date}")
    _print_params(take_profit, trailing_activate, trailing_callback, stop_loss, max_hold)

    code_data, run_groups = load_from_csv(csv_path, start_date, end_date)

    print(f"\n  回测中...")
    all_trades = []
    signal_count = 0
    filtered_count = 0

    for rg in run_groups:
        code = rg["code"]
        close = rg["close"]
        high = rg["high"]
        low = rg["low"]
        open_ = rg["open"]
        volume = rg["volume"]
        dates = rg["dates"]
        threshold = rg["threshold"]
        fld_ts = rg["fld_ts"]
        fld_str = rg["fld"]

        # 在 df 中找到 first_limit_date 的位置
        fld_pos = None
        for i, d in enumerate(dates):
            d_str = d.strftime("%Y-%m-%d") if hasattr(d, 'strftime') else str(d)
            if d_str == fld_str:
                fld_pos = i
                break
        if fld_pos is None or fld_pos < 2:
            continue

        # 检查第一板：前一天不涨停
        if fld_pos >= 2:
            prev_ret = close[fld_pos - 1] / close[fld_pos - 2] - 1
            if prev_ret >= threshold * 0.98:
                continue  # 不是第一板

        signal_count += 1

        # 提取特征
        feat = extract_limit_up_features(close, open_, high, low, volume, dates, code, fld_pos)
        if feat is None:
            continue

        # 过滤
        if not passes_filter(feat):
            continue
        filtered_count += 1

        # T+1 开盘买入
        buy_idx = fld_pos + 1
        if buy_idx >= len(close):
            continue
        entry_price = open_[buy_idx]
        if entry_price <= 0:
            continue

        branch = feat.get("branch", "10pct" if not is_20pct_board(code) else "20pct")
        obs = BOARD_PARAMS[branch].get("open_break_stop", 0.0)

        result = simulate_trade(
            close, high, low, open_,
            buy_idx, entry_price, threshold,
            take_profit, trailing_activate, trailing_callback,
            stop_loss, max_hold, open_break_stop=obs,
        )
        exit_date = dates[result["exit_idx"]]

        all_trades.append({
            "code": code,
            "board": feat["board"],
            "branch": feat.get("branch", "10pct"),
            "signal_date": fld_str,
            "buy_date": dates[buy_idx].strftime("%Y-%m-%d") if hasattr(dates[buy_idx], 'strftime') else str(dates[buy_idx]),
            "exit_date": exit_date.strftime("%Y-%m-%d") if hasattr(exit_date, 'strftime') else str(exit_date),
            "entry_price": round(entry_price, 3),
            "exit_price": round(close[result["exit_idx"]], 3),
            "pnl_pct": result["pnl_pct"],
            "hold_days": result["hold_days"],
            "exit_reason": result["exit_reason"],
            "fl_return": round(feat["fl_return"], 1),
            "fl_seal": round(feat["fl_seal"], 1),
            "fl_upper_shadow": round(feat["fl_upper_shadow"], 1),
            "fl_amplitude": round(feat["fl_amplitude"], 1),
            "fl_vol_ratio": round(feat.get("fl_vol_ratio", 0), 2),
            "volatility": round(feat["volatility"], 1),
            "run_n_limit_ups": rg["run_n_limit_ups"],
        })

    print(f"   第一板信号: {signal_count}  通过过滤: {filtered_count}  交易: {len(all_trades)}")
    return all_trades


# ================================================================
# 公共
# ================================================================

def _print_params(tp, ta, tc, sl, mh):
    print(f"  分支A (主板10%): 涨幅≥{BOARD_PARAMS['10pct']['min_return']}% 封板≤{BOARD_PARAMS['10pct']['max_seal']}% "
          f"波动≤{BOARD_PARAMS['10pct']['max_volatility']}% 量比≤{BOARD_PARAMS['10pct']['max_vol_ratio']}")
    print(f"  分支B (创/科20%): 涨幅≥{BOARD_PARAMS['20pct']['min_return']}% 封板≤{BOARD_PARAMS['20pct']['max_seal']}% "
          f"上影{BOARD_PARAMS['20pct']['min_upper']}~{BOARD_PARAMS['20pct']['max_upper']}% 波动≤{BOARD_PARAMS['20pct']['max_volatility']}%")
    print(f"  出场: 止盈{tp}% 追踪{ta}%→{tc}% 止损{sl}% 最大{mh}天")


def _trade_record(feat, code, data, buy_idx, entry_price, exit_date, result):
    return {
        "code": code,
        "board": feat["board"],
        "branch": feat.get("branch", "10pct"),
        "signal_date": feat["date_str"],
        "buy_date": data["dates"][buy_idx].strftime("%Y-%m-%d"),
        "exit_date": exit_date.strftime("%Y-%m-%d"),
        "entry_price": round(entry_price, 3),
        "exit_price": round(data["close"][result["exit_idx"]], 3),
        "pnl_pct": result["pnl_pct"],
        "hold_days": result["hold_days"],
        "exit_reason": result["exit_reason"],
        "fl_return": round(feat["fl_return"], 1),
        "fl_seal": round(feat["fl_seal"], 1),
        "fl_upper_shadow": round(feat["fl_upper_shadow"], 1),
        "fl_amplitude": round(feat["fl_amplitude"], 1),
        "fl_vol_ratio": round(feat.get("fl_vol_ratio", 0), 2),
        "volatility": round(feat["volatility"], 1),
    }


# ================================================================
# 分析输出
# ================================================================

def analyze_results(trades: List[Dict[str, Any]]):
    """分析回测结果"""
    print(f"\n{'='*70}")
    print(f"  📊 连板猎手 v2 回测结果")
    print(f"{'='*70}")

    if not trades:
        print("  ❌ 无交易")
        return

    pnl = [t["pnl_pct"] for t in trades]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p < 0]
    n = len(pnl)
    avg = sum(pnl) / n
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0
    pf = avg_win / avg_loss if avg_loss > 0 else float("inf")
    wr = len(wins) / n * 100

    bins = [(-999, -15), (-15, -10), (-10, -5), (-5, 0), (0, 5), (5, 10), (10, 15), (15, 20), (20, 999)]
    labels = ["<-15%", "-15~-10%", "-10~-5%", "-5~0%", "0~5%", "5~10%", "10~15%", "15~20%", ">20%"]

    print(f"\n  交易数: {n}  盈利: {len(wins)}  亏损: {len(losses)}")
    print(f"  胜率: {wr:.1f}%")
    print(f"  平均收益: {avg:+.2f}%  中位数: {sorted(pnl)[n//2]:+.2f}%")
    print(f"  盈利均值: {avg_win:+.2f}%  亏损均值: {-avg_loss:+.2f}%")
    print(f"  盈亏比: {pf:.2f}")
    print(f"  期望值: {avg:+.2f}%/笔")
    print(f"  最大盈利: {max(pnl):+.2f}%  最大亏损: {min(pnl):+.2f}%")

    hd = [t["hold_days"] for t in trades]
    print(f"\n  持仓天数: 均值={sum(hd)/len(hd):.1f}  中位数={sorted(hd)[len(hd)//2]}")

    print(f"\n  出场原因:")
    reason_groups = defaultdict(list)
    for t in trades:
        reason_groups[t["exit_reason"]].append(t["pnl_pct"])
    for reason, pnls in sorted(reason_groups.items(), key=lambda x: -len(x[1])):
        avg_r = sum(pnls) / len(pnls)
        wr_r = sum(1 for p in pnls if p > 0) / len(pnls) * 100
        print(f"    {reason:40s}  {len(pnls):3d}笔  均值={avg_r:+.2f}%  胜率={wr_r:.0f}%")

    print(f"\n  按板块:")
    board_groups = defaultdict(list)
    for t in trades:
        board_groups[t["board"]].append(t["pnl_pct"])
    for board, pnls in sorted(board_groups.items(), key=lambda x: -len(x[1])):
        avg_b = sum(pnls) / len(pnls)
        wr_b = sum(1 for p in pnls if p > 0) / len(pnls) * 100
        print(f"    {board:8s} ({len(pnls):3d}笔): 胜率={wr_b:.0f}%  均值={avg_b:+.2f}%")

    print(f"\n  收益分桶:")
    for (lo, hi), label in zip(bins, labels):
        bucket = [t for t in trades if lo <= t["pnl_pct"] < hi]
        cnt = len(bucket)
        if cnt == 0:
            boards_str = ""
        else:
            bc = defaultdict(int)
            for t in bucket:
                bc[t["board"]] += 1
            boards_str = "  " + " ".join(f"{b}:{c}" for b, c in sorted(bc.items(), key=lambda x: -x[1]))
        bar = "█" * int(cnt / n * 40)
        print(f"    {label:12s}  {cnt:4d} ({cnt/n*100:5.1f}%)  {bar}{boards_str}")

    print(f"\n  按年度:")
    year_groups = defaultdict(list)
    for t in trades:
        year_groups[t["signal_date"][:4]].append(t["pnl_pct"])
    for year, pnls in sorted(year_groups.items()):
        avg_y = sum(pnls) / len(pnls)
        wr_y = sum(1 for p in pnls if p > 0) / len(pnls) * 100
        print(f"    {year} ({len(pnls):3d}笔): 胜率={wr_y:.0f}%  均值={avg_y:+.2f}%")

    sorted_trades = sorted(trades, key=lambda x: x["pnl_pct"])
    print(f"\n  亏损最多5笔:")
    for t in sorted_trades[:5]:
        print(f"    {t['code']:>8s} {t['signal_date']} → {t['exit_date']} "
              f"持仓{t['hold_days']}天 收益{t['pnl_pct']:+.2f}% {t['exit_reason']}")
    print(f"\n  盈利最多5笔:")
    for t in sorted_trades[-5:][::-1]:
        print(f"    {t['code']:>8s} {t['signal_date']} → {t['exit_date']} "
              f"持仓{t['hold_days']}天 收益{t['pnl_pct']:+.2f}% {t['exit_reason']}")


def save_trades(trades: List[Dict[str, Any]], output: str):
    if not trades:
        return
    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    fields = ["code", "board", "branch", "signal_date", "buy_date", "exit_date",
              "entry_price", "exit_price", "pnl_pct", "hold_days", "exit_reason",
              "fl_return", "fl_seal", "fl_upper_shadow", "fl_amplitude", "fl_vol_ratio",
              "volatility", "run_n_limit_ups"]
    with open(output, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for t in trades:
            w.writerow(t)
    print(f"\n💾 交易明细已保存: {output}")


# ================================================================
# CLI
# ================================================================

def main():
    print("🔖 REV: 20260522-1201  双数据源(db/csv)")
    parser = argparse.ArgumentParser(description="连板猎手 v2 — 双分支过滤 + 机械出场")
    parser.add_argument("--source", choices=["db", "csv"], required=True,
                        help="数据源: db (PostgreSQL) 或 csv (dragon_ohlcv.csv)")
    parser.add_argument("--csv", type=str, default="analysis_output/dragon_ohlcv.csv",
                        help="CSV 文件路径（--source csv 时使用）")
    parser.add_argument("--start", type=str, default="2023-01-01")
    parser.add_argument("--end", type=str, default="2026-05-21")
    parser.add_argument("--quick", action="store_true", help="抽样500只（仅 db 模式）")
    parser.add_argument("--sample", type=int, default=0)

    # 出场参数
    parser.add_argument("--tp", type=float, default=15.0, help="止盈%")
    parser.add_argument("--trail-activate", type=float, default=5.0, help="追踪止损激活%")
    parser.add_argument("--trail-callback", type=float, default=8.0, help="追踪止损回撤%")
    parser.add_argument("--stop-loss", type=float, default=10.0, help="固定止损%")
    parser.add_argument("--max-hold", type=int, default=20, help="最大持仓天数")

    # 输出
    parser.add_argument("--output", type=str, default="analysis_output/dragon_filter_trades.csv")

    args = parser.parse_args()

    print("🚀 连板猎手 v2")

    if args.source == "db":
        codes = get_all_codes_db()
        print(f"   全市场: {len(codes)} 只股票")
        if args.quick:
            codes = codes[:500]
        elif args.sample > 0:
            codes = codes[:args.sample]
        trades = run_strategy_db(
            codes=codes, start_date=args.start, end_date=args.end,
            take_profit=args.tp, trailing_activate=args.trail_activate,
            trailing_callback=args.trail_callback, stop_loss=args.stop_loss,
            max_hold=args.max_hold,
        )
    else:
        trades = run_strategy_csv(
            csv_path=args.csv, start_date=args.start, end_date=args.end,
            take_profit=args.tp, trailing_activate=args.trail_activate,
            trailing_callback=args.trail_callback, stop_loss=args.stop_loss,
            max_hold=args.max_hold,
        )

    analyze_results(trades)
    save_trades(trades, args.output)


if __name__ == "__main__":
    main()
