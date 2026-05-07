"""
🇨🇳 china-market-tools — 中国金融市场分析工具箱

宏观数据 / A股贪恐指数 / AI政策解读 / 热门板块分析 / 板块历史趋势+周期+预测

用法:
    from china_market_tools import ChinaData, fear_greed_index
    from china_market_tools import get_hot_industry_boards, get_hot_concept_boards
    from china_market_tools import SectorAnalyzer, SectorHistoryScheduler

    data = ChinaData()
    df = data.gdp()

    result = fear_greed_index()
    sectors = get_all_hot_sectors()

    # 历史分析
    analyzer = SectorAnalyzer(db)
    trend = analyzer.full_analysis("industry")

    # 统一入口（带缓存，推荐）
    from app.market_cn import get_china_macro, get_fear_greed, get_policy
    macro = get_china_macro()  # 自动缓存

    # 需要原始版（无缓存）直接从子模块导入
    from app.market_cn.sector_history import get_sector_trend
"""

__version__ = "1.3.0"

from .china_stock import ChinaData
from .fear_greed_index import fear_greed_index
from .hot_sectors import (
    get_hot_industry_boards,
    get_hot_concept_boards,
    get_sector_detail,
    get_all_hot_sectors,
)
from .sector_history import (
    SectorAnalyzer,
    SectorHistoryScheduler,
)
from .china_market import (
    get_china_macro,
    get_fear_greed,
    get_policy,
    get_hot_sectors,
    get_sector_stocks,
    get_sector_trend,
    get_sector_prediction,
    get_sector_history,
    get_sector_cycle,
    get_emotion_history,
    refresh as refresh_cn,
)
