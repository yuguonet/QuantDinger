"""
数据加载器 — 桥接 db_market 与 QuantDinger 框架

所有数据统一从 db_market (CNStock_db) 读取，不再依赖本地 CSV 文件。

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
from datetime import datetime

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
    import os
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
    except ImportError:
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


# 交易所后缀推断规则
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


def load_csv(code: str, timeframe: str = "daily") -> pd.DataFrame:
    """
    从 db_market 加载单只股票数据 → 标准化 DataFrame

    Returns:
        DataFrame with columns: open, high, low, close, volume
        index: DatetimeIndex
    """
    tf_map = {"daily": "1D", "15m": "15m", "1m": "1m", "5m": "5m",
              "30m": "30m", "60m": "60m", "1H": "60m"}
    tf = tf_map.get(timeframe, timeframe)

    writer = _get_writer()
    data = writer.query("CNStock", code, tf, limit=10000)

    if not data:
        raise FileNotFoundError(f"db_market 中无数据: {code} / {tf}")

    df = pd.DataFrame(data)
    df["date"] = pd.to_datetime(df["time"], unit="s")
    df = df.set_index("date").drop(columns=["time"])
    df = df.sort_index()

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    return df


def list_all_codes(timeframe: str = "daily") -> list:
    """列出所有可用股票代码（不含后缀）"""
    writer = _get_writer()
    stats = writer.stats("CNStock")
    if not stats.get("exists"):
        return []
    return stats.get("symbol_list", [])


# ============================================================
# 探查函数
# ============================================================
def explore():
    """数据全貌探查"""
    print("=" * 60)
    print("  A 股数据探查 (db_market)")
    print("=" * 60)

    mgr = _get_mgr()
    writer = _get_writer()

    if not mgr.market_db_exists("CNStock"):
        print("\n❌ CNStock_db 不存在，请先运行 tdx_download --merge-db")
        return

    stats = writer.stats("CNStock")
    print(f"\n📊 CNStock_db 概况:")
    print(f"   股票数: {stats.get('symbols', 0)}")
    print(f"   总行数: {stats.get('total_rows', 0):,}")
    print(f"   表: {stats.get('tables', [])}")
    dr = stats.get("date_range", {})
    print(f"   日期范围: {dr.get('start', 'N/A')} ~ {dr.get('end', 'N/A')}")

    # 抽样看几个
    symbols = stats.get("symbol_list", [])[:3]
    for sym in symbols:
        data = writer.query("CNStock", sym, "1D", limit=5)
        if data:
            print(f"\n   📄 {sym} (前5条):")
            for d in data[:5]:
                dt = datetime.fromtimestamp(d["time"]).strftime("%Y-%m-%d")
                print(f"      {dt}  O={d['open']:.2f}  H={d['high']:.2f}  "
                      f"L={d['low']:.2f}  C={d['close']:.2f}  V={d['volume']:.0f}")


def validate():
    """数据质量校验 — 抽样100只股票"""
    print("=" * 60)
    print("  数据质量校验 (db_market)")
    print("=" * 60)

    codes = list_all_codes()
    if not codes:
        print("❌ CNStock_db 中无数据")
        return

    sample = codes[:100] if len(codes) > 100 else codes
    issues = []

    for code in sample:
        try:
            df = load_csv(code, "daily")
            checks = []

            null_counts = df[["open", "high", "low", "close", "volume"]].isnull().sum()
            if null_counts.any():
                checks.append(f"空值: {null_counts.to_dict()}")

            bad_hl = (df["high"] < df["low"]).sum()
            if bad_hl > 0:
                checks.append(f"high < low: {bad_hl} 行")

            neg_price = (df["close"] <= 0).sum()
            if neg_price > 0:
                checks.append(f"收盘价<=0: {neg_price} 行")

            if len(df) > 1:
                pct = df["close"].pct_change().dropna()
                extreme = (pct.abs() > 0.22).sum()
                if extreme > 0:
                    checks.append(f"极端涨跌幅(>22%): {extreme} 行")

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
    tf_map = {"daily": "1D", "15m": "15m"}
    tf = tf_map.get(timeframe, timeframe)

    writer = _get_writer()
    return writer.query("CNStock", code, tf, limit=10000)


def patch_framework_data_source():
    """
    Monkey-patch QuantDinger 的数据源，让它读 db_market

    在 runner.py 之前调用此函数，即可无缝对接
    """
    try:
        from app.data_sources.factory import DataSourceFactory
        from optimizer.data_warehouse.storage import read_local

        _orig_get_kline = DataSourceFactory.get_kline.__func__

        def _get_kline_from_db(cls, market, symbol, timeframe, limit,
                                before_time=None, after_time=None):
            try:
                data = read_local(
                    market=market, timeframe=timeframe, symbol=symbol,
                    limit=limit, before_time=before_time, after_time=after_time,
                )
                if data and len(data) >= 10:
                    print(f"  [db_market] 命中 {symbol} {timeframe}: {len(data)} 条")
                    return data
            except Exception:
                pass
            return _orig_get_kline(cls, market, symbol, timeframe, limit,
                                    before_time=before_time, after_time=after_time)

        DataSourceFactory.get_kline = classmethod(_get_kline_from_db)
        print("✅ 数据源已 patch，QuantDinger 将优先读取 db_market")

    except ImportError:
        print("⚠️  无法导入 QuantDinger 模块，跳过 patch")


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
        codes = list_all_codes()
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
