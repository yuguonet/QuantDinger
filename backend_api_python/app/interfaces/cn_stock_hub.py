"""
AShareDataHub — A股数据统一入口（门面）

组合所有 Interface 对象，对外提供唯一调用入口。
"""

from app.data_sources.a_stock import AStockDataSource


class AShareDataHub:
    """
    A股数据统一入口 — 组合所有 Interface 对象，供 routes/emotion_scheduler 调用。

    属性:
        index:           IndexInterface           指数行情
        market_snapshot: MarketSnapshotInterface   市场快照
        zt_pool:         ZTPoolInterface           涨停池
        limit_down:      LimitDownInterface        跌停池
        broken_board:    BrokenBoardInterface      炸板池
        dragon_tiger:    DragonTigerInterface      龙虎榜
        hot_rank:        HotRankInterface          热榜/人气榜
        stock_info:      StockInfoInterface        个股信息
        stock_fund_flow: StockFundFlowInterface    个股资金流
        fund_flow:       FundFlowInterface         板块资金流
    """

    def __init__(self, sources=None, db=None):
        from app.interfaces.index import IndexInterface
        from app.interfaces.market_snapshot import MarketSnapshotInterface
        from app.interfaces.zt_pool import ZTPoolInterface
        from app.interfaces.limit_down import LimitDownInterface
        from app.interfaces.broken_board import BrokenBoardInterface
        from app.interfaces.dragon_tiger import DragonTigerInterface
        from app.interfaces.hot_rank import HotRankInterface
        from app.interfaces.stock_info import StockInfoInterface
        from app.interfaces.stock_fund_flow import StockFundFlowInterface
        from app.interfaces.fund_flow import FundFlowInterface
        from app.interfaces.cache_file import cache_db

        # 数据源列表: 默认使用 AStockDataSource (多源 fallback)
        if sources is None:
            _ds = AStockDataSource()
            sources = [_ds]

        if db is None:
            db = cache_db()

        # 缓存统一由 AStockDataSource._info_cache 负责，不再使用 RealtimeCache
        self.index = IndexInterface(sources, db)
        self.market_snapshot = MarketSnapshotInterface(sources, db)
        self.zt_pool = ZTPoolInterface(sources, db)
        self.limit_down = LimitDownInterface(sources, db)
        self.broken_board = BrokenBoardInterface(sources, db)
        self.dragon_tiger = DragonTigerInterface(sources, db)
        self.hot_rank = HotRankInterface(sources, db)
        self.stock_info = StockInfoInterface(sources, db)
        self.stock_fund_flow = StockFundFlowInterface(sources)
        self.fund_flow = FundFlowInterface(sources, db)
