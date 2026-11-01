# -*- coding: utf-8 -*-
"""
涨跌停 / 炸板三池 —— 本地持久化层（CNStock_db / cnd_limit_pool_list）

═══════════════════════════════════════════════════════════════
  定位
  ────
  本模块**只做本地库读写**（DDL / 查询 / UPSERT），**不访问任何远端源**。
  远端取数统一走 `coord_pool.coordinate(kind, trade_date=...)`，由
  `provider_pool/akshare.py` 持有 —— 与 `dragon_tiger_local.py` 同一分层范式。

  为什么要这张表（2026-10-31 用户裁定）
  ────────────────────────────────────
  三池是**按日恒定**的榜单：某交易日的涨停池收盘后就再不变化，却每次分析都
  重新走一遍 akshare（单池 2.9~5.2s，三池 ~12s，加近 8 日回溯更贵）。落库后
  历史日命中即零外网。库表是**唯一基准**：字段名/值与 `provider_pool` 归一后
  的行逐一对应，不做二次改名或换算。

  表结构
  ──────
  cnd_limit_pool_list —— UNIQUE(kind, trade_date, stock_code)
    · kind ∈ limit_up_pool | limit_down_pool | broken_board_pool
      （直接复用 coord_pool 的 kind 名，不做二次映射；跨层同名便于对账）
    · 跌停/炸板没有的列（连板数、封单资金、炸板次数等）一律 NULL，
      **不回落 0** —— 0 是「开板 0 次」这种有意义的取值，语义不可混。
    · trade_date 用 VARCHAR(10)（与 cnd_dragon_tiger_list 一致，便于按日 join/对账）。

  易错点
  ──────
  · 空池也可能落库：非交易日的空是**确定事实**（不该反复回源），交易日为空的
    可能是源故障 —— 该判断属于调用方（cn_stock_ext），本层只忠实写入。
  · 该表是**历史快照**：当日盘中池仍在变，调用方不得把盘中数据写进来当权威。
═══════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)

TABLE = "cnd_limit_pool_list"

#: 与 coord_pool.POOL_CAPS 的 kind 同名，不做二次映射
KINDS = ("limit_up_pool", "limit_down_pool", "broken_board_pool")

#: 落库列（除 kind/trade_date/stock_code 三个主键列）
VALUE_COLUMNS = (
    "stock_name", "price", "change_percent", "continuous_zt_days", "zt_time",
    "break_time", "seal_amount", "turnover_rate", "volume", "amount",
    "sector", "reason", "open_count",
)

_INT_COLUMNS = frozenset({"continuous_zt_days", "open_count"})

_ENTRY_COLUMNS = ("kind", "trade_date", "stock_code") + VALUE_COLUMNS

_FLOAT_COLUMNS = frozenset(VALUE_COLUMNS) - _INT_COLUMNS - {"stock_name", "zt_time",
                                                            "break_time", "sector", "reason"}

_CREATE = """
CREATE TABLE IF NOT EXISTS cnd_limit_pool_list (
    kind               VARCHAR(20) NOT NULL,
    trade_date         VARCHAR(10) NOT NULL,
    stock_code         VARCHAR(10) NOT NULL,
    stock_name         VARCHAR(50) DEFAULT '',
    price              DOUBLE PRECISION,
    change_percent     DOUBLE PRECISION,
    continuous_zt_days INTEGER,
    zt_time            VARCHAR(16) DEFAULT '',
    break_time         VARCHAR(16) DEFAULT '',
    seal_amount        DOUBLE PRECISION,
    turnover_rate      DOUBLE PRECISION,
    volume             DOUBLE PRECISION,
    amount             DOUBLE PRECISION,
    sector             VARCHAR(50) DEFAULT '',
    reason             VARCHAR(200) DEFAULT '',
    open_count         INTEGER,
    UNIQUE(kind, trade_date, stock_code)
)
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_lp_kind_date ON cnd_limit_pool_list(kind, trade_date)",
    "CREATE INDEX IF NOT EXISTS idx_lp_code_date ON cnd_limit_pool_list(stock_code, trade_date)",
)

_INSERT_UPDATE = ",\n    ".join(
    "%s = EXCLUDED.%s" % (c, c) for c in VALUE_COLUMNS)

# 占位符作为**参数值**参与格式化，不会再被降级 ⇒ 这里写 "%s" 而非 "%%s"
_INSERT_SQL = """
INSERT INTO cnd_limit_pool_list (%s)
VALUES (%s)
ON CONFLICT (kind, trade_date, stock_code) DO UPDATE SET
    %s
""" % (", ".join(_ENTRY_COLUMNS), ", ".join(["%s"] * len(_ENTRY_COLUMNS)), _INSERT_UPDATE)

_SELECT_SQL = """
SELECT %s FROM cnd_limit_pool_list
WHERE kind = %%s AND trade_date = %%s
ORDER BY COALESCE(seal_amount, 0) DESC, stock_code
""" % ", ".join(_ENTRY_COLUMNS)

_tables_ensured = False


def get_pool():
    """CNStock_db 连接池（与 dragon_tiger_local 同一入口）。"""
    from app.utils.db_market import get_market_db_manager
    mgr = get_market_db_manager()
    return mgr._get_pool("CNStock")


def ensure_table(conn) -> None:
    """建表 + 索引（幂等，进程内只执行一次）。"""
    global _tables_ensured
    if _tables_ensured:
        return
    cur = conn.cursor()
    try:
        cur.execute(_CREATE)
        for sql in _INDEXES:
            cur.execute(sql)
        conn.commit()
        _tables_ensured = True
        logger.info("[limit_pool_local] 建表完成: %s", TABLE)
    except Exception as e:
        conn.rollback()
        logger.warning("[limit_pool_local] 建表失败: %s", e)
    finally:
        cur.close()


def _opt_int(v) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _opt_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _opt_str(v, width: int) -> str:
    return (str(v).strip() if v is not None else "")[:width]


def _entry(kind: str, trade_date: str, row: Dict[str, Any]) -> tuple:
    """provider 归一后的行 → 库列元组。缺列写 NULL（不回落 0）。"""
    out: List[Any] = [kind, trade_date, str(row.get("stock_code") or "").strip()]
    for col in VALUE_COLUMNS:
        v = row.get(col)
        if col in _INT_COLUMNS:
            out.append(_opt_int(v))
        elif col in _FLOAT_COLUMNS:
            out.append(_opt_float(v))
        elif col == "reason":
            out.append(_opt_str(v, 200))
        elif col == "sector":
            out.append(_opt_str(v, 50))
        else:
            out.append(_opt_str(v, 16))
    return tuple(out)


def _write(conn, entries: List[tuple]) -> int:
    """一批已构造好的列元组写库（调用方负责连接生命周期）。"""
    try:
        ensure_table(conn)
        cur = conn.cursor()
        try:
            cur.executemany(_INSERT_SQL, entries)
        finally:
            cur.close()
        conn.commit()
        return len(entries)
    except Exception as e:
        conn.rollback()
        logger.error("[limit_pool_local] 写入失败(%d 条): %s", len(entries), e)
        return 0


def save_rows(kind: str, trade_date: str, rows: List[Dict[str, Any]],
              conn=None) -> int:
    """UPSERT 一批池行，返回写入条数；空行返回 0。

    conn 由调用方给（复用同一事务）时用它的连接；conn=None 时自取连接池。
    空池（rows=[]）不写 —— 「该日无票」由调用方决定是否单独登记，本层不臆造行。
    """
    data = [r for r in (rows or []) if r and str(r.get("stock_code") or "").strip()]
    if not data:
        return 0
    entries = [_entry(kind, trade_date, r) for r in data]
    if conn is not None:
        return _write(conn, entries)
    pool = get_pool()
    with pool.connection() as c:
        return _write(c, entries)


def query_pool(kind: str, trade_date: str) -> List[Dict[str, Any]]:
    """按 (kind, trade_date) 读库；无数据返回 []（不抛 —— 调用方据此判「库缺」）。"""
    if not kind or not trade_date:
        return []
    try:
        pool = get_pool()
        with pool.connection() as conn:
            ensure_table(conn)
            cur = conn.cursor()
            try:
                cur.execute(_SELECT_SQL, (kind, trade_date))
                cols = [d[0] for d in cur.description]
                rows = cur.fetchall()
            finally:
                cur.close()
        return [dict(zip(cols, r)) for r in rows]
    except Exception as e:
        logger.warning("[limit_pool_local] %s %s 读取失败: %s", kind, trade_date, e)
        return []


def has_date(kind: str, trade_date: str) -> bool:
    """该 (kind, 日期) 是否已落库（有行才算 —— 空池不算已同步）。"""
    return bool(query_pool(kind, trade_date))
