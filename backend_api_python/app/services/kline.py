"""
K线数据服务

核心改造：
  - 日线/周线/月线使用本地 feather 缓存（KlineCacheManager）
  - 惰性加载：优先读本地缓存，缓存未命中触发预热
  - 预热：查自选股去重 → 比对缓存 → 全量或增量拉取
  - 降级：预热失败 → 单只拉取 → 去掉当日存入缓存
  - 市场时段：用分钟线合成当日未完成 K 线
"""
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional, Tuple

from app.data_sources import DataSourceFactory
from app.services.kline_cache_manager import (
    KlineCacheManager,
    aggregate_daily_to_weekly,
    aggregate_daily_to_monthly,
    _is_market_hours,
    _today_str,
    _ts_from_date,
    _iso_week_start,
    _month_start,
    _bar_field,
    DAILY_LIMIT,
    WEEKLY_LIMIT,
    MONTHLY_LIMIT,
)
from app.utils.cache import CacheManager
from app.utils.logger import get_logger
from app.config import CacheConfig

logger = get_logger(__name__)

# 非日/周/月线的降级映射
_AGGREGATION_FALLBACK: Dict[str, Tuple[str, int]] = {
    '5m':  ('1m',  5),
    '30m': ('15m', 2),
    '1H':  ('30m', 2),
    '2H':  ('1H',  2),
    '4H':  ('1H',  4),
}


def _aggregate_fixed_window(source_klines, group_size, limit):
    result = []
    total = len(source_klines)
    for i in range(0, total, group_size):
        chunk = source_klines[i:i + group_size]
        result.append({
            'time': chunk[0]['time'],
            'open': _bar_field(chunk[0], 'open'),
            'high': max(_bar_field(b, 'high') for b in chunk),
            'low': min(_bar_field(b, 'low') for b in chunk),
            'close': _bar_field(chunk[-1], 'close'),
            'volume': round(sum(_bar_field(b, 'volume') for b in chunk), 2),
        })
    return result[-limit:] if len(result) > limit else result


class KlineService:
    """K线数据服务（feather 缓存优先）"""

    def __init__(self):
        self.cache = CacheManager()
        self.cache_ttl = CacheConfig.KLINE_CACHE_TTL
        self._kc = KlineCacheManager()

    def get_cache_dir(self) -> str:
        """获取缓存目录路径（供外部查询用）"""
        return self._kc.data_dir

    # ═══════════════════════════════════════════════════════════════════
    #  对外入口
    # ═══════════════════════════════════════════════════════════════════

    def get_kline(
        self,
        market: str,
        symbol: str,
        timeframe: str,
        limit: int = 1000,
        before_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        if timeframe in ("1D", "1W", "1M") and not before_time:
            return self._get_cached_kline(market, symbol, timeframe, limit)
        return self._get_remote_kline(market, symbol, timeframe, limit, before_time)

    # ═══════════════════════════════════════════════════════════════════
    #  日/周/月线：缓存优先
    # ═══════════════════════════════════════════════════════════════════

    def _get_cached_kline(
        self, market: str, symbol: str, tf: str, limit: int,
    ) -> List[Dict[str, Any]]:
        """
        缓存优先加载：
          命中 → 返回缓存 + 合成当前周期
          未命中 → 触发预热 → 预热成功从缓存读 / 预热失败降级单只
        """
        fetch = lambda m, s, t, l: DataSourceFactory.get_kline(m, s, t, l)

        # 1) 读缓存
        cached = self._kc.get_cached(tf, symbol)
        if cached:
            return self._serve(symbol, tf, limit, cached, market, fetch)

        # 2) 缓存未命中 → 触发预热
        logger.info(f"[KlineCache] 缓存未命中 {symbol} {tf}，触发预热")
        warmed = self._try_prewarm(market, symbol, tf, fetch)

        if warmed:
            cached = self._kc.get_cached(tf, symbol)
            if cached:
                return self._serve(symbol, tf, limit, cached, market, fetch)

        # 3) 预热失败 → 降级单只拉取 → 去掉当日存入
        logger.info(f"[KlineCache] 预热未覆盖 {symbol}，降级单只拉取")
        klines = fetch(market, symbol, tf, limit)
        if klines:
            self._kc.store_single(tf, symbol, klines)

        today_candle = self._kc.synthesize_today_candle(symbol, fetch, market)
        return self._append_current(tf, klines or [], today_candle, symbol, market, fetch, limit)

    # ── 预热 ─────────────────────────────────────────────────────────

    def _try_prewarm(self, market: str, symbol: str, tf: str, fetch) -> bool:
        """
        触发预热：获取所有自选股去重 + 确保当前股票在内 → 批量拉取。

        日线：1天1次全量 / 增量缺失
        周线：1周1次全量 / 增量缺失
        月线：1月1次全量 / 增量缺失
        """
        try:
            symbols = self._get_all_watchlist_symbols()
            if symbol and symbol not in symbols:
                symbols.append(symbol)
            symbols = list(dict.fromkeys(s.strip() for s in symbols if s.strip()))
            if not symbols:
                return False

            return self._kc.prewarm(tf, symbols, fetch, market)
        except Exception as e:
            logger.warning(f"[KlineCache] 预热失败: {e}")
            return False

    def _get_all_watchlist_symbols(self) -> List[str]:
        try:
            from app.utils.db import get_db_connection
            with get_db_connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT DISTINCT code FROM qd_watchlist")
                rows = cur.fetchall() or []
                cur.close()
            return [r['code'] for r in rows if r.get('code')]
        except Exception as e:
            logger.debug(f"[KlineCache] 获取自选股失败: {e}")
            return []

    # ── 从缓存生成响应 ───────────────────────────────────────────────

    def _serve(self, symbol, tf, limit, cached, market, fetch):
        today_candle = self._kc.synthesize_today_candle(symbol, fetch, market)
        return self._append_current(tf, cached, today_candle, symbol, market, fetch, limit)

    def _append_current(self, tf, bars, today_candle, symbol, market, fetch, limit):
        """追加合成当前周期"""
        if tf == "1D":
            result = list(bars)
            if today_candle:
                result.append(today_candle)
            result.sort(key=lambda x: x["time"])
            return result[-limit:]

        if tf == "1W":
            return self._append_current_week(bars, today_candle, symbol, market, fetch, limit)

        if tf == "1M":
            return self._append_current_month(bars, today_candle, symbol, market, fetch, limit)

        return bars[-limit:]

    def _append_current_week(self, cached_weekly, today_candle, symbol, market, fetch, limit):
        now_ts = today_candle["time"] if today_candle else int(
            datetime.now(timezone(timedelta(hours=8))).timestamp()
        )
        week_start = _iso_week_start(now_ts)
        historical = [b for b in cached_weekly if b["time"] < week_start]

        daily = self._kc.get_cached("1D", symbol) or []
        this_week = [b for b in daily if b["time"] >= week_start]
        if today_candle:
            this_week.append(today_candle)

        result = list(historical)
        if this_week:
            this_week.sort(key=lambda x: x["time"])
            result.append({
                "time": week_start,
                "open": _bar_field(this_week[0], "open"),
                "high": max(_bar_field(b, "high") for b in this_week),
                "low": min(_bar_field(b, "low") for b in this_week),
                "close": _bar_field(this_week[-1], "close"),
                "volume": round(sum(_bar_field(b, "volume") for b in this_week), 2),
            })
        result.sort(key=lambda x: x["time"])
        return result[-limit:]

    def _append_current_month(self, cached_monthly, today_candle, symbol, market, fetch, limit):
        now_ts = today_candle["time"] if today_candle else int(
            datetime.now(timezone(timedelta(hours=8))).timestamp()
        )
        month_start = _month_start(now_ts)
        historical = [b for b in cached_monthly if b["time"] < month_start]

        daily = self._kc.get_cached("1D", symbol) or []
        this_month = [b for b in daily if b["time"] >= month_start]
        if today_candle:
            this_month.append(today_candle)

        result = list(historical)
        if this_month:
            this_month.sort(key=lambda x: x["time"])
            result.append({
                "time": month_start,
                "open": _bar_field(this_month[0], "open"),
                "high": max(_bar_field(b, "high") for b in this_month),
                "low": min(_bar_field(b, "low") for b in this_month),
                "close": _bar_field(this_month[-1], "close"),
                "volume": round(sum(_bar_field(b, "volume") for b in this_month), 2),
            })
        result.sort(key=lambda x: x["time"])
        return result[-limit:]

    # ═══════════════════════════════════════════════════════════════════
    #  统一预热入口（供 API 调用）
    # ═══════════════════════════════════════════════════════════════════

    def prewarm_all(self, symbols: List[str], market: str = "CNStock") -> Dict[str, bool]:
        """
        统一预热入口：日/周/月线各自独立预热。

        日线：1天1次
        周线：1周1次
        月线：1月1次

        Returns:
            {"1D": True/False, "1W": True/False, "1M": True/False}
        """
        fetch = lambda m, s, tf, lim: DataSourceFactory.get_kline(m, s, tf, lim)
        results = {}

        for tf in ("1D", "1W", "1M"):
            try:
                results[tf] = self._kc.prewarm(tf, symbols, fetch, market)
            except Exception as e:
                logger.warning(f"[KlineCache] 预热 {tf} 失败: {e}")
                results[tf] = False

        return results

    # ═══════════════════════════════════════════════════════════════════
    #  分钟/小时线（原逻辑）
    # ═══════════════════════════════════════════════════════════════════

    def _get_remote_kline(self, market, symbol, timeframe, limit, before_time):
        if not before_time:
            cache_key = f"kline:{market}:{symbol}:{timeframe}:{limit}"
            cached = self.cache.get(cache_key)
            if cached:
                return cached

        klines = DataSourceFactory.get_kline(
            market=market, symbol=symbol, timeframe=timeframe,
            limit=limit, before_time=before_time,
        )

        if not klines:
            klines = self._try_aggregate_from_lower_timeframe(
                market, symbol, timeframe, limit, before_time,
            )

        if klines and not before_time:
            ttl = self.cache_ttl.get(timeframe, 300)
            self.cache.set(cache_key, klines, ttl)
        return klines

    def _try_aggregate_from_lower_timeframe(self, market, symbol, target_timeframe, limit, before_time):
        fallback = _AGGREGATION_FALLBACK.get(target_timeframe)
        if not fallback:
            return []
        source_tf, group_size = fallback
        source_limit = limit * group_size + group_size
        try:
            source_klines = DataSourceFactory.get_kline(
                market=market, symbol=symbol, timeframe=source_tf,
                limit=source_limit, before_time=None,
            )
        except Exception:
            return []
        if not source_klines:
            return []
        source_klines.sort(key=lambda x: x['time'])
        return _aggregate_fixed_window(source_klines, group_size, limit)

    # ═══════════════════════════════════════════════════════════════════
    #  价格（保留原逻辑）
    # ═══════════════════════════════════════════════════════════════════

    def get_latest_price(self, market: str, symbol: str) -> Optional[Dict[str, Any]]:
        klines = self.get_kline(market, symbol, '1m', 1)
        return klines[-1] if klines else None

    def get_realtime_price(self, market: str, symbol: str, force_refresh: bool = False) -> Dict[str, Any]:
        cache_key = f"realtime_price:{market}:{symbol}"
        if not force_refresh:
            cached = self.cache.get(cache_key)
            if cached:
                return cached

        result = {
            'price': 0, 'change': 0, 'changePercent': 0,
            'high': 0, 'low': 0, 'open': 0, 'previousClose': 0, 'source': 'unknown'
        }

        try:
            ticker = DataSourceFactory.get_ticker(market, symbol)
            if ticker and ticker.get('last', 0) > 0:
                result = {
                    'price': ticker.get('last', 0),
                    'change': ticker.get('change', 0),
                    'changePercent': ticker.get('changePercent') or ticker.get('percentage', 0),
                    'high': ticker.get('high', 0), 'low': ticker.get('low', 0),
                    'open': ticker.get('open', 0), 'previousClose': ticker.get('previousClose', 0),
                    'source': 'ticker'
                }
                self.cache.set(cache_key, result, 30)
                return result
        except Exception:
            pass

        try:
            klines = self.get_kline(market, symbol, '1m', 2)
            if klines and len(klines) > 0:
                latest = klines[-1]
                prev = klines[-2]['close'] if len(klines) > 1 else latest.get('open', 0)
                price = latest.get('close', 0)
                chg = round(price - prev, 4) if prev else 0
                pct = round(chg / prev * 100, 2) if prev and prev > 0 else 0
                result = {
                    'price': price, 'change': chg, 'changePercent': pct,
                    'high': latest.get('high', 0), 'low': latest.get('low', 0),
                    'open': latest.get('open', 0), 'previousClose': prev,
                    'source': 'kline_1m'
                }
                self.cache.set(cache_key, result, 30)
                return result
        except Exception:
            pass

        try:
            klines = self.get_kline(market, symbol, '1D', 2)
            if klines and len(klines) > 0:
                latest = klines[-1]
                prev = klines[-2]['close'] if len(klines) > 1 else latest.get('open', 0)
                price = latest.get('close', 0)
                chg = round(price - prev, 4) if prev else 0
                pct = round(chg / prev * 100, 2) if prev and prev > 0 else 0
                result = {
                    'price': price, 'change': chg, 'changePercent': pct,
                    'high': latest.get('high', 0), 'low': latest.get('low', 0),
                    'open': latest.get('open', 0), 'previousClose': prev,
                    'source': 'kline_1d'
                }
                self.cache.set(cache_key, result, 300)
                return result
        except Exception:
            pass

        return result
