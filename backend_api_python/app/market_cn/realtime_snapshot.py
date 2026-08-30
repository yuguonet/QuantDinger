"""
realtime_snapshot.py — 全市场实时行情快照原始数据采集

═══════════════════════════════════════════════════════════════
  本文件负责盘中原始快照存储，不负责 K 线转换。
  精确 1m K 线由 backfill_db.run_1m() 盘后回填。
═══════════════════════════════════════════════════════════════

在架构中的位置:
  scheduler.py (interval=60, trading_only=True)
    → collect_realtime_snapshot()          ← 本文件
      → basicinfo_db.market_all_codes()    ← 股票列表
      → coordinator.coordinate_batch_quotes() ← 多源并发拉取
      → UPSERT 到 realtime_quote_snapshot_YYYY

  scheduler.py (post_market_batch)
    → backfill_db.run_1m()                 ← 盘后精确1m K线

核心职责:
  1. 盘中每分钟拉取全市场实时行情快照 (原始数据)
  2. 存入独立表 realtime_quote_snapshot_YYYY (按年分表)
  3. 供盘中 VWAP/换手率等指标直接读取，不经过 1m K 线中转

设计原则:
  1. 存原始数据，不做任何 K 线转换或指标计算
  2. ON CONFLICT (symbol, time) DO UPDATE 幂等写入，重复跑不产生重复数据
  3. 独立表，不与 kline 表混用
  4. 表自动创建 (CREATE TABLE IF NOT EXISTS)，无需手动建表

表结构 (realtime_quote_snapshot_YYYY):
  核心字段 — 与 coordinator 返回的行情 dict 字段一一对应:
    symbol, time, "last", open, high, low, "previousClose", volume
  扩展字段 — extras JSONB，各源返回的额外数据 (amount/change/changePercent 等)，
    有就存，没有就是 NULL，不同源返回内容不同，不保证一致性。

⚠️ 容易踩坑的点:
  1. "last" 和 "previousClose" 是 PostgreSQL 保留字/特殊名，SQL 中必须加双引号
  2. coordinator 返回的字段名是 camelCase (previousClose)，不是 snake_case
  3. volume 单位是"股"（各源已归一化），不是"手"
  4. extras 内容因源而异，不要在业务逻辑中依赖 extras 的具体字段
  5. 与 backfill_db 的 kline 表是两套独立数据，不要混用
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)

TZ_CN = timezone(timedelta(hours=8))

# 表名前缀 — 按年分表: realtime_quote_snapshot_YYYY
_TABLE_PREFIX = "realtime_quote_snapshot"


# ================================================================
# 建表
# ================================================================

def _ensure_snapshot_table(year: int):
    """确保快照表存在 (idempotent)"""
    from app.utils.db_market import get_market_db_manager
    mgr = get_market_db_manager()
    pool = mgr._get_pool("CNStock")
    table = f"{_TABLE_PREFIX}_{year}"
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS "{table}" (
                    symbol           VARCHAR(16) NOT NULL,
                    time             TIMESTAMP WITHOUT TIME ZONE NOT NULL,
                    "last"           DOUBLE PRECISION,
                    open             DOUBLE PRECISION,
                    high             DOUBLE PRECISION,
                    low              DOUBLE PRECISION,
                    "previousClose" DOUBLE PRECISION,
                    volume           DOUBLE PRECISION,
                    extras           JSONB,
                    PRIMARY KEY (symbol, time)
                )
            """)
            conn.commit()


# ================================================================
# 股票列表
# ================================================================

def _load_all_codes() -> List[str]:
    """从 basicinfo_db 获取全市场活跃股票代码 (纯6位数字)"""
    from app.utils.basicinfo_db import get_stock_basic_db
    db = get_stock_basic_db()
    return db.market_all_codes(status="active")


# ================================================================
# 快照数据提取
# ================================================================

# 核心字段集合 — 在这些范围内的 key 存独立列，其余打入 extras JSONB
# 注意: "close" 和 "price" 是部分源对 "last" 的别名，归入核心字段避免重复存
_CORE_KEYS = {"symbol", "last", "open", "high", "low", "previousClose", "prev_close", "volume", "close", "price"}


def _extract_snapshot_records(quotes: List[Dict], ts_str: str) -> List[Dict]:
    """
    coordinator 返回的行情 dict → 快照记录列表。

    核心字段存独立列，其余字段打包到 extras JSONB (有就存，没有忽略)。
    """
    records = []
    for q in quotes:
        sym = q.get("symbol", "")
        last = q.get("last") or q.get("close") or q.get("price") or 0
        if last <= 0:
            continue

        # 核心字段
        rec = {
            "symbol": sym,
            "time": ts_str,
            "last": last,
            "open": q.get("open") or 0,
            "high": q.get("high") or 0,
            "low": q.get("low") or 0,
            "previousClose": q.get("previousClose") or q.get("prev_close") or 0,
            "volume": q.get("volume") or 0,
        }

        # 扩展字段 → extras (有就存，没有不加)
        extras = {k: v for k, v in q.items() if k not in _CORE_KEYS and v is not None and v != ""}
        if extras:
            rec["extras"] = extras

        records.append(rec)
    return records


# ================================================================
# DB 写入 (UPSERT)
# ================================================================

def _bulk_upsert(records: List[Dict], year: int) -> int:
    """
    批量 UPSERT 快照到 realtime_quote_snapshot_YYYY。

    ON CONFLICT (symbol, time) DO UPDATE — 幂等，同一 symbol+time 覆盖更新。
    按 symbol 分组写入，单个 symbol 失败不影响其他。

    Returns:
        成功写入的记录数
    """
    if not records:
        return 0

    import json as _json
    from psycopg2.extras import execute_values
    from app.utils.db_market import get_market_db_manager, _ensure_datetime

    mgr = get_market_db_manager()
    pool = mgr._get_pool("CNStock")
    table = f"{_TABLE_PREFIX}_{year}"

    # 按 symbol 分组
    by_symbol: Dict[str, List[Dict]] = {}
    for rec in records:
        sym = rec["symbol"]
        by_symbol.setdefault(sym, []).append(rec)

    total = 0
    with pool.connection() as conn:
        cur = conn.cursor()
        for sym, recs in by_symbol.items():
            values = [
                (r["symbol"], _ensure_datetime(r["time"]),
                 r["last"], r["open"], r["high"], r["low"],
                 r["previousClose"], r["volume"],
                 _json.dumps(r["extras"]) if r.get("extras") else None)
                for r in recs
            ]
            sql = f"""
                INSERT INTO "{table}"
                    (symbol, time, "last", open, high, low, "previousClose", volume, extras)
                VALUES %s
                ON CONFLICT (symbol, time) DO UPDATE SET
                    "last"          = EXCLUDED."last",
                    open            = EXCLUDED.open,
                    high            = EXCLUDED.high,
                    low             = EXCLUDED.low,
                    "previousClose" = EXCLUDED."previousClose",
                    volume          = EXCLUDED.volume,
                    extras          = EXCLUDED.extras
            """
            try:
                execute_values(cur, sql, values, page_size=5000)
                total += len(values)
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.warning("[realtime_snapshot] 写入 %s 失败: %s", sym, e)
        cur.close()

    return total


# ================================================================
# 主入口 — scheduler 调用
# ================================================================

def collect_realtime_snapshot() -> Dict:
    """
    全市场实时行情快照采集。

    流程: 拉代码 → coordinator 批量拉取 → 原始数据 UPSERT 写入

    Returns:
        {"status": "ok"|"error", "stocks": int, "written": int,
         "failed": int, "elapsed": float}
    """
    t0 = time.time()

    # ── 1. 获取全市场代码 ──
    try:
        codes = _load_all_codes()
    except Exception as e:
        logger.error("[realtime_snapshot] 获取股票列表失败: %s", e)
        return {"status": "error", "stocks": 0, "written": 0,
                "failed": 0, "elapsed": time.time() - t0}

    if not codes:
        logger.warning("[realtime_snapshot] 股票列表为空")
        return {"status": "error", "stocks": 0, "written": 0,
                "failed": 0, "elapsed": time.time() - t0}

    # ── 2. coordinator 批量拉取 ──
    from app.data_sources.coordinator import get_coordinator
    coord = get_coordinator()
    quotes = coord.coordinate_batch_quotes(
        symbols=codes, market="CNStock", timeout=45,
    )

    if not quotes:
        logger.warning("[realtime_snapshot] 拉取 0 条行情")
        return {"status": "error", "stocks": len(codes), "written": 0,
                "failed": len(codes), "elapsed": time.time() - t0}

    # ── 3. 提取原始快照 ──
    now = datetime.now(TZ_CN)
    ts_str = now.strftime("%Y-%m-%d %H:%M:00")
    year = now.year
    records = _extract_snapshot_records(quotes, ts_str)

    # ── 4. 建表 + UPSERT ──
    _ensure_snapshot_table(year)
    written = _bulk_upsert(records, year)

    elapsed = time.time() - t0
    failed = len(codes) - len(quotes)

    logger.info(
        "[realtime_snapshot] 完成: %d/%d 只, %d 条写入, %d 失败, %.2fs",
        len(quotes), len(codes), written, failed, elapsed,
    )

    return {
        "status": "ok" if len(quotes) / len(codes) > 0.8 else "error",
        "stocks": len(codes),
        "written": written,
        "failed": failed,
        "elapsed": round(elapsed, 2),
    }
