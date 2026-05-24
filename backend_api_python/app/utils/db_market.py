"""
db_market.py — 多市场行情数据读写（上层）

职责:
  1. 接口A: 增量写入（单条/小批量，UPSERT）
  2. 接口B: 大批量写入（自动分市场、分年存储）
  3. 接口C: 查询（按 symbol + 时间范围）
  4. 全局单例管理（get_market_db_manager / get_market_kline_writer）

依赖:
  - db_multi.py（下层）：连接池、MarketDBManager、共享常量

用法:
  from app.utils.db_market import get_market_kline_writer, get_market_db_manager

  mgr = get_market_db_manager()
  mgr.ensure_market_db("CNStock")
  writer = get_market_kline_writer()

  # 增量写入
  writer.upsert("CNStock", "600519", "15m", [
      {"time": datetime(2024,4,12,9,45), "open": 15.79, "high": 15.88,
       "low": 15.72, "close": 15.81, "volume": 580200}
  ])

  # 批量写入
  writer.bulk_write("CNStock", [
      {"symbol": "600519", "timeframe": "15m", "time": datetime(...), ...},
  ])

  # 查询
  rows = writer.query("CNStock", "600519", "15m", start_time=..., end_time=...)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict

from psycopg2.extras import execute_values

from app.utils.logger import get_logger
from app.utils.db_multi import (
    MarketDBManager,
    _market_db_name, _resolve_market, _table_name,
)

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# db_market 专属常量
# ---------------------------------------------------------------------------

def _year_from_ts(ts) -> int:
    """从 datetime 对象或整数时间戳提取年份"""
    if isinstance(ts, datetime):
        return ts.year
    return datetime.fromtimestamp(ts, tz=timezone.utc).year


def _ensure_datetime(value) -> datetime:
    """将各种时间格式统一转为 datetime 对象（兼容 TIMESTAMP 列）

    TIMESTAMP WITHOUT TIME ZONE 列不存储时区信息。
    写入前统一去掉时区，避免 psycopg2 按 session timezone 做隐式转换导致日期偏移。

    支持:
      - datetime 对象 → 去掉时区后返回
      - int/float → 视为 Unix 时间戳（秒），转为 UTC naive datetime
      - str → 尝试 ISO 格式解析
    """
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).replace(tzinfo=None)
    if isinstance(value, str):
        # ISO 格式: "2024-04-12T09:45:00" 或 "2024-04-12 09:45"
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                     "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(value.strip(), fmt)
                return dt  # naive datetime，无需补时区
            except ValueError:
                continue
    raise ValueError(f"无法解析时间值: {value!r}")

# ---------------------------------------------------------------------------
# MarketKlineWriter — K线数据写入
# ---------------------------------------------------------------------------

class MarketKlineWriter:
    """
    K线数据写入器。

    列名与现有 KlineCacheManager / BaseDataSource.format_kline 一致：
      time, open, high, low, close, volume

    接口A: upsert()      — 增量写入（单条/小批量，UPSERT）
    接口B: bulk_write()   — 大批量写入（自动分市场分年存储）
    """

    def __init__(self, manager: MarketDBManager = None):
        self._mgr = manager or MarketDBManager()
        # 分区表列表缓存：{(market, timeframe): set(years)}
        self._partition_cache: Dict[Tuple[str, str], set] = {}

    # ================================================================
    # 接口A: 增量写入
    # ================================================================

    def upsert(
        self,
        market: str,
        symbol: str,
        timeframe: str,
        records: List[Dict[str, Any]],
        atomic: bool = True,
    ) -> Dict[str, Any]:
        """
        增量写入 K 线数据（UPSERT）。

        已存在的 (symbol, time) 会被更新，不存在的会插入。
        自动按年分表，跨年的数据会拆分到对应年份表。

        Args:
            market:    市场标识，如 "CNStock", "us", "crypto"
            symbol:    品种代码，如 "600519", "BTC/USDT"
            timeframe: K线周期，如 "15m", "1H", "1D"
            records:   K线数据列表
            atomic:    True（默认）= 一条失败不影响其余，最终全部提交
                       False = 遇到第一条异常立即回滚全部

        Returns:
            {"inserted": int, "updated": int, "errors": int,
             "tables_used": [str], "years": [int]}
        """
        if not records:
            return {"inserted": 0, "updated": 0, "errors": 0,
                    "tables_used": [], "years": []}

        self._mgr.ensure_market_db(market)
        by_year = self._group_by_year(records)

        for year in by_year:
            self._mgr.ensure_year_table(market, timeframe, year)

        pool = self._mgr._get_pool(market)
        total_inserted = 0
        total_updated = 0
        total_errors = 0
        tables_used = []

        with pool.connection() as conn:
            cur = conn.cursor()
            try:
                for year, year_records in by_year.items():
                    table = _table_name(timeframe, year)
                    tables_used.append(table)

                    for rec in year_records:
                        try:
                            time_val = _ensure_datetime(rec["time"])
                            cur.execute(f"""
                                INSERT INTO "{table}"
                                    (symbol, time, open, high, low, close, volume)
                                VALUES (%s, %s, %s, %s, %s, %s, %s)
                                ON CONFLICT (symbol, time) DO UPDATE SET
                                    open       = EXCLUDED.open,
                                    high       = EXCLUDED.high,
                                    low        = EXCLUDED.low,
                                    close      = EXCLUDED.close,
                                    volume     = EXCLUDED.volume
                            """, (
                                symbol, time_val, rec["open"], rec["high"],
                                rec["low"], rec["close"], rec.get("volume", 0),
                            ))
                            if cur.rowcount == 1:
                                total_inserted += 1
                            else:
                                total_updated += 1
                        except Exception as e:
                            total_errors += 1
                            logger.warning(f"upsert 失败: {market}/{symbol} t={rec.get('time')}: {e}")
                            if not atomic:
                                conn.rollback()
                                raise

                conn.commit()
            except Exception:
                conn.rollback()
                raise

        result = {
            "inserted": total_inserted, "updated": total_updated,
            "errors": total_errors, "tables_used": tables_used,
            "years": sorted(by_year.keys()),
        }
        logger.info(
            f"upsert 完成: {market}/{symbol}/{timeframe} "
            f"→ +{total_inserted} ~{total_updated} ✗{total_errors} 表={tables_used}"
        )
        return result

    # ================================================================
    # 接口B: 大批量写入
    # ================================================================

    def bulk_write(
        self,
        market: str,
        records: List[Dict[str, Any]],
        on_conflict: str = "update",
        batch_size: int = 5000,
    ) -> Dict[str, Any]:
        """
        大批量写入 K 线数据。

        使用 psycopg2.extras.execute_values 批量 INSERT（比字符串拼接快 3-5 倍）。
        失败时先拆半重试，再逐条 INSERT，避免一批冲突导致整批变逐条。

        Args:
            market:      市场标识
            records:     K线数据列表，每条必须包含:
                         {"symbol": str, "timeframe": str, "time": int,
                          "open": float, "high": float, "low": float,
                          "close": float, "volume": float}
            on_conflict: "update"（默认）/ "skip" / "error"
            batch_size:  每批写入条数（默认 5000）

        Returns:
            {"total": int, "inserted": int, "skipped": int, "errors": int,
             "by_symbol": {str: {...}}, "by_table": {str: int}, "years": [int]}
        """
        if not records:
            return {"total": 0, "inserted": 0, "skipped": 0, "errors": 0,
                    "by_symbol": {}, "by_table": {}, "years": []}

        self._mgr.ensure_market_db(market)
        groups = self._group_by_symbol_tf_year(records)

        years_needed = {(tf, year) for (_, tf, year) in groups}
        for tf, year in years_needed:
            self._mgr.ensure_year_table(market, tf, year)

        pool = self._mgr._get_pool(market)
        total_inserted = 0
        total_skipped = 0
        total_errors = 0
        by_symbol: Dict[str, Dict[str, int]] = defaultdict(
            lambda: {"inserted": 0, "skipped": 0, "errors": 0}
        )
        by_table: Dict[str, int] = defaultdict(int)

        conflict_clause = self._conflict_clause(on_conflict)

        with pool.connection() as conn:
            cur = conn.cursor()
            for (symbol, timeframe, year), group_records in groups.items():
                table = _table_name(timeframe, year)

                base_sql = f"""
                    INSERT INTO "{table}"
                        (symbol, time, open, high, low, close, volume)
                    VALUES %s
                    {conflict_clause}
                """

                for batch_start in range(0, len(group_records), batch_size):
                    batch = group_records[batch_start:batch_start + batch_size]
                    value_list = [
                        (symbol, _ensure_datetime(rec["time"]), rec["open"],
                         rec["high"], rec["low"], rec["close"],
                         rec.get("volume", 0))
                        for rec in batch
                    ]

                    try:
                        execute_values(cur, base_sql, value_list, page_size=len(value_list))
                        affected = cur.rowcount
                        if on_conflict == "skip":
                            total_skipped += affected
                            by_symbol[symbol]["skipped"] += affected
                            by_table[table] += affected
                        else:
                            total_inserted += affected
                            by_symbol[symbol]["inserted"] += affected
                            by_table[table] += affected
                        conn.commit()
                    except Exception as e:
                        conn.rollback()
                        logger.warning(
                            f"批量写入失败，拆半重试: {market}/{symbol} "
                            f"{table} batch_size={len(batch)}: {e}"
                        )
                        ok, fail = self._retry_with_split(
                            conn, cur, base_sql, value_list,
                            symbol, table, on_conflict,
                        )
                        total_inserted += ok
                        total_errors += fail
                        by_symbol[symbol]["inserted"] += ok
                        by_symbol[symbol]["errors"] += fail
                        by_table[table] += ok
                        conn.commit()

            cur.close()

        result = {
            "total": len(records), "inserted": total_inserted,
            "skipped": total_skipped, "errors": total_errors,
            "by_symbol": dict(by_symbol), "by_table": dict(by_table),
            "years": sorted({y for (_, _, y) in groups}),
        }
        logger.info(
            f"bulk_write 完成: {market} 总计={len(records)} "
            f"+{total_inserted} ~{total_skipped} ✗{total_errors} "
            f"品种={len(by_symbol)} 表={len(by_table)}"
        )
        return result

    @staticmethod
    def _retry_with_split(conn, cur, base_sql, value_list, symbol, table, on_conflict):
        """批量失败时先拆半重试，再逐条 INSERT。

        拆半能快速隔离冲突行，比直接逐条 INSERT 快 5-10 倍。
        每次重试前 conn.rollback() 清除 aborted 状态，重试后由外层统一 commit。

        Returns:
            (ok_count, fail_count)
        """
        if len(value_list) <= 1:
            # 只剩 1 条，逐条处理
            try:
                conn.rollback()  # 清除 aborted 状态
                execute_values(cur, base_sql, value_list, page_size=1)
                if on_conflict == "skip":
                    return (0, 0)
                return (cur.rowcount, 0)
            except Exception:
                conn.rollback()
                return (0, 1)

        # 拆半递归
        mid = len(value_list) // 2
        ok1, fail1 = MarketKlineWriter._retry_with_split(
            conn, cur, base_sql, value_list[:mid], symbol, table, on_conflict)
        ok2, fail2 = MarketKlineWriter._retry_with_split(
            conn, cur, base_sql, value_list[mid:], symbol, table, on_conflict)
        return (ok1 + ok2, fail1 + fail2)

    # ================================================================
    # 查询辅助
    # ================================================================

    def query(
        self,
        market: str,
        symbol: str,
        timeframe: str,
        start_time=None,
        end_time=None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """
        查询 K 线数据。

        start_time/end_time: datetime 对象、ISO 字符串或 Unix 时间戳（整数）均可。
        返回格式与 BaseDataSource.format_kline 一致：
        [{"time": datetime, "open": float, "high": float,
          "low": float, "close": float, "volume": float}, ...]
        """
        if not self._mgr.market_db_exists(market):
            return []

        # 确保 start_time / end_time 是 datetime 对象（TIMESTAMP 列兼容）
        start_dt = _ensure_datetime(start_time) if start_time is not None else None
        end_dt = _ensure_datetime(end_time) if end_time is not None else None

        years = set()
        if start_dt and end_dt:
            for y in range(start_dt.year, end_dt.year + 1):
                years.add(y)
        elif start_dt:
            years.add(start_dt.year)
        elif end_dt:
            years.add(end_dt.year)

        pool = self._mgr._get_pool(market)

        if not years:
            # 不指定时间范围 → 用缓存或扫描分区表
            cache_key = (_resolve_market(market), timeframe)
            if cache_key in self._partition_cache:
                years = self._partition_cache[cache_key]
            else:
                with pool.cursor() as cur:
                    cur.execute("""
                        SELECT table_name FROM information_schema.tables
                        WHERE table_schema = 'public'
                          AND table_type = 'BASE TABLE'
                          AND table_name LIKE %s
                    """, (f'kline_{timeframe}_%',))
                    for row in cur.fetchall():
                        parts = row[0].rsplit('_', 1)
                        if len(parts) == 2:
                            try:
                                years.add(int(parts[1]))
                            except ValueError:
                                pass
                if years:
                    self._partition_cache[cache_key] = years
            if not years:
                years = {datetime.now().year}

        all_rows = []

        with pool.cursor() as cur:
            for year in sorted(years):
                table = _table_name(timeframe, year)
                cur.execute("""
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = %s
                """, (table,))
                if cur.fetchone() is None:
                    continue

                conditions, params = ["symbol = %s"], [symbol]
                if start_dt is not None:
                    conditions.append("time >= %s")
                    params.append(start_dt)
                if end_dt is not None:
                    conditions.append("time <= %s")
                    params.append(end_dt)

                cur.execute(f"""
                    SELECT time, open, high, low, close, volume
                    FROM "{table}"
                    WHERE {' AND '.join(conditions)}
                    ORDER BY time ASC
                """, params)

                for row in cur.fetchall():
                    all_rows.append({
                        "time": row[0],
                        "open": row[1],
                        "high": row[2],
                        "low": row[3],
                        "close": row[4],
                        "volume": row[5],
                    })

        all_rows.sort(key=lambda r: r["time"])
        if limit and len(all_rows) > limit:
            all_rows = all_rows[-limit:]
        return all_rows

    def stats(self, market: str) -> Dict[str, Any]:
        if not self._mgr.market_db_exists(market):
            return {"market": market, "exists": False}

        pool = self._mgr._get_pool(market)

        with pool.cursor() as cur:
            cur.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name LIKE 'kline_%'
                  AND table_name NOT LIKE 'kline_1d_%'
                ORDER BY table_name
            """)
            tables = [r[0] for r in cur.fetchall()]

            total_rows = 0
            all_symbols = set()
            min_time = max_time = None

            for tbl in tables:
                try:
                    cur.execute(f'SELECT COUNT(*), MIN(time), MAX(time) FROM "{tbl}"')
                    row = cur.fetchone()
                    if row:
                        total_rows += row[0] or 0
                        if row[1] and (min_time is None or row[1] < min_time):
                            min_time = row[1]
                        if row[2] and (max_time is None or row[2] > max_time):
                            max_time = row[2]
                    cur.execute(f'SELECT DISTINCT symbol FROM "{tbl}"')
                    for r in cur.fetchall():
                        all_symbols.add(r[0])
                except Exception:
                    pass

        # min_time/max_time 可能是 datetime 对象（TIMESTAMP 列），直接用
        def _fmt_time(t):
            if t is None:
                return None
            if isinstance(t, datetime):
                return t.isoformat()
            return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()

        return {
            "market": market,
            "db_name": _market_db_name(market),
            "exists": True,
            "tables": tables,
            "symbols": len(all_symbols),
            "symbol_list": sorted(all_symbols),
            "total_rows": total_rows,
            "date_range": {
                "start": _fmt_time(min_time),
                "end": _fmt_time(max_time),
            },
        }

    # ================================================================
    # 内部工具
    # ================================================================

    @staticmethod
    def _group_by_year(records: List[Dict[str, Any]]) -> Dict[int, List[Dict[str, Any]]]:
        by_year: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
        for rec in records:
            by_year[_year_from_ts(rec["time"])].append(rec)
        return dict(by_year)

    @staticmethod
    def _group_by_symbol_tf_year(
        records: List[Dict[str, Any]],
    ) -> Dict[Tuple[str, str, int], List[Dict[str, Any]]]:
        groups: Dict[Tuple[str, str, int], List[Dict[str, Any]]] = defaultdict(list)
        for rec in records:
            groups[(rec["symbol"], rec.get("timeframe", "15m"), _year_from_ts(rec["time"]))].append(rec)
        return dict(groups)

    @staticmethod
    def _conflict_clause(on_conflict: str) -> str:
        if on_conflict == "update":
            return """
                ON CONFLICT (symbol, time) DO UPDATE SET
                    open       = EXCLUDED.open,
                    high       = EXCLUDED.high,
                    low        = EXCLUDED.low,
                    close      = EXCLUDED.close,
                    volume     = EXCLUDED.volume
            """
        elif on_conflict == "skip":
            return "ON CONFLICT (symbol, time) DO NOTHING"
        elif on_conflict == "error":
            return ""
        else:
            raise ValueError(f"未知冲突策略: {on_conflict}")


# ---------------------------------------------------------------------------
# 便捷全局实例
# ---------------------------------------------------------------------------

_manager: Optional[MarketDBManager] = None
_writer: Optional[MarketKlineWriter] = None


def get_market_db_manager() -> MarketDBManager:
    """
    获取全局 MarketDBManager 实例。

    连接信息从 DATABASE_URL 解析，strategy_db 名从 STRATEGY_DB_NAME 或 URL 推导。
    """
    global _manager
    if _manager is None:
        _manager = MarketDBManager()
    return _manager


def get_market_kline_writer() -> MarketKlineWriter:
    global _writer
    if _writer is None:
        _writer = MarketKlineWriter(get_market_db_manager())
    return _writer
