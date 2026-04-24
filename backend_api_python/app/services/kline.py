"""
K线数据服务

核心改造：
  - 日线使用本地 feather 缓存（KlineCacheManager）
  - 周线/月线由日线实时聚合，不单独缓存
  - 惰性加载：优先读本地缓存，缓存未命中触发预热
  - 预热：查自选股去重 → 比对缓存 → 全量或增量拉取
  - 降级：预热失败 → 单只拉取 → 去掉当日存入缓存
  - 市场时段：用分钟线合成当日未完成 K 线
  - 闭市后到次日开盘前：日线缓存已包含当日确认数据，无需补齐
"""
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional, Tuple

from app.data_sources import DataSourceFactory
from app.services.kline_cache_manager import (
    KlineCacheManager,
    aggregate_daily_to_monthly,
    _is_market_hours,
    _today_str,
    _ts_from_date,
    _iso_week_start,
    _month_start,
    _bar_field,
    DAILY_LIMIT,
)
from app.interfaces.trading_calendar import is_trading_day_today
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
        # 周线/月线：从日线实时聚合，不走单独缓存
        if timeframe == "1W" and not before_time:
            return self._get_weekly_from_daily(market, symbol, limit)
        if timeframe == "1M" and not before_time:
            return self._get_monthly_from_daily(market, symbol, limit)
        if timeframe == "1D" and not before_time:
            return self._get_cached_kline(market, symbol, timeframe, limit)
        return self._get_remote_kline(market, symbol, timeframe, limit, before_time)

    def _get_weekly_from_daily(
        self, market: str, symbol: str, limit: int,
    ) -> List[Dict[str, Any]]:
        """周线由日线实时聚合（日线走缓存，周线不单独存储）"""
        # 每周约 5 个交易日，多拉一些保证覆盖
        daily_limit = min(limit * 5 + 50, DAILY_LIMIT)
        daily = self.get_kline(market, symbol, "1D", daily_limit)
        if not daily:
            return []
        weekly = self._aggregate_weekly(daily, limit)
        # 追加当日合成
        fetch = lambda m, s, t, l: DataSourceFactory.get_kline(m, s, t, l)
        today_candle = self._kc.synthesize_today_candle(symbol, fetch, market)
        if today_candle:
            now_ts = today_candle["time"]
            week_start = _iso_week_start(now_ts)
            historical = [b for b in weekly if b["time"] < week_start]
            # 从日线取本周数据（daily 已含 _append_current 追加的当日 bar，需去重）
            this_week = [b for b in daily if b["time"] >= week_start and b.get("time") != now_ts]
            this_week.append(today_candle)
            this_week.sort(key=lambda x: x["time"])
            if this_week:
                historical.append({
                    "time": week_start,
                    "open": _bar_field(this_week[0], "open"),
                    "high": max(_bar_field(b, "high") for b in this_week),
                    "low": min(_bar_field(b, "low") for b in this_week),
                    "close": _bar_field(this_week[-1], "close"),
                    "volume": round(sum(_bar_field(b, "volume") for b in this_week), 2),
                })
            historical.sort(key=lambda x: x["time"])
            return historical[-limit:]
        return weekly

    @staticmethod
    def _aggregate_weekly(daily_bars: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
        """将日线聚合为周线"""
        if not daily_bars:
            return []
        bars = sorted(daily_bars, key=lambda x: x.get("time", 0))
        groups: Dict[int, List[Dict]] = {}
        order: List[int] = []
        for bar in bars:
            t = bar.get("time", 0)
            if not t:
                continue
            wk = _iso_week_start(t)
            if wk not in groups:
                groups[wk] = []
                order.append(wk)
            groups[wk].append(bar)
        result = []
        for wk in order:
            chunk = groups[wk]
            if not chunk:
                continue
            result.append({
                "time": wk,
                "open": _bar_field(chunk[0], "open"),
                "high": max(_bar_field(b, "high") for b in chunk),
                "low": min(_bar_field(b, "low") for b in chunk),
                "close": _bar_field(chunk[-1], "close"),
                "volume": round(sum(_bar_field(b, "volume") for b in chunk), 2),
            })
        return result[-limit:] if len(result) > limit else result

    # ── 月线：由日线实时聚合 ─────────────────────────────────────────

    def _get_monthly_from_daily(
        self, market: str, symbol: str, limit: int,
    ) -> List[Dict[str, Any]]:
        """月线由日线实时聚合（日线走缓存，月线不单独存储）

        闭市后到次日开盘前：日线缓存已包含当日确认数据，聚合结果完整。
        盘中：追加分钟线合成的当日未完成 K 线。
        """
        # 每月约 22 个交易日，多拉一些保证覆盖
        daily_limit = min(limit * 22 + 50, DAILY_LIMIT)
        daily = self.get_kline(market, symbol, "1D", daily_limit)
        if not daily:
            return []

        monthly = aggregate_daily_to_monthly(daily, limit)

        # 盘中：合成当日 K 线追加到当月
        fetch = lambda m, s, t, l: DataSourceFactory.get_kline(m, s, t, l)
        today_candle = self._kc.synthesize_today_candle(symbol, fetch, market)
        if today_candle:
            now_ts = today_candle["time"]
            month_start = _month_start(now_ts)
            historical = [b for b in monthly if b["time"] < month_start]
            # 从日线取当月数据（daily 已含 _append_current 追加的当日 bar，需去重）
            this_month = [b for b in daily if b["time"] >= month_start and b.get("time") != now_ts]
            this_month.append(today_candle)
            this_month.sort(key=lambda x: x["time"])
            if this_month:
                historical.append({
                    "time": month_start,
                    "open": _bar_field(this_month[0], "open"),
                    "high": max(_bar_field(b, "high") for b in this_month),
                    "low": min(_bar_field(b, "low") for b in this_month),
                    "close": _bar_field(this_month[-1], "close"),
                    "volume": round(sum(_bar_field(b, "volume") for b in this_month), 2),
                })
            historical.sort(key=lambda x: x["time"])
            return historical[-limit:]

        return monthly

    # ═══════════════════════════════════════════════════════════════════
    #  日线：缓存优先
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
        cached = self._kc.get_cached(tf, symbol, market)
        if cached:
            return self._serve(symbol, tf, limit, cached, market, fetch)

        # 2) 缓存未命中 → 触发预热
        logger.info(f"[KlineCache] 缓存未命中 {symbol} {tf}，触发预热")
        warmed = self._try_prewarm(market, symbol, tf, fetch)

        if warmed:
            cached = self._kc.get_cached(tf, symbol, market)
            if cached:
                return self._serve(symbol, tf, limit, cached, market, fetch)

        # 3) 预热失败 → 降级单只拉取 → 去掉当日存入
        logger.info(f"[KlineCache] 预热未覆盖 {symbol}，降级单只拉取")
        klines = fetch(market, symbol, tf, limit)
        if klines:
            self._kc.store_single(tf, market, symbol, klines)

        today_candle = self._kc.synthesize_today_candle(symbol, fetch, market)
        return self._append_current(tf, klines or [], today_candle, symbol, market, fetch, limit)

    # ── 预热 ─────────────────────────────────────────────────────────

    def _try_prewarm(self, market: str, symbol: str, tf: str, fetch) -> bool:
        """
        触发全市场预热：遍历所有自选股市场，逐市场批量拉取。

        仅预热日线缓存（周线/月线由日线实时聚合）。
        批量拉取优先级高于单只拉取：一次遍历所有市场，全部预热。
        返回当前请求的 market 是否预热成功。
        """
        if not market:
            return False

        # 周线/月线的预热实际只预热日线
        cache_tf = "1D"

        # 一次查出所有市场的自选股
        all_by_market = self._get_all_watchlist_symbols_by_market()

        # 确保当前 symbol 在对应市场中
        if symbol:
            syms = all_by_market.setdefault(market, [])
            if symbol not in syms:
                syms.append(symbol)

        # 去重每个市场
        for mkt in list(all_by_market.keys()):
            all_by_market[mkt] = list(dict.fromkeys(
                s.strip() for s in all_by_market[mkt] if s.strip()
            ))
            if not all_by_market[mkt]:
                del all_by_market[mkt]

        if not all_by_market:
            return False

        # 批量拉取函数
        batch_fetch = lambda m, syms, tf, lim, cs=None: DataSourceFactory.get_kline_batch(m, syms, tf, lim, cached_symbols=cs)

        # 逐市场预热，记录当前 market 的结果
        current_ok = False
        for mkt, syms in all_by_market.items():
            logger.info(f"[KlineCache] 预热 {mkt} {len(syms)} 只: {syms[:5]}{'...' if len(syms) > 5 else ''}")
            ok = self._kc.prewarm(cache_tf, syms, fetch, mkt, batch_fetch_func=batch_fetch)
            if mkt == market:
                current_ok = ok

        return current_ok

    def _get_all_watchlist_symbols_by_market(self) -> Dict[str, List[str]]:
        """
        获取所有自选股，按市场类型分组返回。

        Returns:
            {"CNStock": ["600519", "000001"], "Crypto": ["BTC/USDT", "ETH/USDT"], ...}
        """
        try:
            from app.utils.db import get_db_connection
            with get_db_connection() as conn:
                cur = conn.cursor()
                try:
                    cur.execute("SELECT market, symbol FROM qd_watchlist WHERE market IS NOT NULL AND symbol IS NOT NULL")
                    rows = cur.fetchall() or []
                finally:
                    cur.close()
            result: Dict[str, List[str]] = {}
            for r in rows:
                mkt = (r.get('market') or '').strip()
                sym = (r.get('symbol') or '').strip()
                if mkt and sym:
                    result.setdefault(mkt, []).append(sym)
            return result
        except Exception as e:
            logger.debug(f"[KlineCache] 获取自选股失败: {e}")
            return {}

    def _get_all_watchlist_symbols(self) -> List[str]:
        """向后兼容：获取所有自选股的 symbol（不分市场）"""
        result = self._get_all_watchlist_symbols_by_market()
        all_symbols = []
        for syms in result.values():
            all_symbols.extend(syms)
        return all_symbols

    # ── 从缓存生成响应 ───────────────────────────────────────────────

    def _serve(self, symbol, tf, limit, cached, market, fetch):
        today_candle = self._kc.synthesize_today_candle(symbol, fetch, market)
        return self._append_current(tf, cached, today_candle, symbol, market, fetch, limit)

    def _append_current(self, tf, bars, today_candle, symbol, market, fetch, limit):
        """追加当日 K 线（盘中合成 / 闭市后取远程已完成 bar）"""
        if tf == "1D":
            result = list(bars)
            if today_candle:
                today_ts = today_candle["time"]
                result = [b for b in result if b.get("time") != today_ts]
                result.append(today_candle)
            else:
                # 盘中 synthesize 无结果（闭市/午休/非交易日）→ 尝试取当日已完成 bar
                completed = self._try_fetch_completed_bar(market, symbol, fetch)
                if completed:
                    today_ts = completed["time"]
                    result = [b for b in result if b.get("time") != today_ts]
                    result.append(completed)
            result.sort(key=lambda x: x["time"])
            return result[-limit:]

        return bars[-limit:]

    def _try_fetch_completed_bar(self, market, symbol, fetch):
        """闭市后当日已完成日线（不存 feather，带内存缓存防重复调用）

        判断逻辑：
          - _is_market_hours() == False（盘中由 synthesize_today_candle 处理）
          - is_trading_day_today() == True（非交易日不需要当日 bar）
          - 非午休时段（11:30-13:00 半日数据不完整，由 synthesize 覆盖）
          - 远程数据源已更新当日收盘数据（有 time == today_ts 的 bar）
        """
        if not market or _is_market_hours() or not is_trading_day_today():
            return None

        # 午休时段不从远程取当日 bar（数据只有上午部分，不完整）
        # synthesize_today_candle 已用上午分钟线合成，此处跳过
        from datetime import time as dt_time
        now = datetime.now(timezone(timedelta(hours=8)))
        if dt_time(11, 30) < now.time() < dt_time(13, 0):
            return None

        today_ts = _ts_from_date(_today_str())
        cache_key = f"today_completed:{market}:{symbol}"

        cached = self.cache.get(cache_key)
        if cached and cached.get("time") == today_ts:
            return cached

        try:
            bars = fetch(market, symbol, "1D", 2)
            if bars:
                for b in bars:
                    if b.get("time") == today_ts:
                        self.cache.set(cache_key, b, 3600)
                        return b
        except Exception as e:
            logger.debug(f"[Kline] 获取当日已完成 bar 失败 {market}:{symbol}: {e}")

        return None

    # ═══════════════════════════════════════════════════════════════════
    #  统一预热入口（供 API 调用）
    # ═══════════════════════════════════════════════════════════════════

    def prewarm_all(self, symbols: List[str], market: str = "CNStock") -> Dict[str, bool]:
        """
        统一预热入口：仅预热日线（周线/月线由日线实时聚合）。

        日线：1天1次

        Returns:
            {"1D": True/False}
        """
        fetch = lambda m, s, tf, lim: DataSourceFactory.get_kline(m, s, tf, lim)
        batch_fetch = lambda m, syms, tf, lim, cs=None: DataSourceFactory.get_kline_batch(m, syms, tf, lim, cached_symbols=cs)
        results = {}

        try:
            results["1D"] = self._kc.prewarm("1D", symbols, fetch, market, batch_fetch_func=batch_fetch)
        except Exception as e:
            logger.warning(f"[KlineCache] 预热 1D 失败: {e}")
            results["1D"] = False

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
