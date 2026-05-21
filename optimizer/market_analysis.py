"""
A 股全市场数据分析 — 策略设计前的数据画像 + 连板猎手

数据来源：db_market (CNStock_db)，不再依赖本地 CSV。

用法:
    python market_analysis.py              # 完整分析（抽样500只）
    python market_analysis.py --full       # 全量分析（全部5000+只）
    python market_analysis.py --15m        # 包含15分钟线分析
    python market_analysis.py --quick      # 快速模式（抽样200只）

连板猎手模式:
    python market_analysis.py --dragon                    # 全量扫描（≥2板）
    python market_analysis.py --dragon --quick            # 抽样扫描
    python market_analysis.py --dragon --min-streak=3     # ≥3板
    python market_analysis.py --dragon --max-gap=2        # 允许2天洗盘
    python market_analysis.py --dragon --full             # 全量5000+只
"""
import os
import sys
import pandas as pd
import numpy as np
from datetime import datetime
from collections import defaultdict
import json

# 确保 backend_api_python 在 path 中
_optimizer_dir = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.dirname(_optimizer_dir)
_backend_root = os.path.join(_project_root, "backend_api_python")
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

def _load_env():
    """加载 .env 文件"""
    try:
        from dotenv import load_dotenv
        _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _backend_root = os.path.join(_project_root, "backend_api_python")
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
    """延迟导入 db_market writer"""
    _load_env()
    from app.utils.db_market import get_market_kline_writer
    return get_market_kline_writer()


def _get_mgr():
    """延迟导入 db_market manager"""
    _load_env()
    from app.utils.db_market import get_market_db_manager
    return get_market_db_manager()


OUTPUT_DIR = os.path.join(_optimizer_dir, "analysis_output")


def load_csv(code: str, timeframe: str = "daily") -> pd.DataFrame:
    """从 db_market 加载数据并标准化"""
    tf_map = {"daily": "1D", "15m": "15m"}
    tf = tf_map.get(timeframe, timeframe)

    writer = _get_writer()
    data = writer.query("CNStock", code, tf, limit=10000)

    if not data:
        return None

    df = pd.DataFrame(data)
    df["date"] = pd.to_datetime(df["time"], unit="s")
    df = df.set_index("date").drop(columns=["time"])
    df = df.sort_index()

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    return df


def get_board(code: str) -> str:
    """判断板块"""
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


def get_all_codes() -> list:
    """获取全部日线股票代码"""
    writer = _get_writer()
    stats = writer.stats("CNStock")
    if not stats.get("exists"):
        print("❌ CNStock_db 不存在，请先运行 tdx_download --merge-db")
        sys.exit(1)
    return stats.get("symbol_list", [])


def load_15m_range(code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """加载指定日期范围的15分钟线数据"""
    writer = _get_writer()
    data = writer.query("CNStock", code, "15m",
                        start_time=start_date, end_time=end_date, limit=0)
    if not data:
        return None
    df = pd.DataFrame(data)
    df["date"] = pd.to_datetime(df["time"], unit="s")
    df = df.set_index("date").drop(columns=["time"])
    df = df.sort_index()
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def guess_exchange(code: str) -> str:
    """根据股票代码推断交易所后缀"""
    c = code.strip()
    if c.startswith("6"):
        return f"{c}.SH"
    elif c.startswith(("0", "3")):
        return f"{c}.SZ"
    elif c.startswith(("8", "4")):
        return f"{c}.BJ"
    else:
        return f"{c}.SZ"


# ============================================================
# 1. 市场整体统计
# ============================================================
def market_overview(all_codes: list, sample_n: int = 500):
    """市场整体概况"""
    print("\n" + "=" * 70)
    print("  1. 市场整体概况")
    print("=" * 70)

    sample = all_codes[:sample_n] if len(all_codes) > sample_n else all_codes

    # 板块分布
    boards = defaultdict(int)
    for code in all_codes:
        boards[get_board(code)] += 1

    print(f"\n📊 全市场股票数: {len(all_codes)}")
    print(f"   板块分布:")
    for board, count in sorted(boards.items(), key=lambda x: -x[1]):
        print(f"     {board}: {count} ({count/len(all_codes)*100:.1f}%)")

    # 加载数据统计
    all_returns = []
    all_volumes = []
    all_amounts = []
    date_ranges = []
    row_counts = []

    total = len(sample)
    for i, code in enumerate(sample):
        if (i + 1) % 100 == 0 or i == 0:
            print(f"\r   加载中: {i+1}/{total} ({(i+1)/total*100:.0f}%)", end="", flush=True)
        df = load_csv(code)
        if df is None or len(df) < 100:
            continue

        returns = df["close"].pct_change().dropna()
        all_returns.extend(returns.tolist())

        if "volume" in df.columns:
            all_volumes.append(df["volume"].mean())

        date_ranges.append((df.index.min(), df.index.max()))
        row_counts.append(len(df))

    all_returns = np.array(all_returns)
    all_returns = all_returns[~np.isnan(all_returns)]

    print(f"\n📈 收益率分布 (抽样 {len(sample)} 只, 共 {len(all_returns):,} 个交易日):")
    print(f"   均值: {all_returns.mean()*100:.4f}%")
    print(f"   中位数: {np.median(all_returns)*100:.4f}%")
    print(f"   标准差: {all_returns.std()*100:.4f}%")
    print(f"   偏度: {pd.Series(all_returns).skew():.4f}")
    print(f"   峰度: {pd.Series(all_returns).kurtosis():.4f}")
    print(f"   >5% 天数占比: {(all_returns > 0.05).sum() / len(all_returns) * 100:.2f}%")
    print(f"   >9.5% (涨停)占比: {(all_returns >= 0.095).sum() / len(all_returns) * 100:.4f}%")
    print(f"   <-5% 天数占比: {(all_returns < -0.05).sum() / len(all_returns) * 100:.2f}%")
    print(f"   <-9.5% (跌停)占比: {(all_returns <= -0.095).sum() / len(all_returns) * 100:.4f}%")

    if date_ranges:
        all_starts = [d[0] for d in date_ranges]
        all_ends = [d[1] for d in date_ranges]
        print(f"\n📅 数据覆盖:")
        print(f"   最早: {min(all_starts).strftime('%Y-%m-%d')}")
        print(f"   最晚: {max(all_ends).strftime('%Y-%m-%d')}")
        print(f"   平均交易日数: {int(np.median(row_counts))}")

    return all_returns


# ============================================================
# 2. 横截面分析 — 哪些股票适合做策略
# ============================================================
def cross_sectional_analysis(all_codes: list, sample_n: int = 500):
    """横截面分析：找出适合策略的股票特征"""
    print("\n" + "=" * 70)
    print("  2. 横截面分析 — 股票特征画像")
    print("=" * 70)

    sample = all_codes[:sample_n] if len(all_codes) > sample_n else all_codes

    stats = []
    total = len(sample)
    for i, code in enumerate(sample):
        if (i + 1) % 100 == 0 or i == 0:
            print(f"\r   分析中: {i+1}/{total} ({(i+1)/total*100:.0f}%)", end="", flush=True)
        df = load_csv(code)
        if df is None or len(df) < 200:
            continue

        returns = df["close"].pct_change().dropna()

        if len(returns) < 200:
            continue

        daily_vol = returns.std()
        annual_return = (1 + returns.mean()) ** 252 - 1
        annual_vol = daily_vol * np.sqrt(252)
        sharpe = annual_return / annual_vol if annual_vol > 0 else 0
        max_dd = ((df["close"] / df["close"].cummax()) - 1).min()

        limit_up = (returns >= 0.095).sum()
        limit_down = (returns <= -0.095).sum()

        vol_mean = df["volume"].mean()
        vol_std = df["volume"].std()
        vol_cv = vol_std / vol_mean if vol_mean > 0 else 0

        vol_price_corr = returns.corr(df["volume"].pct_change()) if len(returns) > 10 else 0
        autocorr = returns.autocorr(lag=1) if len(returns) > 10 else 0

        if "high" in df.columns and "low" in df.columns:
            amplitude = ((df["high"] - df["low"]) / df["close"]).mean()
        else:
            amplitude = 0

        stats.append({
            "code": code,
            "board": get_board(code),
            "n_days": len(returns),
            "annual_return": annual_return,
            "annual_vol": annual_vol,
            "sharpe": sharpe,
            "max_drawdown": max_dd,
            "daily_vol": daily_vol,
            "limit_up_count": limit_up,
            "limit_down_count": limit_down,
            "vol_mean": vol_mean,
            "vol_cv": vol_cv,
            "vol_price_corr": vol_price_corr,
            "autocorr": autocorr,
            "amplitude": amplitude,
        })

    df_stats = pd.DataFrame(stats)

    # 按板块分组统计
    print(f"\n📊 按板块统计 (抽样 {len(stats)} 只):")
    for board, group in df_stats.groupby("board"):
        print(f"\n  【{board}】({len(group)} 只)")
        print(f"    年化收益:  中位数 {group['annual_return'].median()*100:+.1f}%  "
              f"| 均值 {group['annual_return'].mean()*100:+.1f}%")
        print(f"    年化波动:  中位数 {group['annual_vol'].median()*100:.1f}%")
        print(f"    Sharpe:    中位数 {group['sharpe'].median():.3f}  "
              f"| >0 占比 {(group['sharpe']>0).mean()*100:.0f}%")
        print(f"    最大回撤:  中位数 {group['max_drawdown'].median()*100:.1f}%")
        print(f"    日均振幅:  中位数 {group['amplitude'].median()*100:.2f}%")
        print(f"    量价相关:  中位数 {group['vol_price_corr'].median():.3f}")
        print(f"    收益自相关: 中位数 {group['autocorr'].median():.4f}")
        print(f"    涨停次数:  均值 {group['limit_up_count'].mean():.1f} / 5年")

    # Top/Bottom 特征股票
    print(f"\n🏆 高波动 + 高振幅（适合短线策略）:")
    df_stats["short_score"] = df_stats["daily_vol"].rank(pct=True) * 0.5 + \
                               df_stats["amplitude"].rank(pct=True) * 0.3 + \
                               df_stats["vol_cv"].rank(pct=True) * 0.2
    top_short = df_stats.nlargest(15, "short_score")
    for _, row in top_short.iterrows():
        print(f"    {row['code']:>8s}  vol={row['daily_vol']*100:.2f}%  "
              f"amp={row['amplitude']*100:.2f}%  "
              f"涨停={int(row['limit_up_count'])}次  "
              f"板块={row['board']}")

    print(f"\n📈 高趋势性（适合趋势跟踪）:")
    df_stats["trend_score"] = df_stats["autocorr"].rank(pct=True) * 0.5 + \
                               df_stats["sharpe"].rank(pct=True) * 0.3 + \
                               (1 - df_stats["max_drawdown"].abs()).rank(pct=True) * 0.2
    top_trend = df_stats.nlargest(15, "trend_score")
    for _, row in top_trend.iterrows():
        print(f"    {row['code']:>8s}  autocorr={row['autocorr']:.4f}  "
              f"sharpe={row['sharpe']:.3f}  "
              f"回撤={row['max_drawdown']*100:.1f}%  "
              f"板块={row['board']}")

    print(f"\n🏆 高量价相关（适合量价策略）:")
    top_vp = df_stats.nlargest(15, "vol_price_corr")
    for _, row in top_vp.iterrows():
        print(f"    {row['code']:>8s}  量价相关={row['vol_price_corr']:.4f}  "
              f"sharpe={row['sharpe']:.3f}  "
              f"板块={row['board']}")

    return df_stats


# ============================================================
# 3. 时间序列分析 — 寻找时间规律
# ============================================================
def time_series_analysis(all_codes: list, sample_n: int = 300):
    """时间序列规律分析"""
    print("\n" + "=" * 70)
    print("  3. 时间序列分析 — 寻找时间规律")
    print("=" * 70)

    sample = all_codes[:sample_n] if len(all_codes) > sample_n else all_codes

    all_data = []
    total = len(sample)
    for i, code in enumerate(sample):
        if (i + 1) % 100 == 0 or i == 0:
            print(f"\r   加载中: {i+1}/{total} ({(i+1)/total*100:.0f}%)", end="", flush=True)
        df = load_csv(code)
        if df is None or len(df) < 200:
            continue
        returns = df["close"].pct_change().dropna()
        for idx, ret in returns.items():
            all_data.append({
                "date": idx,
                "return": ret,
                "code": code,
                "board": get_board(code),
            })

    df_all = pd.DataFrame(all_data)
    df_all["date"] = pd.to_datetime(df_all["date"])
    df_all["weekday"] = df_all["date"].dt.weekday
    df_all["month"] = df_all["date"].dt.month
    df_all["year"] = df_all["date"].dt.year

    # 3.1 星期效应
    print("\n📅 星期效应 (全市场平均日收益率):")
    weekday_names = ["周一", "周二", "周三", "周四", "周五"]
    for wd in range(5):
        mask = df_all["weekday"] == wd
        mean_ret = df_all.loc[mask, "return"].mean()
        win_rate = (df_all.loc[mask, "return"] > 0).mean()
        print(f"   {weekday_names[wd]}: 均值={mean_ret*100:.4f}%  胜率={win_rate*100:.1f}%")

    # 3.2 月份效应
    print("\n📅 月份效应 (全市场平均日收益率):")
    for m in range(1, 13):
        mask = df_all["month"] == m
        if mask.sum() == 0:
            continue
        mean_ret = df_all.loc[mask, "return"].mean()
        win_rate = (df_all.loc[mask, "return"] > 0).mean()
        print(f"   {m:>2d}月: 均值={mean_ret*100:.4f}%  胜率={win_rate*100:.1f}%  样本={mask.sum():,}")

    # 3.3 年度表现
    print("\n📅 年度表现 (全市场平均年化收益率):")
    for year in sorted(df_all["year"].unique()):
        mask = df_all["year"] == year
        year_returns = df_all.loc[mask].groupby("code")["return"].sum()
        mean_annual = year_returns.mean()
        median_annual = year_returns.median()
        positive_pct = (year_returns > 0).mean()
        print(f"   {year}: 均值={mean_annual*100:+.1f}%  中位数={median_annual*100:+.1f}%  "
              f"正收益占比={positive_pct*100:.0f}%")

    # 3.4 连涨/连跌统计
    print("\n📊 连涨/连跌统计 (抽样):")
    streak_data = []
    for code in sample[:100]:
        df = load_csv(code)
        if df is None or len(df) < 200:
            continue
        returns = df["close"].pct_change().dropna()
        signs = (returns > 0).astype(int)

        streak = 0
        current_sign = None
        for s in signs:
            if s == current_sign:
                streak += 1
            else:
                if current_sign is not None:
                    streak_data.append({"sign": current_sign, "length": streak})
                current_sign = s
                streak = 1
        streak_data.append({"sign": current_sign, "length": streak})

    df_streak = pd.DataFrame(streak_data)
    print("   连涨天数分布:")
    for n in range(1, 11):
        up_streak = ((df_streak["sign"] == 1) & (df_streak["length"] >= n)).sum()
        total_up = (df_streak["sign"] == 1).sum()
        print(f"     ≥{n}天: {up_streak} ({up_streak/total_up*100:.1f}%)")

    print("   连跌天数分布:")
    for n in range(1, 11):
        down_streak = ((df_streak["sign"] == 0) & (df_streak["length"] >= n)).sum()
        total_down = (df_streak["sign"] == 0).sum()
        print(f"     ≥{n}天: {down_streak} ({down_streak/total_down*100:.1f}%)")

    return df_all


# ============================================================
# 4. 量价关系分析
# ============================================================
def volume_price_analysis(all_codes: list, sample_n: int = 300):
    """量价关系深度分析"""
    print("\n" + "=" * 70)
    print("  4. 量价关系分析")
    print("=" * 70)

    sample = all_codes[:sample_n] if len(all_codes) > sample_n else all_codes

    bins = [-0.12, -0.07, -0.05, -0.03, -0.01, 0.01, 0.03, 0.05, 0.07, 0.12]
    bin_labels = ["<-7%", "-7~-5%", "-5~-3%", "-3~-1%", "-1~1%", "1~3%", "3~5%", "5~7%", ">7%"]

    vol_change_by_return = defaultdict(list)

    total = len(sample)
    for i, code in enumerate(sample):
        if (i + 1) % 100 == 0 or i == 0:
            print(f"\r   分析中: {i+1}/{total} ({(i+1)/total*100:.0f}%)", end="", flush=True)
        df = load_csv(code)
        if df is None or len(df) < 200:
            continue

        returns = df["close"].pct_change().dropna()
        vol_change = df["volume"].pct_change().dropna()

        common_idx = returns.index.intersection(vol_change.index)
        r = returns.loc[common_idx]
        v = vol_change.loc[common_idx]

        binned = pd.cut(r, bins=bins, labels=bin_labels, right=False, include_lowest=True)
        for label, vc in zip(binned, v):
            if pd.isna(label) or pd.isna(vc):
                continue
            vol_change_by_return[str(label)].append(vc)

    print("\n📊 涨跌幅 vs 成交量变化 (全市场统计):")
    print(f"   {'涨跌幅':>12s}  {'成交量变化均值':>14s}  {'成交量变化中位数':>16s}  {'样本数':>8s}")
    for label in bin_labels:
        vals = vol_change_by_return.get(label, [])
        if vals:
            mean_vc = np.mean(vals)
            median_vc = np.median(vals)
            print(f"   {label:>12s}  {mean_vc*100:>+13.1f}%  {median_vc*100:>+15.1f}%  {len(vals):>8,}")

    # 大涨后回调概率
    print("\n📊 大涨后次日表现 (涨幅>5% 后):")
    next_day_after_surge = []
    for code in sample:
        df = load_csv(code)
        if df is None or len(df) < 200:
            continue
        returns = df["close"].pct_change().dropna()
        for i in range(len(returns) - 1):
            if returns.iloc[i] > 0.05:
                next_day_after_surge.append(returns.iloc[i + 1])

    if next_day_after_surge:
        arr = np.array(next_day_after_surge)
        print(f"   样本数: {len(arr):,}")
        print(f"   次日均值: {arr.mean()*100:+.4f}%")
        print(f"   次日中位数: {np.median(arr)*100:+.4f}%")
        print(f"   次日上涨概率: {(arr > 0).mean()*100:.1f}%")
        print(f"   次日继续涨>3%: {(arr > 0.03).mean()*100:.1f}%")
        print(f"   次日跌>3%: {(arr < -0.03).mean()*100:.1f}%")

    # 缩量回调后反弹概率
    print("\n📊 缩量回调后表现 (连续3天缩量+小幅下跌后):")
    bounce_after_shrink = []
    for code in sample[:200]:
        df = load_csv(code)
        if df is None or len(df) < 200:
            continue
        returns = df["close"].pct_change().dropna()
        vol_change = df["volume"].pct_change().dropna()
        common_idx = returns.index.intersection(vol_change.index)
        r = returns.loc[common_idx]
        v = vol_change.loc[common_idx]

        for i in range(3, len(r) - 1):
            if (v.iloc[i-2] < 0 and v.iloc[i-1] < 0 and v.iloc[i] < 0 and
                r.iloc[i-2] < 0 and r.iloc[i-1] < 0 and r.iloc[i] < 0 and
                r.iloc[i] > -0.05):
                bounce_after_shrink.append(r.iloc[i + 1])

    if bounce_after_shrink:
        arr = np.array(bounce_after_shrink)
        print(f"   样本数: {len(arr):,}")
        print(f"   次日均值: {arr.mean()*100:+.4f}%")
        print(f"   次日上涨概率: {(arr > 0).mean()*100:.1f}%")

    return vol_change_by_return


# ============================================================
# 5. 板块轮动分析
# ============================================================
def sector_rotation_analysis(all_codes: list, sample_n: int = 500):
    """板块轮动分析"""
    print("\n" + "=" * 70)
    print("  5. 板块轮动分析")
    print("=" * 70)

    sample = all_codes[:sample_n] if len(all_codes) > sample_n else all_codes

    board_monthly = defaultdict(lambda: defaultdict(list))

    total = len(sample)
    for i, code in enumerate(sample):
        if (i + 1) % 100 == 0 or i == 0:
            print(f"\r   加载中: {i+1}/{total} ({(i+1)/total*100:.0f}%)", end="", flush=True)
        df = load_csv(code)
        if df is None or len(df) < 200:
            continue
        board = get_board(code)
        returns = df["close"].pct_change().dropna()

        monthly = returns.resample("ME").sum()
        for date, ret in monthly.items():
            key = f"{date.year}-{date.month:02d}"
            board_monthly[board][key].append(ret)

    print("\n📊 各板块月度平均收益 (近3年):")
    months = sorted(set(key for bd in board_monthly.values() for key in bd.keys()))
    recent_months = [m for m in months if m >= "2023-01"][-24:]

    boards = sorted(board_monthly.keys())
    header = f"{'月份':>8s}" + "".join(f"  {b:>8s}" for b in boards)
    print(f"   {header}")

    for month in recent_months:
        row = f"   {month:>8s}"
        for board in boards:
            vals = board_monthly[board].get(month, [])
            if vals:
                mean_ret = np.mean(vals) * 100
                row += f"  {mean_ret:>+7.1f}%"
            else:
                row += f"  {'N/A':>8s}"
        print(row)

    print("\n📊 板块月度收益相关性:")
    board_returns = {}
    for board in boards:
        monthly_means = []
        for month in recent_months:
            vals = board_monthly[board].get(month, [])
            monthly_means.append(np.mean(vals) if vals else 0)
        board_returns[board] = monthly_means

    df_br = pd.DataFrame(board_returns)
    corr = df_br.corr()
    print(f"   {'':>8s}" + "".join(f"  {b:>8s}" for b in boards))
    for b1 in boards:
        row = f"   {b1:>8s}"
        for b2 in boards:
            row += f"  {corr.loc[b1, b2]:>8.3f}"
        print(row)


# ============================================================
# 6. 连板猎手 — 寻找多板股的共同特征
# ============================================================

def _limit_up_threshold(code: str) -> float:
    """根据板块返回涨停阈值"""
    board = get_board(code)
    if board in ("创业板", "科创板"):
        return 0.198
    return 0.098  # 主板+北交所


def _detect_limit_up_days(df: pd.DataFrame, code: str) -> pd.Series:
    """返回布尔 Series，True 表示该日涨停"""
    threshold = _limit_up_threshold(code)
    returns = df["close"].pct_change()
    # 涨停 = 涨幅 >= 阈值（考虑四舍五入）
    return returns >= threshold


def _find_limit_up_runs(df: pd.DataFrame, code: str,
                         min_streak: int = 2, max_gap: int = 1) -> list:
    """
    找出连板段。

    规则：
      - 连续涨停 >= min_streak 天算一个连板段
      - 允许中间有 max_gap 天非涨停（洗盘日），但整体必须是"准连续"
      - 一个连板段 = (start_idx, end_idx, n_limit_ups, n_total_days, gap_positions)

    Returns:
      list of dict: [{
        "start_date": Timestamp,
        "end_date": Timestamp,       # 最后一个涨停日
        "n_limit_ups": int,          # 涨停天数
        "n_total_days": int,         # 总天数（含洗盘日）
        "gaps": list,                # 洗盘日位置
        "max_consecutive": int,      # 最长连续涨停数
      }, ...]
    """
    is_lu = _detect_limit_up_days(df, code)
    dates = df.index

    runs = []
    i = 0
    while i < len(is_lu):
        if not is_lu.iloc[i]:
            i += 1
            continue

        # 找到一个涨停，开始向后扫描
        streak_start = i
        lu_count = 0
        consecutive = 0
        max_consecutive = 0
        gap_positions = []
        j = i

        while j < len(is_lu):
            if is_lu.iloc[j]:
                lu_count += 1
                consecutive += 1
                max_consecutive = max(max_consecutive, consecutive)
                j += 1
            else:
                # 非涨停 — 看是否在容忍范围内
                # 往后看：如果下一个还是涨停，算洗盘日
                if j + 1 < len(is_lu) and is_lu.iloc[j + 1] and consecutive > 0:
                    gap_positions.append(j - streak_start)
                    j += 1
                    consecutive = 0  # 重置连续计数
                else:
                    break

        total_days = j - streak_start
        if lu_count >= min_streak:
            runs.append({
                "start_date": dates[streak_start],
                "end_date": dates[j - 1],
                "n_limit_ups": lu_count,
                "n_total_days": total_days,
                "gaps": gap_positions,
                "max_consecutive": max_consecutive,
            })

        i = j

    return runs


def _find_peak_after_run(df: pd.DataFrame, run_end_idx: int,
                          lookahead: int = 5) -> dict:
    """
    向后搜索最高点。

    如果最高点在 lookahead 窗口末尾，则继续向后推 lookahead 天（最多推3轮）。

    Returns:
      {"peak_date": Timestamp, "peak_price": float, "peak_idx": int,
       "days_to_peak": int, "peak_at_edge": bool}
    """
    start = run_end_idx + 1
    peak_price = -1
    peak_idx = start
    extended = 0

    while extended < 3:  # 最多扩展3轮
        end = min(start + lookahead, len(df))
        window = df.iloc[start:end]
        if len(window) == 0:
            break

        local_peak_idx = window["high"].idxmax()
        local_peak_price = window["high"].loc[local_peak_idx]

        if local_peak_price > peak_price:
            peak_price = local_peak_price
            peak_idx = df.index.get_loc(local_peak_idx)

        # 如果最高点在窗口最后一根K线，继续扩展
        if local_peak_idx == window.index[-1] and end < len(df):
            start = end
            extended += 1
        else:
            break

    if peak_price < 0:
        # 没找到，用 run_end_idx 当天的 high
        peak_price = df["high"].iloc[run_end_idx]
        peak_idx = run_end_idx

    return {
        "peak_date": df.index[peak_idx],
        "peak_price": float(peak_price),
        "peak_idx": peak_idx,
        "days_to_peak": peak_idx - run_end_idx,
        "peak_at_edge": (peak_idx >= len(df) - 1),
    }


def _extract_daily_features(df: pd.DataFrame, start_idx: int, end_idx: int,
                              label: str = "") -> dict:
    """
    从日线中提取指定区间的特征。

    用于分析连板前5-10天 / 连板期间 / 见顶区间的共同点。
    """
    seg = df.iloc[start_idx:end_idx + 1]
    if len(seg) == 0:
        return {}

    close = seg["close"]
    high = seg["high"]
    low = seg["low"]
    volume = seg["volume"]
    returns = close.pct_change().dropna()

    features = {}

    # 均线排列
    if len(df) > start_idx + 60:
        pre = df.iloc[max(0, start_idx - 60):start_idx]
        if len(pre) >= 20:
            ma5 = pre["close"].tail(5).mean()
            ma10 = pre["close"].tail(10).mean()
            ma20 = pre["close"].tail(20).mean()
            price_before = pre["close"].iloc[-1]
            features["ma5_above_ma20"] = bool(ma5 > ma20)
            features["ma10_above_ma20"] = bool(ma10 > ma20)
            features["price_above_ma20"] = bool(price_before > ma20)

    # 成交量变化
    if len(volume) >= 2:
        vol_start = volume.iloc[:max(1, len(volume) // 3)].mean()
        vol_end = volume.iloc[-max(1, len(volume) // 3):].mean()
        features["vol_ratio_end_vs_start"] = round(vol_end / vol_start, 2) if vol_start > 0 else 0

    # 涨幅统计
    if len(returns) > 0:
        features["total_return"] = round(float((close.iloc[-1] / close.iloc[0]) - 1) * 100, 2)
        features["max_single_return"] = round(float(returns.max()) * 100, 2)
        features["min_single_return"] = round(float(returns.min()) * 100, 2)

    # 振幅
    amplitude = (high - low) / close.shift(1)
    amplitude = amplitude.dropna()
    if len(amplitude) > 0:
        features["avg_amplitude"] = round(float(amplitude.mean()) * 100, 2)

    # 换手率近似（用成交量变化率）
    if len(volume) > 5:
        vol_mean = volume.mean()
        vol_std = volume.std()
        features["vol_cv"] = round(float(vol_std / vol_mean), 3) if vol_mean > 0 else 0

    # 缺口（开盘价 vs 前日收盘价）
    if len(seg) >= 2:
        gaps = (seg["open"].iloc[1:].values - close.iloc[:-1].values) / close.iloc[:-1].values
        up_gaps = (gaps > 0.01).sum()
        features["n_up_gaps"] = int(up_gaps)

    return features


def _extract_15m_features(code: str, start_date: str, end_date: str) -> dict:
    """
    从15分钟线中提取特征。

    分析开盘竞价、分时形态、资金流入节奏。
    """
    df = load_15m_range(code, start_date, end_date)
    if df is None or len(df) < 10:
        return {}

    features = {}

    # 分析每天的开盘30分钟（前2根15m K线）
    df["date"] = df.index.date
    first_30m = df.groupby("date").head(2)
    if len(first_30m) > 0:
        first_30m_returns = (first_30m.groupby("date")["close"].last() /
                             first_30m.groupby("date")["open"].first() - 1)
        features["avg_open_30m_return"] = round(float(first_30m_returns.mean()) * 100, 2)
        features["open_30m_positive_rate"] = round(float((first_30m_returns > 0).mean()) * 100, 1)

    # 午后走势（13:00后的K线）
    afternoon = df[df.index.hour >= 13]
    if len(afternoon) > 0:
        aft_returns = afternoon["close"].pct_change().dropna()
        features["avg_afternoon_return"] = round(float(aft_returns.mean()) * 100, 4)

    # 尾盘30分钟（最后2根15m K线）
    last_30m = df.groupby("date").tail(2)
    if len(last_30m) > 0:
        last_30m_returns = (last_30m.groupby("date")["close"].last() /
                            last_30m.groupby("date")["open"].first() - 1)
        features["avg_close_30m_return"] = round(float(last_30m_returns.mean()) * 100, 2)
        features["close_30m_positive_rate"] = round(float((last_30m_returns > 0).mean()) * 100, 1)

    # 日内波动率
    daily_range = df.groupby("date").apply(
        lambda g: (g["high"].max() - g["low"].min()) / g["open"].iloc[0] if len(g) > 0 else 0
    )
    features["avg_intraday_range"] = round(float(daily_range.mean()) * 100, 2)

    # 量能分布：上午 vs 下午
    morning_vol = df[df.index.hour < 12]["volume"].sum()
    afternoon_vol = df[df.index.hour >= 13]["volume"].sum()
    total_vol = morning_vol + afternoon_vol
    if total_vol > 0:
        features["morning_vol_pct"] = round(float(morning_vol / total_vol) * 100, 1)

    return features


def multi_bagger_scan(all_codes: list, sample_n: int = 0,
                       min_streak: int = 1, max_gap: int = 1,
                       start_date: str = "2023-01-01",
                       end_date: str = "2026-05-21") -> list:
    """
    全市场扫描连板股，直接导出原始K线到CSV。

    每个连板段导出：
      - 日线: 起涨前10个交易日 ~ 最高点后5个交易日
      - 15分钟线: 同上范围

    输出文件:
      analysis_output/dragon_runs.csv       — 连板段摘要（一行一段）
      analysis_output/dragon_daily.csv      — 日线明细（一行一天）
      analysis_output/dragon_15m.csv        — 15分钟线明细（一行一根K线）
    """
    print("\n" + "=" * 70)
    print(f"  连板猎手扫描  (≥{min_streak}板, 允许{max_gap}天洗盘)")
    print(f"  区间: {start_date} ~ {end_date}")
    print("=" * 70)

    codes = all_codes[:sample_n] if sample_n > 0 else all_codes
    total = len(codes)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    summary_path = os.path.join(OUTPUT_DIR, "dragon_runs.csv")
    daily_path = os.path.join(OUTPUT_DIR, "dragon_daily.csv")
    m15_path = os.path.join(OUTPUT_DIR, "dragon_15m.csv")

    summary_rows = []
    daily_rows = []
    m15_rows = []

    run_id = 0
    stocks_with_runs = 0
    error_count = 0

    for i, code in enumerate(codes):
        if (i + 1) % 200 == 0 or i == 0:
            print(f"\r   扫描中: {i+1}/{total}  已发现 {run_id} 个连板段",
                  end="", flush=True)
        try:
            df = load_csv(code)
            if df is None or len(df) < 60:
                continue

            sd = pd.Timestamp(start_date)
            ed = pd.Timestamp(end_date)
            df = df[(df.index >= sd) & (df.index <= ed)]
            if len(df) < 30:
                continue

            runs = _find_limit_up_runs(df, code, min_streak=min_streak, max_gap=max_gap)
            if not runs:
                continue

            stocks_with_runs += 1
            board = get_board(code)
            exchange = guess_exchange(code)

            for run in runs:
                run_id += 1
                run_start_idx = df.index.get_loc(run["start_date"])
                run_end_idx = df.index.get_loc(run["end_date"])

                # 找最高点
                peak = _find_peak_after_run(df, run_end_idx)
                peak_idx = peak["peak_idx"]

                # 导出范围: 起涨前10个交易日 ~ 最高点后5个交易日
                export_start = max(0, run_start_idx - 10)
                export_end = min(len(df) - 1, peak_idx + 5)

                # 找出连板段内每天是否涨停
                is_lu = _detect_limit_up_days(df, code)

                # ── 日线明细 ──
                for idx in range(export_start, export_end + 1):
                    row = df.iloc[idx]
                    ret = float(df["close"].pct_change().iloc[idx]) * 100 if idx > 0 else 0
                    daily_rows.append({
                        "run_id": run_id,
                        "code": code,
                        "board": board,
                        "date": df.index[idx].strftime("%Y-%m-%d"),
                        "open": round(float(row["open"]), 3),
                        "high": round(float(row["high"]), 3),
                        "low": round(float(row["low"]), 3),
                        "close": round(float(row["close"]), 3),
                        "volume": int(row["volume"]),
                        "return_pct": round(ret, 2),
                        "is_limit_up": bool(is_lu.iloc[idx]),
                        "is_run_start": (idx == run_start_idx),
                        "is_run_end": (idx == run_end_idx),
                        "is_peak": (idx == peak_idx),
                        "days_from_run_start": idx - run_start_idx,
                        "days_from_peak": idx - peak_idx,
                        # 连板段信息
                        "run_n_limit_ups": run["n_limit_ups"],
                        "run_max_consecutive": run["max_consecutive"],
                        "peak_date": peak["peak_date"].strftime("%Y-%m-%d"),
                        "peak_price": round(peak["peak_price"], 3),
                    })

                # ── 连板段摘要 ──
                summary_rows.append({
                    "run_id": run_id,
                    "code": code,
                    "board": board,
                    "exchange": exchange,
                    "run_start": run["start_date"].strftime("%Y-%m-%d"),
                    "run_end": run["end_date"].strftime("%Y-%m-%d"),
                    "n_limit_ups": run["n_limit_ups"],
                    "n_total_days": run["n_total_days"],
                    "max_consecutive": run["max_consecutive"],
                    "peak_date": peak["peak_date"].strftime("%Y-%m-%d"),
                    "peak_price": round(peak["peak_price"], 3),
                    "days_to_peak": peak["days_to_peak"],
                    "run_start_price": round(float(df["close"].iloc[run_start_idx]), 3),
                    "run_return_pct": round(
                        (float(df["close"].iloc[run_end_idx]) /
                         float(df["close"].iloc[run_start_idx]) - 1) * 100, 2),
                    "peak_return_pct": round(
                        (peak["peak_price"] /
                         float(df["close"].iloc[run_start_idx]) - 1) * 100, 2),
                })

                # ── 15分钟线 ──
                m15_start = df.index[export_start].strftime("%Y-%m-%d")
                m15_end = (df.index[export_end] + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
                try:
                    df_15m = load_15m_range(code, m15_start, m15_end)
                    if df_15m is not None and len(df_15m) > 0:
                        for ts, row_15m in df_15m.iterrows():
                            ret_15m = float(df_15m["close"].pct_change().loc[ts]) * 100 if ts != df_15m.index[0] else 0
                            m15_rows.append({
                                "run_id": run_id,
                                "code": code,
                                "datetime": ts.strftime("%Y-%m-%d %H:%M"),
                                "date": ts.strftime("%Y-%m-%d"),
                                "open": round(float(row_15m["open"]), 3),
                                "high": round(float(row_15m["high"]), 3),
                                "low": round(float(row_15m["low"]), 3),
                                "close": round(float(row_15m["close"]), 3),
                                "volume": int(row_15m["volume"]),
                                "return_pct": round(ret_15m, 3),
                            })
                except Exception:
                    pass  # 15分钟线可能没有，不报错

        except Exception as e:
            error_count += 1
            continue

    print(f"\n   完成: 扫描 {total} 只, {stocks_with_runs} 只有涨停, "
          f"{run_id} 个连板段, {error_count} 个错误")

    # ── 写CSV ──
    if summary_rows:
        df_summary = pd.DataFrame(summary_rows)
        df_summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
        print(f"\n💾 连板段摘要: {summary_path}  ({len(summary_rows)} 行)")

    if daily_rows:
        df_daily = pd.DataFrame(daily_rows)
        df_daily.to_csv(daily_path, index=False, encoding="utf-8-sig")
        print(f"💾 日线明细:   {daily_path}  ({len(daily_rows)} 行)")

    if m15_rows:
        df_m15 = pd.DataFrame(m15_rows)
        df_m15.to_csv(m15_path, index=False, encoding="utf-8-sig")
        print(f"💾 15分钟明细: {m15_path}  ({len(m15_rows)} 行)")

    return summary_rows


def summarize_multi_bagger_patterns(summary_rows: list):
    """
    从连板段摘要中输出基础统计。
    详细分析请直接用 pandas 读 CSV。
    """
    if not summary_rows:
        print("❌ 无数据可汇总")
        return

    df = pd.DataFrame(summary_rows)

    print("\n" + "=" * 70)
    print(f"  连板猎手 — 汇总  (共 {len(df)} 个连板段)")
    print("=" * 70)

    # 连板天数分布
    print("\n📊 连板天数分布:")
    for n in sorted(df["n_limit_ups"].unique()):
        count = (df["n_limit_ups"] == n).sum()
        print(f"   {n}板: {count} 个 ({count/len(df)*100:.1f}%)")

    # 板块分布
    print("\n📊 板块分布:")
    for board, count in df["board"].value_counts().items():
        print(f"   {board}: {count} 个 ({count/len(df)*100:.1f}%)")

    # 见顶时间
    print("\n📊 见顶时间 (涨停结束后第N天):")
    for d in [0, 1, 2, 3, 5]:
        if d == 0:
            count = (df["days_to_peak"] <= 0).sum()
            label = "当天"
        else:
            count = ((df["days_to_peak"] > d - 1) & (df["days_to_peak"] <= d)).sum()
            label = f"第{d}天"
        print(f"   {label}: {count} 个 ({count/len(df)*100:.1f}%)")

    # 涨幅
    print(f"\n📊 见顶涨幅:")
    print(f"   均值: {df['peak_return_pct'].mean():.1f}%")
    print(f"   中位数: {df['peak_return_pct'].median():.1f}%")
    print(f"   最大: {df['peak_return_pct'].max():.1f}%")

    print(f"\n💡 详细分析请用 pandas 读取 CSV:")
    print(f"   df_summary = pd.read_csv('analysis_output/dragon_runs.csv')")
    print(f"   df_daily   = pd.read_csv('analysis_output/dragon_daily.csv')")
    print(f"   df_15m     = pd.read_csv('analysis_output/dragon_15m.csv')")


# ============================================================
# 8. 策略方向建议
# ============================================================
def strategy_suggestions(df_stats, all_returns):
    """基于分析结果给出策略方向建议"""
    print("\n" + "=" * 70)
    print("  6. 策略方向建议 (基于数据分析)")
    print("=" * 70)

    print("""
┌─────────────────────────────────────────────────────────────────────┐
│                       策略方向矩阵                                  │
├──────────────┬──────────────────────────────────────────────────────┤
│  策略类型    │  适用条件 (你的数据是否支持)                          │
├──────────────┼──────────────────────────────────────────────────────┤""")

    if df_stats is not None and len(df_stats) > 0:
        median_autocorr = df_stats["autocorr"].median()
        median_vol_price = df_stats["vol_price_corr"].median()
        median_amplitude = df_stats["amplitude"].median()
        limit_up_pct = (df_stats["limit_up_count"] > 0).mean()

        trend_support = "✅ 强" if median_autocorr > 0.02 else "⚠️ 弱" if median_autocorr > -0.01 else "❌ 不支持"
        print(f"│  趋势跟踪    │  收益自相关={median_autocorr:.4f} {trend_support:<20s}│")

        vp_support = "✅ 强" if median_vol_price > 0.1 else "⚠️ 中" if median_vol_price > 0 else "❌ 弱"
        print(f"│  量价策略    │  量价相关={median_vol_price:.3f} {vp_support:<22s}│")

        amp_support = "✅ 适合" if median_amplitude > 0.03 else "⚠️ 一般" if median_amplitude > 0.02 else "❌ 波动太小"
        print(f"│  短线策略    │  日均振幅={median_amplitude*100:.2f}% {amp_support:<20s}│")

        limit_support = "✅ 有信号源" if limit_up_pct > 0.3 else "⚠️ 信号稀少"
        print(f"│  涨停策略    │  有涨停股票={limit_up_pct*100:.0f}% {limit_support:<19s}│")

    kurt = pd.Series(all_returns).kurtosis()
    mr_support = "✅ 肥尾明显" if kurt > 5 else "⚠️ 轻微肥尾" if kurt > 3 else "❌ 近似正态"
    print(f"│  均值回归    │  峰度={kurt:.1f} {mr_support:<24s}│")

    print(f"""├──────────────┼──────────────────────────────────────────────────────┤
│  多因子组合  │  ✅ 全市场数据+多指标 → 天然适合多因子选股           │
└──────────────┴──────────────────────────────────────────────────────┘""")

    print("""
💡 建议优先级:
   1. 量价策略 — 你的数据有 volume+amount，量价关系是最直接的 alpha 来源
   2. 多因子选股 — 全市场5000+只股票，横截面分化大，适合做选股
   3. 均值回归 — 肥尾分布意味着超跌反弹机会多
   4. 趋势跟踪 — 需要看板块，某些板块趋势性强
""")


# ============================================================
# Main
# ============================================================
def main():
    quick = "--quick" in sys.argv
    full = "--full" in sys.argv
    include_15m = "--15m" in sys.argv
    scan_dragon = "--dragon" in sys.argv or "--连板" in sys.argv

    if full:
        sample_n = 99999  # 不限制
    elif quick:
        sample_n = 200
    else:
        sample_n = 500

    print("🚀 A 股全市场数据分析 (db_market)")
    print(f"   模式: {'全量' if full else '快速' if quick else '完整'}")

    all_codes = get_all_codes()
    print(f"   股票总数: {len(all_codes)}")

    if scan_dragon:
        # 连板猎手模式 — 专注扫描连板股
        min_streak = 1
        max_gap = 1
        for arg in sys.argv:
            if arg.startswith("--min-streak="):
                min_streak = int(arg.split("=")[1])
            if arg.startswith("--max-gap="):
                max_gap = int(arg.split("=")[1])

        runs = multi_bagger_scan(
            all_codes, sample_n=0 if full else sample_n,
            min_streak=min_streak, max_gap=max_gap,
            start_date="2023-01-01", end_date="2026-05-21",
        )

        summarize_multi_bagger_patterns(runs)
        return

    # 1. 市场概况
    all_returns = market_overview(all_codes, sample_n)

    # 2. 横截面分析
    df_stats = cross_sectional_analysis(all_codes, sample_n)

    # 3. 时间序列
    df_all = time_series_analysis(all_codes, sample_n // 2)

    # 4. 量价关系
    volume_price_analysis(all_codes, sample_n // 2)

    # 5. 板块轮动
    sector_rotation_analysis(all_codes, sample_n)

    # 6. 策略建议
    strategy_suggestions(df_stats, all_returns)

    # 保存结果
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    if df_stats is not None and len(df_stats) > 0:
        stats_path = os.path.join(OUTPUT_DIR, "stock_stats.csv")
        df_stats.to_csv(stats_path, index=False, encoding="utf-8-sig")
        print(f"\n💾 股票统计已保存: {stats_path}")

    print("\n✅ 分析完成")


if __name__ == "__main__":
    main()
