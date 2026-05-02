"""
db_market.py — 多市场行情数据库管理器（独立连接池，不修改 db.py）

职责:
  1. 创建 / 管理多个市场数据库（{market}_db 命名）
  2. 接口A: 增量写入（单条/小批量，UPSERT）
  3. 接口B: 大批量写入（自动分市场、分年存储）

设计依据:
  - strategy_db（public schema）已有，存储用户策略/信号/回测/持仓
  - market_db 按市场隔离：CNStock_db, USStock_db, Crypto_db ...
  - 每个市场库内按年分区：kline_15m_2021, kline_15m_2022 ...
  - 15分钟K线为唯一原始数据源，日线通过 VIEW 从15分钟聚合
  - 增量写入使用 UPSERT（ON CONFLICT DO UPDATE）
  - 自建连接池，不修改现有 db.py
  - 列名与现有 KlineCacheManager 保持一致：time, open, high, low, close, volume

用法:
  from app.utils.db_market import get_market_kline_writer, get_market_db_manager

  mgr = get_market_db_manager()
  mgr.ensure_market_db("CNStock")
  mgr.ensure_market_db("us")          # 别名，自动映射为 USStock

  writer = get_market_kline_writer()

  # 接口A: 增量写入
  writer.upsert("CNStock", "600519", "15m", [
      {"time": 1714636800, "open": 1650.0, "high": 1660.0,
       "low": 1645.0, "close": 1655.0, "volume": 12345}
  ])

  # 接口B: 大批量写入（自动分市场分年）
  writer.bulk_write("CNStock", [
      {"symbol": "600519", "timeframe": "15m", "time": 1714636800, ...},
      {"symbol": "000001", "timeframe": "15m", "time": 1609459200, ...},
  ])
"""

from __future__ import annotations

import os
import re
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict
from contextlib import contextmanager

from app.utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

# 市场名标准定义（PascalCase，与后端 market.py / init.sql 一致）
KNOWN_MARKETS = {
    "CNStock": "A股",
    "USStock": "美股",
    "HKStock": "港股",
    "Crypto":  "加密货币",
    "Forex":   "外汇",
    "Futures": "期货",
}

# 小写别名映射（方便调用时不用记大小写）
_MARKET_ALIASES = {
    "cn":       "CNStock",
    "us":       "USStock",
    "hk":       "HKStock",
    "crypto":   "Crypto",
    "forex":    "Forex",
    "futures":  "Futures",
    "cstock":   "CNStock",
    "ustock":   "USStock",
    "hkstock":  "HKStock",
    "a股":      "CNStock",
    "美股":     "USStock",
    "港股":     "HKStock",
    "加密":     "Crypto",
    "外汇":     "Forex",
    "期货":     "Futures",
}

TIMEFRAMES = ["1m", "5m", "15m", "30m", "1H", "4H", "1D", "1W"]

# 连接池配置（市场库独立池，不复用 db.py 的 strategy_db 池）
POOL_MIN = int(os.getenv("MARKET_DB_POOL_MIN", "2"))
POOL_MAX = int(os.getenv("MARKET_DB_POOL_MAX", "10"))
POOL_ACQUIRE_TIMEOUT = int(os.getenv("MARKET_DB_POOL_ACQUIRE_TIMEOUT", "10"))

# K线表列定义（与 KlineCacheManager.KLINE_COLUMNS 一致）
KLINE_COLUMNS = """
    time        BIGINT       NOT NULL,
    open        DOUBLE PRECISION NOT NULL,
    high        DOUBLE PRECISION NOT NULL,
    low         DOUBLE PRECISION NOT NULL,
    close       DOUBLE PRECISION NOT NULL,
    volume      DOUBLE PRECISION DEFAULT 0,
    updated_at  TIMESTAMP    DEFAULT NOW(),
    PRIMARY KEY (symbol, time)
"""


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _market_db_name(market: str) -> str:
    """市场名 → 数据库名: CNStock → CNStock_db"""
    return f"{_resolve_market(market)}_db"


def _resolve_market(market: str) -> str:
    """
    统一市场名：支持别名 → 标准 PascalCase。
    cn → CNStock, us → USStock, crypto → Crypto, ...
    已经是标准名的直接返回。
    """
    m = market.strip()
    if m in KNOWN_MARKETS:
        return m
    resolved = _MARKET_ALIASES.get(m.lower())
    if resolved:
        return resolved
    logger.warning(f"未知市场名 '{market}'，使用原值")
    return m


def _table_name(timeframe: str, year: int) -> str:
    return f"kline_{timeframe}_{year}"


def _daily_view_name(timeframe: str) -> str:
    return f"kline_1d_from_{timeframe}"


def _year_from_ts(ts: int) -> int:
    return datetime.fromtimestamp(ts, tz=timezone.utc).year


def _is_valid_market(market: str) -> bool:
    """检查市场名是否合法（标准名或别名均可）"""
    m = market.strip()
    if m in KNOWN_MARKETS:
        return True
    if m.lower() in _MARKET_ALIASES:
        return True
    return bool(re.match(r'^[a-zA-Z][a-zA-Z0-9_]*$', m))


def _parse_database_url(url: str) -> Dict[str, Any]:
    if url.startswith("postgresql://"):
        url = url[13:]
    elif url.startswith("postgres://"):
        url = url[11:]
    result = {}
    if "@" in url:
        auth, hostpart = url.rsplit("@", 1)
        if ":" in auth:
            result["user"], result["password"] = auth.split(":", 1)
        else:
            result["user"] = auth
            result["password"] = ""
    else:
        hostpart = url
    if "/" in hostpart:
        hostport, _ = hostpart.split("/", 1)
    else:
        hostport = hostpart
    if ":" in hostport:
        result["host"], port_str = hostport.split(":", 1)
        result["port"] = int(port_str)
    else:
        result["host"] = hostport
        result["port"] = 5432
    return result


# ---------------------------------------------------------------------------
# MarketPool — 单个市场库的连接池
# ---------------------------------------------------------------------------

class MarketPool:
    """单个市场数据库的线程安全连接池"""

    def __init__(self, db_name: str, params: Dict[str, Any],
                 minconn: int = POOL_MIN, maxconn: int = POOL_MAX):
        import psycopg2
        from psycopg2 import pool as pg_pool

        self._db_name = db_name
        self._pool = pg_pool.ThreadedConnectionPool(
            minconn=minconn,
            maxconn=maxconn,
            host=params["host"],
            port=params["port"],
            user=params["user"],
            password=params["password"],
            dbname=db_name,
            connect_timeout=10,
            options="-c timezone=UTC",
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=3,
        )
        logger.debug(f"连接池已创建: {db_name} (min={minconn}, max={maxconn})")

    @contextmanager
    def connection(self):
        """获取连接（上下文管理器，自动归还）"""
        conn = self._pool.getconn()
        broken = False
        try:
            yield conn
        except Exception:
            conn.rollback()
            if getattr(conn, "closed", 0):
                broken = True
            raise
        finally:
            self._pool.putconn(conn, close=broken)

    @contextmanager
    def cursor(self):
        """获取游标（上下文管理器，自动 commit/rollback）"""
        with self.connection() as conn:
            cur = conn.cursor()
            try:
                yield cur
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                cur.close()

    def close(self):
        """关闭连接池"""
        if self._pool:
            self._pool.closeall()
            logger.debug(f"连接池已关闭: {self._db_name}")


# ---------------------------------------------------------------------------
# MarketDBManager — 数据库生命周期管理
# ---------------------------------------------------------------------------

class MarketDBManager:
    """
    多市场数据库管理器。

    - DDL 操作（CREATE/DROP DATABASE）：直接连接（autocommit，连 postgres 系统库）
    - 数据操作（读写）：自建连接池（每个市场库独立池）
    - FDW 桥接：需要知道 strategy_db 的真实库名

    连接解析逻辑：
      1. 从 DATABASE_URL 提取 host/port/user/password（连接凭据）
      2. 从 STRATEGY_DB_NAME 或 DATABASE_URL 提取 strategy_db 库名
      3. DDL 操作连 postgres 系统库（autocommit）
      4. 市场库连接池连 {market}_db
    """

    def __init__(self, base_conn_url: str = None, strategy_db_name: str = None):
        self._base_conn_url = base_conn_url or os.getenv("DATABASE_URL", "")
        self._params = _parse_database_url(self._base_conn_url)

        # strategy_db 真实库名：优先参数 > 环境变量 > URL 解析
        self._strategy_db_name = (
            strategy_db_name
            or os.getenv("STRATEGY_DB_NAME", "")
            or self._params.get("dbname", "")
        )

        # 连接凭据（host/port/user/password），不含 dbname
        self._conn_params = {
            "host": self._params["host"],
            "port": self._params["port"],
            "user": self._params["user"],
            "password": self._params["password"],
        }

        self._pools: Dict[str, MarketPool] = {}
        self._pool_lock = threading.Lock()

        logger.info(
            f"MarketDBManager 初始化: "
            f"host={self._conn_params['host']}:{self._conn_params['port']} "
            f"strategy_db={self._strategy_db_name}"
        )

    # ---- 连接池管理 ----

    def _get_pool(self, market: str) -> MarketPool:
        """获取指定市场库的连接池（不存在则创建）"""
        resolved = _resolve_market(market)
        if resolved in self._pools:
            return self._pools[resolved]

        with self._pool_lock:
            if resolved in self._pools:
                return self._pools[resolved]

            db_name = _market_db_name(resolved)
            pool = MarketPool(db_name, self._conn_params)
            self._pools[resolved] = pool
            return pool

    def close_pool(self, market: str):
        """关闭指定市场库的连接池"""
        resolved = _resolve_market(market)
        with self._pool_lock:
            pool = self._pools.pop(resolved, None)
            if pool:
                pool.close()

    def close_all_pools(self):
        """关闭所有市场库连接池"""
        with self._pool_lock:
            for pool in self._pools.values():
                pool.close()
            self._pools.clear()
            logger.info("所有市场库连接池已关闭")

    # ---- DDL 操作（直接连接，autocommit） ----

    def _admin_conn(self):
        """获取管理员连接（autocommit，用于 CREATE DATABASE）"""
        import psycopg2
        conn = psycopg2.connect(
            host=self._conn_params["host"],
            port=self._conn_params["port"],
            user=self._conn_params["user"],
            password=self._conn_params["password"],
            dbname="postgres",
            connect_timeout=10,
        )
        conn.autocommit = True
        return conn

    def market_db_exists(self, market: str) -> bool:
        db_name = _market_db_name(market)
        conn = self._admin_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
            return cur.fetchone() is not None
        finally:
            conn.close()

    def create_market_db(self, market: str) -> bool:
        resolved = _resolve_market(market)
        if not _is_valid_market(resolved):
            raise ValueError(f"非法市场名: {market}")

        db_name = _market_db_name(resolved)

        if self.market_db_exists(market):
            logger.info(f"市场数据库已存在: {db_name}")
            return True

        conn = self._admin_conn()
        try:
            cur = conn.cursor()
            cur.execute(f'CREATE DATABASE "{db_name}" TEMPLATE template0')
            logger.info(f"✅ 已创建市场数据库: {db_name}")
        except Exception as e:
            logger.error(f"创建数据库 {db_name} 失败: {e}")
            raise
        finally:
            conn.close()

        self._init_market_schema(resolved)
        return True

    def ensure_market_db(self, market: str) -> bool:
        return self.create_market_db(market)

    def drop_market_db(self, market: str) -> bool:
        self.close_pool(market)
        db_name = _market_db_name(market)
        conn = self._admin_conn()
        try:
            cur = conn.cursor()
            cur.execute(f"""
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = %s AND pid != pg_backend_pid()
            """, (db_name,))
            cur.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
            logger.warning(f"⚠️ 已删除市场数据库: {db_name}")
            return True
        finally:
            conn.close()

    def list_market_dbs(self) -> List[Dict[str, Any]]:
        conn = self._admin_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT datname FROM pg_database
                WHERE datname LIKE '%_db'
                  AND datname NOT IN ('template0', 'template1', 'postgres')
                ORDER BY datname
            """)
            db_names = [row[0] for row in cur.fetchall()]
        finally:
            conn.close()

        result = []
        for db_name in db_names:
            market = db_name.replace("_db", "")
            info = {
                "market": market, "db_name": db_name, "exists": True,
                "tables": [], "views": [], "total_rows": 0,
            }
            try:
                pool = self._get_pool(market)
                with pool.cursor() as cur:
                    cur.execute("""
                        SELECT table_name FROM information_schema.tables
                        WHERE table_schema = 'public'
                          AND table_name LIKE 'kline_%'
                          AND table_name NOT LIKE 'kline_1d_%'
                        ORDER BY table_name
                    """)
                    info["tables"] = [r[0] for r in cur.fetchall()]

                    cur.execute("""
                        SELECT table_name FROM information_schema.views
                        WHERE table_schema = 'public'
                        ORDER BY table_name
                    """)
                    info["views"] = [r[0] for r in cur.fetchall()]

                    total = 0
                    for tbl in info["tables"]:
                        try:
                            cur.execute(f'SELECT COUNT(*) FROM "{tbl}"')
                            total += cur.fetchone()[0]
                        except Exception:
                            pass
                    info["total_rows"] = total
            except Exception as e:
                info["error"] = str(e)
            result.append(info)

        return result

    # ---- 表结构初始化（通过连接池） ----

    def _init_market_schema(self, market: str):
        current_year = datetime.now().year
        pool = self._get_pool(market)

        with pool.cursor() as cur:
            for year in [current_year - 1, current_year]:
                self._ensure_kline_table(cur, "15m", year)
            self._ensure_daily_view(cur, "15m")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS _market_meta (
                    key   VARCHAR(50) PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                INSERT INTO _market_meta (key, value)
                VALUES ('market', %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
            """, (market,))
            cur.execute("""
                INSERT INTO _market_meta (key, value)
                VALUES ('schema_version', '1')
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
            """)

        logger.info(f"✅ 已初始化 {market}_db 表结构（{current_year - 1}-{current_year}）")

    @staticmethod
    def _ensure_kline_table(cursor, timeframe: str, year: int):
        table = _table_name(timeframe, year)
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS "{table}" (
                {KLINE_COLUMNS}
            )
        """)
        cursor.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_{table}_symbol_time
            ON "{table}" (symbol, time)
        """)
        cursor.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_{table}_time
            ON "{table}" (time)
        """)

    @staticmethod
    def _ensure_daily_view(cursor, source_timeframe: str):
        view_name = _daily_view_name(source_timeframe)
        cursor.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name LIKE 'kline_%s_%%'
              AND table_name NOT LIKE 'kline_1d_%%'
            ORDER BY table_name
        """ % source_timeframe)
        tables = [r[0] for r in cursor.fetchall()]

        if not tables:
            logger.warning(f"无法创建日线 VIEW：没有找到 {source_timeframe} 表")
            return

        union_parts = []
        for tbl in tables:
            union_parts.append(f'''
                SELECT
                    symbol,
                    (time / 86400) * 86400 AS day_open_time,
                    (ARRAY_AGG(open  ORDER BY time ASC))[1]  AS open,
                    MAX(high)                                  AS high,
                    MIN(low)                                   AS low,
                    (ARRAY_AGG(close ORDER BY time DESC))[1]   AS close,
                    SUM(volume)                                AS volume
                FROM "{tbl}"
                GROUP BY symbol, time / 86400
            ''')

        query = f"""
            CREATE OR REPLACE VIEW "{view_name}" AS
            {' UNION ALL '.join(union_parts)}
        """
        cursor.execute(query)

    def ensure_year_table(self, market: str, timeframe: str, year: int):
        pool = self._get_pool(market)
        with pool.cursor() as cur:
            self._ensure_kline_table(cur, timeframe, year)
        logger.debug(f"确保表存在: {market}_db.{_table_name(timeframe, year)}")

    # ---- FDW 桥接（可选） ----

    def setup_fdw_from_strategy_db(self, market: str):
        """
        从 strategy_db 建立 postgres_fdw 映射。

        strategy_db 的库名从 STRATEGY_DB_NAME 环境变量或 DATABASE_URL 解析。
        """
        from app.utils.db import get_db_connection

        db_name = _market_db_name(market)
        schema_name = f"fdw_{market}"

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("CREATE EXTENSION IF NOT EXISTS postgres_fdw")

            server_name = f"server_{market}_db"
            cursor.execute(f"""
                CREATE SERVER IF NOT EXISTS {server_name}
                FOREIGN DATA WRAPPER postgres_fdw
                OPTIONS (host %s, port %s, dbname %s)
            """, (self._conn_params["host"], str(self._conn_params["port"]), db_name))

            cursor.execute(f"""
                CREATE USER MAPPING IF NOT EXISTS FOR CURRENT_USER
                SERVER {server_name}
                OPTIONS (user %s, password %s)
            """, (self._conn_params["user"], self._conn_params["password"]))

            cursor.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema_name}"')
            cursor.execute(f"""
                IMPORT FOREIGN SCHEMA public
                FROM SERVER {server_name}
                INTO "{schema_name}"
            """)
            conn.commit()
            logger.info(
                f"✅ FDW 桥接已建立: {self._strategy_db_name}.{schema_name} → {db_name}"
            )


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

    # ================================================================
    # 接口A: 增量写入
    # ================================================================

    def upsert(
        self,
        market: str,
        symbol: str,
        timeframe: str,
        records: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        增量写入 K 线数据（UPSERT）。

        已存在的 (symbol, time) 会被更新，不存在的会插入。
        自动按年分表，跨年的数据会拆分到对应年份表。

        Args:
            market:    市场标识，如 "CNStock", "us", "crypto"
            symbol:    品种代码，如 "600519", "BTC/USDT"
            timeframe: K线周期，如 "15m", "1H", "1D"
            records:   K线数据列表，每条包含:
                       {"time": int, "open": float, "high": float,
                        "low": float, "close": float, "volume": float}

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
                            cur.execute(f"""
                                INSERT INTO "{table}"
                                    (symbol, time, open, high, low, close, volume)
                                VALUES (%s, %s, %s, %s, %s, %s, %s)
                                ON CONFLICT (symbol, time) DO UPDATE SET
                                    open       = EXCLUDED.open,
                                    high       = EXCLUDED.high,
                                    low        = EXCLUDED.low,
                                    close      = EXCLUDED.close,
                                    volume     = EXCLUDED.volume,
                                    updated_at = NOW()
                            """, (
                                symbol, rec["time"], rec["open"], rec["high"],
                                rec["low"], rec["close"], rec.get("volume", 0),
                            ))
                            if cur.rowcount == 1:
                                total_inserted += 1
                            else:
                                total_updated += 1
                        except Exception as e:
                            total_errors += 1
                            logger.warning(f"upsert 失败: {market}/{symbol} t={rec.get('time')}: {e}")

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

        自动按 symbol + timeframe + year 三维分组，批量写入对应表。

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

        with pool.connection() as conn:
            cur = conn.cursor()
            try:
                for (symbol, timeframe, year), group_records in groups.items():
                    table = _table_name(timeframe, year)

                    for batch_start in range(0, len(group_records), batch_size):
                        batch = group_records[batch_start:batch_start + batch_size]
                        values, params = [], []
                        for rec in batch:
                            values.append("(%s, %s, %s, %s, %s, %s, %s)")
                            params.extend([
                                symbol, rec["time"], rec["open"], rec["high"],
                                rec["low"], rec["close"], rec.get("volume", 0),
                            ])

                        conflict_clause = self._conflict_clause(on_conflict)
                        sql = f"""
                            INSERT INTO "{table}"
                                (symbol, time, open, high, low, close, volume)
                            VALUES {', '.join(values)}
                            {conflict_clause}
                        """

                        try:
                            cur.execute(sql, params)
                            affected = cur.rowcount
                            if on_conflict == "skip":
                                total_skipped += affected
                                by_symbol[symbol]["skipped"] += affected
                            else:
                                total_inserted += affected
                                by_symbol[symbol]["inserted"] += affected
                                by_table[table] += affected
                        except Exception as e:
                            logger.warning(
                                f"批量写入失败，回退逐条: {market}/{symbol} "
                                f"{table} batch_size={len(batch)}: {e}"
                            )
                            conn.rollback()
                            single_sql = f"""
                                INSERT INTO "{table}"
                                    (symbol, time, open, high, low, close, volume)
                                VALUES (%s, %s, %s, %s, %s, %s, %s)
                                {conflict_clause}
                            """
                            for rec in batch:
                                try:
                                    cur.execute(single_sql, [
                                        symbol, rec["time"], rec["open"], rec["high"],
                                        rec["low"], rec["close"], rec.get("volume", 0),
                                    ])
                                    if on_conflict == "skip":
                                        total_skipped += cur.rowcount
                                        by_symbol[symbol]["skipped"] += cur.rowcount
                                    else:
                                        total_inserted += cur.rowcount
                                        by_symbol[symbol]["inserted"] += cur.rowcount
                                        by_table[table] += cur.rowcount
                                except Exception as e2:
                                    total_errors += 1
                                    by_symbol[symbol]["errors"] += 1
                                    logger.debug(f"逐条写入失败: {market}/{symbol} t={rec.get('time')}: {e2}")

                conn.commit()
            except Exception:
                conn.rollback()
                raise

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

    # ================================================================
    # 查询辅助
    # ================================================================

    def query(
        self,
        market: str,
        symbol: str,
        timeframe: str,
        start_time: int = None,
        end_time: int = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """
        查询 K 线数据。

        返回格式与 BaseDataSource.format_kline 一致：
        [{"time": int, "open": float, "high": float,
          "low": float, "close": float, "volume": float}, ...]
        """
        if not self._mgr.market_db_exists(market):
            return []

        years = set()
        if start_time:
            years.add(_year_from_ts(start_time))
        if end_time:
            years.add(_year_from_ts(end_time))
        if not years:
            years = {datetime.now().year}

        pool = self._mgr._get_pool(market)
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
                if start_time:
                    conditions.append("time >= %s")
                    params.append(start_time)
                if end_time:
                    conditions.append("time <= %s")
                    params.append(end_time)

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

        return {
            "market": market,
            "db_name": _market_db_name(market),
            "exists": True,
            "tables": tables,
            "symbols": len(all_symbols),
            "symbol_list": sorted(all_symbols),
            "total_rows": total_rows,
            "date_range": {
                "start": datetime.fromtimestamp(min_time, tz=timezone.utc).isoformat() if min_time else None,
                "end": datetime.fromtimestamp(max_time, tz=timezone.utc).isoformat() if max_time else None,
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
                    volume     = EXCLUDED.volume,
                    updated_at = NOW()
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
