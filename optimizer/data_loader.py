"""
数据加载器 — 桥接你的 CSV 数据与 QuantDinger 框架
用法:
    python data_loader.py explore          # 探查数据概况
    python data_loader.py load 000001      # 加载单只股票（日线）
    python data_loader.py load 000001 15m  # 加载单只股票（15分钟线）
    python data_loader.py list             # 列出所有可用股票代码
    python data_loader.py validate         # 数据质量校验（抽样100只）
"""
import os
import sys
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime

# ============================================================
# 配置 — 按需修改
# ============================================================
BASE_DIR = Path(__file__).parent.parent / "optimizer_output" / "CNStock"
DAILY_DIR = BASE_DIR / "daily"
MIN15_DIR = BASE_DIR / "15m"

# 交易所后缀推断规则
def guess_exchange(code: str) -> str:
    """根据股票代码推断交易所后缀"""
    c = code.strip()
    if c.startswith(("6",)):
        return f"{c}.SH"
    elif c.startswith(("0", "3")):
        return f"{c}.SZ"
    elif c.startswith("68"):
        return f"{c}.SH"
    elif c.startswith(("8", "4")):
        return f"{c}.BJ"
    else:
        return f"{c}.SZ"  # 默认深圳


def get_csv_path(code: str, timeframe: str = "daily") -> Path:
    """获取 CSV 文件路径"""
    d = DAILY_DIR if timeframe == "daily" else MIN15_DIR
    if timeframe == "daily":
        return d / f"{code}.csv"
    else:
        return d / f"{code}_15min.csv"


def load_csv(code: str, timeframe: str = "daily") -> pd.DataFrame:
    """
    加载单只股票 CSV → 标准化 DataFrame
    
    Returns:
        DataFrame with columns: date, open, high, low, close, volume, amount
        index: DatetimeIndex
    """
    path = get_csv_path(code, timeframe)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {path}")
    
    df = pd.read_csv(path)
    
    # 标准化列名（统一小写、去空格）
    df.columns = [c.strip().lower() for c in df.columns]
    
    # 确保日期列
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
    elif "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"])
        df = df.set_index("time")
    
    # 确保必要列存在
    required = ["open", "high", "low", "close", "volume"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"缺少必要列: {missing}，实际列: {list(df.columns)}")
    
    # 排序
    df = df.sort_index()
    
    # 类型转换
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if "amount" in df.columns:
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    
    return df


def list_all_codes(timeframe: str = "daily") -> list:
    """列出所有可用股票代码（不含后缀）"""
    d = DAILY_DIR if timeframe == "daily" else MIN15_DIR
    if not d.exists():
        return []
    if timeframe == "daily":
        codes = sorted([f.stem for f in d.glob("*.csv")])
    else:
        # 15m 文件名: 000001_15min.csv → 提取 000001
        codes = sorted([f.stem.replace("_15min", "") for f in d.glob("*_15min.csv")])
    return codes


# ============================================================
# 探查函数
# ============================================================
def explore():
    """数据全貌探查"""
    print("=" * 60)
    print("  A 股数据探查")
    print("=" * 60)
    
    for tf, d in [("daily", DAILY_DIR), ("15m", MIN15_DIR)]:
        if not d.exists():
            print(f"\n❌ {tf} 目录不存在: {d}")
            continue
        
        files = list(d.glob("*.csv"))
        print(f"\n📁 {tf} 数据:")
        print(f"   路径: {d}")
        print(f"   文件数: {len(files)}")
        
        if not files:
            continue
        
        # 抽样看几个文件
        if tf == "15m":
            samples = list(d.glob("*_15min.csv"))[:3]
        else:
            samples = files[:3]
        for f in samples:
            df = pd.read_csv(f, nrows=5)
            print(f"\n   📄 {f.name} (前5行):")
            print(f"      列: {list(df.columns)}")
            print(f"      行数(总): {sum(1 for _ in open(f)) - 1}")
            print(df.to_string(index=False, header=True))
        
        # 统计日期范围和行数分布
        row_counts = []
        date_ranges = []
        for f in files[:100]:  # 抽样100个
            try:
                df = pd.read_csv(f)
                df.columns = [c.strip().lower() for c in df.columns]
                if "date" in df.columns:
                    dates = pd.to_datetime(df["date"])
                elif "time" in df.columns:
                    dates = pd.to_datetime(df["time"])
                else:
                    continue
                row_counts.append(len(df))
                date_ranges.append((dates.min(), dates.max()))
            except:
                pass
        
        if row_counts:
            print(f"\n   📊 统计 (抽样 {len(row_counts)} 个文件):")
            print(f"      行数: min={min(row_counts)}, max={max(row_counts)}, median={int(np.median(row_counts))}")
            if date_ranges:
                all_starts = [d[0] for d in date_ranges]
                all_ends = [d[1] for d in date_ranges]
                print(f"      最早日期: {min(all_starts).strftime('%Y-%m-%d')}")
                print(f"      最晚日期: {max(all_ends).strftime('%Y-%m-%d')}")


def validate():
    """数据质量校验 — 抽样100只股票"""
    print("=" * 60)
    print("  数据质量校验")
    print("=" * 60)
    
    codes = list_all_codes("daily")
    if not codes:
        print("❌ 没有找到 daily 数据")
        return
    
    sample = codes[:100] if len(codes) > 100 else codes
    issues = []
    
    for code in sample:
        try:
            df = load_csv(code, "daily")
            
            # 检查项
            checks = []
            
            # 1. 空值
            null_counts = df[["open", "high", "low", "close", "volume"]].isnull().sum()
            if null_counts.any():
                checks.append(f"空值: {null_counts.to_dict()}")
            
            # 2. OHLC 逻辑
            bad_hl = (df["high"] < df["low"]).sum()
            if bad_hl > 0:
                checks.append(f"high < low: {bad_hl} 行")
            
            # 3. 价格异常（负数或极端值）
            neg_price = (df["close"] <= 0).sum()
            if neg_price > 0:
                checks.append(f"收盘价<=0: {neg_price} 行")
            
            # 4. 涨跌幅异常（日线超过涨跌停）
            if len(df) > 1:
                pct = df["close"].pct_change().dropna()
                extreme = (pct.abs() > 0.22).sum()  # 超过20%涨跌停+容差
                if extreme > 0:
                    checks.append(f"极端涨跌幅(>22%): {extreme} 行")
            
            # 5. 数据连续性（是否有大段缺失）
            if len(df) > 100:
                date_diff = df.index.to_series().diff().dt.days
                max_gap = date_diff.max()
                if max_gap > 10:
                    checks.append(f"最大交易日间隔: {max_gap} 天")
            
            if checks:
                issues.append((code, checks))
                
        except Exception as e:
            issues.append((code, [f"加载失败: {e}"]))
    
    print(f"\n校验完成: {len(sample)} 只股票")
    if issues:
        print(f"⚠️  有问题: {len(issues)} 只")
        for code, checks in issues[:20]:
            print(f"  {code}: {'; '.join(checks)}")
        if len(issues) > 20:
            print(f"  ... 还有 {len(issues) - 20} 只")
    else:
        print("✅ 全部通过")


# ============================================================
# 框架对接：转为 QuantDinger 期望的格式
# ============================================================
def to_framework_format(code: str, timeframe: str = "daily") -> list:
    """
    加载数据并转为 QuantDinger BacktestService 期望的格式
    
    Returns:
        list of dict: [{"time": unix_ts, "open": float, "high": float, 
                        "low": float, "close": float, "volume": float}, ...]
    """
    df = load_csv(code, timeframe)
    
    records = []
    for idx, row in df.iterrows():
        ts = int(idx.timestamp())
        records.append({
            "time": ts,
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
        })
    return records


def patch_framework_data_source():
    """
    Monkey-patch QuantDinger 的数据源，让它直接读你的 CSV
    
    在 runner.py 之前调用此函数，即可无缝对接
    """
    try:
        from app.data_sources.factory import DataSourceFactory
        
        _orig_get_kline = DataSourceFactory.get_kline.__func__
        
        def _get_kline_from_csv(cls, market, symbol, timeframe, limit, 
                                 before_time=None, after_time=None):
            # 尝试从本地 CSV 读取
            try:
                # symbol 可能带后缀: "000001.SZ" → "000001"
                code = symbol.split(".")[0] if "." in symbol else symbol
                tf = "15m" if timeframe in ("15m", "15M") else "daily"
                data = to_framework_format(code, tf)
                
                if after_time:
                    data = [d for d in data if d["time"] >= after_time]
                if before_time:
                    data = [d for d in data if d["time"] <= before_time]
                if limit and len(data) > limit:
                    data = data[-limit:]
                
                if data and len(data) >= 10:
                    print(f"  [CSV数据] 命中 {symbol} {timeframe}: {len(data)} 条")
                    return data
            except Exception:
                pass
            
            # fallback 到原始数据源
            return _orig_get_kline(cls, market, symbol, timeframe, limit,
                                    before_time=before_time, after_time=after_time)
        
        DataSourceFactory.get_kline = classmethod(_get_kline_from_csv)
        print("✅ 数据源已 patch，QuantDinger 将优先读取本地 CSV")
        
    except ImportError:
        print("⚠️  无法导入 QuantDinger 模块，跳过 patch")
        print("   确保在 QuantDinger 项目根目录下运行")


# ============================================================
# CLI
# ============================================================
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)
    
    cmd = sys.argv[1]
    
    if cmd == "explore":
        explore()
    elif cmd == "validate":
        validate()
    elif cmd == "list":
        codes = list_all_codes("daily")
        print(f"共 {len(codes)} 只股票:")
        for c in codes[:50]:
            print(f"  {guess_exchange(c)}")
        if len(codes) > 50:
            print(f"  ... 还有 {len(codes) - 50} 只")
    elif cmd == "load":
        if len(sys.argv) < 3:
            print("用法: python data_loader.py load <code> [timeframe]")
            sys.exit(1)
        code = sys.argv[2]
        tf = sys.argv[3] if len(sys.argv) > 3 else "daily"
        df = load_csv(code, tf)
        print(f"\n{guess_exchange(code)} ({tf}):")
        print(f"  日期范围: {df.index.min().strftime('%Y-%m-%d')} ~ {df.index.max().strftime('%Y-%m-%d')}")
        print(f"  总行数: {len(df)}")
        print(f"  列: {list(df.columns)}")
        print(f"\n前5行:")
        print(df.head())
        print(f"\n后5行:")
        print(df.tail())
    else:
        print(f"未知命令: {cmd}")
        print(__doc__)
