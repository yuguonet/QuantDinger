"""涨停板接口 - 获取当日涨停股票池数据"""
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime

from app.data_sources.base import BaseDataSource
from .cache_file import cache_db

logger = logging.getLogger(__name__)


class ZTPoolInterface:
    """涨停板接口

    获取当日涨停股票池数据：代码、名称、涨停价、封板资金、连板天数等。
    缓存由 AStockDataSource._info_cache (TTL=60s) 负责，此处不再重复缓存。
    历史数据存储在 cache_db (Feather) 中。
    """

    TABLE = "cnd_zt_pool"

    def __init__(self, sources: List[BaseDataSource], db: cache_db):
        self.sources = sources
        self.db = db

    def get_realtime(self, date: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取实时涨停池数据

        缓存由数据源层 (AStockDataSource._info_cache) 负责，此处直接调数据源。

        Args:
            date: 交易日期 YYYY-MM-DD，默认当天

        Returns:
            涨停池数据列表
        """
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        for source in self.sources:
            if not source.enabled:
                continue
            try:
                data = source.get_zt_pool(date)
                if data:
                    self.db.insert_batch(self.TABLE, data)
                    logger.info(f"从 [{source.name}] 获取 {date} 涨停池数据 {len(data)} 条")
                    return data
            except Exception as e:
                logger.warning(f"[{source.name}] get_zt_pool 失败: {e}")
                continue

        logger.error("所有数据源获取涨停池均失败")
        return []

    # 最大回溯天数限制（防止逐天请求导致性能灾难）
    MAX_HISTORY_DAYS = 30

    def get_history(self, start_date: str, end_date: str) -> List[Dict[str, Any]]:
        """获取历史涨停池数据

        优先从数据库查询，仅补拉缺失日期（最多 MAX_HISTORY_DAYS 天）。

        Args:
            start_date: 开始日期 YYYY-MM-DD
            end_date: 结束日期 YYYY-MM-DD

        Returns:
            历史涨停池数据
        """
        from datetime import timedelta

        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")

        # 天数限制
        total_days = (end - start).days + 1
        if total_days > self.MAX_HISTORY_DAYS:
            logger.warning(f"请求 {total_days} 天超过上限 {self.MAX_HISTORY_DAYS}，已截断")
            start = end - timedelta(days=self.MAX_HISTORY_DAYS - 1)
            start_date = start.strftime("%Y-%m-%d")

        # 检查数据库中已有哪些日期
        existing_dates = self.db.query_dates_exist(self.TABLE, "trade_date", start_date, end_date)
        existing_set = set(existing_dates)

        # 只拉缺失的交易日（跳过周末）
        current = start
        missing_dates = []
        while current <= end:
            d = current.strftime("%Y-%m-%d")
            if d not in existing_set and current.weekday() < 5:
                missing_dates.append(d)
            current += timedelta(days=1)

        # 补拉缺失日期
        for d in missing_dates:
            logger.info(f"拉取 {d} 涨停池数据")
            self.get_realtime(d)

        # 从数据库返回指定范围
        return self.db.query_between_dates(
            self.TABLE, "trade_date", start_date, end_date,
            order_by="trade_date DESC"
        )

    def get_by_sector(self, sector: str, trade_date: Optional[str] = None) -> List[Dict[str, Any]]:
        """按板块筛选涨停股"""
        data = self.get_realtime(trade_date)
        return [item for item in data if sector in str(item.get("sector", ""))]

    def get_continuous_zt(self, min_days: int = 2, trade_date: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取连板股票"""
        data = self.get_realtime(trade_date)
        return [
            item for item in data
            if (item.get("continuous_zt_days") or 0) >= min_days
        ]
