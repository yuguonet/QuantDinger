"""炸板池接口 - 获取当日炸板(开板)股票数据"""
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime

from app.data_sources.base import BaseDataSource
from .cache_file import cache_db

logger = logging.getLogger(__name__)


class BrokenBoardInterface:
    """炸板池接口

    获取当日炸板(开板)股票数据：代码、名称、涨停时间、开板时间等。
    缓存由 AStockDataSource._info_cache (TTL=60s) 负责。
    """

    TABLE = "cnd_zt_pool_zbgc"

    def __init__(self, sources: List[BaseDataSource], db: cache_db):
        self.sources = sources
        self.db = db

    def get_realtime(self, date: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取实时炸板池数据

        Args:
            date: 交易日期 YYYY-MM-DD，默认当天

        Returns:
            炸板池数据列表
        """
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        for source in self.sources:
            if not source.enabled:
                continue
            try:
                data = source.get_broken_board(date)
                if data:
                    self.db.insert_batch(self.TABLE, data)
                    logger.info(f"从 [{source.name}] 获取 {date} 炸板池 {len(data)} 条")
                    return data
            except Exception as e:
                logger.warning(f"[{source.name}] get_broken_board 失败: {e}")
                continue
        return []

    def get_count(self, date: Optional[str] = None) -> int:
        """获取炸板家数"""
        return len(self.get_realtime(date))
