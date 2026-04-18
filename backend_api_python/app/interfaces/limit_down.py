"""跌停池接口 - 获取当日跌停股票数据"""
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime

from app.data_sources.base import BaseDataSource
from .cache_file import cache_db

logger = logging.getLogger(__name__)


class LimitDownInterface:
    """跌停池接口

    获取当日跌停股票数据：代码、名称、跌停价、封单量等。
    缓存由 AStockDataSource._info_cache (TTL=60s) 负责。
    """

    TABLE = "cnd_dt_pool"

    def __init__(self, sources: List[BaseDataSource], db: cache_db):
        self.sources = sources
        self.db = db

    def get_realtime(self, date: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取实时跌停池数据

        Args:
            date: 交易日期 YYYY-MM-DD，默认当天

        Returns:
            跌停池数据列表
        """
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        for source in self.sources:
            if not source.enabled:
                continue
            try:
                data = source.get_limit_down(date)
                if data:
                    self.db.insert_batch(self.TABLE, data)
                    logger.info(f"从 [{source.name}] 获取 {date} 跌停池 {len(data)} 条")
                    return data
            except Exception as e:
                logger.warning(f"[{source.name}] get_limit_down 失败: {e}")
                continue
        return []

    def get_count(self, date: Optional[str] = None) -> int:
        """获取跌停家数"""
        return len(self.get_realtime(date))
