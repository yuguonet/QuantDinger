"""
🇨🇳 china-market-tools — 中国金融市场分析工具箱

宏观数据 / A股贪恐指数 / AI政策解读

用法:
    from china_market_tools import ChinaData, fear_greed_index

    data = ChinaData()
    df = data.gdp()

    result = fear_greed_index()
"""

__version__ = "1.0.0"

from .data_sources import ChinaData
from .fear_greed_index import fear_greed_index
