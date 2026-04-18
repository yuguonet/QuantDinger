"""热榜/人气榜接口 - 获取实时股票人气排名"""
import copy
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime

from app.data_sources.base import BaseDataSource
from .cache_file import cache_db

logger = logging.getLogger(__name__)


class HotRankInterface:
    """热榜/人气榜接口

    获取实时股票人气排名数据：排名、代码、名称、人气分数、价格、涨跌幅等。
    缓存由 AStockDataSource._info_cache (TTL=300s) 负责。
    历史数据存储在 cache_db (Feather) 表 cnd_hot_rank 中。
    """

    TABLE = "cnd_hot_rank"

    def __init__(self, sources: List[BaseDataSource], db: cache_db):
        self.sources = sources
        self.db = db

    def get_realtime(self) -> List[Dict[str, Any]]:
        """获取实时热榜数据

        缓存由数据源层 (AStockDataSource._info_cache) 负责。

        Returns:
            热榜数据列表，按排名排序
        """
        for source in self.sources:
            if not source.enabled:
                continue
            try:
                data = source.get_hot_rank()
                if data:
                    today = datetime.now().strftime("%Y-%m-%d")
                    db_data = [{**copy.deepcopy(item), "trade_date": today} for item in data]
                    self.db.insert_batch(self.TABLE, db_data)
                    logger.info(f"从 [{source.name}] 获取热榜数据 {len(data)} 条")
                    return data
            except Exception as e:
                logger.warning(f"[{source.name}] get_hot_rank 失败: {e}")
                continue

        logger.error("所有数据源获取热榜均失败")
        return []

    def get_history(self, trade_date: str) -> List[Dict[str, Any]]:
        """获取历史热榜数据"""
        data = self.db.query(
            self.TABLE,
            conditions={"trade_date": trade_date},
            order_by="rank ASC"
        )
        if data:
            return data
        return []

    def get_top_n(self, n: int = 10) -> List[Dict[str, Any]]:
        """获取热榜前N名"""
        return self.get_realtime()[:n]
