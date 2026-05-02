"""
db_multi.py — 多市场数据库连接池与 DDL 管理（下层）

职责:
  1. 共享常量与工具函数（KLINE_COLUMNS、市场名解析、表名生成）
  2. PostgreSQL 连接池管理（MarketPool，线程安全）
  3. 数据库生命周期管理（MarketDBManager：CREATE/DROP DATABASE）
  4. 表结构初始化（kline 分区表 + 聚合 VIEW）
  5. FDW 桥接（可选）

连接方式:
  - DDL 操作（CREATE/DROP DATABASE）：直连 postgres 系统库，autocommit
  - 数据操作（读写）：ThreadedConnectionPool，每个市场库独立池
  - 管理员连接复用长连接，自动检测断线重连

被 db_market.py（上层）导入，不依赖任何 app 内部模块（除 logger）。
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
# 共享常量
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

# 连接池配置
POOL_MIN = int(os.getenv("MARKET_DB_POOL_MIN", "2"))
POOL_MAX = int(os.getenv("MARKET_DB_POOL_MAX", "10"))

# K线表列定义（time 为 TIMESTAMP 类型，与 CSV 标准格式兼容）
KLINE_COLUMNS = """
    symbol      VARCHAR(20)  NOT NULL,
    time        TIMESTAMP NOT NULL,
    open        FLOAT NOT NULL,
    high        FLOAT NOT NULL,
    low         FLOAT NOT NULL,
    close       FLOAT NOT NULL,
    volume      DOUBLE PRECISION DEFAULT 0,
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


def _is_valid_market(market: str) -> bool:
    """检查市场名是否合法（标准名或别名均可）"""
    m = market.strip()
    if m in KNOWN_MARKETS:
        return True
    if m.lower() in _MARKET_ALIASES:
        return True
    return bool(re.match(r'^[a-zA-Z][a-zA-Z0-9_]*$', m))


def _parse_database_url(url: str) -> dict:
    """解析 DATABASE_URL 为连接参数字典"""
    if not url:
        return {"host": "localhost", "port": 5432, "user": "postgres", "password": ""}
    if url.startswith("postgresql://"):
        url = url[13:]
    elif url.startswith("postgres://"):
        url = url[11:]
    result = {"host": "localhost", "port": 5432, "user": "postgres", "password": ""}
    if "@" in url:
        auth, hostpart = url.rsplit("@", 1)
        if ":" in auth:
            result["user"], result["password"] = auth.split(":", 1)
        else:
            result["user"] = auth
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
        except BaseException:
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
        # 数据库存在性缓存（避免每次都连 postgres 系统库查询）
        self._db_exists_cache: Dict[str, bool] = {}
        # 年份表存在性缓存（避免重复查 information_schema）
        self._year_table_cache: set[str] = set()
        # 管理员连接复用（避免每次都新建连接到 postgres 系统库）
        self._admin_conn_instance = None
        self._admin_conn_lock = threading.Lock()
        # schema 初始化回调（由上层注册，用于创建聚合 VIEW 等业务逻辑）
        self._on_schema_init = None

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
        """关闭所有市场库连接池 + 管理员连接"""
        with self._pool_lock:
            for pool in self._pools.values():
                pool.close()
            self._pools.clear()
        self.close_admin_conn()
        logger.info("所有市场库连接池已关闭")

    def close_admin_conn(self):
        """关闭管理员连接"""
        if self._admin_conn_instance is not None:
            try:
                self._admin_conn_instance.close()
            except Exception:
                pass
            self._admin_conn_instance = None

    # ---- DDL 操作（直接连接，autocommit） ----

    def _admin_conn(self):
        """获取管理员连接（autocommit，复用长连接，线程安全）"""
        with self._admin_conn_lock:
            if self._admin_conn_instance is not None:
                try:
                    cur = self._admin_conn_instance.cursor()
                    cur.execute("SELECT 1")
                    cur.close()
                    return self._admin_conn_instance
                except Exception:
                    try:
                        self._admin_conn_instance.close()
                    except Exception:
                        pass
                    self._admin_conn_instance = None

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
            self._admin_conn_instance = conn
            return conn

    def market_db_exists(self, market: str) -> bool:
        # 使用缓存，避免每次都连 postgres 系统库
        resolved = _resolve_market(market)
        if resolved in self._db_exists_cache:
            return self._db_exists_cache[resolved]
        db_name = _market_db_name(resolved)
        try:
            conn = self._admin_conn()
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
            exists = cur.fetchone() is not None
            cur.close()
            self._db_exists_cache[resolved] = exists
            return exists
        except Exception as e:
            logger.error(f"检查数据库 {db_name} 存在性失败: {e}")
            return False

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
            cur.close()
            self._db_exists_cache[resolved] = True  # 更新缓存
            logger.info(f"✅ 已创建市场数据库: {db_name}")
        except Exception as e:
            logger.error(f"创建数据库 {db_name} 失败: {e}")
            raise

        self._init_market_schema(resolved)
        return True

    def ensure_market_db(self, market: str) -> bool:
        return self.create_market_db(market)

    def drop_market_db(self, market: str) -> bool:
        self.close_pool(market)
        resolved = _resolve_market(market)
        db_name = _market_db_name(resolved)
        conn = self._admin_conn()
        try:
            cur = conn.cursor()
            cur.execute(f"""
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = %s AND pid != pg_backend_pid()
            """, (db_name,))
            cur.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
            cur.close()
            self._db_exists_cache.pop(resolved, None)  # 清除缓存
            logger.warning(f"⚠️ 已删除市场数据库: {db_name}")
            return True
        except Exception as e:
            logger.error(f"删除数据库 {db_name} 失败: {e}")
            raise

    def list_market_dbs(self) -> List[Dict[str, Any]]:
        conn = self._admin_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT datname FROM pg_database
            WHERE datname LIKE '%_db'
              AND datname NOT IN ('template0', 'template1', 'postgres')
            ORDER BY datname
        """)
        db_names = [row[0] for row in cur.fetchall()]
        cur.close()

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
            # 15m 分区表：2 年（通达信 15m 数据只有 2 年）
            for year in [current_year - 1, current_year]:
                self._ensure_kline_table(cur, "15m", year)

            # 1D 分区表：5 年（日线数据需要 5 年）
            for year in range(current_year - 4, current_year + 1):
                self._ensure_kline_table(cur, "1D", year)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS _market_meta (
                    key   VARCHAR(50) PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            cur.execute("""
                INSERT INTO _market_meta (key, value)
                VALUES ('market', %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """, (market,))
            cur.execute("""
                INSERT INTO _market_meta (key, value)
                VALUES ('schema_version', '1')
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """)

        # 回调钩子：由上层（db_market.py）注册聚合 VIEW 等业务逻辑
        if self._on_schema_init:
            self._on_schema_init(self, market)

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

    def ensure_year_table(self, market: str, timeframe: str, year: int):
        table_key = f"{_resolve_market(market)}_{timeframe}_{year}"
        if table_key in self._year_table_cache:
            return
        pool = self._get_pool(market)
        with pool.cursor() as cur:
            self._ensure_kline_table(cur, timeframe, year)
        self._year_table_cache.add(table_key)
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

