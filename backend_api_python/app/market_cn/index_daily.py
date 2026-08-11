#!/usr/bin/env python3
"""
sync_index_daily.py — A股主要指数日K线同步

将大盘指数日线（OHLCV）写入 kline_1D_YYYY 表，与个股共用同一套存储。
symbol 格式: "000001.SH" / "399001.SZ" 等（tushare 格式）

数据源优先级: akshare → baostock → tushare（自动降级）

用法:
  python scripts/sync_index_daily.py                     # 同步最近一个交易日
  python scripts/sync_index_daily.py --date 2026-05-26   # 指定日期
  python scripts/sync_index_daily.py --backfill           # 回填全部历史（约10年）
  python scripts/sync_index_daily.py --backfill --start 2020-01-01   # 从指定日期回填
  python scripts/sync_index_daily.py --dry-run            # 只看不写
  python scripts/sync_index_daily.py --indices 000001.SH,000300.SH   # 只同步指定指数

放在 scripts/ 目录，由 scheduler._post_market_batch 1D 完成后触发。
"""

import sys
import argparse
import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ============================================================
# 路径 & 环境
# ============================================================
_root = Path(__file__).resolve().parent.parent  # scripts/ → QuantDinger/
sys.path.insert(0, str(_root / "backend_api_python"))

try:
    from dotenv import load_dotenv
    load_dotenv(_root / "backend_api_python" / ".env")
    load_dotenv(_root / ".env")
except ImportError:
    pass

sys.path.insert(0, str(_root))
sys.path.insert(0, str(_root / "backend_api_python"))

import pandas as pd

from app.utils.db_market import normalize_1d_time

# ============================================================
# 指数配置 — symbol: {name, ak, bs}
# ============================================================
# symbol 用 tushare 格式存储（000001.SH），akshare/baostock 各自转换

INDICES: Dict[str, dict] = {
    # 上证
    "000001.SH": {"name": "上证指数",  "ak": "sh000001", "bs": "sh.000001"},
    "000016.SH": {"name": "上证50",    "ak": "sh000016", "bs": "sh.000016"},
    "000300.SH": {"name": "沪深300",   "ak": "sh000300", "bs": "sh.000300"},
    "000905.SH": {"name": "中证500",   "ak": "sh000905", "bs": "sh.000905"},
    "000852.SH": {"name": "中证1000",  "ak": "sh000852", "bs": "sh.000852"},
    # 深证
    "399001.SZ": {"name": "深证成指",  "ak": "sz399001", "bs": "sz.399001"},
    "399006.SZ": {"name": "创业板指",  "ak": "sz399006", "bs": "sz.399006"},
    "399005.SZ": {"name": "中小板指",  "ak": "sz399005", "bs": "sz.399005"},
    # 科创/北证
    "000688.SH": {"name": "科创50",    "ak": "sh000688", "bs": "sh.000688"},
}

# ============================================================
# 数据源: akshare
# ============================================================

def fetch_ak(symbol_cfg: dict, start: str = "", end: str = "") -> Optional[pd.DataFrame]:
    """akshare 指数日线。返回 DataFrame[date, open, high, low, close, volume]"""
    try:
        import akshare as ak
        code = symbol_cfg["ak"]
        df = ak.stock_zh_index_daily(symbol=code)
        if df is None or df.empty:
            return None
        df = df.rename(columns={"date": "date"})
        df["date"] = pd.to_datetime(df["date"])
        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df[["date", "open", "high", "low", "close", "volume"]].dropna(subset=["close"])
        df = df.sort_values("date").reset_index(drop=True)
        if start:
            df = df[df["date"] >= start]
        if end:
            df = df[df["date"] <= end]
        return df
    except Exception as e:
        print(f"    [akshare] 失败: {e}")
        return None


# ============================================================
# 数据源: baostock
# ============================================================

def fetch_bs(symbol_cfg: dict, start: str = "", end: str = "") -> Optional[pd.DataFrame]:
    """baostock 指数日线。返回 DataFrame[date, open, high, low, close, volume]"""
    try:
        import baostock as bs
        bs.login()
        try:
            code = symbol_cfg["bs"]
            today = datetime.date.today().strftime("%Y-%m-%d")
            s = start or (datetime.date.today() - datetime.timedelta(days=3650)).strftime("%Y-%m-%d")
            e = end or today
            rs = bs.query_history_k_data_plus(
                code, "date,open,high,low,close,volume",
                start_date=s, end_date=e, frequency="d", adjustflag="3"
            )
            data = []
            while rs.next():
                data.append(rs.get_row_data())
            if not data:
                return None
            df = pd.DataFrame(data, columns=rs.fields)
            df["date"] = pd.to_datetime(df["date"])
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.dropna(subset=["close"])
            df = df.sort_values("date").reset_index(drop=True)
            return df
        finally:
            bs.logout()
    except Exception as e:
        print(f"    [baostock] 失败: {e}")
        return None


# ============================================================
# 数据源: tushare
# ============================================================

def fetch_ts(symbol: str, start: str = "", end: str = "") -> Optional[pd.DataFrame]:
    """通过 index.py 获取指数日线。返回 DataFrame[date, open, high, low, close, volume]"""
    try:
        from app.market_cn.index import get_index_daily_kline
        # 转换 symbol 格式: "000001.SH" -> "000001"
        code = symbol.split(".")[0]
        data = get_index_daily_kline(code, 800)
        if not data:
            return None
        df = pd.DataFrame(data)
        df["date"] = pd.to_datetime(df["date"])
        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df[["date", "open", "high", "low", "close", "volume"]].dropna(subset=["close"])
        df = df.sort_values("date").reset_index(drop=True)
        if start:
            df = df[df["date"] >= start]
        if end:
            df = df[df["date"] <= end]
        return df
    except Exception as e:
        print(f"    [index.py] 失败: {e}")
        return None


# ============================================================
# 多源降级拉取
# ============================================================

def fetch_index_daily(symbol: str, start: str = "", end: str = "") -> Optional[pd.DataFrame]:
    """多源降级拉取指数日线"""
    cfg = INDICES.get(symbol)
    if not cfg:
        print(f"  ⚠️  未知指数: {symbol}，跳过")
        return None

    print(f"  📊 {cfg['name']} ({symbol})")

    # akshare 优先
    df = fetch_ak(cfg, start, end)
    if df is not None and len(df) > 0:
        print(f"    ✅ akshare: {len(df)} 条 ({df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()})")
        return df

    # baostock
    df = fetch_bs(cfg, start, end)
    if df is not None and len(df) > 0:
        print(f"    ✅ baostock: {len(df)} 条 ({df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()})")
        return df

    # tushare
    df = fetch_ts(symbol, start, end)
    if df is not None and len(df) > 0:
        print(f"    ✅ tushare: {len(df)} 条 ({df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()})")
        return df

    print(f"    ❌ 所有数据源均失败")
    return None


# ============================================================
# 写入数据库
# ============================================================

def _df_to_records(df: pd.DataFrame, symbol: str) -> List[dict]:
    """将 DataFrame 转为待写入记录，1D bar 时间统一归一到当天 15:00:00（收盘时间）。"""
    records: List[dict] = []
    for _, row in df.iterrows():
        dt = normalize_1d_time(row["date"].to_pydatetime())
        records.append({
            "symbol": symbol,
            "time": dt,
            "open": float(row["open"]) if pd.notna(row["open"]) else 0,
            "high": float(row["high"]) if pd.notna(row["high"]) else 0,
            "low": float(row["low"]) if pd.notna(row["low"]) else 0,
            "close": float(row["close"]) if pd.notna(row["close"]) else 0,
            "volume": float(row["volume"]) if pd.notna(row["volume"]) else 0,
        })
    return records


def write_to_db(pool, symbol: str, df: pd.DataFrame, dry_run: bool = False) -> int:
    """将 DataFrame 写入 kline_1D_YYYY 表，按年分表，UPSERT"""
    if df is None or df.empty:
        return 0

    records = _df_to_records(df, symbol)

    # 按年分组
    year_groups: Dict[int, list] = {}
    for rec in records:
        dt = rec["time"]
        year = dt.year
        if year not in year_groups:
            year_groups[year] = []
        year_groups[year].append(rec)

    total_written = 0
    for year, records in sorted(year_groups.items()):
        table = f'"kline_1D_{year}"'
        if dry_run:
            print(f"    [dry-run] {table}: {len(records)} 条 (不写入)")
            total_written += len(records)
            continue

        try:
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    # 确保表存在
                    cur.execute(f"""
                        CREATE TABLE IF NOT EXISTS {table} (
                            symbol   VARCHAR(20) NOT NULL,
                            time     TIMESTAMP   NOT NULL,
                            open     DOUBLE PRECISION,
                            high     DOUBLE PRECISION,
                            low      DOUBLE PRECISION,
                            close    DOUBLE PRECISION,
                            volume   DOUBLE PRECISION,
                            PRIMARY KEY (symbol, time)
                        )
                    """)

                    # UPSERT
                    for rec in records:
                        cur.execute(f"""
                            INSERT INTO {table} (symbol, time, open, high, low, close, volume)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (symbol, time) DO UPDATE SET
                                open   = EXCLUDED.open,
                                high   = EXCLUDED.high,
                                low    = EXCLUDED.low,
                                close  = EXCLUDED.close,
                                volume = EXCLUDED.volume
                        """, (
                            rec["symbol"], rec["time"],
                            rec["open"], rec["high"], rec["low"], rec["close"], rec["volume"],
                        ))
                    conn.commit()
                    total_written += len(records)
        except Exception as e:
            print(f"    ❌ {table} 写入失败: {e}")

    return total_written


# ============================================================
# 主逻辑
# ============================================================

def get_pool():
    """获取 CNStock 连接池"""
    from app.utils.db_market import get_market_db_manager
    mgr = get_market_db_manager()
    mgr.ensure_market_db("CNStock")
    return mgr._get_pool("CNStock")


def main():
    parser = argparse.ArgumentParser(description="A股主要指数日K线同步")
    parser.add_argument("--date", help="指定日期 (YYYY-MM-DD)，默认今天")
    parser.add_argument("--start", help="回填起始日期 (YYYY-MM-DD)")
    parser.add_argument("--backfill", action="store_true", help="历史回填模式（不指定 --start 则拉全部历史）")
    parser.add_argument("--indices", help="指定指数，逗号分隔 (如 000001.SH,000300.SH)")
    parser.add_argument("--dry-run", action="store_true", help="只看不写")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细输出")
    args = parser.parse_args()

    # 确定要同步的指数
    if args.indices:
        symbols = [s.strip() for s in args.indices.split(",") if s.strip()]
        symbols = [s for s in symbols if s in INDICES]
        if not symbols:
            print("❌ 无有效指数代码")
            sys.exit(1)
    else:
        symbols = list(INDICES.keys())

    # 确定日期范围
    if args.backfill:
        start = args.start or ""
        end = args.date or ""
        mode = f"历史回填 ({start or '最早'} ~ {end or '今天'})"
    else:
        # 默认: 只同步当天
        target = args.date or datetime.date.today().strftime("%Y-%m-%d")
        start = target
        end = target
        mode = f"单日 ({target})"

    print("=" * 60)
    print("  sync_index_daily — 指数日K线同步")
    print("=" * 60)
    print(f"  模式: {mode}")
    print(f"  指数: {len(symbols)} 个")
    if args.dry_run:
        print("  ⚠️  dry-run 模式，不写入数据库")
    print("=" * 60)

    pool = get_pool()

    total_written = 0
    failed = []

    for symbol in symbols:
        df = fetch_index_daily(symbol, start=start, end=end)
        if df is None or df.empty:
            failed.append(symbol)
            continue

        written = write_to_db(pool, symbol, df, dry_run=args.dry_run)
        total_written += written
        if args.verbose:
            print(f"    → 写入 {written} 条")

    # 汇总
    print("\n" + "=" * 60)
    print(f"  完成: {total_written} 条写入, {len(failed)} 个失败")
    if failed:
        print(f"  失败: {', '.join(failed)}")
    print("=" * 60)


# ═══════════════════════════════════════════════════════════
#  调度入口 — 供 scheduler 调用
# ═══════════════════════════════════════════════════════════

def sync_index_daily(target_date: str = None, dry_run: bool = False) -> int:
    """同步指数日K线到 kline_1D_YYYY。返回写入条数。"""
    if target_date is None:
        target_date = datetime.date.today().strftime("%Y-%m-%d")

    pool = get_pool()
    total = 0
    for symbol in INDICES:
        df = fetch_index_daily(symbol, start=target_date, end=target_date)
        if df is not None and not df.empty:
            total += write_to_db(pool, symbol, df, dry_run=dry_run)
    return total


if __name__ == "__main__":
    main()
