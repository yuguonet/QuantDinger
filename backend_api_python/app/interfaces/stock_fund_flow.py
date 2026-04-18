"""个股资金流接口 - 获取单只股票的资金流向数据

无需历史缓存、无需数据库表，直接从数据源获取返回。
"""
import logging
from typing import List, Dict, Any, Optional

from app.data_sources.base import BaseDataSource

logger = logging.getLogger(__name__)


class StockFundFlowInterface:
    """个股资金流接口

    获取单只股票的资金流向：主力/大单/中单/小单的流入流出。
    无缓存、无数据库，每次调用直接从数据源获取。
    """

    def __init__(self, sources: List[BaseDataSource]):
        self.sources = sources

    def get_flow(self, stock_code: str) -> Optional[Dict[str, Any]]:
        """获取个股实时资金流向

        列说明:
            stock_code             str    股票代码                          来源: akshare, eastmoney
            stock_name             str    股票名称                          来源: eastmoney
            price                  float  当前价格                          来源: eastmoney
            change_percent         float  涨跌幅(%)                        来源: eastmoney
            main_net_flow          float  主力净流入(元)                    来源: akshare, eastmoney
            main_inflow            float  主力流入(元)                      来源: akshare, eastmoney
            main_outflow           float  主力流出(元)                      来源: akshare, eastmoney
            large_order_net_flow   float  大单净流入(元)                    来源: akshare
            large_order_inflow     float  大单流入(元)                      来源: akshare
            large_order_outflow    float  大单流出(元)                      来源: akshare
            medium_order_net_flow  float  中单净流入(元)                    来源: akshare
            medium_order_inflow    float  中单流入(元)                      来源: akshare
            medium_order_outflow   float  中单流出(元)                      来源: akshare
            small_order_net_flow   float  小单净流入(元)                    来源: akshare
            small_order_inflow     float  小单流入(元)                      来源: akshare
            small_order_outflow    float  小单流出(元)                      来源: akshare
            retail_net_flow        float  散户净流入(元)                    来源: eastmoney

        Args:
            stock_code: 股票代码，如 "000001"

        Returns:
            资金流向字典，所有数据源均失败返回 None
        """
        for source in self.sources:
            if not source.enabled:
                continue
            try:
                data = source.get_stock_fund_flow(stock_code)
                if data:
                    logger.info(f"从 [{source.name}] 获取 {stock_code} 资金流成功")
                    return data
            except Exception as e:
                logger.warning(f"[{source.name}] get_stock_fund_flow({stock_code}) 失败: {e}")
                continue

        logger.warning(f"所有数据源获取 {stock_code} 资金流均失败")
        return None

    def batch_get_flow(self, stock_codes: List[str]) -> Dict[str, Optional[Dict[str, Any]]]:
        """批量获取个股资金流向

        Args:
            stock_codes: 股票代码列表

        Returns:
            {stock_code: flow_dict} 映射
        """
        result = {}
        for code in stock_codes:
            result[code] = self.get_flow(code)
        return result
