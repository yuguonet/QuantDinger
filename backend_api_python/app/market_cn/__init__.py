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
"""

__version__ = "1.2.0"

from .data_sources import ChinaData
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
    get_sector_trend,
    get_sector_history,
)
