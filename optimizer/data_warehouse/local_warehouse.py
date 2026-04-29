# -*- coding: utf-8 -*-
"""
本地数据仓库数据源
集成到 DataSourceFactory，作为最高优先级数据源（本地优先 → API fallback）。
"""
from typing import Any, Dict, List, Optional

from app.data_sources.base import BaseDataSource
from app.data_warehouse.storage import read_local, exists, list_local
from app.utils.logger import get_logger

logger = get_logger(__name__)


class LocalWarehouseSource(BaseDataSource):
    """
    本地数据仓库数据源。

    特点:
    - 零网络延迟，读取速度 >100x
    - 作为 fallback chain 的第一优先级
    - 不支持实时行情（需配合远程源）
    """

    name = "local_warehouse"

    def __init__(self, market: str):
        self._market = market

    def get_kline(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """从本地仓库读取 K 线数据"""
        data = read_local(
            market=self._market,
            timeframe=timeframe,
            symbol=symbol,
            limit=limit,
            before_time=before_time,
            after_time=after_time,
        )
        if data:
            logger.debug(f"[本地仓库] 命中 {symbol} {timeframe}: {len(data)} 条")
        return data

    def has_data(self, symbol: str, timeframe: str) -> bool:
        """检查本地是否有该股票数据"""
        return exists(self._market, timeframe, symbol)

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """实时行情不支持，需配合远程源"""
        raise NotImplementedError("本地仓库不支持实时行情")

    def list_symbols(self, timeframe: str = "1D") -> List[str]:
        """列出本地已有数据的股票代码"""
        return list_local(self._market, timeframe)
