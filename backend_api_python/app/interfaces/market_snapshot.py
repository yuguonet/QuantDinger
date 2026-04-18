"""市场快照接口 - 获取全市场涨跌统计"""
import logging
from typing import Dict, Any, List

from app.data_sources.base import BaseDataSource
from .cache_file import cache_db

logger = logging.getLogger(__name__)


class MarketSnapshotInterface:
    """市场快照接口

    获取全市场涨跌统计：上涨家数、下跌家数、涨停、跌停、情绪指标等。
    缓存由 AStockDataSource._info_cache (TTL=120s) 负责。
    """

    def __init__(self, sources: List[BaseDataSource], db: cache_db):
        self.sources = sources
        self.db = db

    def get_realtime(self) -> Dict[str, Any]:
        """获取实时市场快照

        缓存由数据源层 (AStockDataSource._info_cache) 负责。

        Returns:
            市场快照字典: {
                "up_count": int, "down_count": int, "flat_count": int,
                "limit_up": int, "limit_down": int,
                "total_amount": float, "emotion": int, "north_net_flow": float
            }
        """
        for source in self.sources:
            if not source.enabled:
                continue
            try:
                data = source.get_market_snapshot()
                if data and (data.get("up_count", 0) + data.get("down_count", 0)) > 0:
                    logger.info(f"从 [{source.name}] 获取市场快照成功")
                    return data
            except Exception as e:
                logger.warning(f"[{source.name}] get_market_snapshot 失败: {e}")
                continue

        logger.error("所有数据源获取市场快照均失败")
        return {
            "up_count": 0, "down_count": 0, "flat_count": 0,
            "limit_up": 0, "limit_down": 0, "total_amount": 0.0,
            "emotion": 50, "north_net_flow": 0.0,
        }
