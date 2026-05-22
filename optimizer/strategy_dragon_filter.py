#!/usr/bin/env python3
"""
连板猎手 v2 — 横向过滤 + 机械出场

策略逻辑:
  ┌─────────────────────────────────────────────────────┐
  │ 信号（T日收盘前可观测）:                                │
  │   1. 当天涨停（非一字板，有实际交易）                     │
  │   2. 第一板涨幅 ≥ min_return (默认 20%)                │
  │   3. 封板强度 ≤ max_seal (默认 2.8%)                   │
  │   4. 上影线 ≥ min_upper (默认 2.0%)  [排除一字板]       │
  │   5. 上影线 ≤ max_upper (默认 8.0%)  [排除冲高回落]     │
  │   6. 前5天波动率 ≤ max_volatility (默认 10%)            │
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

用法:
    python strategy_dragon_filter.py
    python strategy_dragon_filter.py --min-return 25 --max-seal 2.0
    python strategy_dragon_filter.py --quick
    python strategy_dragon_filter.py --start 2024-01-01
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


def get_all_codes() -> list:
    writer = _get_writer()
    stats = writer.stats("CNStock")
    return stats.get("symbol_list", []) if stats.get("exists") else []


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


def board_scale(code: str) -> float:
    """归一化系数: 主板10%→20%基准需×2, 创业板/科创板20%→20%基准×1"""
    return 2.0 if get_board(code) in ("沪主板", "深主板") else 1.0


def is_20pct_board(code: str) -> bool:
    """创业板/科创板: 20%涨停"""
    return get_board(code) in ("创业板", "科创板")


# ── 分板块默认参数 ──
# 主板10%涨停 vs 创业板/科创板20%涨停，物理特性不同，独立参数
BOARD_PARAMS = {
    "10pct": {  # 沪主板、深主板
        "min_return": 9.8,       # 涨停即通过（10%板）
        "max_seal": 8.0,         # 主板封板普遍偏大，放宽
        "min_upper": 0.0,        # 主板99.8%上影<1%，不筛
        "max_upper": 8.0,
        "max_volatility": 3.0,   # 前5天低波动 → 突破更有效
        "max_vol_ratio": 1.0,    # 关键！缩量涨停 >> 放量涨停
        "open_break_stop": -5.0, # 开板日跌>5%直接出场
    },
    "20pct": {  # 创业板、科创板
        "min_return": 19.8,
        "max_seal": 2.8,
        "min_upper": 2.0,
        "max_upper": 8.0,
        "max_volatility": 10.0,
        # 注意: 振幅/实体比是连板窗口均值，首板日无法计算，暂不加入过滤
        # 横向分析结论留待runner参数优化时使用
    },
}


def load_daily(code: str, start: str, end: str) -> Optional[pd.DataFrame]:
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


# ================================================================
# 特征提取
# ================================================================

def extract_limit_up_features(
    df: pd.DataFrame,
    code: str,
    i: int,
    lookback: int = 5,
) -> Optional[Dict[str, Any]]:
    """
    提取第 i 天涨停时的特征（只用 i 及之前数据）。

    i 必须是涨停日的 index 位置。
    返回 None 如果数据不足。
    """
    if i < lookback + 1:
        return None

    close = df["close"].values
    open_ = df["open"].values
    high = df["high"].values
    low = df["low"].values
    volume = df["volume"].values.astype(float)

    fl_c = close[i]
    fl_o = open_[i]
    fl_h = high[i]
    fl_l = low[i]
    fl_v = volume[i]
    prev_c = close[i - 1]

    # 涨停阈值
    threshold = lim_thresh(code)

    # 判断是否一字板（开盘即涨停，振幅极小）
    # 容差: 开盘价距涨停价 ≤1%，且振幅 ≤1%
    limit_up_price = prev_c * (1 + threshold)
    gap_to_limit = abs(fl_o - limit_up_price) / limit_up_price
    amp = (fl_h - fl_l) / prev_c if prev_c > 0 else 999
    is_yizi = gap_to_limit < 0.01 and amp < 0.01

    if is_yizi:
        return None  # 一字板无法买入，跳过

    # 归一化系数: 主板×2, 创业板/科创板×1 → 统一到20%基准
    scale = board_scale(code)

    # 当日涨幅（原始）
    fl_return = (fl_c / prev_c - 1) * 100

    # 封板强度：(close - low) / close * 100
    fl_seal = (fl_c - fl_l) / fl_c * 100 if fl_c > 0 else 999

    # 上影线
    fl_upper_shadow = (fl_h - fl_c) / prev_c * 100

    # 振幅
    fl_amplitude = (fl_h - fl_l) / prev_c * 100

    # 实体比: |close - open| / (high - low)
    bar_range = fl_h - fl_l
    fl_body_ratio = abs(fl_c - fl_o) / bar_range if bar_range > 0 else 1.0

    # 量比（vs 前5天均量）
    vol_window = volume[max(0, i - lookback):i]
    avg_vol = vol_window.mean() if len(vol_window) > 0 else fl_v
    fl_vol_ratio = fl_v / avg_vol if avg_vol > 0 else 0

    # 前5天波动率（日收益率标准差）
    rets = []
    for j in range(i - lookback, i):
        if close[j - 1] > 0:
            rets.append(close[j] / close[j - 1] - 1)
    volatility = np.std(rets) * 100 if len(rets) >= 3 else 999

    # 前5天累计涨幅
    if close[i - lookback - 1] > 0:
        prev5_return = (prev_c / close[i - lookback - 1] - 1) * 100
    else:
        prev5_return = 0

    return {
        "code": code,
        "board": get_board(code),
        "branch": "20pct" if is_20pct_board(code) else "10pct",
        "date": df.index[i],
        "date_str": df.index[i].strftime("%Y-%m-%d"),
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
    # 主板：量比
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
) -> Dict[str, Any]:
    """
    逐日推进出场，不看未来数据。

    入场: entry_idx 的 open（已传入 entry_price）
    出场优先级:
      1. 固定止损（任何状态都生效）
      2. 开板判定（当天不涨停 → 收盘卖出）
      3. 止盈 / 追踪止损（开板后才生效，涨停封板期间不触发）
      4. 最大持仓天数

    关键设计: 涨停封板期间只检查止损和开板，不检查止盈/追踪。
    这样多板股可以持续持有直到开板。
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
        l = low[pos]
        prev_c = close[pos - 1]

        # 更新最高价
        if h > peak:
            peak = h

        ret = (c / entry_price - 1) * 100

        # 1. 固定止损（最高优先级，任何状态都生效）
        #    加 0.01% 容差防止浮点精度问题
        if ret <= -stop_loss + 0.01:
            return {
                "exit_idx": pos,
                "pnl_pct": round(ret, 2),
                "hold_days": hold,
                "exit_reason": f"止损{stop_loss}%",
            }

        # 2. 开板判定
        #    核心逻辑: 涨停封板 = 收盘价 ≈ 前收×(1+涨停阈值)
        #    用 close 而非 open 判定，因为 A 股 T+1：涨停封板时只能收盘才能确认
        #    容差: 当天收盘距涨停价 > 2% → 认为开板
        if prev_c > 0:
            limit_price = prev_c * (1 + threshold)
            gap_from_limit = (limit_price - c) / limit_price
            is_limit_up = gap_from_limit < 0.02  # 收盘距涨停<2% → 还在涨停
        else:
            is_limit_up = False

        if not is_limit_up:
            # 开板了！检查止盈/追踪止损
            peak_ret = (peak / entry_price - 1) * 100

            # 止盈
            if ret >= take_profit:
                return {
                    "exit_idx": pos,
                    "pnl_pct": round(ret, 2),
                    "hold_days": hold,
                    "exit_reason": f"止盈{take_profit}%",
                }

            # 追踪止损
            if peak_ret >= trailing_activate:
                dd = (peak - c) / entry_price * 100
                if dd >= trailing_callback:
                    return {
                        "exit_idx": pos,
                        "pnl_pct": round(ret, 2),
                        "hold_days": hold,
                        "exit_reason": f"追踪止损(峰+{peak_ret:.1f}%→回撤{dd:.1f}%)",
                    }

            # 开板即卖（默认行为）
            return {
                "exit_idx": pos,
                "pnl_pct": round(ret, 2),
                "hold_days": hold,
                "exit_reason": "开板卖出",
            }

        # 3. 涨停封板中：继续持有
        #    不触发止盈/追踪（涨停封板期间卖不出去）

        # 4. 最大持仓天数（防止极端情况）
        if hold >= max_hold:
            return {
                "exit_idx": pos,
                "pnl_pct": round(ret, 2),
                "hold_days": hold,
                "exit_reason": f"持仓{max_hold}天",
            }

    # 数据结束
    ret = (close[-1] / entry_price - 1) * 100 if entry_price > 0 else 0
    return {
        "exit_idx": n - 1,
        "pnl_pct": round(ret, 2),
        "hold_days": hold,
        "exit_reason": "数据结束",
    }


# ================================================================
# 主策略
# ================================================================

def run_strategy(
    codes: List[str],
    start_date: str = "2023-01-01",
    end_date: str = "2026-05-21",
    min_return: float = 20.0,
    max_seal: float = 2.8,
    min_upper: float = 2.0,
    max_upper: float = 8.0,
    max_volatility: float = 10.0,
    take_profit: float = 15.0,
    trailing_activate: float = 5.0,
    trailing_callback: float = 8.0,
    stop_loss: float = 10.0,
    max_hold: int = 20,
) -> List[Dict[str, Any]]:
    """
    全市场逐日扫描，过滤 + 出场引擎。
    """
    print(f"\n{'='*70}")
    print(f"  连板猎手 v2 — 双分支过滤 + 机械出场")
    print(f"{'='*70}")
    print(f"  区间: {start_date} ~ {end_date}")
    print(f"  分支A (主板10%): 涨幅≥{BOARD_PARAMS['10pct']['min_return']}% 封板≤{BOARD_PARAMS['10pct']['max_seal']}% "
          f"上影{BOARD_PARAMS['10pct']['min_upper']}~{BOARD_PARAMS['10pct']['max_upper']}% 波动≤{BOARD_PARAMS['10pct']['max_volatility']}%")
    print(f"  分支B (创/科20%): 涨幅≥{BOARD_PARAMS['20pct']['min_return']}% 封板≤{BOARD_PARAMS['20pct']['max_seal']}% "
          f"上影{BOARD_PARAMS['20pct']['min_upper']}~{BOARD_PARAMS['20pct']['max_upper']}% 波动≤{BOARD_PARAMS['20pct']['max_volatility']}%")
    print(f"  出场: 止盈{take_profit}% 追踪{trailing_activate}%→{trailing_callback}% 止损{stop_loss}% 最大{max_hold}天")
    print(f"  股票: {len(codes)} 只")

    # 加载范围多取一些
    buffer = max_hold + 15
    query_start = (datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=buffer * 2)).strftime("%Y-%m-%d")
    query_end = (datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=buffer)).strftime("%Y-%m-%d")

    # 第一遍：加载所有股票数据，按日期收集涨停信号
    print(f"\n  [1/2] 扫描涨停信号...")
    day_signals = defaultdict(list)  # date_str -> [features]
    code_data = {}  # code -> (df, dates_arr, close_arr, ...)
    total = len(codes)
    loaded = 0

    for idx, code in enumerate(codes):
        if (idx + 1) % 500 == 0 or idx == 0:
            print(f"\r   加载: {idx+1}/{total}  已加载: {loaded}", end="", flush=True)

        try:
            df = load_daily(code, query_start, query_end)
            if df is None or len(df) < 30:
                continue

            # 限定扫描区间
            sd = pd.Timestamp(start_date)
            ed = pd.Timestamp(end_date)
            df_scan = df[(df.index >= sd) & (df.index <= ed)]
            if len(df_scan) < 10:
                continue

            loaded += 1
            threshold = lim_thresh(code)

            # 找涨停日
            close_arr = df_scan["close"].values
            dates = df_scan.index

            for i in range(1, len(close_arr)):
                day_ret = close_arr[i] / close_arr[i - 1] - 1
                if day_ret < threshold * 0.98:  # 留 2% 容差
                    continue

                # 涨停！提取特征
                feat = extract_limit_up_features(df_scan, code, i)
                if feat is None:
                    continue

                # 检查是否是第一板（前一天不涨停）
                if i >= 1:
                    prev_ret = close_arr[i - 1] / close_arr[i - 2] - 1 if i >= 2 else 0
                    if prev_ret >= threshold * 0.98:
                        continue  # 不是第一板，跳过

                day_signals[feat["date_str"]].append(feat)

            # 缓存数据（用于回测）
            code_data[code] = {
                "df": df_scan,
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

    # 第二遍：逐日过滤 + 模拟交易
    print(f"\n  [2/2] 过滤 + 回测...")
    all_trades = []
    signal_count = 0
    filtered_count = 0

    for date_str in sorted(day_signals):
        feats = day_signals[date_str]
        signal_count += len(feats)

        # 应用过滤
        passed = [f for f in feats if passes_filter(
            f, min_return, max_seal, min_upper, max_upper, max_volatility
        )]
        filtered_count += len(passed)

        # 对通过过滤的信号模拟交易
        for feat in passed:
            code = feat["code"]
            if code not in code_data:
                continue

            data = code_data[code]
            dates = data["dates"]

            # 找到信号日的位置
            signal_date = feat["date"]
            if signal_date not in dates:
                continue
            signal_idx = dates.get_loc(signal_date)

            # T+1 开盘买入
            buy_idx = signal_idx + 1
            if buy_idx >= len(data["close"]):
                continue

            entry_price = data["open"][buy_idx]
            if entry_price <= 0:
                continue

            # 模拟出场
            result = simulate_trade(
                data["close"], data["high"], data["low"], data["open"],
                buy_idx, entry_price, data["threshold"],
                take_profit, trailing_activate, trailing_callback,
                stop_loss, max_hold,
            )

            # 记录
            exit_date = data["dates"][result["exit_idx"]]
            all_trades.append({
                "code": code,
                "board": feat["board"],
                "branch": feat.get("branch", "10pct"),
                "signal_date": date_str,
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
                "volatility": round(feat["volatility"], 1),
            })

    print(f"   涨停信号: {signal_count}  通过过滤: {filtered_count}  交易: {len(all_trades)}")
    return all_trades


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

    print(f"\n  交易数: {n}  盈利: {len(wins)}  亏损: {len(losses)}")
    print(f"  胜率: {wr:.1f}%")
    print(f"  平均收益: {avg:+.2f}%  中位数: {sorted(pnl)[n//2]:+.2f}%")
    print(f"  盈利均值: {avg_win:+.2f}%  亏损均值: {-avg_loss:+.2f}%")
    print(f"  盈亏比: {pf:.2f}")
    print(f"  期望值: {avg:+.2f}%/笔")
    print(f"  最大盈利: {max(pnl):+.2f}%  最大亏损: {min(pnl):+.2f}%")

    # 持仓天数
    hd = [t["hold_days"] for t in trades]
    print(f"\n  持仓天数: 均值={sum(hd)/len(hd):.1f}  中位数={sorted(hd)[len(hd)//2]}")

    # 出场原因
    print(f"\n  出场原因:")
    reason_groups = defaultdict(list)
    for t in trades:
        reason_groups[t["exit_reason"]].append(t["pnl_pct"])
    for reason, pnls in sorted(reason_groups.items(), key=lambda x: -len(x[1])):
        avg_r = sum(pnls) / len(pnls)
        wr_r = sum(1 for p in pnls if p > 0) / len(pnls) * 100
        print(f"    {reason:40s}  {len(pnls):3d}笔  均值={avg_r:+.2f}%  胜率={wr_r:.0f}%")

    # 按板块分支
    print(f"\n  按板块:")
    board_groups = defaultdict(list)
    for t in trades:
        board_groups[t["board"]].append(t["pnl_pct"])
    for board, pnls in sorted(board_groups.items(), key=lambda x: -len(x[1])):
        avg_b = sum(pnls) / len(pnls)
        wr_b = sum(1 for p in pnls if p > 0) / len(pnls) * 100
        print(f"    {board:8s} ({len(pnls):3d}笔): 胜率={wr_b:.0f}%  均值={avg_b:+.2f}%")

    # 收益分桶（按原始收益，标注板块）
    print(f"\n  收益分桶（原始收益）:")
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

    # 按年度
    print(f"\n  按年度:")
    year_groups = defaultdict(list)
    for t in trades:
        year_groups[t["signal_date"][:4]].append(t["pnl_pct"])
    for year, pnls in sorted(year_groups.items()):
        avg_y = sum(pnls) / len(pnls)
        wr_y = sum(1 for p in pnls if p > 0) / len(pnls) * 100
        print(f"    {year} ({len(pnls):3d}笔): 胜率={wr_y:.0f}%  均值={avg_y:+.2f}%")

    # 收益分布
    print(f"\n  收益分布:")
    bins = [(-999, -15), (-15, -10), (-10, -5), (-5, 0), (0, 5), (5, 10), (10, 15), (15, 20), (20, 999)]
    labels = ["<-15%", "-15~-10%", "-10~-5%", "-5~0%", "0~5%", "5~10%", "10~15%", "15~20%", ">20%"]
    for (lo, hi), label in zip(bins, labels):
        cnt = sum(1 for p in pnl if lo <= p < hi)
        bar = "█" * int(cnt / n * 40)
        print(f"    {label:12s}  {cnt:4d} ({cnt/n*100:5.1f}%)  {bar}")

    # 最差5笔
    sorted_trades = sorted(trades, key=lambda x: x["pnl_pct"])
    print(f"\n  亏损最多5笔:")
    for t in sorted_trades[:5]:
        print(f"    {t['code']:>8s} {t['signal_date']} → {t['exit_date']} "
              f"持仓{t['hold_days']}天 收益{t['pnl_pct']:+.2f}% {t['exit_reason']}")

    # 最好5笔
    print(f"\n  盈利最多5笔:")
    for t in sorted_trades[-5:][::-1]:
        print(f"    {t['code']:>8s} {t['signal_date']} → {t['exit_date']} "
              f"持仓{t['hold_days']}天 收益{t['pnl_pct']:+.2f}% {t['exit_reason']}")


def main():
    parser = argparse.ArgumentParser(description="连板猎手 v2 — 横向过滤 + 机械出场")
    parser.add_argument("--start", type=str, default="2023-01-01")
    parser.add_argument("--end", type=str, default="2026-05-21")
    parser.add_argument("--quick", action="store_true", help="抽样500只")
    parser.add_argument("--sample", type=int, default=0)

    # 过滤参数
    parser.add_argument("--min-return", type=float, default=20.0, help="第一板最小涨幅%")
    parser.add_argument("--max-seal", type=float, default=2.8, help="封板强度上限%")
    parser.add_argument("--min-upper", type=float, default=2.0, help="上影线下限%")
    parser.add_argument("--max-upper", type=float, default=8.0, help="上影线上限%")
    parser.add_argument("--max-volatility", type=float, default=10.0, help="前5天波动率上限%")

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
    codes = get_all_codes()
    print(f"   全市场: {len(codes)} 只股票")

    if args.quick:
        codes = codes[:500]
    elif args.sample > 0:
        codes = codes[:args.sample]

    trades = run_strategy(
        codes=codes,
        start_date=args.start,
        end_date=args.end,
        min_return=args.min_return,
        max_seal=args.max_seal,
        min_upper=args.min_upper,
        max_upper=args.max_upper,
        max_volatility=args.max_volatility,
        take_profit=args.tp,
        trailing_activate=args.trail_activate,
        trailing_callback=args.trail_callback,
        stop_loss=args.stop_loss,
        max_hold=args.max_hold,
    )

    analyze_results(trades)

    if trades:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        fields = ["code", "board", "branch", "signal_date", "buy_date", "exit_date",
                  "entry_price", "exit_price", "pnl_pct", "hold_days", "exit_reason",
                  "fl_return", "fl_seal", "fl_upper_shadow", "fl_amplitude", "volatility"]
        with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for t in trades:
                w.writerow({k: t.get(k, "") for k in fields})
        print(f"\n💾 交易明细已保存: {args.output}")


if __name__ == "__main__":
    main()
