"""市场指数接口 - 获取上证、深证等主要指数实时行情"""
import logging
from typing import List, Dict, Any, Optional

from app.data_sources.base import BaseDataSource
from .cache_file import cache_db

logger = logging.getLogger(__name__)

# 默认市场指数代码列表
INDEX_CODES = {
    "000001": "上证指数",
    "399001": "深证成指",
    "399006": "创业板指",
    "899050": "北证50",
    "000688": "科创50",
}

# 默认监控的指数代码
DEFAULT_INDEX_CODES = list(INDEX_CODES.keys())


class IndexInterface:
    """市场指数接口

    获取上证指数(000001)、深证成指(399001)、创业板指(399006)、
    北证50(899050)、科创50(000688) 的实时行情。
    缓存由 AStockDataSource._info_cache (TTL=120s) 负责。
    """

    def __init__(self, sources: List[BaseDataSource], db: cache_db):
        self.sources = sources
        self.db = db

    def get_realtime(self, codes: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """获取指数实时行情

        缓存由数据源层 (AStockDataSource._info_cache) 负责。

        Args:
            codes: 指数代码列表，默认使用配置中的列表

        Returns:
            指数行情列表，每项包含 code, name, price, change, change_percent 等字段
        """
        if codes is None:
            codes = DEFAULT_INDEX_CODES

        for source in self.sources:
            if not source.enabled:
                continue
            try:
                data = source.get_index_quotes(codes)
                if data:
                    logger.info(f"从 [{source.name}] 获取指数行情成功，共 {len(data)} 条")
                    return data
            except Exception as e:
                logger.warning(f"[{source.name}] get_index_quotes 失败: {e}")
                continue

        logger.error("所有数据源获取指数行情均失败")
        return []

    def get_index_by_code(self, code: str) -> Optional[Dict[str, Any]]:
        """获取单个指数的实时行情"""
        data = self.get_realtime([code])
        for item in data:
            if item.get("code") == code:
                return item
        return None

    def get_index_names(self) -> Dict[str, str]:
        """获取默认指数代码与名称的映射"""
        return dict(INDEX_CODES)
