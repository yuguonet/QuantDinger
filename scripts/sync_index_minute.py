#!/usr/bin/env python3
"""
sync_index_minute.py — A股主要指数 5m K线同步 (kline_index_5m)

将大盘指数 5 分钟线写入独立表 kline_index_5m (不与个股混表, 不与 1m 混表)。
symbol 格式: "000001.SH" / "399001.SZ" 等（tushare 格式，与 sync_index_daily 一致）

数据源:
  - sina CN_MarketData.getKLineData (scale=5)，服务器硬顶 5001 根 ≈ 104 个交易日
    (默认主力军, 每日增量+回补同一入口, volume=官方口径)
  - mootdx index_bars (frequency=0 → 5m)，单次 800 根 ≈ 17 个交易日 (--source mootdx 备用)

**mootdx 已降级备用的根因 (2026-09-12 定案, tmp/_em_crosscheck.out)**:
  pytdx GetIndexBars 对指数分钟包字段错位 — 解码 "vol" 实际是 成交额(元)/100
  (三方逐 bar 对比: TDX vol×100 ≡ EM amount, 误差 0.003%; TDX vol/真实volume
  = 当日均价/100, 日内漂移 0.138~0.20 即均价波动)。up_count/down_count 解码正确,
  但用户裁定涨跌家数改由个股分时线计算, mootdx 宽度列不再是采集理由。

两源对齐结论 (2026-09-12 实测, tmp/_sina_align.out / _sina_vol.out):
  - 时间戳: 两源一致 (bar 结束时刻, 09:35 起), 直接对齐 48/48 根, 无需换算
  - volume: 新浪 = 官方日线精确一致 (57,912,314,500); TDX 5m vol 系统性偏小
    (≈官方 1/6) 且逐 bar 比值漂移 0.138~0.20 非固定倍数 → 以新浪为准,
    回补重叠窗口经 UPSERT 顺带归一 TDX 坏 volume
  - 价格: 新浪 3 位小数精度高于 mootdx, close 差 ≤0.005 (精度噪声)

设计点:
  - 独立单表不分年: 4~9 指数 × 48 根/天 ≈ 4.8万行/年, 无分区必要
  - UPSERT (symbol, time) → 幂等, 重复跑不重不漏
  - 800 根窗口 = 断采一周仍可自动补回 (健壮性远优于 1m 的 3.3 天, 粒度决策见
    tmp/数据基建评估_大盘分钟与龙虎榜.md)
  - 附带修正窗口内最近几根 bar 的盘中抖动 (同一根 bar 反复覆盖为最新值)

用法:
  python scripts/sync_index_minute.py                    # 默认新浪 (≤5001 根, 每日跑即可)
  python scripts/sync_index_minute.py --indices 000001.SH,000300.SH
  python scripts/sync_index_minute.py --bars 2000        # 只拉最近 2000 根
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
SINA_BARS = 5001               # 新浪服务器硬顶, ≈104 个交易日

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


def fetch_5m_sina(symbol: str, datalen: int = SINA_BARS) -> Optional[pd.DataFrame]:
    """新浪指数 5m 回补 (CN_MarketData.getKLineData, scale=5)。

    返回与 fetch_5m 同构的 DataFrame[time, open, high, low, close, volume,
    up_count, down_count] (up/down_count 新浪无 → NA, UPSERT COALESCE 保留 TDX 值)。

    易错点 (2026-09-12 实测):
      - 5001 根大响应偶发读超时 → timeout=40 + 4 次重试
      - 连续请求会被限流断连 → 调用方在指数间 sleep
      - 无效代码返回非列表 JSON → 校验 isinstance(list)
      - volume 即官方口径 (与 kline_1D 日线精确一致), 不做任何倍数换算
    """
    import json
    import time as _time
    import urllib.request

    code, suffix = symbol.split(".")
    sina_code = ("sh" if suffix == "SH" else "sz") + code
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={sina_code}&scale=5&ma=no&datalen={datalen}")
    data = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=40) as r:
                data = json.loads(r.read().decode("utf-8"))
            break
        except Exception as e:
            if attempt == 3:
                print(f"    [sina] 拉取失败: {e}")
                return None
            _time.sleep(3)
    if not isinstance(data, list) or not data:
        print(f"    [sina] 无数据 ({sina_code})")
        return None
    df = pd.DataFrame(data)
    need = ["open", "high", "low", "close", "volume"]
    for c in need:
        if c not in df.columns:
            print(f"    [sina] 缺列 {c}, 实际: {list(df.columns)}")
            return None
    df["time"] = pd.to_datetime(df["day"]) if "day" in df.columns else pd.to_datetime(df.iloc[:, 0])
    for c in need:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["up_count"] = pd.NA
    df["down_count"] = pd.NA
    df = df[["time"] + need + ["up_count", "down_count"]].dropna(subset=["close"])
    df = df.sort_values("time").reset_index(drop=True)
    return df


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
         verbose: bool = False, source: str = "sina",
         bars: Optional[int] = None) -> Dict[str, int]:
    """同步入口 (scheduler 与 CLI 共用)。返回 {written, failed} 统计。

    source: "sina"   主力军 (≤5001 根, volume=官方口径, 每日增量即全量覆盖) |
            "mootdx" 备用 (800 根; ⚠️ vol 字段错位=成交额/100, 不可用, 见文件头)
    """
    symbols = symbols or list(INDICES.keys())
    pool = get_pool()
    total, failed = 0, []
    for symbol in symbols:
        name = INDICES.get(symbol, symbol)
        print(f"  📊 {name} ({symbol})")
        if source == "sina":
            df = fetch_5m_sina(symbol, datalen=bars or SINA_BARS)
            src_label = "sina"
        else:
            df = fetch_5m(symbol)
            src_label = "mootdx"
        if df is None or df.empty:
            failed.append(symbol)
            print("    ❌ 拉取失败")
            continue
        if verbose:
            print(f"    {src_label}: {len(df)} 根 ({df['time'].iloc[0]} ~ {df['time'].iloc[-1]})")
        written = write_to_db(pool, symbol, df, dry_run=dry_run)
        total += written
        if verbose:
            print(f"    → 写入 {written} 条")
        if source == "sina":
            import time as _time
            _time.sleep(2)  # 新浪限流防护 (2026-09-12 实测连续请求会断连)
    print(f"\n  完成: {total} 条写入, {len(failed)} 个失败"
          + (f" ({', '.join(failed)})" if failed else ""))
    return {"written": total, "failed": len(failed)}


def main():
    parser = argparse.ArgumentParser(description="A股主要指数 5m K线同步")
    parser.add_argument("--indices", help="指定指数, 逗号分隔 (如 000001.SH,000300.SH)")
    parser.add_argument("--source", choices=["sina", "mootdx"], default="sina",
                        help="数据源: sina=主力军≤5001根(默认) mootdx=备用(vol字段错位不可用)")
    parser.add_argument("--bars", type=int, help="覆盖拉取根数 (sina 上限 5001)")
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
    if args.source == "sina":
        print(f"  模式: 新浪主力军 (≤{args.bars or SINA_BARS} 根 ≈ 104 交易日)")
    else:
        print(f"  模式: mootdx 备用 (最近 {PULL_BARS} 根 ≈ 17 交易日) ⚠️ vol 字段错位不可用")
    print(("  [dry-run]" if args.dry_run else ""))
    print("=" * 60)
    sync(symbols, dry_run=args.dry_run, verbose=args.verbose,
         source=args.source, bars=args.bars)


if __name__ == "__main__":
    main()
