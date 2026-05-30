"""
连板猎手策略 — 全市场回测

策略逻辑:
  买: 第一板涨停当天，量比(vs前一天) > vol_ratio_threshold
  持: 涨停就拿着
  卖: 开板日（当天不涨停）收盘卖出

全市场扫描 db_market，找出所有第一板涨停日，
应用买入条件，跟踪持仓到开板，统计收益。

用法:
    python strategy_dragon_board.py                        # 全量
    python strategy_dragon_board.py --quick                 # 抽样500只
    python strategy_dragon_board.py --vol-ratio 1.5         # 自定义量比阈值
    python strategy_dragon_board.py --start 2024-01-01      # 自定义起始日
"""
from __future__ import annotations

import os
import sys
import argparse
from datetime import datetime, timedelta
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


def load_daily(code: str, start: str, end: str) -> Optional[pd.DataFrame]:
    from adjust_utils import adjust_daily_df
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
    return adjust_daily_df(df, code)


def run_strategy(
    codes: List[str],
    vol_ratio_threshold: float = 1.2,
    start_date: str = "2023-01-01",
    end_date: str = "2026-05-21",
    max_hold_days: int = 20,
) -> pd.DataFrame:
    """
    全市场扫描连板策略。

    对每只股票:
      1. 逐日扫描，找到涨停日
      2. 检查量比是否满足条件
      3. 满足则买入，跟踪到开板卖出

    Returns:
        DataFrame，每行一笔交易
    """
    # 加载范围多取一些数据用于量比计算
    buffer = 10
    query_start = (datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=buffer*2)).strftime("%Y-%m-%d")
    query_end = (datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=5)).strftime("%Y-%m-%d")

    trades = []
    total = len(codes)
    errors = 0

    for idx, code in enumerate(codes):
        if (idx+1) % 200 == 0 or idx == 0:
            print(f"\r   扫描: {idx+1}/{total}  交易: {len(trades)}  错误: {errors}",
                  end="", flush=True)
        try:
            df = load_daily(code, query_start, query_end)
            if df is None or len(df) < 20:
                continue

            # 限定区间
            sd = pd.Timestamp(start_date)
            ed = pd.Timestamp(end_date)
            df = df[(df.index >= sd) & (df.index <= ed)]
            if len(df) < 10:
                continue

            threshold = lim_thresh(code)
            board = get_board(code)
            close = df["close"].values
            high = df["high"].values
            low = df["low"].values
            volume = df["volume"].values.astype(float)
            dates = df.index

            i = 0
            while i < len(df) - 1:
                # 检查今天是否涨停
                if i == 0:
                    i += 1
                    continue

                day_ret = close[i] / close[i-1] - 1
                if day_ret < threshold:
                    i += 1
                    continue

                # 今天涨停！检查量比
                vol_ratio = volume[i] / volume[i-1] if volume[i-1] > 0 else 0

                if vol_ratio < vol_ratio_threshold:
                    i += 1
                    continue

                # 满足买入条件
                buy_price = close[i]
                buy_date = dates[i]
                buy_idx = i

                # 跟踪持仓
                sell_price = None
                sell_date = None
                hold_days = 0
                peak_price = buy_price
                max_dd = 0

                for j in range(i+1, min(i+max_hold_days+1, len(df))):
                    hold_days += 1

                    # 更新最高价和回撤
                    if high[j] > peak_price:
                        peak_price = high[j]
                    dd = (peak_price - low[j]) / peak_price if peak_price > 0 else 0
                    if dd > max_dd:
                        max_dd = dd

                    # 判断是否开板
                    if j > 0:
                        ret = close[j] / close[j-1] - 1
                        if ret < threshold:
                            # 开板，卖出
                            sell_price = close[j]
                            sell_date = dates[j]
                            break

                if sell_price is None:
                    # 持到最后一天
                    last_idx = min(buy_idx + max_hold_days, len(df)-1)
                    sell_price = close[last_idx]
                    sell_date = dates[last_idx]
                    hold_days = last_idx - buy_idx

                pnl = (sell_price / buy_price - 1) * 100

                trades.append({
                    "code": code,
                    "board": board,
                    "buy_date": buy_date.strftime("%Y-%m-%d"),
                    "sell_date": sell_date.strftime("%Y-%m-%d"),
                    "buy_price": round(buy_price, 3),
                    "sell_price": round(sell_price, 3),
                    "pnl_pct": round(pnl, 2),
                    "hold_days": hold_days,
                    "vol_ratio": round(vol_ratio, 2),
                    "max_dd_pct": round(max_dd*100, 1),
                    "buy_day_return": round(day_ret*100, 2),
                })

                # 跳到卖出日之后继续扫描
                i = df.index.get_loc(sell_date) + 1 if sell_date in df.index else j + 1
                continue

            # end while

        except Exception as e:
            errors += 1
            continue

    print(f"\n   完成: {total} 只股票, {len(trades)} 笔交易, {errors} 个错误")
    return pd.DataFrame(trades)


def analyze_results(df: pd.DataFrame, vol_threshold: float):
    """分析回测结果"""
    print(f"\n{'='*70}")
    print(f"  连板策略回测结果 (量比阈值: {vol_threshold}x)")
    print(f"{'='*70}")

    if len(df) == 0:
        print("  ❌ 无交易")
        return

    print(f"\n📊 总体:")
    print(f"  交易笔数: {len(df)}")
    print(f"  涉及股票: {df['code'].nunique()} 只")
    print(f"  均值收益: {df['pnl_pct'].mean():+.2f}%")
    print(f"  中位数收益: {df['pnl_pct'].median():+.2f}%")
    print(f"  胜率(>0%): {(df['pnl_pct']>0).mean()*100:.1f}%")
    print(f"  盈亏比: {df[df['pnl_pct']>0]['pnl_pct'].mean() / abs(df[df['pnl_pct']<0]['pnl_pct'].mean()):.2f}" if len(df[df['pnl_pct']<0])>0 else "  盈亏比: N/A")
    print(f"  大赚(>10%): {(df['pnl_pct']>10).mean()*100:.1f}%")
    print(f"  大亏(<-5%): {(df['pnl_pct']<-5).mean()*100:.1f}%")
    print(f"  持仓天数: 均值={df['hold_days'].mean():.1f}  中位数={df['hold_days'].median():.0f}")
    print(f"  最大回撤: 均值={df['max_dd_pct'].mean():.1f}%")

    # 按板块
    print(f"\n📊 按板块:")
    for board, g in df.groupby("board"):
        print(f"  {board}({len(g):4d}): 均值={g['pnl_pct'].mean():+.2f}% "
              f"胜率={(g['pnl_pct']>0).mean()*100:.0f}% "
              f"大亏={(g['pnl_pct']<-5).mean()*100:.0f}%")

    # 按量比分组
    print(f"\n📊 按第一板量比分组:")
    bins = [(1.2,1.5),(1.5,2),(2,3),(3,5),(5,999)]
    for lo,hi in bins:
        s = df[(df["vol_ratio"]>=lo)&(df["vol_ratio"]<hi)]
        if len(s)==0: continue
        label = f"{lo:.1f}~{hi:.1f}x" if hi<999 else f">{lo:.1f}x"
        print(f"  {label:>10s}({len(s):4d}): 均值={s['pnl_pct'].mean():+.2f}% "
              f"胜率={(s['pnl_pct']>0).mean()*100:.0f}% "
              f"大亏={(s['pnl_pct']<-5).mean()*100:.0f}%")

    # 收益分布
    print(f"\n📊 收益分布:")
    bins_r = [(-999,-10),(-10,-5),(-5,0),(0,5),(5,10),(10,20),(20,50),(50,999)]
    labels = ["<-10%","-10~-5%","-5~0%","0~5%","5~10%","10~20%","20~50%",">50%"]
    for (lo,hi), label in zip(bins_r, labels):
        cnt = ((df["pnl_pct"]>=lo)&(df["pnl_pct"]<hi)).sum()
        pct = cnt/len(df)*100
        bar = "█" * int(pct/2)
        print(f"  {label:>8s}: {cnt:>5d} ({pct:>5.1f}%) {bar}")

    # 最差和最好
    print(f"\n📊 亏损最多5笔:")
    for _, r in df.nsmallest(5, "pnl_pct").iterrows():
        print(f"  {r['code']:>8s}  {r['buy_date']}→{r['sell_date']}  "
              f"持仓{r['hold_days']}天  收益{r['pnl_pct']:+.2f}%  量比{r['vol_ratio']}x  {r['board']}")

    print(f"\n📊 盈利最多5笔:")
    for _, r in df.nlargest(5, "pnl_pct").iterrows():
        print(f"  {r['code']:>8s}  {r['buy_date']}→{r['sell_date']}  "
              f"持仓{r['hold_days']}天  收益{r['pnl_pct']:+.2f}%  量比{r['vol_ratio']}x  {r['board']}")


def main():
    parser = argparse.ArgumentParser(description="连板猎手策略全市场回测")
    parser.add_argument("--vol-ratio", type=float, default=1.2, help="量比阈值 (默认 1.2)")
    parser.add_argument("--start", type=str, default="2023-01-01")
    parser.add_argument("--end", type=str, default="2026-05-21")
    parser.add_argument("--quick", action="store_true", help="抽样500只")
    parser.add_argument("--sample", type=int, default=0)
    parser.add_argument("--max-hold", type=int, default=20, help="最大持仓天数")
    parser.add_argument("--output", type=str, default="analysis_output/dragon_strategy_trades.csv")

    args = parser.parse_args()

    print("🚀 连板猎手策略全市场回测")
    codes = get_all_codes()
    print(f"   全市场: {len(codes)} 只")

    if args.quick:
        codes = codes[:500]
    elif args.sample > 0:
        codes = codes[:args.sample]

    trades = run_strategy(
        codes=codes,
        vol_ratio_threshold=args.vol_ratio,
        start_date=args.start,
        end_date=args.end,
        max_hold_days=args.max_hold,
    )

    if len(trades) > 0:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        trades.to_csv(args.output, index=False, encoding="utf-8-sig")
        print(f"\n💾 交易明细: {args.output}")

    analyze_results(trades, args.vol_ratio)


if __name__ == "__main__":
    main()
