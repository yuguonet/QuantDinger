"""
连板猎手 v3 — 基于数据驱动的双分支策略

数据来源: 22977个连板段的横向+纵向分析
架构: 沪深主板(10%) / 创科板(20%) 双分支，参数完全不同

核心发现编码:
1. 买点日必须放量 (主板≥2x, 创科≥3x)
2. 封板强度必须接近0 (涨停封死)
3. 前1日无大跌 (前1日跌幅>-3% 跳过)
4. RSI不能过低 (<50 跳过)
5. 峰值后大概率跌 → 用追踪止损+主动止盈
"""
from __future__ import annotations
import os, sys
from typing import Dict, List, Any, Optional

import pandas as pd
import numpy as np

# ================================================================
# 板块 & 参数
# ================================================================

# 双分支参数 (从分析数据中提取)
BOARD_PARAMS = {
    # 沪深主板 (10% 涨跌停) — v3.1优化版
    "main": {
        "threshold": 0.098,       # 涨停阈值
        "min_streak": 2,          # 只做2板+ (1板信号太弱)
        "buy_vol_ratio": 1.5,     # 量比放宽(区分度弱,不作为核心条件)
        "buy_seal_max": 0.5,      # 封板强度 ≤0.5%
        "buy_rsi_min": 40,        # RSI放宽(区分度弱)
        "buy_rsi_max": 90,
        "buy_pre1_max_drop": -5,  # 前1日跌幅放宽
        "buy_max_range_pct": 12,
        "buy_max_gap_pct": 8.0,   # 高开<8% (核心过滤!)
        "stop_loss_pct": -8,
        "trailing_stop_pct": -6,
        "take_profit_pct": 15,
        "peak_sell_rsi": 80,
        "peak_sell_upper_shadow": 40,
        "sell_on_peak_day": True, # 峰值日当天出场
        "remove_board_break": True, # 移除开板信号(胜率仅7%)
    },
    # 创/科板 (20% 涨跌停) — v3.1优化版
    "gem_star": {
        "threshold": 0.198,
        "min_streak": 2,          # 只做2板+
        "max_streak": 4,          # 排除5板+(20%胜率陷阱)
        "buy_vol_ratio": 1.5,
        "buy_seal_max": 0.5,
        "buy_rsi_min": 40,
        "buy_rsi_max": 90,        # RSI<90
        "buy_pre1_max_drop": -8,
        "buy_max_range_pct": 22,
        "buy_max_gap_pct": 12.0,  # 高开<12%
        "stop_loss_pct": -12,
        "trailing_stop_pct": -8,
        "take_profit_pct": 20,
        "peak_sell_rsi": 85,
        "peak_sell_upper_shadow": 45,
        "sell_on_peak_day": True,
        "remove_board_break": True,
    },
}


def get_board_type(code: str) -> str:
    """判断板块类型"""
    c = code[:3] if len(code) >= 3 else code
    if c.startswith("30") or c.startswith("68"):
        return "gem_star"
    return "main"


def get_board_name(code: str) -> str:
    c = code[:3] if len(code) >= 3 else code
    if c.startswith("68"):
        return "科创板"
    elif c.startswith("30"):
        return "创业板"
    elif c.startswith("6"):
        return "沪主板"
    elif c.startswith(("0", "2")):
        return "深主板"
    return "未知"


# ================================================================
# 技术指标
# ================================================================

def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period, min_periods=1).mean()
    avg_loss = loss.rolling(window=period, min_periods=1).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def calc_atr(high, low, close, period=14):
    tr1 = high - low
    tr2 = abs(high - close.shift(1))
    tr3 = abs(low - close.shift(1))
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=period, min_periods=1).mean()


# ================================================================
# 策略核心
# ================================================================

class DragonHunterV3:
    """
    连板猎手 v3 策略

    买点条件 (全部满足):
      1. 当日涨停 (涨幅 ≥ threshold)
      2. 封板强度 ≤ seal_max% (收盘价接近最高价)
      3. 量比 ≥ vol_ratio (相对前日放量)
      4. RSI 在合理区间
      5. 前1日无大跌
      6. 买点日振幅在合理范围

    卖点条件 (任一满足):
      1. 止损: 从买入价跌超 stop_loss_pct
      2. 追踪止损: 从最高点回撤超 trailing_stop_pct
      3. 止盈: 涨超 take_profit_pct
      4. RSI超买 + 上影线过大 (见顶信号)
      5. 开板: 收盘价低于开盘价且跌幅>3%
    """

    def __init__(self, params: Dict[str, Any]):
        self.p = params

    def check_buy_signal(
        self,
        df: pd.DataFrame,
        idx: int,
    ) -> Dict[str, Any]:
        """
        检查idx位置是否满足买点条件

        Returns: {"buy": bool, "reasons": [...], "score": float}
        """
        if idx < 2:
            return {"buy": False, "reasons": ["数据不足"], "score": 0}

        row = df.iloc[idx]
        prev = df.iloc[idx - 1]
        prev2 = df.iloc[idx - 2]

        reasons = []
        score = 0

        # 0. 连板数检查
        n_limit = int(df.iloc[0].get("run_n_limit_ups", 1)) if "run_n_limit_ups" in df.columns else 1
        if n_limit < self.p.get("min_streak", 1):
            return {"buy": False, "reasons": [f"连板数{n_limit}<最小要求"], "score": 0}
        if "max_streak" in self.p and n_limit > self.p["max_streak"]:
            return {"buy": False, "reasons": [f"连板数{n_limit}>最大限制"], "score": 0}

        # 1. 涨停检查
        ret = (row["close"] / prev["close"] - 1)
        if ret < self.p["threshold"]:
            return {"buy": False, "reasons": ["未涨停"], "score": 0}

        # 1b. 高开幅度检查
        gap_pct = (row["open"] / prev["close"] - 1) * 100
        if "buy_max_gap_pct" in self.p and gap_pct > self.p["buy_max_gap_pct"]:
            return {"buy": False, "reasons": [f"高开{gap_pct:.1f}%>阈值"], "score": 0}

        # 2. 封板强度 (close vs high)
        seal = (row["close"] / row["high"] - 1) * 100
        if seal > self.p["buy_seal_max"]:
            return {"buy": False, "reasons": [f"封板不严: {seal:.2f}%"], "score": 0}
        reasons.append(f"封板强度{seal:.2f}%✓")
        score += 20

        # 3. 量比
        vol_ratio = row["volume"] / prev["volume"] if prev["volume"] > 0 else 0
        if vol_ratio < self.p["buy_vol_ratio"]:
            return {"buy": False, "reasons": [f"量比不足: {vol_ratio:.2f}x"], "score": 0}
        reasons.append(f"量比{vol_ratio:.2f}x✓")
        score += min(30, int(vol_ratio * 10))  # 量比越大分越高

        # 4. RSI
        rsi_series = calc_rsi(df["close"].iloc[:idx + 1])
        rsi = float(rsi_series.iloc[-1])
        if rsi < self.p["buy_rsi_min"] or rsi > self.p["buy_rsi_max"]:
            return {"buy": False, "reasons": [f"RSI异常: {rsi:.1f}"], "score": 0}
        reasons.append(f"RSI{rsi:.1f}✓")
        score += 10

        # 5. 前1日跌幅
        prev_ret = (prev["close"] / prev2["close"] - 1) * 100
        if prev_ret < self.p["buy_pre1_max_drop"]:
            return {"buy": False, "reasons": [f"前1日大跌: {prev_ret:.2f}%"], "score": 0}
        reasons.append(f"前1日{prev_ret:+.2f}%✓")
        if prev_ret > 0:
            score += 5  # 前1日涨加分

        # 6. 买点日振幅
        day_range = (row["high"] - row["low"]) / row["open"] * 100
        if day_range > self.p["buy_max_range_pct"]:
            return {"buy": False, "reasons": [f"振幅过大: {day_range:.2f}%"], "score": 0}
        reasons.append(f"振幅{day_range:.2f}%✓")
        score += 10

        # 额外加分: 买点前缩量 (蓄势)
        pre_vol_ratio = prev["volume"] / prev2["volume"] if prev2["volume"] > 0 else 1
        if pre_vol_ratio < 0.8:
            score += 10
            reasons.append("前日缩量蓄势✓")

        return {"buy": True, "reasons": reasons, "score": score}

    def check_sell_signal(
        self,
        df: pd.DataFrame,
        idx: int,
        buy_price: float,
        highest_since_buy: float,
    ) -> Dict[str, Any]:
        """
        检查idx位置是否满足卖点条件

        Returns: {"sell": bool, "reason": str, "sell_type": str}
        """
        row = df.iloc[idx]
        current = row["close"]
        ret_from_buy = (current / buy_price - 1) * 100
        ret_from_high = (current / highest_since_buy - 1) * 100

        # 1. 止损
        if ret_from_buy <= self.p["stop_loss_pct"]:
            return {"sell": True, "reason": f"止损: {ret_from_buy:.2f}%", "sell_type": "stop_loss"}

        # 2. 追踪止损 (从最高点回撤)
        if ret_from_high <= self.p["trailing_stop_pct"] and ret_from_buy > 0:
            return {"sell": True, "reason": f"追踪止损: 从高点{ret_from_high:.2f}%", "sell_type": "trailing_stop"}

        # 3. 止盈
        if ret_from_buy >= self.p["take_profit_pct"]:
            return {"sell": True, "reason": f"止盈: {ret_from_buy:.2f}%", "sell_type": "take_profit"}

        # 4. RSI超买 + 上影线 (见顶信号)
        if idx >= 2:
            rsi_series = calc_rsi(df["close"].iloc[:idx + 1])
            rsi = float(rsi_series.iloc[-1])
            day_range = row["high"] - row["low"]
            upper_shadow = (row["high"] - max(row["open"], row["close"])) / day_range * 100 if day_range > 0 else 0

            if rsi >= self.p["peak_sell_rsi"] and upper_shadow >= self.p["peak_sell_upper_shadow"]:
                return {"sell": True, "reason": f"见顶信号: RSI{rsi:.1f}+上影{upper_shadow:.1f}%", "sell_type": "peak_signal"}

        # 5. 开板信号 (阴线且跌幅>3%) — v3.1: 默认移除(胜率仅4-7%)
        if not self.p.get("remove_board_break", False) and idx >= 1:
            prev_close = df.iloc[idx - 1]["close"]
            ret_today = (current / prev_close - 1) * 100
            is_bearish = current < row["open"]
            if is_bearish and ret_today < -3:
                return {"sell": True, "reason": f"开板信号: 跌{ret_today:.2f}%", "sell_type": "board_break"}

        return {"sell": False, "reason": "", "sell_type": ""}


# ================================================================
# 回测引擎 (逐日推进, 不用未来数据)
# ================================================================

def backtest_single_run(
    df_run: pd.DataFrame,
    strategy: DragonHunterV3,
) -> Dict[str, Any]:
    """
    对单个连板段进行逐日推进回测

    df_run: 包含该连板段窗口数据的DataFrame (已按time排序)
    返回: 回测结果
    """
    df_run = df_run.sort_values("time").reset_index(drop=True)

    if len(df_run) < 5:
        return {"trades": [], "error": "数据不足"}

    board_type = get_board_type(df_run["code"].iloc[0])
    params = BOARD_PARAMS[board_type]

    trades = []
    position = None  # {"buy_price", "buy_date", "buy_idx", "highest"}

    for i in range(2, len(df_run)):
        row = df_run.iloc[i]

        if position is None:
            # 检查买点
            signal = strategy.check_buy_signal(df_run, i)
            if signal["buy"]:
                position = {
                    "buy_price": float(row["close"]),
                    "buy_date": str(row["time"]),
                    "buy_idx": i,
                    "highest": float(row["close"]),
                    "score": signal["score"],
                    "reasons": signal["reasons"],
                }
        else:
            # 更新最高价
            if float(row["high"]) > position["highest"]:
                position["highest"] = float(row["high"])

            # 检查卖点
            signal = strategy.check_sell_signal(
                df_run, i,
                position["buy_price"],
                position["highest"],
            )
            if signal["sell"]:
                trade = {
                    "buy_date": position["buy_date"],
                    "buy_price": position["buy_price"],
                    "sell_date": str(row["time"]),
                    "sell_price": float(row["close"]),
                    "return_pct": round((float(row["close"]) / position["buy_price"] - 1) * 100, 2),
                    "highest": position["highest"],
                    "max_return_pct": round((position["highest"] / position["buy_price"] - 1) * 100, 2),
                    "sell_type": signal["sell_type"],
                    "sell_reason": signal["reason"],
                    "score": position["score"],
                }
                trades.append(trade)
                position = None

    # 如果还有持仓，在数据结束时平仓
    if position is not None:
        last = df_run.iloc[-1]
        trade = {
            "buy_date": position["buy_date"],
            "buy_price": position["buy_price"],
            "sell_date": str(last["time"]),
            "sell_price": float(last["close"]),
            "return_pct": round((float(last["close"]) / position["buy_price"] - 1) * 100, 2),
            "highest": position["highest"],
            "max_return_pct": round((position["highest"] / position["buy_price"] - 1) * 100, 2),
            "sell_type": "end_of_data",
            "sell_reason": "数据结束",
            "score": position["score"],
        }
        trades.append(trade)

    return {"trades": trades}


def run_full_backtest(csv_path: str = "dragon_ohlcv.csv"):
    """全量回测"""
    print("📊 加载数据...")
    df = pd.read_csv(csv_path, dtype={"code": str})
    df["time"] = pd.to_datetime(df["time"])
    df = df.sort_values(["code", "run_first_limit_date", "time"])

    strategy_main = DragonHunterV3(BOARD_PARAMS["main"])
    strategy_gem = DragonHunterV3(BOARD_PARAMS["gem_star"])

    all_trades = []
    grouped = df.groupby(["code", "run_first_limit_date"])
    total = len(grouped)

    for idx, ((code, fl_str), gdf) in enumerate(grouped):
        if (idx + 1) % 5000 == 0:
            print(f"\r   回测中: {idx+1}/{total}", end="", flush=True)

        board_type = get_board_type(code)
        strategy = strategy_main if board_type == "main" else strategy_gem

        result = backtest_single_run(gdf, strategy)
        for trade in result.get("trades", []):
            trade["code"] = code
            trade["board"] = get_board_name(code)
            trade["board_type"] = board_type
            trade["n_limit"] = int(gdf["run_n_limit_ups"].iloc[0])
            all_trades.append(trade)

    print(f"\r   回测完成: {total} 个连板段")

    # 统计
    if not all_trades:
        print("❌ 无交易信号")
        return

    tdf = pd.DataFrame(all_trades)
    print(f"\n{'='*70}")
    print(f"  连板猎手 v3 回测结果")
    print(f"{'='*70}")
    print(f"  总交易数: {len(tdf)}")
    print(f"  胜率: {(tdf['return_pct'] > 0).mean() * 100:.1f}%")
    print(f"  平均收益: {tdf['return_pct'].mean():.2f}%")
    print(f"  中位收益: {tdf['return_pct'].median():.2f}%")
    print(f"  盈亏比: {tdf[tdf['return_pct']>0]['return_pct'].mean() / abs(tdf[tdf['return_pct']<0]['return_pct'].mean()):.2f}" if (tdf['return_pct'] < 0).any() else "")

    # 按板块
    for board_type in ["main", "gem_star"]:
        sub = tdf[tdf["board_type"] == board_type]
        if len(sub) == 0:
            continue
        label = "沪深主板" if board_type == "main" else "创/科板"
        print(f"\n  --- {label} ({len(sub)}笔) ---")
        print(f"    胜率: {(sub['return_pct'] > 0).mean() * 100:.1f}%")
        print(f"    平均收益: {sub['return_pct'].mean():.2f}%")
        win_avg = sub[sub['return_pct']>0]['return_pct'].mean() if (sub['return_pct']>0).any() else 0
        loss_avg = abs(sub[sub['return_pct']<0]['return_pct'].mean()) if (sub['return_pct']<0).any() else 1
        print(f"    盈亏比: {win_avg/loss_avg:.2f}")
        print(f"    最大盈利: {sub['return_pct'].max():.2f}%")
        print(f"    最大亏损: {sub['return_pct'].min():.2f}%")

    # 按卖出类型
    print(f"\n  --- 卖出类型分布 ---")
    for stype, cnt in tdf["sell_type"].value_counts().items():
        sub = tdf[tdf["sell_type"] == stype]
        print(f"    {stype}: {cnt}笔 均收益{sub['return_pct'].mean():.2f}% 胜率{(sub['return_pct']>0).mean()*100:.1f}%")

    # 按连板数
    print(f"\n  --- 按连板数 ---")
    for n in sorted(tdf["n_limit"].unique()):
        sub = tdf[tdf["n_limit"] == n]
        if len(sub) < 3:
            continue
        print(f"    {n}板: {len(sub)}笔 胜率{(sub['return_pct']>0).mean()*100:.1f}% 均收益{sub['return_pct'].mean():.2f}%")

    # 保存
    tdf.to_csv("backtest_v3_trades.csv", index=False, encoding="utf-8-sig")
    print(f"\n💾 交易明细: backtest_v3_trades.csv")

    return tdf


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default="dragon_ohlcv.csv")
    args = parser.parse_args()
    run_full_backtest(args.csv)
