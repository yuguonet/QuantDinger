"""
数据源工厂
根据市场类型返回对应的数据源
"""
from typing import Dict, List, Any, Optional

from app.data_sources.base import BaseDataSource
from app.utils.logger import get_logger

logger = get_logger(__name__)


class DataSourceFactory:
    """数据源工厂"""
    
    _sources: Dict[str, BaseDataSource] = {}
    
    @classmethod
    def get_source(cls, market: str) -> BaseDataSource:
        """
        获取指定市场的数据源
        
        Args:
            market: 市场类型 (Crypto, USStock, Forex, Futures)
            
        Returns:
            数据源实例
        """
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
        # Default to CNStock for safety (A-share is the primary market).
        return cls.get_source("CNStock")
    
    @classmethod
    def _create_source(cls, market: str) -> BaseDataSource:
        """创建数据源实例"""
        if market == 'Crypto':
            from app.data_sources.crypto import CryptoDataSource
            return CryptoDataSource()
        elif market == 'CNStock':
            from app.data_sources.cn_stock import CNStockDataSource
            # 返回 AStockDataSource（继承 CNStockDataSource，补充龙虎榜/热榜/涨跌停池等扩展方法）
            try:
                from app.interfaces.cn_stock_extent import AStockDataSource
                return AStockDataSource()
            except ImportError:
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
        before_time: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        获取K线数据的便捷方法

        Args:
            market: 市场类型
            symbol: 交易对/股票代码
            timeframe: 时间周期
            limit: 数据条数
            before_time: 获取此时间之前的数据

        Returns:
            K线数据列表
        """
        try:
            if not market:
                logger.error(f"Missing market parameter for symbol: {symbol}")
                return []
            source = cls.get_source(market)
            klines = source.get_kline(symbol, timeframe, limit, before_time)

            # 确保数据按时间排序
            klines.sort(key=lambda x: x['time'])

            return klines
        except Exception as e:
            logger.error(f"Failed to fetch K-lines {market}:{symbol} - {str(e)}")
            return []

    @classmethod
    def get_kline_batch(
        cls,
        market: str,
        symbols: List[str],
        timeframe: str,
        limit: int,
        cached_symbols: Optional[set] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        批量获取多只股票的 K 线数据。

        一次调用返回所有成功拉取的 symbol → klines 映射，
        内部串行调用底层数据源（K 线 API 不支持多股拼请求）。

        Args:
            market: 市场类型
            symbols: 股票代码列表
            timeframe: 时间周期
            limit: 数据条数
            cached_symbols: 已有缓存的 symbol 集合（用于优化：有缓存的只补当日）

        Returns:
            {symbol: [kline_bars]} — 仅包含成功返回非空数据的 symbol
        """
        try:
            if not market or not symbols:
                return {}
            source = cls.get_source(market)
            if hasattr(source, 'get_kline_batch'):
                return source.get_kline_batch(symbols, timeframe, limit, cached_symbols=cached_symbols)
            # fallback: 串行逐只拉取
            result: Dict[str, List[Dict[str, Any]]] = {}
            for sym in symbols:
                try:
                    klines = source.get_kline(sym, timeframe, limit)
                    if klines:
                        klines.sort(key=lambda x: x['time'])
                        result[sym] = klines
                except Exception as e:
                    logger.warning(f"Batch fetch failed for {market}:{sym} - {e}")

            logger.info(f"Batch fetch {market} {timeframe}: {len(result)}/{len(symbols)} succeeded (serial)")
            return result
        except Exception as e:
            logger.error(f"Failed to batch fetch K-lines {market} - {str(e)}")
            return {}
    
    @classmethod
    def get_ticker(cls, market: str, symbol: str) -> Dict[str, Any]:
        """
        获取实时报价的便捷方法
        
        Args:
            market: 市场类型
            symbol: 交易对/股票代码
            
        Returns:
            实时报价数据: {
                'last': 最新价,
                'change': 涨跌额,
                'changePercent': 涨跌幅,
                ...
            }
        """
        try:
            if not market:
                logger.error(f"Missing market parameter for symbol: {symbol}")
                return {'last': 0, 'symbol': symbol}
            source = cls.get_source(market)
            return source.get_ticker(symbol)
        except NotImplementedError:
            logger.warning(f"get_ticker not implemented for market: {market}")
            return {'last': 0, 'symbol': symbol}
        except Exception as e:
            logger.error(f"Failed to fetch ticker {market}:{symbol} - {str(e)}")
            return {'last': 0, 'symbol': symbol}

