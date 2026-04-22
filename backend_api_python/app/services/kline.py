"""
K线数据服务
"""
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional, Tuple

from app.data_sources import DataSourceFactory
from app.utils.cache import CacheManager
from app.utils.logger import get_logger
from app.config import CacheConfig

logger = get_logger(__name__)

# ──────────────────────────────────────────────
# 本地聚合降级映射
# target_timeframe → (source_timeframe, 每组根数)
# 当远端取不到目标周期数据时，用更小周期的本地数据聚合出目标周期
# ──────────────────────────────────────────────
_AGGREGATION_FALLBACK: Dict[str, Tuple[str, int]] = {
    '5m':  ('1m',  5),     # 5根1分钟 → 1根5分钟
    '30m': ('15m', 2),     # 2根15分钟 → 1根30分钟（如无15m可再降级到5m×6）
    '1H':  ('30m', 2),     # 2根30分钟 → 1根1小时
    '2H':  ('1H',  2),     # 2根1小时   → 1根2小时
    '4H':  ('1H',  4),     # 4根1小时   → 1根4小时
    '1W':  ('1D',  5),     # 5根日线    → 1根周线（约5个交易日）
    '1M':  ('1D',  22),    # 22根日线   → 1根月线（约22个交易日）
}


def _aggregate_klines(
    source_klines: List[Dict[str, Any]],
    target_timeframe: str,
    group_size: int,
    limit: int
) -> List[Dict[str, Any]]:
    """
    将小周期 K 线数据聚合成大周期 K 线。

    - 固定窗口聚合（5m, 30m, 1H, 2H, 4H）：按 group_size 连续分组。
    - 周线 / 月线聚合（1W, 1M）：按日历周/月分组。

    Args:
        source_klines: 已按 time 升序排列的小周期 K 线列表
        target_timeframe: 目标周期
        group_size: 每组根数（周/月线仅作参考，实际按日历分组）
        limit: 需要返回的最大条数

    Returns:
        聚合后的 K 线列表
    """
    if not source_klines:
        return []

    # 按时间排序（防御性）
    source_klines.sort(key=lambda x: x['time'])

    if target_timeframe == '1W':
        return _aggregate_weekly(source_klines, limit)
    elif target_timeframe == '1M':
        return _aggregate_monthly(source_klines, limit)
    else:
        return _aggregate_fixed_window(source_klines, group_size, limit)


def _bar_field(bar: Dict[str, Any], field: str, default: float = 0.0) -> float:
    """安全取 K 线字段，缺失或类型异常时返回默认值。"""
    val = bar.get(field, default)
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _aggregate_fixed_window(
    source_klines: List[Dict[str, Any]],
    group_size: int,
    limit: int
) -> List[Dict[str, Any]]:
    """按固定窗口（连续 N 根）聚合，用于分钟/小时级周期。"""
    result: List[Dict[str, Any]] = []
    total = len(source_klines)

    for i in range(0, total, group_size):
        chunk = source_klines[i:i + group_size]
        # 不满 group_size 的尾巴组也保留（尽量不丢数据）

        aggregated = {
            'time':  chunk[0]['time'],
            'open':  _bar_field(chunk[0], 'open'),
            'high':  max(_bar_field(b, 'high') for b in chunk),
            'low':   min(_bar_field(b, 'low')  for b in chunk),
            'close': _bar_field(chunk[-1], 'close'),
            'volume': round(sum(_bar_field(b, 'volume') for b in chunk), 2),
        }
        result.append(aggregated)

    # 取最新的 limit 条
    if len(result) > limit:
        result = result[-limit:]

    return result


def _get_iso_week_start(ts: int) -> int:
    """返回该时间戳所在 ISO 周的周一 00:00:00 UTC 时间戳。"""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    # isoweekday: 1=Monday … 7=Sunday
    monday = dt - timedelta(days=dt.isoweekday() - 1)
    monday_midnight = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(monday_midnight.timestamp())


def _aggregate_weekly(
    source_klines: List[Dict[str, Any]],
    limit: int
) -> List[Dict[str, Any]]:
    """按 ISO 日历周聚合日线为周线。"""
    groups: Dict[int, List[Dict[str, Any]]] = {}
    order: List[int] = []  # 保持周的出现顺序

    for bar in source_klines:
        week_key = _get_iso_week_start(bar['time'])
        if week_key not in groups:
            groups[week_key] = []
            order.append(week_key)
        groups[week_key].append(bar)

    result: List[Dict[str, Any]] = []
    for week_key in order:
        chunk = groups[week_key]
        aggregated = {
            'time':  week_key,
            'open':  _bar_field(chunk[0], 'open'),
            'high':  max(_bar_field(b, 'high') for b in chunk),
            'low':   min(_bar_field(b, 'low')  for b in chunk),
            'close': _bar_field(chunk[-1], 'close'),
            'volume': round(sum(_bar_field(b, 'volume') for b in chunk), 2),
        }
        result.append(aggregated)

    if len(result) > limit:
        result = result[-limit:]

    return result


def _get_month_start(ts: int) -> int:
    """返回该时间戳所在月份 1 号 00:00:00 UTC 时间戳。"""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    first = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(first.timestamp())


def _aggregate_monthly(
    source_klines: List[Dict[str, Any]],
    limit: int
) -> List[Dict[str, Any]]:
    """按日历月聚合日线为月线。"""
    groups: Dict[int, List[Dict[str, Any]]] = {}
    order: List[int] = []

    for bar in source_klines:
        month_key = _get_month_start(bar['time'])
        if month_key not in groups:
            groups[month_key] = []
            order.append(month_key)
        groups[month_key].append(bar)

    result: List[Dict[str, Any]] = []
    for month_key in order:
        chunk = groups[month_key]
        aggregated = {
            'time':  month_key,
            'open':  _bar_field(chunk[0], 'open'),
            'high':  max(_bar_field(b, 'high') for b in chunk),
            'low':   min(_bar_field(b, 'low')  for b in chunk),
            'close': _bar_field(chunk[-1], 'close'),
            'volume': round(sum(_bar_field(b, 'volume') for b in chunk), 2),
        }
        result.append(aggregated)

    if len(result) > limit:
        result = result[-limit:]

    return result


class KlineService:
    """K线数据服务"""
    
    def __init__(self):
        self.cache = CacheManager()
        self.cache_ttl = CacheConfig.KLINE_CACHE_TTL
    
    def get_kline(
        self,
        market: str,
        symbol: str,
        timeframe: str,
        limit: int = 1000,
        before_time: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        获取K线数据
        
        Args:
            market: 市场类型 (Crypto, USStock, Forex, Futures)
            symbol: 交易对/股票代码
            timeframe: 时间周期
            limit: 数据条数
            before_time: 获取此时间之前的数据
            
        Returns:
            K线数据列表
        """
        # 构建缓存键（历史数据不缓存）
        if not before_time:
            cache_key = f"kline:{market}:{symbol}:{timeframe}:{limit}"
            cached = self.cache.get(cache_key)
            if cached:
                return cached
        
        # 获取数据
        klines = DataSourceFactory.get_kline(
            market=market,
            symbol=symbol,
            timeframe=timeframe,
            limit=limit,
            before_time=before_time
        )
        
        # ── 降级：远端无数据时，尝试本地聚合 ──
        if not klines:
            klines = self._try_aggregate_from_lower_timeframe(
                market=market,
                symbol=symbol,
                target_timeframe=timeframe,
                limit=limit,
                before_time=before_time
            )
        
        # 设置缓存（仅最新数据）
        if klines and not before_time:
            ttl = self.cache_ttl.get(timeframe, 300)
            self.cache.set(cache_key, klines, ttl)
        
        return klines
    
    def _try_aggregate_from_lower_timeframe(
        self,
        market: str,
        symbol: str,
        target_timeframe: str,
        limit: int,
        before_time: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        当远端取不到 target_timeframe 数据时，
        用更小周期的本地数据聚合出目标周期。
        
        聚合映射:
            1W  ← 1D  (5根日线 → 1根周线)
            1M  ← 1D  (约22根日线 → 1根月线)
            5m  ← 1m  (5根1分钟 → 1根5分钟)
            30m ← 15m (2根15分钟 → 1根30分钟)
            1H  ← 30m (2根30分钟 → 1根1小时)
            2H  ← 1H  (2根1小时 → 1根2小时)
            4H  ← 1H  (4根1小时 → 1根4小时)
        """
        fallback = _AGGREGATION_FALLBACK.get(target_timeframe)
        if not fallback:
            return []
        
        source_tf, group_size = fallback
        
        # 计算需要拉取多少根源 K 线
        # 多取一些 buffer，防止分组不满或跨日/跨周对齐丢数据
        source_limit = limit * group_size + group_size
        
        logger.info(
            f"K-line fallback: {market}:{symbol} {target_timeframe} remote empty, "
            f"trying local aggregation from {source_tf} (group_size={group_size})"
        )
        
        try:
            source_klines = DataSourceFactory.get_kline(
                market=market,
                symbol=symbol,
                timeframe=source_tf,
                limit=source_limit,
                before_time=None  # 降级时不限制 before_time，尽量取满源数据
            )
        except Exception as e:
            logger.warning(
                f"K-line fallback: failed to fetch {source_tf} for "
                f"{market}:{symbol}: {e}"
            )
            return []
        
        if not source_klines:
            logger.warning(
                f"K-line fallback: {source_tf} data also empty for {market}:{symbol}"
            )
            return []
        
        # 按时间排序
        source_klines.sort(key=lambda x: x['time'])
        
        # 聚合
        aggregated = _aggregate_klines(
            source_klines=source_klines,
            target_timeframe=target_timeframe,
            group_size=group_size,
            limit=limit
        )
        
        if aggregated:
            logger.info(
                f"K-line fallback OK: {market}:{symbol} {target_timeframe} "
                f"aggregated {len(source_klines)}×{source_tf} → {len(aggregated)} bars"
            )
        else:
            logger.warning(
                f"K-line fallback: aggregation produced empty result for "
                f"{market}:{symbol} {target_timeframe}"
            )
        
        return aggregated
    
    def get_latest_price(self, market: str, symbol: str) -> Optional[Dict[str, Any]]:
        """获取最新价格（使用1分钟K线，已弃用，建议使用 get_realtime_price）"""
        klines = self.get_kline(market, symbol, '1m', 1)
        if klines:
            return klines[-1]
        return None
    
    def get_realtime_price(self, market: str, symbol: str, force_refresh: bool = False) -> Dict[str, Any]:
        """
        获取实时价格（优先使用 ticker API，降级使用分钟 K 线）
        
        Args:
            market: 市场类型 (Crypto, USStock, Forex, Futures)
            symbol: 交易对/股票代码
            force_refresh: 是否强制刷新（跳过缓存）
            
        Returns:
            实时价格数据: {
                'price': 最新价格,
                'change': 涨跌额,
                'changePercent': 涨跌幅,
                'high': 最高价,
                'low': 最低价,
                'open': 开盘价,
                'previousClose': 昨收价,
                'source': 数据来源 ('ticker' 或 'kline')
            }
        """
        # 构建缓存键（短时间缓存，避免频繁请求）
        cache_key = f"realtime_price:{market}:{symbol}"
        
        # 如果不是强制刷新，尝试使用缓存
        if not force_refresh:
            cached = self.cache.get(cache_key)
            if cached:
                return cached
        
        result = {
            'price': 0,
            'change': 0,
            'changePercent': 0,
            'high': 0,
            'low': 0,
            'open': 0,
            'previousClose': 0,
            'source': 'unknown'
        }
        
        # 优先尝试使用 ticker API 获取实时价格
        try:
            ticker = DataSourceFactory.get_ticker(market, symbol)
            if ticker and ticker.get('last', 0) > 0:
                result = {
                    'price': ticker.get('last', 0),
                    'change': ticker.get('change', 0),
                    'changePercent': ticker.get('changePercent') or ticker.get('percentage', 0),
                    'high': ticker.get('high', 0),
                    'low': ticker.get('low', 0),
                    'open': ticker.get('open', 0),
                    'previousClose': ticker.get('previousClose', 0),
                    'source': 'ticker'
                }
                # 缓存 30 秒
                self.cache.set(cache_key, result, 30)
                return result
        except Exception as e:
            logger.debug(f"Ticker API failed for {market}:{symbol}, falling back to kline: {e}")
        
        # 降级：使用 1 分钟 K 线
        try:
            klines = self.get_kline(market, symbol, '1m', 2)
            if klines and len(klines) > 0:
                latest = klines[-1]
                prev_close = klines[-2]['close'] if len(klines) > 1 else latest.get('open', 0)
                current_price = latest.get('close', 0)
                
                change = round(current_price - prev_close, 4) if prev_close else 0
                change_pct = round(change / prev_close * 100, 2) if prev_close and prev_close > 0 else 0
                
                result = {
                    'price': current_price,
                    'change': change,
                    'changePercent': change_pct,
                    'high': latest.get('high', 0),
                    'low': latest.get('low', 0),
                    'open': latest.get('open', 0),
                    'previousClose': prev_close,
                    'source': 'kline_1m'
                }
                # 缓存 30 秒
                self.cache.set(cache_key, result, 30)
                return result
        except Exception as e:
            logger.debug(f"1m kline failed for {market}:{symbol}, trying daily: {e}")
        
        # 最后降级：使用日线数据（适用于非交易时间）
        try:
            klines = self.get_kline(market, symbol, '1D', 2)
            if klines and len(klines) > 0:
                latest = klines[-1]
                prev_close = klines[-2]['close'] if len(klines) > 1 else latest.get('open', 0)
                current_price = latest.get('close', 0)
                
                change = round(current_price - prev_close, 4) if prev_close else 0
                change_pct = round(change / prev_close * 100, 2) if prev_close and prev_close > 0 else 0
                
                result = {
                    'price': current_price,
                    'change': change,
                    'changePercent': change_pct,
                    'high': latest.get('high', 0),
                    'low': latest.get('low', 0),
                    'open': latest.get('open', 0),
                    'previousClose': prev_close,
                    'source': 'kline_1d'
                }
                # 日线数据缓存 5 分钟
                self.cache.set(cache_key, result, 300)
                return result
        except Exception as e:
            logger.error(f"All price sources failed for {market}:{symbol}: {e}")
        
        return result

