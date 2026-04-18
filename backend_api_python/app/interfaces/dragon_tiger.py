"""龙虎榜接口 - 获取每日龙虎榜数据（PostgreSQL 持久存储）"""
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta

from app.data_sources.base import BaseDataSource
from app.utils.db import get_db_connection

logger = logging.getLogger(__name__)


# 首次使用时自动建表（幂等）
_TABLE_INITIALIZED = False


def _ensure_table():
    """确保 cnd_dragon_tiger_list 表存在（CREATE TABLE IF NOT EXISTS）"""
    global _TABLE_INITIALIZED
    if _TABLE_INITIALIZED:
        return
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cnd_dragon_tiger_list (
                    id SERIAL PRIMARY KEY,
                    trade_date VARCHAR(10) NOT NULL,
                    stock_code VARCHAR(10) NOT NULL,
                    stock_name VARCHAR(50) DEFAULT '',
                    reason VARCHAR(200) DEFAULT '',
                    buy_amount DOUBLE PRECISION DEFAULT 0,
                    sell_amount DOUBLE PRECISION DEFAULT 0,
                    net_amount DOUBLE PRECISION DEFAULT 0,
                    change_percent DOUBLE PRECISION DEFAULT 0,
                    close_price DOUBLE PRECISION DEFAULT 0,
                    turnover_rate DOUBLE PRECISION DEFAULT 0,
                    amount DOUBLE PRECISION DEFAULT 0,
                    buy_seat_count INTEGER DEFAULT 0,
                    sell_seat_count INTEGER DEFAULT 0,
                    fetch_time VARCHAR(20) DEFAULT '',
                    created_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(trade_date, stock_code, reason)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_dt_trade_date ON cnd_dragon_tiger_list(trade_date)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_dt_stock_code ON cnd_dragon_tiger_list(stock_code)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_dt_date_code ON cnd_dragon_tiger_list(trade_date, stock_code)")
            conn.commit()
            cur.close()
        _TABLE_INITIALIZED = True
        logger.info("cnd_dragon_tiger_list 表已就绪")
    except Exception as e:
        logger.error(f"初始化龙虎榜表失败: {e}")


class DragonTigerInterface:
    """龙虎榜接口

    获取每日龙虎榜数据：股票代码、名称、买卖金额、净买入额、上榜原因等。
    历史数据存储在 PostgreSQL 表 cnd_dragon_tiger_list 中，不删除历史数据。
    """

    TABLE = "cnd_dragon_tiger_list"

    def __init__(self, sources: List[BaseDataSource], db=None):
        """
        Args:
            sources: 数据源列表（多源 fallback）
            db: 保留参数（兼容 AShareDataHub 调用），此实现使用 PostgreSQL 不依赖 cache_db
        """
        self.sources = sources
        _ensure_table()

    # ─── PostgreSQL 查询 ───────────────────────────────────

    def _query_between_dates(self, start_date: str, end_date: str) -> List[Dict[str, Any]]:
        """从 PostgreSQL 查询日期范围内的龙虎榜数据"""
        try:
            with get_db_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    f"""SELECT trade_date, stock_code, stock_name, reason,
                               buy_amount, sell_amount, net_amount, change_percent,
                               close_price, turnover_rate, amount,
                               buy_seat_count, sell_seat_count
                        FROM {self.TABLE}
                        WHERE trade_date BETWEEN %s AND %s
                        ORDER BY trade_date DESC, net_amount DESC""",
                    (start_date, end_date),
                )
                rows = cur.fetchall() or []
                cur.close()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"PostgreSQL 查询龙虎榜失败: {e}")
            return []

    def _existing_dates(self, start_date: str, end_date: str) -> set:
        """查询已存在的交易日"""
        try:
            with get_db_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    f"""SELECT DISTINCT trade_date FROM {self.TABLE}
                        WHERE trade_date BETWEEN %s AND %s""",
                    (start_date, end_date),
                )
                rows = cur.fetchall() or []
                cur.close()
            return {r["trade_date"] for r in rows}
        except Exception as e:
            logger.error(f"PostgreSQL 查询龙虎榜日期失败: {e}")
            return set()

    def _insert_batch(self, data: List[Dict[str, Any]]) -> int:
        """批量写入 PostgreSQL（INSERT ON CONFLICT 跳过重复，不删除历史）"""
        if not data:
            return 0
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sql = f"""
            INSERT INTO {self.TABLE}
                (trade_date, stock_code, stock_name, reason,
                 buy_amount, sell_amount, net_amount, change_percent,
                 close_price, turnover_rate, amount,
                 buy_seat_count, sell_seat_count, fetch_time)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (trade_date, stock_code, reason) DO NOTHING
        """
        try:
            with get_db_connection() as conn:
                cur = conn.cursor()
                rows = [
                    (
                        str(item.get("trade_date", "")),
                        str(item.get("stock_code", "")),
                        str(item.get("stock_name", "")),
                        str(item.get("reason", ""))[:200],
                        float(item.get("buy_amount", 0) or 0),
                        float(item.get("sell_amount", 0) or 0),
                        float(item.get("net_amount", 0) or 0),
                        float(item.get("change_percent", 0) or 0),
                        float(item.get("close_price", 0) or 0),
                        float(item.get("turnover_rate", 0) or 0),
                        float(item.get("amount", 0) or 0),
                        int(item.get("buy_seat_count", 0) or 0),
                        int(item.get("sell_seat_count", 0) or 0),
                        now,
                    )
                    for item in data
                ]
                cur.executemany(sql, rows)
                conn.commit()
                cur.close()
            logger.debug(f"PostgreSQL 写入龙虎榜 {len(data)} 条")
            return len(data)
        except Exception as e:
            logger.error(f"PostgreSQL 写入龙虎榜失败: {e}")
            return 0

    # ─── 公开 API ─────────────────────────────────────────

    def get_history(self, start_date: str, end_date: str,
                    force_refresh: bool = False) -> List[Dict[str, Any]]:
        """获取龙虎榜历史数据

        优先从 PostgreSQL 查询，数据不完整时自动补拉缺失日期。
        历史数据永久保留，不删除。

        Args:
            start_date: 开始日期 YYYY-MM-DD
            end_date: 结束日期 YYYY-MM-DD
            force_refresh: 强制刷新，忽略数据库缓存

        Returns:
            龙虎榜数据列表
        """
        if not force_refresh:
            # 检查数据库中已有哪些日期
            existing_set = self._existing_dates(start_date, end_date)

            # 找出缺失的交易日（跳过周末）
            start = datetime.strptime(start_date, "%Y-%m-%d")
            end = datetime.strptime(end_date, "%Y-%m-%d")
            missing_dates = []
            current = start
            while current <= end:
                d = current.strftime("%Y-%m-%d")
                if d not in existing_set and current.weekday() < 5:
                    missing_dates.append(d)
                current += timedelta(days=1)

            # 补拉缺失日期
            if missing_dates:
                logger.info(f"龙虎榜缺失 {len(missing_dates)} 个交易日，尝试补拉: {missing_dates[:5]}...")
                for source in self.sources:
                    if not source.enabled:
                        continue
                    try:
                        data = source.get_dragon_tiger(missing_dates[0], missing_dates[-1])
                        if data:
                            self._insert_batch(data)
                            logger.info(f"从 [{source.name}] 补拉龙虎榜 {len(data)} 条")
                            break
                    except Exception as e:
                        logger.warning(f"[{source.name}] 补拉龙虎榜失败: {e}")
                        continue

            # 从 PostgreSQL 返回指定范围
            data = self._query_between_dates(start_date, end_date)
            if data:
                logger.info(f"从 PostgreSQL 获取龙虎榜数据 {len(data)} 条")
                return data

        # force_refresh 或数据库仍无数据时，从数据源全量获取
        for source in self.sources:
            if not source.enabled:
                continue
            try:
                data = source.get_dragon_tiger(start_date, end_date)
                if data:
                    self._insert_batch(data)
                    logger.info(f"从 [{source.name}] 获取龙虎榜数据 {len(data)} 条")
                    return data
            except Exception as e:
                logger.warning(f"[{source.name}] get_dragon_tiger 失败: {e}")
                continue

        logger.error("所有数据源获取龙虎榜均失败")
        return []

    def get_by_stock(self, stock_code: str, start_date: str, end_date: str) -> List[Dict[str, Any]]:
        """查询某只股票的龙虎榜历史

        Args:
            stock_code: 股票代码
            start_date: 开始日期
            end_date: 结束日期

        Returns:
            该股票的龙虎榜记录
        """
        # 先确保数据已拉取
        self.get_history(start_date, end_date)

        # 从 PostgreSQL 查询
        try:
            with get_db_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    f"""SELECT trade_date, stock_code, stock_name, reason,
                               buy_amount, sell_amount, net_amount, change_percent,
                               close_price, turnover_rate, amount,
                               buy_seat_count, sell_seat_count
                        FROM {self.TABLE}
                        WHERE stock_code = %s AND trade_date BETWEEN %s AND %s
                        ORDER BY trade_date DESC""",
                    (stock_code, start_date, end_date),
                )
                rows = cur.fetchall() or []
                cur.close()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"PostgreSQL 查询股票龙虎榜失败: {e}")
            return []
