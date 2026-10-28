"""
龙虎榜 & 热榜 —— 本地持久化层（CNStock_db / cnd_dragon_tiger_list · cnd_hot_rank_list）

定位
----
本模块**只做本地库读写**（DDL / 查询 / UPSERT），**不访问任何远端数据源**。
远端取数统一走 `coord_pool.coordinate("dragon_tiger" | "hot_rank", ...)`，
由 `provider_pool/` 持有 —— 见 docs/数据源外围收敛改造方案_20261028.md §0 方向红线。

来源
----
2026-10-28 从 `app/market_cn/dragon_tiger_store.py` **整体下沉**（Phase 0 正向化）：
原文件混装了"本地库读写"与"akshare/东财远端拉取"，导致
`app/data_sources/{cn_stock_ext,cn_stock_putdb}.py` 反依赖 `app.market_cn`（下层调上层）。
现按职责拆开：
  · 本地库读写 → 本模块（data_sources 层，最底层）
  · 远端拉取 + 调度入口 save_daily/import_history → 仍在 `market_cn/dragon_tiger_store.py`，
    改为正向 import 本模块；对外 API 由 re-export 保持完全不变。

迁移改动（仅两处，均不影响行为）：
  1. 移除 `query_dragon_tiger` / `query_hot_rank` 内未被使用的
     `from app.utils.db import get_db_connection`（函数内死导入）。
  2. 日志文案前缀保持 `[dragon_tiger_store]` 不变，避免既有日志检索/告警规则失配。

表结构
------
  cnd_dragon_tiger_list  龙虎榜（UNIQUE(trade_date, stock_code, reason)）
  cnd_hot_rank_list      热榜（UNIQUE(trade_date, stock_code)）
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from app.utils.logger import get_logger

logger = get_logger(__name__)

# 建表标记，避免重复执行 DDL
_tables_ensured = False


# ══════════════════════════════════════════════════════════════
#  建表（首次使用时自动执行）
# ══════════════════════════════════════════════════════════════

_CREATE_DRAGON_TIGER = """
CREATE TABLE IF NOT EXISTS cnd_dragon_tiger_list (
    id              SERIAL PRIMARY KEY,
    trade_date      VARCHAR(10) NOT NULL,
    stock_code      VARCHAR(10) NOT NULL,
    stock_name      VARCHAR(50) DEFAULT '',
    reason          VARCHAR(200) DEFAULT '',
    buy_amount      DOUBLE PRECISION DEFAULT 0,
    sell_amount     DOUBLE PRECISION DEFAULT 0,
    net_amount      DOUBLE PRECISION DEFAULT 0,
    change_percent  DOUBLE PRECISION DEFAULT 0,
    close_price     DOUBLE PRECISION DEFAULT 0,
    turnover_rate   DOUBLE PRECISION DEFAULT 0,
    amount          DOUBLE PRECISION DEFAULT 0,
    buy_seat_count  INTEGER DEFAULT 0,
    sell_seat_count INTEGER DEFAULT 0,
    UNIQUE(trade_date, stock_code, reason)
)
"""

_CREATE_HOT_RANK = """
CREATE TABLE IF NOT EXISTS cnd_hot_rank_list (
    id                  SERIAL PRIMARY KEY,
    trade_date          VARCHAR(10) NOT NULL,
    rank                INTEGER DEFAULT 0,
    stock_code          VARCHAR(10) NOT NULL,
    stock_name          VARCHAR(50) DEFAULT '',
    price               DOUBLE PRECISION DEFAULT 0,
    change_percent      DOUBLE PRECISION DEFAULT 0,
    popularity_score    DOUBLE PRECISION DEFAULT 0,
    rank_change         VARCHAR(20) DEFAULT '',
    UNIQUE(trade_date, stock_code)
)
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_dt_trade_date ON cnd_dragon_tiger_list(trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_dt_stock_code ON cnd_dragon_tiger_list(stock_code)",
    "CREATE INDEX IF NOT EXISTS idx_dt_date_code ON cnd_dragon_tiger_list(trade_date, stock_code)",
    "CREATE INDEX IF NOT EXISTS idx_hr_trade_date ON cnd_hot_rank_list(trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_hr_stock_code ON cnd_hot_rank_list(stock_code)",
    "CREATE INDEX IF NOT EXISTS idx_hr_rank ON cnd_hot_rank_list(trade_date, rank)",
]


def _ensure_tables(conn) -> None:
    """建表 + 索引（幂等，进程内只执行一次）"""
    global _tables_ensured
    if _tables_ensured:
        return

    raw_cur = conn.cursor()
    try:
        raw_cur.execute(_CREATE_DRAGON_TIGER)
        raw_cur.execute(_CREATE_HOT_RANK)
        for idx_sql in _CREATE_INDEXES:
            raw_cur.execute(idx_sql)
        conn.commit()
        _tables_ensured = True
        logger.info("[dragon_tiger_store] 建表完成")
    except Exception as e:
        conn.rollback()
        logger.warning("[dragon_tiger_store] 建表失败: %s", e)
    finally:
        raw_cur.close()


# ══════════════════════════════════════════════════════════════
#  数据库写入
# ══════════════════════════════════════════════════════════════

_INSERT_DRAGON_TIGER = """
INSERT INTO cnd_dragon_tiger_list (
    trade_date, stock_code, stock_name, reason,
    buy_amount, sell_amount, net_amount,
    change_percent, close_price, turnover_rate, amount,
    buy_seat_count, sell_seat_count
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (trade_date, stock_code, reason) DO UPDATE SET
    stock_name     = EXCLUDED.stock_name,
    buy_amount     = EXCLUDED.buy_amount,
    sell_amount    = EXCLUDED.sell_amount,
    net_amount     = EXCLUDED.net_amount,
    change_percent = EXCLUDED.change_percent,
    close_price    = EXCLUDED.close_price,
    turnover_rate  = EXCLUDED.turnover_rate,
    amount         = EXCLUDED.amount,
    buy_seat_count = EXCLUDED.buy_seat_count,
    sell_seat_count= EXCLUDED.sell_seat_count
"""

_INSERT_HOT_RANK = """
INSERT INTO cnd_hot_rank_list (
    trade_date, rank, stock_code, stock_name,
    price, change_percent, popularity_score, rank_change
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (trade_date, stock_code) DO UPDATE SET
    rank             = EXCLUDED.rank,
    stock_name       = EXCLUDED.stock_name,
    price            = EXCLUDED.price,
    change_percent   = EXCLUDED.change_percent,
    popularity_score = EXCLUDED.popularity_score,
    rank_change      = EXCLUDED.rank_change
"""


def _save_dragon_tiger(conn, data: List[Dict[str, Any]], trade_date: str) -> int:
    """写入龙虎榜数据，返回写入条数"""
    if not data:
        return 0

    raw_cur = conn.cursor()
    written = 0
    try:
        for row in data:
            stock_code = row.get("stock_code", "")
            if not stock_code:
                continue
            params = (
                row.get("trade_date", trade_date),
                stock_code,
                row.get("stock_name", ""),
                (row.get("reason", "") or "")[:200],
                row.get("buy_amount", 0),
                row.get("sell_amount", 0),
                row.get("net_amount", 0),
                row.get("change_percent", 0),
                row.get("close_price", 0),
                row.get("turnover_rate", 0),
                row.get("amount", 0),
                row.get("buy_seat_count", 0),
                row.get("sell_seat_count", 0),
            )
            raw_cur.execute(_INSERT_DRAGON_TIGER, params)
            written += 1
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error("[dragon_tiger_store] 龙虎榜写入失败: %s", e)
        return 0
    finally:
        raw_cur.close()

    return written


def _save_hot_rank(conn, data: List[Dict[str, Any]], trade_date: str) -> int:
    """写入热榜数据，返回写入条数"""
    if not data:
        return 0

    raw_cur = conn.cursor()
    written = 0
    try:
        for row in data:
            stock_code = row.get("stock_code", "")
            if not stock_code:
                continue
            params = (
                trade_date,
                row.get("rank", 0),
                stock_code,
                row.get("stock_name", ""),
                row.get("price", 0),
                row.get("change_percent", 0),
                row.get("popularity_score", 0),
                row.get("current_rank_change", row.get("rank_change", "")),
            )
            raw_cur.execute(_INSERT_HOT_RANK, params)
            written += 1
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error("[dragon_tiger_store] 热榜写入失败: %s", e)
        return 0
    finally:
        raw_cur.close()

    return written


# ══════════════════════════════════════════════════════════════
#  数据库连接（使用 CNStock_db）
# ══════════════════════════════════════════════════════════════

def _get_cnstock_pool():
    """获取 CNStock_db 连接池"""
    from app.utils.db_market import get_market_db_manager
    mgr = get_market_db_manager()
    return mgr._get_pool("CNStock")


# ══════════════════════════════════════════════════════════════
#  数据库读取（供 Agent 工具和前端卡片使用）
# ══════════════════════════════════════════════════════════════

def query_dragon_tiger(trade_date: str = "", stock_code: str = "", days: int = 30) -> List[Dict[str, Any]]:
    """查询龙虎榜数据

    Args:
        trade_date: 指定日期，空=最近
        stock_code: 指定股票代码，空=全部
        days: 回溯天数（当 trade_date 为空时生效）

    Returns:
        龙虎榜记录列表
    """
    conditions = []
    params: List[Any] = []

    if trade_date:
        conditions.append("trade_date = %s")
        params.append(trade_date)
    elif stock_code:
        pass
    else:
        # 默认最近 N 天
        from datetime import timedelta
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        conditions.append("trade_date >= %s")
        params.append(start)

    if stock_code:
        conditions.append("stock_code = %s")
        params.append(stock_code)

    where = " AND ".join(conditions) if conditions else "1=1"
    sql = f"""
        SELECT trade_date, stock_code, stock_name, reason,
               buy_amount, sell_amount, net_amount,
               change_percent, close_price, turnover_rate, amount,
               buy_seat_count, sell_seat_count
        FROM cnd_dragon_tiger_list
        WHERE {where}
        ORDER BY trade_date DESC, net_amount DESC
        LIMIT 500
    """

    try:
        pool = _get_cnstock_pool()
        with pool.connection() as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
            cur.close()
            return [dict(zip(columns, row)) for row in rows]
    except Exception as e:
        logger.warning("[dragon_tiger_store] 查询龙虎榜失败: %s", e)
        return []


def query_dragon_tiger_latest(limit: int = 500) -> List[Dict[str, Any]]:
    """查询数据库中最后一个有数据交易日的龙虎榜（单日全量快照）

    与 query_dragon_tiger(trade_date="") 的区别: 后者空日期取"最近 N 天混合"
    （days=30, LIMIT 500 会把多日数据混在一页）。市场卡片需要的是"最后一日"
    快照 —— 非交易时段/后端重启后内存缓存未刷新时, 直接展示库里最新一日的
    数据, 展示不依赖当前时间是否落在交易时段。

    Args:
        limit: 单日最多返回条数（与 query_dragon_tiger 一致）

    Returns:
        龙虎榜记录列表（按净买入额 DESC）；库为空时 []。
    """
    sql = """
        SELECT trade_date, stock_code, stock_name, reason,
               buy_amount, sell_amount, net_amount,
               change_percent, close_price, turnover_rate, amount,
               buy_seat_count, sell_seat_count
        FROM cnd_dragon_tiger_list
        WHERE trade_date = (SELECT MAX(trade_date) FROM cnd_dragon_tiger_list)
        ORDER BY net_amount DESC
        LIMIT %s
    """

    try:
        pool = _get_cnstock_pool()
        with pool.connection() as conn:
            cur = conn.cursor()
            cur.execute(sql, (limit,))
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
            cur.close()
            return [dict(zip(columns, row)) for row in rows]
    except Exception as e:
        logger.warning("[dragon_tiger_store] 查询最近交易日龙虎榜失败: %s", e)
        return []


def query_hot_rank(trade_date: str = "", stock_code: str = "") -> List[Dict[str, Any]]:
    """查询热榜数据

    Args:
        trade_date: 指定日期，空=最近交易日
        stock_code: 指定股票代码，空=全部

    Returns:
        热榜记录列表
    """
    conditions = []
    params: List[Any] = []

    if trade_date:
        conditions.append("trade_date = %s")
        params.append(trade_date)

    if stock_code:
        conditions.append("stock_code = %s")
        params.append(stock_code)

    where = " AND ".join(conditions) if conditions else "1=1"
    sql = f"""
        SELECT trade_date, rank, stock_code, stock_name,
               price, change_percent, popularity_score, rank_change
        FROM cnd_hot_rank_list
        WHERE {where}
        ORDER BY trade_date DESC, rank ASC
        LIMIT 200
    """

    try:
        pool = _get_cnstock_pool()
        with pool.connection() as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
            cur.close()
            return [dict(zip(columns, row)) for row in rows]
    except Exception as e:
        logger.warning("[dragon_tiger_store] 查询热榜失败: %s", e)
        return []


# ══════════════════════════════════════════════════════════════
#  内存缓存（仅龙虎榜一项）
# ══════════════════════════════════════════════════════════════
# 2026-10-28 从 app/market_cn/dragon_limit.py 下沉（Phase 0 正向化收尾）：
#   起因 —— cn_stock_putdb 落库后要刷新该缓存，被迫 `from app.market_cn.dragon_limit
#           import load_dragon_tiger_from_db`，属下层反依赖上层。
#   解法 —— 缓存**所有权**下沉到本层；putdb 落库后写缓存、dragon_limit 读缓存，
#          双边都变成「同层 / 正向」访问，反依赖归零。
#   边界 —— 仅下沉龙虎榜一项。zt/dt/broken/hot 四个池的缓存无跨层消费者，
#          保留在 dragon_limit 原地，**不做过度移植**。

_rt_dragon_tiger = None


def get_cached_dragon_tiger():
    """返回内存缓存中的龙虎榜；未加载时 None。"""
    return _rt_dragon_tiger


def set_cached_dragon_tiger(rows) -> None:
    """写入内存缓存（供 market_cn 侧 refresh_dragon_tiger 调用）。"""
    global _rt_dragon_tiger
    _rt_dragon_tiger = rows


def load_dragon_tiger_from_db(trade_date: str = "") -> None:
    """从 CNStock_db 加载龙虎榜数据到内存缓存

    trade_date 为空时: 加载数据库中最后一个有数据交易日的单日全量
    （query_dragon_tiger_latest）—— 不按"最近 N 天"混合, 保证拿到的是完整的一日快照。
    指定日期时: 按日精确查询（save_daily 落库后刷新缓存用）。
    """
    global _rt_dragon_tiger
    try:
        if trade_date:
            data = query_dragon_tiger(trade_date=trade_date)
        else:
            data = query_dragon_tiger_latest()
        if data:
            _rt_dragon_tiger = data
            logger.info("[db] 龙虎榜从DB加载: %d 条", len(data))
    except Exception as e:
        logger.warning("[db] 从DB加载龙虎榜失败: %s", e)
