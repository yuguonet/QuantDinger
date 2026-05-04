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
import time as _time
from typing import Dict, List, Any, Optional, Tuple

from app.data_sources import DataSourceFactory
from app.services.kline_cache_manager import (
    KlineCacheManager,
    aggregate_daily_to_monthly,
    _is_market_hours,
    _today_str,
    _ts_from_date,
    _dt_from_ts,
    _iso_week_start,
    _bar_field,
    DAILY_LIMIT,
)
from app.interfaces.trading_calendar import is_trading_day_today, prev_trading_day
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

    # synthesize_today_candle 短 TTL 缓存，避免每次缓存命中都打远端分钟线
    _SYNTHESIZE_TTL = 30  # 秒

    def __init__(self):
        self.cache = CacheManager()
        self.cache_ttl = CacheConfig.KLINE_CACHE_TTL
        self._kc = KlineCacheManager()
        # 批量当日 K 线缓存：{market: {symbol: candle_dict}}
        self._today_batch: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._today_batch_ts: Dict[str, float] = {}

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
        """周线由日线实时聚合（日线走缓存，周线不单独存储）

        日线已通过 _append_current 包含当日 K 线（盘中合成/闭市已完成），
        聚合后无需再次调用 synthesize_today_candle，避免重复 API 调用。
        """
        daily_limit = min(limit * 5 + 50, DAILY_LIMIT)
        daily = self.get_kline(market, symbol, "1D", daily_limit)
        if not daily:
            return []
        weekly = self._aggregate_weekly(daily, limit)
        return weekly[-limit:] if len(weekly) > limit else weekly

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

        日线已通过 _append_current 包含当日 K 线，聚合后无需再次调用
        synthesize_today_candle，避免重复 API 调用。
        """
        daily_limit = min(limit * 22 + 50, DAILY_LIMIT)
        daily = self.get_kline(market, symbol, "1D", daily_limit)
        if not daily:
            return []
        monthly = aggregate_daily_to_monthly(daily, limit)
        return monthly[-limit:] if len(monthly) > limit else monthly

    # ═══════════════════════════════════════════════════════════════════
    #  日线：缓存优先
    # ═══════════════════════════════════════════════════════════════════

    def _get_cached_kline(
        self, market: str, symbol: str, tf: str, limit: int,
    ) -> List[Dict[str, Any]]:
        """
        缓存优先加载：
          命中 → 检查数据完整性 → 返回缓存 + 合成当前周期
          缓存有缺口 → 从远程拉取完整数据刷新缓存
          未命中 → 触发预热 → 预热成功从缓存读 / 预热失败降级单只
        """
        fetch = lambda m, s, t, l: DataSourceFactory.get_kline(m, s, t, l)

        # 1) 读缓存
        cached = self._kc.get_cached(tf, symbol, market)
        if cached:
            # 检查缓存数据是否有缺口（数据源返回不完整数据导致）
            cached = self._refresh_if_gap(cached, market, symbol, tf, fetch)
            return self._serve(symbol, tf, limit, cached, market, fetch)

        # 2) 缓存未命中 → 触发预热
        logger.info(f"[KlineCache] 缓存未命中 {symbol} {tf}，触发预热")
        warmed = self._try_prewarm(market, symbol, tf)

        if warmed:
            cached = self._kc.get_cached(tf, symbol, market)
            if cached:
                cached = self._refresh_if_gap(cached, market, symbol, tf, fetch)
                return self._serve(symbol, tf, limit, cached, market, fetch)

        # 3) 预热失败 → 降级单只拉取 → 去掉当日存入
        logger.info(f"[KlineCache] 预热未覆盖 {symbol}，降级单只拉取")
        klines = fetch(market, symbol, tf, limit)
        if klines:
            self._kc.store_single(tf, market, symbol, klines)

        today_candle = self._kc.synthesize_today_candle(symbol, fetch, market)
        return self._append_current(tf, klines or [], today_candle, symbol, market, fetch, limit)

    def _refresh_if_gap(self, cached, market, symbol, tf, fetch):
        """检查缓存数据是否有缺口，有则从远程拉取完整数据刷新缓存。

        问题背景：数据源（腾讯/新浪/东财）偶尔返回不完整的历史数据，
        导致缓存文件虽然 mtime 很新（刚写入），但数据只到几天前，
        造成 K 线图出现多日跳空。

        修复逻辑：比较缓存数据最后日期与前一个交易日，若缺口 > 1 天，
        从远程拉取完整数据（DAILY_LIMIT 条）并刷新缓存文件。
        """
        if tf != "1D" or not cached or not market:
            return cached

        try:
            last_td = prev_trading_day()
            max_ts = max(b.get("time", 0) for b in cached)
            if max_ts <= 0:
                return cached

            last_bar_date = _dt_from_ts(max_ts).strftime("%Y-%m-%d")
            if last_bar_date >= last_td:
                return cached  # 数据完整，无需刷新

            # 计算缺口天数
            gap_days = (datetime.strptime(last_td, "%Y-%m-%d") -
                        datetime.strptime(last_bar_date, "%Y-%m-%d")).days
            if gap_days <= 1:
                return cached  # 1天缺口可能是正常的当日未更新

            logger.warning(
                f"[KlineCache] 缓存缺口 {market}:{symbol}: "
                f"最后日期={last_bar_date}, 需覆盖到={last_td}, 缺口={gap_days}天 → 刷新缓存"
            )

            # 从远程拉取完整数据
            fresh = fetch(market, symbol, tf, DAILY_LIMIT)
            if fresh and len(fresh) > len(cached):
                # 过滤当日未确认数据后写入缓存
                cutoff = _ts_from_date(_today_str())
                bars_to_store = [b for b in fresh if b.get("time", 0) < cutoff]
                if bars_to_store:
                    self._kc.store_single(tf, market, symbol, bars_to_store)
                    logger.info(
                        f"[KlineCache] 缓存刷新成功 {market}:{symbol}: "
                        f"{len(cached)}条 → {len(bars_to_store)}条"
                    )
                return fresh

            logger.warning(f"[KlineCache] 远程数据不足，保留原缓存 {market}:{symbol}")

        except Exception as e:
            logger.error(f"[KlineCache] 缺口刷新失败 {market}:{symbol}: {e}")

        return cached

    # ── 预热 ─────────────────────────────────────────────────────────

    def _try_prewarm(self, market: str, symbol: str, tf: str) -> bool:
        """
        缓存未命中时的补救：只拉取当前请求的这一只股票。

        全量预热（批量拉取所有自选股）应通过 /kline/prewarm API 或定时任务触发，
        不应在单次请求中触发，避免雷群效应。
        """
        if not market or not symbol:
            return False

        batch_fetch = lambda m, syms, tf, lim, cs=None: DataSourceFactory.get_kline_batch(
            m, syms, tf, lim, cached_symbols=cs,
        )

        ok = self._kc.prewarm("1D", [symbol], market, batch_fetch_func=batch_fetch)
        if ok:
            logger.info(f"[KlineCache] 单只预热成功 {market}:{symbol}")
        return ok

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

    def _ensure_today_batch(self, market: str, symbol: str = "") -> Dict[str, Dict[str, Any]]:
        """批量获取当日 OHLCV（一个 HTTP 请求拉全市场），带 30s 缓存。

        通过东方财富 clist 接口一次拿到所有 A 股当日行情，
        避免 N 只股票逐只调分钟线 API。
        仅 CNStock 市场走批量，其他市场返回空（回退逐只）。

        Args:
            market: 市场类型
            symbol: 当前请求的 symbol（可选，确保非自选股也能命中批量结果）
        """
        if market != "CNStock":
            return {}

        now = _time.time()
        ts = self._today_batch_ts.get(market, 0)
        if (now - ts) < self._SYNTHESIZE_TTL:
            batch = self._today_batch.get(market, {})
            # 缓存命中且包含目标 symbol → 直接返回
            if not symbol or symbol in batch:
                return batch
            # 缓存命中但不包含目标 symbol → 需要扩展拉取
            # （fall through 重新拉取，包含额外 symbol）

        # 收集该市场所有自选股 + 当前请求的 symbol
        all_by_market = self._get_all_watchlist_symbols_by_market()
        symbols = all_by_market.get(market, [])
        if symbol and symbol not in symbols:
            symbols.append(symbol)
        if not symbols:
            return {}

        try:
            from app.data_sources.cn_stock import _fetch_eastmoney_batch_quotes
            result = _fetch_eastmoney_batch_quotes(symbols)
            self._today_batch[market] = result
            self._today_batch_ts[market] = now
            logger.info(
                f"[KlineCache] 批量当日行情 {market}: "
                f"{len(result)}/{len(symbols)} 只命中"
            )
            return result
        except Exception as e:
            logger.warning(f"[KlineCache] 批量当日行情失败: {e}")
            return {}

    def _get_today_candle_cached(self, symbol: str, market: str, fetch) -> Optional[Dict[str, Any]]:
        """带短 TTL 缓存的当日 K 线合成，盘外直接返回 None 不打远端。

        优先走批量缓存（东财 clist 一个请求拉全市场），
        批量未命中才回退逐只分钟线合成。
        """
        if not _is_market_hours():
            return None

        # 1) 逐只 Redis 缓存
        cache_key = f"today_candle:{market}:{symbol}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached  # dict 或 None

        # 2) 批量缓存（东财 clist，1 次 HTTP 拿全市场）
        #    传入 symbol 确保非自选股也能命中批量结果
        batch = self._ensure_today_batch(market, symbol=symbol)
        if symbol in batch:
            candle = batch[symbol]
            self.cache.set(cache_key, candle, self._SYNTHESIZE_TTL)
            return candle

        # 3) 回退逐只分钟线合成
        candle = self._kc.synthesize_today_candle(symbol, fetch, market)
        self.cache.set(cache_key, candle, self._SYNTHESIZE_TTL)
        return candle

    def _serve(self, symbol, tf, limit, cached, market, fetch):
        today_candle = self._get_today_candle_cached(symbol, market, fetch)
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

    # 闭市后远端未更新时的重试间隔（秒）
    _COMPLETED_BAR_RETRY_TTL = 300  # 5 分钟后重试

    def _try_fetch_completed_bar(self, market, symbol, fetch):
        """闭市后当日已完成日线（不存 feather，带内存缓存防重复调用）

        判断逻辑：
          - _is_market_hours() == False（盘中由 synthesize_today_candle 处理）
          - is_trading_day_today() == True（非交易日不需要当日 bar）
          - 非午休时段（11:30-13:00 半日数据不完整，由 synthesize 覆盖）
          - 远程数据源已更新当日收盘数据（有 time == today_ts 的 bar）

        缓存策略：
          - 成功：缓存 1 小时（当日 bar 不会变）
          - 失败（远端未更新）：缓存 5 分钟后重试，避免每次请求都打远端
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
        retry_key = f"today_completed_retry:{market}:{symbol}"

        # 命中成功缓存 → 直接返回
        cached = self.cache.get(cache_key)
        if cached and cached.get("time") == today_ts:
            return cached

        # 命中失败重试缓存 → 5 分钟内不重试
        if self.cache.get(retry_key):
            return None

        try:
            bars = fetch(market, symbol, "1D", 2)
            if bars:
                for b in bars:
                    if b.get("time") == today_ts:
                        self.cache.set(cache_key, b, 3600)
                        # 清除重试缓存
                        self.cache.delete(retry_key)
                        return b
        except Exception as e:
            logger.debug(f"[Kline] 获取当日已完成 bar 失败 {market}:{symbol}: {e}")

        # 远端未更新 → 设置重试缓存，5 分钟后再试
        self.cache.set(retry_key, True, self._COMPLETED_BAR_RETRY_TTL)
        logger.debug(
            f"[Kline] 当日 bar 尚未更新 {market}:{symbol}，"
            f"{self._COMPLETED_BAR_RETRY_TTL}s 后重试"
        )
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
        try:
            cached_symbols = self._kc.get_cached_symbols("1D")
        except Exception:
            cached_symbols = set()
        batch_fetch = lambda m, syms, tf, lim, cs=None: DataSourceFactory.get_kline_batch(
            m, syms, tf, lim, cached_symbols=cs if cs is not None else cached_symbols,
        )
        results = {}

        try:
            results["1D"] = self._kc.prewarm("1D", symbols, market, batch_fetch_func=batch_fetch)
        except Exception as e:
            logger.warning(f"[KlineCache] 预热 1D 失败: {e}")
            results["1D"] = False

        return results

    # ═══════════════════════════════════════════════════════════════════
    #  分钟/小时线（原逻辑）
    # ═══════════════════════════════════════════════════════════════════

    def _get_remote_kline(self, market, symbol, timeframe, limit, before_time):
        # before_time 模式不缓存（历史翻页）
        if not before_time:
            cache_key = f"kline:{market}:{symbol}:{timeframe}"
            cached = self.cache.get(cache_key)
            if cached:
                return cached[-limit:] if len(cached) > limit else cached

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
