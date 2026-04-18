"""
A股接口层 (interfaces)

统一数据访问入口，包含:
- AShareDataHub: 主入口 (cn_stock_extent.py) — 组合所有 Interface 对象
- AStockDataSource: 多源 A股数据源 (cn_stock_extent.py)
- IndexInterface: 指数行情
- MarketSnapshotInterface: 市场快照
- StockInfoInterface: 个股信息
- StockFundFlowInterface: 个股资金流
- FundFlowInterface: 板块资金流
- DragonTigerInterface: 龙虎榜 (多源)
- HotRankInterface: 热榜 (多源)
- ZTPoolInterface: 涨停池 (多源)
- LimitDownInterface: 跌停池 (多源)
- BrokenBoardInterface: 炸板池 (多源)
"""


from .cache_file import cache_db
from .cn_stock_extent import AShareDataHub, AStockDataSource

__all__ = ['AShareDataHub', 'AStockDataSource']