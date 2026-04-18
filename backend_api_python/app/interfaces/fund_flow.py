"""板块/概念资金流向接口"""
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime

from app.data_sources.base import BaseDataSource
from .cache_file import cache_db

logger = logging.getLogger(__name__)


class FundFlowInterface:
    """板块/概念资金流向接口

    获取板块和概念的资金流向数据。
    缓存由数据源层负责。
    """

    SECTOR_TABLE = "cnd_sector_fund_flow"
    CONCEPT_TABLE = "cnd_concept_fund_flow"

    def __init__(self, sources: List[BaseDataSource], db: cache_db):
        self.sources = sources
        self.db = db

    def get_sector_flow(self, trade_date: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取板块资金流向"""
        if trade_date is None:
            trade_date = datetime.now().strftime("%Y-%m-%d")

        # 优先查数据库
        data = self.db.query(
            self.SECTOR_TABLE,
            conditions={"trade_date": trade_date},
            order_by="-main_net_flow"
        )
        if data:
            return data

        # 从数据源获取
        for source in self.sources:
            if not source.enabled:
                continue
            if not hasattr(source, "get_sector_fund_flow"):
                continue
            try:
                flow = source.get_sector_fund_flow(trade_date)
                if flow:
                    self.db.insert_batch(self.SECTOR_TABLE, flow)
                    return flow
            except Exception as e:
                logger.warning(f"[{source.name}] get_sector_fund_flow 失败: {e}")
                continue
        return []

    def get_concept_flow(self, trade_date: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取概念资金流向"""
        if trade_date is None:
            trade_date = datetime.now().strftime("%Y-%m-%d")

        data = self.db.query(
            self.CONCEPT_TABLE,
            conditions={"trade_date": trade_date},
            order_by="-main_net_flow"
        )
        if data:
            return data

        for source in self.sources:
            if not source.enabled:
                continue
            if not hasattr(source, "get_concept_fund_flow"):
                continue
            try:
                flow = source.get_concept_fund_flow(trade_date)
                if flow:
                    self.db.insert_batch(self.CONCEPT_TABLE, flow)
                    return flow
            except Exception as e:
                logger.warning(f"[{source.name}] get_concept_fund_flow 失败: {e}")
                continue
        return []
