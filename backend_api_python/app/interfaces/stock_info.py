"""个股信息接口 - 获取股票基本信息"""
import logging
from typing import List, Dict, Any, Optional

from app.data_sources.base import BaseDataSource
from .cache_file import cache_db

logger = logging.getLogger(__name__)


class StockInfoInterface:
    """个股信息接口

    获取股票基本信息：名称、行业、市值等。
    缓存由 AStockDataSource._info_cache (TTL=3600s) 负责。
    """

    def __init__(self, sources: List[BaseDataSource], db: cache_db):
        self.sources = sources
        self.db = db

    def get_info(self, stock_code: str) -> Optional[Dict[str, Any]]:
        """获取个股基本信息

        缓存由数据源层 (AStockDataSource._info_cache) 负责。

        Args:
            stock_code: 股票代码

        Returns:
            个股信息字典
        """
        for source in self.sources:
            if not source.enabled:
                continue
            try:
                info = source.get_stock_info(stock_code)
                if info:
                    return info
            except Exception as e:
                logger.warning(f"[{source.name}] get_stock_info({stock_code}) 失败: {e}")
                continue
        return None

    def get_batch(self, codes: List[str]) -> Dict[str, Dict[str, Any]]:
        """批量获取个股信息

        Args:
            codes: 股票代码列表

        Returns:
            {stock_code: info_dict} 映射
        """
        result = {}
        for code in codes:
            info = self.get_info(code)
            if info:
                result[code] = info
        return result
