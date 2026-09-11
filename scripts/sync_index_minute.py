#!/usr/bin/env python3
"""
sync_index_minute.py — A股主要指数 5m K线同步 (kline_index_5m)

将大盘指数 5 分钟线写入独立表 kline_index_5m (不与个股混表, 不与 1m 混表)。
symbol 格式: "000001.SH" / "399001.SZ" 等（tushare 格式，与 sync_index_daily 一致）

数据源: mootdx index_bars (frequency=0 → 5m)，单次 800 根 ≈ 17 个交易日。
指数分钟线不可外购回补 (只能向前攒)，每根 bar 自带 up_count/down_count 涨跌家数
(市场宽度时序, 环境特征金矿, 2026-09-11 实测)。

设计点:
  - 独立单表不分年: 4~9 指数 × 48 根/天 ≈ 4.8万行/年, 无分区必要
  - UPSERT (symbol, time) → 幂等, 重复跑不重不漏
  - 800 根窗口 = 断采一周仍可自动补回 (健壮性远优于 1m 的 3.3 天, 粒度决策见
    tmp/数据基建评估_大盘分钟与龙虎榜.md)
  - 附带修正窗口内最近几根 bar 的盘中抖动 (同一根 bar 反复覆盖为最新值)

用法:
  python scripts/sync_index_minute.py                    # 增量同步 (最近 800 根)
  python scripts/sync_index_minute.py --indices 000001.SH,000300.SH
  python scripts/sync_index_minute.py --dry-run          # 只看不写
  python scripts/sync_index_minute.py --verbose

调度: 由 backend scheduler._post_market_batch 在 1m 回填后调用 sync() 函数。
"""

import sys
import argparse
import datetime
from pathlib import Path
from typing import Dict, List, Optional

# ============================================================
# 路径 & 环境 (同 sync_index_daily.py)
# ============================================================
_root = Path(__file__).resolve().parent.parent  # scripts/ → QuantDinger/
sys.path.insert(0, str(_root / "backend_api_python"))

try:
    from dotenv import load_dotenv
    load_dotenv(_root / "backend_api_python" / ".env")
    load_dotenv(_root / ".env")
except ImportError:
    pass

import pandas as pd

# ============================================================
# 指数配置 — 与 sync_index_daily.INDICES 完全同键 (symbol 格式一致)
# ============================================================

INDICES: Dict[str, str] = {
    "000001.SH": "上证指数",
    "000016.SH": "上证50",
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "399005.SZ": "中小板指",
    "000688.SH": "科创50",
}

TABLE = "kline_index_5m"       # 独立表, 不分年
PULL_BARS = 800                # mootdx 单次上限, ≈17 个交易日

# ============================================================
# 数据源: mootdx
# ============================================================

def fetch_5m(symbol: str, offset: int = PULL_BARS) -> Optional[pd.DataFrame]:
    """mootdx 指数 5m。返回 DataFrame[time, open, high, low, close, volume,
    up_count, down_count] (time 为 bar 结束时刻)。"""
    try:
        from app.utils.mootdx_client import get_client
        cli = get_client()
        if cli is None:
            print("    [mootdx] 客户端创建失败")
            return None
        code = symbol.split(".")[0]
        df = cli.index_bars(symbol=code, frequency=0, start=0, offset=offset)
        if df is None or len(df) == 0:
            return None
        # 列名防御: mootdx 原始列同时含 vol 与 volume (2026-09-11 实测),
        # 仅在缺 volume 时才用 vol 补位, 否则 rename 会产生重复列名 (二维取列报错)
        if "volume" not in df.columns and "vol" in df.columns:
            df = df.rename(columns={"vol": "volume"})
        need = ["open", "high", "low", "close", "volume"]
        for c in need:
            if c not in df.columns:
                print(f"    [mootdx] 缺列 {c}, 实际: {list(df.columns)}")
                return None
        # 时间列: 'datetime' 列, 缺失时取首列 (mootdx 惯例, 2026-09-11 实测)
        tcol = "datetime" if "datetime" in df.columns else df.columns[0]
        df["time"] = pd.to_datetime(df[tcol])
        for c in need:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        for c in ("up_count", "down_count"):
            df[c] = pd.to_numeric(df[c], errors="coerce") if c in df.columns else pd.NA
        df = df[["time"] + need + ["up_count", "down_count"]].dropna(subset=["close"])
        df = df.sort_values("time").reset_index(drop=True)
        return df
    except Exception as e:
        print(f"    [mootdx] 失败: {e}")
        return None


# ============================================================
# 写入数据库 — UPSERT 幂等
# ============================================================

def write_to_db(pool, symbol: str, df: pd.DataFrame, dry_run: bool = False) -> int:
    if df is None or df.empty:
        return 0
    table = f'"{TABLE}"'
    if dry_run:
        print(f"    [dry-run] {table} {symbol}: {len(df)} 条 "
              f"({df['time'].iloc[0]} ~ {df['time'].iloc[-1]})")
        return len(df)
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        symbol     VARCHAR(20) NOT NULL,
                        time       TIMESTAMP   NOT NULL,
                        open       DOUBLE PRECISION,
                        high       DOUBLE PRECISION,
                        low        DOUBLE PRECISION,
                        close      DOUBLE PRECISION,
                        volume     DOUBLE PRECISION,
                        up_count   INTEGER,
                        down_count INTEGER,
                        PRIMARY KEY (symbol, time)
                    )
                """)
                for _, row in df.iterrows():
                    cur.execute(f"""
                        INSERT INTO {table}
                            (symbol, time, open, high, low, close, volume,
                             up_count, down_count)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (symbol, time) DO UPDATE SET
                            open       = EXCLUDED.open,
                            high       = EXCLUDED.high,
                            low        = EXCLUDED.low,
                            close      = EXCLUDED.close,
                            volume     = EXCLUDED.volume,
                            up_count   = COALESCE(EXCLUDED.up_count,   {table}.up_count),
                            down_count = COALESCE(EXCLUDED.down_count, {table}.down_count)
                    """, (
                        symbol, row["time"],
                        float(row["open"]), float(row["high"]),
                        float(row["low"]), float(row["close"]), float(row["volume"]),
                        None if pd.isna(row["up_count"]) else int(row["up_count"]),
                        None if pd.isna(row["down_count"]) else int(row["down_count"]),
                    ))
            conn.commit()
        return len(df)
    except Exception as e:
        print(f"    ❌ {table} 写入失败: {e}")
        return 0


# ============================================================
# 主逻辑
# ============================================================

def get_pool():
    from app.utils.db_market import get_market_db_manager
    mgr = get_market_db_manager()
    mgr.ensure_market_db("CNStock")
    return mgr._get_pool("CNStock")


def sync(symbols: Optional[List[str]] = None, dry_run: bool = False,
         verbose: bool = False) -> Dict[str, int]:
    """同步入口 (scheduler 与 CLI 共用)。返回 {written, failed} 统计。"""
    symbols = symbols or list(INDICES.keys())
    pool = get_pool()
    total, failed = 0, []
    for symbol in symbols:
        name = INDICES.get(symbol, symbol)
        print(f"  📊 {name} ({symbol})")
        df = fetch_5m(symbol)
        if df is None or df.empty:
            failed.append(symbol)
            print("    ❌ 拉取失败")
            continue
        if verbose:
            print(f"    mootdx: {len(df)} 根 ({df['time'].iloc[0]} ~ {df['time'].iloc[-1]})")
        written = write_to_db(pool, symbol, df, dry_run=dry_run)
        total += written
        if verbose:
            print(f"    → 写入 {written} 条")
    print(f"\n  完成: {total} 条写入, {len(failed)} 个失败"
          + (f" ({', '.join(failed)})" if failed else ""))
    return {"written": total, "failed": len(failed)}


def main():
    parser = argparse.ArgumentParser(description="A股主要指数 5m K线同步")
    parser.add_argument("--indices", help="指定指数, 逗号分隔 (如 000001.SH,000300.SH)")
    parser.add_argument("--dry-run", action="store_true", help="只看不写")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细输出")
    args = parser.parse_args()

    symbols = None
    if args.indices:
        symbols = [s.strip() for s in args.indices.split(",") if s.strip() in INDICES]
        if not symbols:
            print("❌ 无有效指数代码")
            sys.exit(1)

    print("=" * 60)
    print(f"  sync_index_minute — 指数 5m K线同步 → {TABLE}")
    print(f"  模式: 增量 (最近 {PULL_BARS} 根 ≈ 17 交易日)"
          + ("  [dry-run]" if args.dry_run else ""))
    print("=" * 60)
    sync(symbols, dry_run=args.dry_run, verbose=args.verbose)


if __name__ == "__main__":
    main()
