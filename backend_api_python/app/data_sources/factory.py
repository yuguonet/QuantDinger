"""
数据源工厂
根据市场类型返回对应的数据源
"""
from typing import Dict, List, Any, Optional

from app.data_sources.base import BaseDataSource
from app.utils.logger import get_logger

logger = get_logger(__name__)

# 小写 / 别名 -> 与 _create_source 一致的 PascalCase key
_MARKET_ALIASES: Dict[str, str] = {
    "crypto": "Crypto",
    "cryptocurrency": "Crypto",
    "forex": "Forex",
    "fx": "Forex",
    "usstock": "USStock",
    "us_stocks": "USStock",
    "stock": "USStock",
    "cnstock": "CNStock",
    "hkstock": "HKStock",
    "futures": "Futures",
}


class DataSourceFactory:
    """
    数据源工厂。
    K 线 / 报价 使用哪个接口完全由调用方传入的 market（与自选分类一致）决定，不做根据 symbol 字符串的推断。
    """
    
    _sources: Dict[str, BaseDataSource] = {}
    
    @classmethod
    def normalize_market(cls, market: str) -> str:
        """统一市场枚举大小写与别名，供路由与数据源入口使用。"""
        if not market:
            return "Crypto"
        raw = str(market).strip()
        if raw in ("Crypto", "Forex", "Futures", "USStock", "CNStock", "HKStock"):
            return raw
        key = raw.lower().replace(" ", "").replace("-", "_")
        return _MARKET_ALIASES.get(key, raw)

    @classmethod
    def get_source(cls, market: str) -> BaseDataSource:
        """
        获取指定市场的数据源
        
        Args:
            market: 市场类型 (Crypto, USStock, Forex, Futures, CNStock, HKStock)
            
        Returns:
            数据源实例
        """
        market = cls.normalize_market(market or "")
        if market not in cls._sources:
            cls._sources[market] = cls._create_source(market)
        return cls._sources[market]

    @classmethod
    def get_data_source(cls, name: str) -> BaseDataSource:
        """
        Backward compatible alias used by older code paths.

        Some modules historically called `get_data_source("binance")` to fetch a crypto data source.
        In the localized Python backend we primarily use `get_source("Crypto")`.
        """
        key = (name or "").strip().lower()
        if key in ("crypto", "binance", "okx", "bybit", "bitget", "kucoin", "gate", "mexc", "kraken", "coinbase"):
            return cls.get_source("Crypto")
        if key in ("futures",):
            return cls.get_source("Futures")
        if key in ("forex", "fx"):
            return cls.get_source("Forex")
        # 不再默认兜底到 Crypto — 避免 A 股代码误入加密货币数据源
        logger.warning("get_data_source(%s): 未知数据源名称，默认使用 CNStock", name)
        return cls.get_source("CNStock")
    
    @classmethod
    def _create_source(cls, market: str) -> BaseDataSource:
        """创建数据源实例"""
        if market == 'Crypto':
            from app.data_sources.crypto import CryptoDataSource
            return CryptoDataSource()
        elif market == 'CNStock':
            from app.data_sources.cn_stock import CNStockDataSource
            return CNStockDataSource()
        elif market == 'HKStock':
            from app.data_sources.hk_stock import HKStockDataSource
            return HKStockDataSource()
        elif market == 'USStock':
            from app.data_sources.us_stock import USStockDataSource
            return USStockDataSource()
        elif market == 'Forex':
            from app.data_sources.forex import ForexDataSource
            return ForexDataSource()
        elif market == 'Futures':
            from app.data_sources.futures import FuturesDataSource
            return FuturesDataSource()
        else:
            raise ValueError(f"不支持的市场类型: {market}")
    
    @classmethod
    def get_kline(
        cls,
        market: str,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
        adj: str = "qfq",
    ) -> List[Dict[str, Any]]:
        """
        获取K线数据

        支持单只和批量两种调用方式:
          单只: symbol="600519"  → 返回 List[Dict]
          批量: symbol="600519,000001,000690"  → 返回 Dict[symbol, List[Dict]]

        批量实现规则:
          1. CNStock — 逗号拼接传入 DataSource，内部走 Coordinator 动态队列多源并发
             Provider 层无原生批量 K 线 API，通过 Coordinator 并发单只实现
          2. 其他市场 — 当前无批量实现，逗号模式不生效（走单只路径）

        Args:
            market: 市场类型
            symbol: 交易对/股票代码，多只用逗号分隔
            timeframe: 时间周期
            limit: 数据条数
            before_time: 获取此时间之前的数据
            after_time: 可选，Unix 秒，K 线 time 需 >= 此值（回测左边界）
            adj: 复权方式 — "qfq"(前复权,默认) / "hfq"(后复权) / ""(不复权)
                 仅 A 股(CNStock) 生效，其他市场忽略此参数

        Returns:
            单只: K线数据列表 [bar, ...]
            批量: {symbol: [bar, ...], ...}
        """
        try:
            m = cls.normalize_market(market or "")
            source = cls.get_source(m)
            # 仅 CNStock 支持 adj 参数（A股复权）
            if m == "CNStock":
                klines = source.get_kline(symbol, timeframe, limit, before_time, after_time, adj=adj)
            else:
                klines = source.get_kline(symbol, timeframe, limit, before_time, after_time)
            
            # 确保数据按时间排序
            klines.sort(key=lambda x: x['time'])
            
            return klines
        except Exception as e:
            import traceback
            logger.error(f"Failed to fetch K-lines {market}:{symbol} (normalized={cls.normalize_market(market or '')}) - {str(e)}")
            logger.debug(traceback.format_exc())
            return []

    @classmethod
    def get_ticker(cls, market: str, symbol: str) -> Dict[str, Any]:
        """
        获取实时行情

        支持单只和批量两种调用方式:
          单只: symbol="600519"  → 返回 Dict (ticker)
          批量: symbol="600519,000001,000690"  → 返回 Dict[symbol, Dict]

        批量实现规则:
          1. CNStock — 逗号拼接传入 DataSource，内部走 Provider 批量接口
             腾讯/新浪一次 HTTP 请求拿多只行情，性能最优
          2. 其他市场 — 当前无批量实现，逗号模式不生效（走单只路径）

        Args:
            market: 市场类型
            symbol: 交易对/股票代码，多只用逗号分隔

        Returns:
            单只: {"last", "change", "changePercent", ...}
            批量: {symbol: {"last", "change", "changePercent", ...}, ...}
        """
        try:
            m = cls.normalize_market(market or "")
            source = cls.get_source(m)
            return source.get_ticker(symbol)
        except NotImplementedError:
            logger.warning(f"get_ticker not implemented for market: {market}")
            return {'last': 0, 'symbol': symbol}
        except Exception as e:
            logger.error(f"Failed to fetch ticker {market}:{symbol} (normalized={cls.normalize_market(market or '')}) - {str(e)}")
            return {'last': 0, 'symbol': symbol}

