"""
中国A股数据源 — Coordinator 统一调度 + DB 行情缓存

架构:
  get_ticker()  → Coordinator race 模式（并发，第一个成功的返回）
  get_kline()   → DB 优先 + 远程补充，数据流:
    上游取K线 ← cn_stock接口 ← kline_clean填充 ← 补充数据 ← db_market取数据

补充数据策略（db 只存 15m / 1D）:
  - db 无数据或缺 >1 交易日 → 远程全量拉取，能补则补，无法补放弃
  - 缺当日 → 远程 15m 聚合（最大 200 只股全量内存缓冲）+ 实时行情比对
    只在成交量上有误差，15 分钟内不重复取
  - 实时行情惰性比对缓存（记录最高价、最低价、现价，有成交量就记录）

批量模式优化:
  - 先批量查 DB，命中直接返回
  - 未命中的走远程批量拉取 + 回填 DB
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone, time as dt_time
from typing import Any, Dict, List, Optional

from app.data_sources.base import BaseDataSource
from app.data_sources.normalizer import normalize_cn_code
from app.data_sources.asia_stock_kline import normalize_chart_timeframe
from app.data_sources.circuit_breaker import get_realtime_circuit_breaker
from app.data_sources.coordinator import get_coordinator, Coordinator_direct_call
from app.data_sources.kline_clean import clean_klines
from app.utils.logger import get_logger

logger = get_logger(__name__)

_TZ_CN = timezone(timedelta(hours=8))

# 实时行情惰性缓存刷新间隔（秒）
_QUOTE_CACHE_TTL = 900  # 15 分钟


# ================================================================
# 工具函数
# ================================================================

def _strip_cn_prefix(code: str) -> str:
    """去掉 A 股代码的市场前缀，返回纯 6 位数字。"""
    s = (code or "").strip()
    if len(s) > 6 and s[:2].upper() in ("SH", "SZ", "BJ"):
        return s[2:]
    return s


def _dt_to_ts(dt) -> int:
    """db_market 返回的 datetime → Unix 时间戳（秒），兼容 naive/aware。"""
    if isinstance(dt, (int, float)):
        return int(dt)
    if isinstance(dt, datetime):
        if dt.tzinfo:
            return int(dt.timestamp())
        return int(dt.replace(tzinfo=_TZ_CN).timestamp())
    return 0


def _is_market_hours() -> bool:
    """判断当前是否为 A 股交易时段（含集合竞价 9:15-9:30、午休 11:30-13:00）。"""
    from app.utils.trading_calendar import is_trading_day_today
    if not is_trading_day_today():
        return False
    now = datetime.now(_TZ_CN)
    t = now.time()
    # 9:15 ~ 15:00 都算（含集合竞价和午休，数据仍有参考价值）
    return dt_time(9, 15) <= t <= dt_time(15, 0)


def _today_str() -> str:
    return datetime.now(_TZ_CN).strftime("%Y-%m-%d")


def _today_ts() -> int:
    dt = datetime.strptime(_today_str(), "%Y-%m-%d")
    return int(dt.replace(tzinfo=_TZ_CN).timestamp())


def _day_ts(day_str: str) -> int:
    """'YYYY-MM-DD' → 当天 00:00 的 Unix 时间戳（秒）。"""
    dt = datetime.strptime(day_str, "%Y-%m-%d")
    return int(dt.replace(tzinfo=_TZ_CN).timestamp())


def _latest_trading_day_ts() -> int:
    """从交易日历获取最近一个交易日的时间戳。"""
    from app.utils.trading_calendar import is_trading_day_today, prev_trading_day
    if is_trading_day_today():
        return _today_ts()
    try:
        return _day_ts(prev_trading_day())
    except Exception:
        # 交易日历不可用时降级为今天
        return _today_ts()


# ================================================================
# 实时行情惰性比对缓存
# ================================================================

class _QuoteCacheEntry:
    """单只股票的惰性行情缓存条目。"""
    __slots__ = ('high', 'low', 'price', 'volume', 'ts')

    def __init__(self):
        self.high: float = 0.0
        self.low: float = 0.0
        self.price: float = 0.0
        self.volume: float = 0.0
        self.ts: float = 0.0

    @property
    def is_valid(self) -> bool:
        return self.price > 0 and (time.time() - self.ts) < _QUOTE_CACHE_TTL

    def update_from_ticker(self, ticker: Dict[str, Any]):
        """从 ticker 数据更新缓存（惰性: 只往极端方向写）。"""
        last = float(ticker.get('last', 0) or 0)
        if last <= 0:
            return
        now = time.time()
        high = float(ticker.get('high', 0) or 0)
        low = float(ticker.get('low', 0) or 0)
        vol = float(ticker.get('volume', 0) or ticker.get('baseVolume', 0) or 0)

        # 首次填充或缓存过期
        if self.price <= 0 or (now - self.ts) >= _QUOTE_CACHE_TTL:
            self.high = max(high, last) if high > 0 else last
            self.low = min(low, last) if low > 0 else last
            self.price = last
            self.volume = vol
            self.ts = now
            return

        # 惰性更新: 只往极端方向写
        if high > self.high:
            self.high = high
        if low > 0 and (self.low <= 0 or low < self.low):
            self.low = low
        self.price = last  # 现价始终取最新
        if vol > 0:
            self.volume = vol
        self.ts = now


class RealtimeQuoteCache:
    """全局实时行情惰性比对缓存（线程安全）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._entries: Dict[str, _QuoteCacheEntry] = {}

    def _put(self, symbol: str, result: Dict[str, Any]):
        """将 ticker 结果写入缓存（内部方法）。"""
        raw = _strip_cn_prefix(symbol)
        if not result or float(result.get('last', 0) or 0) <= 0:
            return
        with self._lock:
            entry = self._entries.get(raw)
            if entry is None:
                entry = _QuoteCacheEntry()
                self._entries[raw] = entry
            entry.update_from_ticker(result)

    def get_or_fetch(self, symbol: str, ds: 'CNStockDataSource') -> Optional[_QuoteCacheEntry]:
        """获取缓存行情，过期则惰性刷新。"""
        raw = _strip_cn_prefix(symbol)
        with self._lock:
            entry = self._entries.get(raw)
            if entry and entry.is_valid:
                return entry

        # 缓存失效，远程取（锁外）
        try:
            ticker = ds.get_ticker(raw)
            if not ticker or float(ticker.get('last', 0) or 0) <= 0:
                return None
        except Exception:
            return None

        with self._lock:
            entry = self._entries.get(raw)
            if entry is None:
                entry = _QuoteCacheEntry()
                self._entries[raw] = entry
            entry.update_from_ticker(ticker)
            return entry

    def batch_fetch(self, symbols: List[str], ds: 'CNStockDataSource') -> Dict[str, _QuoteCacheEntry]:
        """批量获取/刷新行情缓存（一次 batch_quote 调用覆盖多只）。"""
        need_refresh: List[str] = []
        result: Dict[str, _QuoteCacheEntry] = {}

        with self._lock:
            for sym in symbols:
                raw = _strip_cn_prefix(sym)
                entry = self._entries.get(raw)
                if entry and entry.is_valid:
                    result[raw] = entry
                else:
                    need_refresh.append(raw)

        if not need_refresh:
            return result

        # 批量取行情（一次 HTTP）
        try:
            tickers = ds._get_tickers(need_refresh)
        except Exception:
            tickers = {}

        with self._lock:
            for raw in need_refresh:
                ticker = tickers.get(raw)
                if not ticker or float(ticker.get('last', 0) or 0) <= 0:
                    continue
                entry = self._entries.get(raw)
                if entry is None:
                    entry = _QuoteCacheEntry()
                    self._entries[raw] = entry
                entry.update_from_ticker(ticker)
                result[raw] = entry

        return result


# ================================================================
# DB 行情桥接层
# ================================================================

class DBKlineBridge:
    """DB 行情桥接层 — 封装 db_market.py 的读写逻辑。"""

    def __init__(self, ds: 'CNStockDataSource'):
        self._ds = ds
        self._mgr = None
        self._writer = None
        self._quote_cache = RealtimeQuoteCache()
        self._init_lock = threading.Lock()
        self._init_attempted = False  # 避免反复尝试失败的初始化

    def _ensure_init(self):
        """惰性初始化 db_market 实例（只尝试一次）。"""
        if self._init_attempted:
            return
        with self._init_lock:
            if self._init_attempted:
                return
            self._init_attempted = True
            try:
                from app.utils.db_market import get_market_db_manager, get_market_kline_writer
                self._mgr = get_market_db_manager()
                self._mgr.ensure_market_db("CNStock")
                self._writer = get_market_kline_writer()
            except Exception as e:
                logger.warning(f"[DB桥接] 初始化失败，降级为纯远程: {e}")
                self._mgr = None
                self._writer = None

    @property
    def available(self) -> bool:
        self._ensure_init()
        return self._writer is not None

    # ── 单只: DB 优先取 K 线 ──

    def get_kline(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """DB 优先取 K 线，自动补充缺失数据。

        周期路由:
          1m / 5m     → 直接走远端（DB 不存）
          15m / 1D    → DB 直接读取
          30m~4h      → DB 从 15m 聚合
          1W / 1M     → DB 从 1D 聚合
        """
        adj = "qfq"
        raw = _strip_cn_prefix(symbol)
        tf = normalize_chart_timeframe(timeframe)
        lim = max(int(limit or 300), 1)

        # ── 1m/5m 不走 DB，直接远端 ──
        if tf in ("1m", "5m"):
            return self._ds._get_kline_remote(
                raw, tf, lim, before_time, after_time, adj
            )

        if not self.available:
            return self._ds._get_kline_remote(
                raw, tf, lim, before_time, after_time, adj
            )

        # ── 第一步: 从 DB 取数据 ──
        db_bars = self._fetch_from_db(raw, tf, lim)

        if db_bars:
            # ── 第二步: kline_clean 填充中间缺失 ──
            cleaned = clean_klines(db_bars, tf)

            # 不再需要 _fill_today_if_needed — 1D 已从 15m 实时聚合，天然包含当日数据

            out = self._ds.filter_and_limit(
                cleaned, limit=lim, before_time=before_time,
                after_time=after_time, truncate=(after_time is None),
            )
            if out:
                logger.info(f"[DB桥接] {raw} tf={tf} DB命中 bars={len(out)}")
                return out

        # ── DB 无数据 → 远程全量拉取 ──
        return self._remote_with_backfill(raw, tf, lim, before_time, after_time, adj)

    # ── 批量: 分类优化 ──

    def get_kline_batch(
        self,
        symbols: List[str],
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
        adj: str = "qfq",
    ) -> Dict[str, List[Dict[str, Any]]]:
        """批量取 K 线 — 先批量查 DB，未命中的再走远程。"""
        if not symbols:
            return {}

        tf = normalize_chart_timeframe(timeframe)
        lim = max(int(limit or 300), 1)

        # ── 1m/5m 不走 DB，直接远端 ──
        if tf in ("1m", "5m"):
            return self._ds._get_klines_remote(symbols, tf, lim, adj)

        if not self.available:
            return self._ds._get_klines_remote(symbols, tf, lim, adj)

        result: Dict[str, List[Dict[str, Any]]] = {}
        need_remote: List[str] = []

        # ── 第一步: 批量查 DB ──
        for sym in symbols:
            raw = _strip_cn_prefix(sym)
            db_bars = self._fetch_from_db(raw, tf, lim)

            if db_bars:
                cleaned = clean_klines(db_bars, tf)
                # 1D 已从 15m 聚合，无需 _fill_today_if_needed
                out = self._ds.filter_and_limit(
                    cleaned, limit=lim, before_time=before_time,
                    after_time=after_time, truncate=(after_time is None),
                )
                if out:
                    result[raw] = out
                    continue

            need_remote.append(raw)

        # ── 第二步: 未命中的走远程（批量） ──
        if need_remote:
            remote_results = self._ds._get_klines_remote(
                need_remote, tf, lim, adj
            )
            for raw, bars in remote_results.items():
                self._backfill_db(raw, tf, bars)
                # 1D 已从 15m 聚合，无需 _fill_today_if_needed
                out = self._ds.filter_and_limit(
                    bars, limit=lim, before_time=before_time,
                    after_time=after_time, truncate=(after_time is None),
                )
                result[raw] = out

        if need_remote:
            logger.info(
                f"[DB桥接批量] tf={tf} 总={len(symbols)} "
                f"DB命中={len(result) - len(need_remote)} "
                f"远程补充={len(need_remote)}"
            )
        return result

    # ── DB 读取 ──

    def _fetch_from_db(self, raw: str, tf: str, limit: int) -> List[Dict[str, Any]]:
        """从 db_market 查询 K 线数据，统一转为 {time: int, ...} 格式。

        周期路由 (调用方已排除 1m/5m):
          15m         → 直接查询（唯一基线数据）
          1D / 30m~4h → 从 15m 聚合
          1W / 1M     → 从 15m 聚合（取更多 15m bar 再按日/周/月分组）
        """
        try:
            if tf == "15m":
                rows = self._writer.query("CNStock", raw, "15m", limit=limit)
            elif tf == "1D":
                # 日线从 15m 聚合，不再单独存 1D
                # 每天约 16 根 15m bar，取 limit*16+冗余
                rows = self._writer.aggregate("CNStock", raw, "15m", limit=limit * 16 + 50)
            elif tf in ("30m", "1h", "2h", "4h"):
                rows = self._writer.aggregate("CNStock", raw, "15m", limit=limit)
            else:
                # 1W / 1M → 从 15m 聚合（先聚到日，再聚到周/月）
                rows = self._writer.aggregate("CNStock", raw, "15m", limit=limit * 16 + 200)
        except Exception as e:
            logger.debug(f"[DB桥接] DB查询异常 {raw} tf={tf}: {e}")
            return []

        if not rows:
            return []

        bars = []
        for row in rows:
            ts = _dt_to_ts(row.get("time"))
            if ts <= 0:
                continue
            bars.append({
                "time": ts,
                "open": round(float(row.get("open", 0)), 4),
                "high": round(float(row.get("high", 0)), 4),
                "low": round(float(row.get("low", 0)), 4),
                "close": round(float(row.get("close", 0)), 4),
                "volume": round(float(row.get("volume", 0)), 2),
            })
        bars.sort(key=lambda b: b["time"])

        # 1D / 1W / 1M 需要二次聚合（15m → 日 → 周/月）
        if tf == "1D":
            return self._aggregate_15m_to_daily(bars, limit)
        elif tf in ("1W", "1M"):
            daily = self._aggregate_15m_to_daily(bars, limit * 8)
            if tf == "1W":
                return self._aggregate_daily_to_weekly(daily, limit)
            else:
                return self._aggregate_daily_to_monthly(daily, limit)
        return bars

    # ── 15m → 日/周/月 聚合 ──

    @staticmethod
    def _aggregate_15m_to_daily(bars: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
        """将 15m K 线按交易日聚合为日线。"""
        if not bars:
            return []
        groups: Dict[int, List[Dict]] = {}
        order: List[int] = []
        for b in bars:
            ts = b.get("time", 0)
            if ts <= 0:
                continue
            # 按北京时间日期分组
            day_ts = int(datetime.fromtimestamp(ts, tz=_TZ_CN).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).timestamp())
            if day_ts not in groups:
                groups[day_ts] = []
                order.append(day_ts)
            groups[day_ts].append(b)
        result = []
        for day_ts in order:
            chunk = groups[day_ts]
            if not chunk:
                continue
            result.append({
                "time": day_ts,
                "open": float(chunk[0].get("open", 0)),
                "high": max(float(b.get("high", 0)) for b in chunk),
                "low": min(float(b.get("low", 0)) for b in chunk),
                "close": float(chunk[-1].get("close", 0)),
                "volume": round(sum(float(b.get("volume", 0)) for b in chunk), 2),
            })
        return result[-limit:] if len(result) > limit else result

    @staticmethod
    def _aggregate_daily_to_weekly(daily_bars: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
        """将日线按 ISO 周聚合为周线。"""
        if not daily_bars:
            return []
        groups: Dict[tuple, List[Dict]] = {}
        order: List[tuple] = []
        for b in daily_bars:
            ts = b.get("time", 0)
            if ts <= 0:
                continue
            dt = datetime.fromtimestamp(ts, tz=_TZ_CN)
            key = dt.isocalendar()[:2]  # (year, week)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(b)
        result = []
        for key in order:
            chunk = groups[key]
            if not chunk:
                continue
            result.append({
                "time": chunk[0]["time"],
                "open": float(chunk[0].get("open", 0)),
                "high": max(float(b.get("high", 0)) for b in chunk),
                "low": min(float(b.get("low", 0)) for b in chunk),
                "close": float(chunk[-1].get("close", 0)),
                "volume": round(sum(float(b.get("volume", 0)) for b in chunk), 2),
            })
        return result[-limit:] if len(result) > limit else result

    @staticmethod
    def _aggregate_daily_to_monthly(daily_bars: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
        """将日线按月聚合为月线。"""
        if not daily_bars:
            return []
        groups: Dict[int, List[Dict]] = {}
        order: List[int] = []
        for b in daily_bars:
            ts = b.get("time", 0)
            if ts <= 0:
                continue
            dt = datetime.fromtimestamp(ts, tz=_TZ_CN)
            month_ts = int(dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
            if month_ts not in groups:
                groups[month_ts] = []
                order.append(month_ts)
            groups[month_ts].append(b)
        result = []
        for month_ts in order:
            chunk = groups[month_ts]
            if not chunk:
                continue
            result.append({
                "time": month_ts,
                "open": float(chunk[0].get("open", 0)),
                "high": max(float(b.get("high", 0)) for b in chunk),
                "low": min(float(b.get("low", 0)) for b in chunk),
                "close": float(chunk[-1].get("close", 0)),
                "volume": round(sum(float(b.get("volume", 0)) for b in chunk), 2),
            })
        return result[-limit:] if len(result) > limit else result

    # ── 远程全量拉取 + 回填 DB ──

    def _remote_with_backfill(
        self, raw: str, tf: str, limit: int,
        before_time: Optional[int], after_time: Optional[int], adj: str
    ) -> List[Dict[str, Any]]:
        """DB 无数据 → 远程全量拉取，能补则补回 DB。"""
        remote_bars = self._ds._get_kline_remote(raw, tf, limit, adj=adj)
        if not remote_bars:
            return []

        self._backfill_db(raw, tf, remote_bars)

        # 1D 已从 15m 聚合，无需 _fill_today_if_needed

        out = self._ds.filter_and_limit(
            remote_bars, limit=limit, before_time=before_time,
            after_time=after_time, truncate=(after_time is None),
        )
        logger.info(f"[DB桥接] {raw} tf={tf} 远程补充 bars={len(out)}")
        return out

    def _backfill_db(self, raw: str, tf: str, bars: List[Dict[str, Any]]):
        """将远程拉取的 K 线回填到 DB（只存 15m，1D 从 15m 聚合）。"""
        if tf != "15m":
            return

        records = []
        for b in bars:
            ts = b.get("time", 0)
            if ts <= 0:
                continue
            dt = datetime.fromtimestamp(ts, tz=_TZ_CN).replace(tzinfo=None)
            records.append({
                "time": dt,
                "open": b.get("open", 0),
                "high": b.get("high", 0),
                "low": b.get("low", 0),
                "close": b.get("close", 0),
                "volume": b.get("volume", 0),
            })

        if not records:
            return

        try:
            result = self._writer.upsert("CNStock", raw, tf, records)
            logger.debug(
                f"[DB桥接] 回填 {raw}/{tf}: "
                f"+{result.get('inserted', 0)} ~{result.get('updated', 0)}"
            )
        except Exception as e:
            logger.debug(f"[DB桥接] 回填失败 {raw}/{tf}: {e}")


# ================================================================
# 数据源类
# ================================================================

class CNStockDataSource(BaseDataSource):
    """A股数据源 — Coordinator 动态队列 + 自动源发现 + DB 行情缓存"""

    name = "CNStock/multi-source"

    def __init__(self):
        self.circuit_breaker = get_realtime_circuit_breaker()
        self._db_bridge = DBKlineBridge(self)

    # ----------------------------------------------------------
    # 实时行情 / 报价
    # ----------------------------------------------------------

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """获取实时行情（单只/批量）。"""
        if ',' in symbol:
            symbols = [s.strip() for s in symbol.split(',') if s.strip()]
            if not symbols:
                return {"last": 0, "symbol": symbol}
            return self._get_tickers(symbols)

        code = normalize_cn_code(symbol)
        raw = _strip_cn_prefix(code)

        result = get_coordinator().coordinate_ticker(
            symbol=code,
            cb=self.circuit_breaker,
            market="CNStock",
            timeout=8,
        )

        if result:
            result["symbol"] = raw
            # 写入惰性行情缓存，供 get_kline 补充当日 bar 使用
            self._db_bridge._quote_cache._put(raw, result)
            return result

        logger.warning(f"[行情] 所有数据源均失败: {symbol}")
        return {"last": 0, "symbol": raw}

    def _get_tickers(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """批量获取实时行情（ticker 格式）— 透传 Provider 层 batch_quote 接口。"""
        from app.data_sources.provider import get_providers
        normalized = [normalize_cn_code(s) for s in symbols if s]
        if not normalized:
            return {}
        providers = get_providers(capability="batch_quote", market="CNStock")
        if not providers:
            return {}
        for p in providers:
            raw_result = Coordinator_direct_call(p.fetch_tickers, normalized)
            if raw_result:
                cleaned = {}
                for k, v in raw_result.items():
                    raw_key = _strip_cn_prefix(k)
                    if isinstance(v, dict):
                        v["symbol"] = raw_key
                        # 批量写入惰性行情缓存
                        self._db_bridge._quote_cache._put(raw_key, v)
                    cleaned[raw_key] = v
                return cleaned
        return {}

    # ----------------------------------------------------------
    # K线数据 — DB 优先
    # ----------------------------------------------------------

    def get_kline(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
        adj: str = "qfq",
    ) -> List[Dict[str, Any]]:
        """获取 K 线数据（DB 优先）。

        数据流:
          上游取K线 ← cn_stock接口 ← kline_clean填充 ← 补充数据 ← db_market取数据

        批量模式（逗号分隔）: 先批量查 DB，未命中的再批量走远程。
        """
        # ── 批量模式 ──
        if ',' in symbol:
            symbols = [s.strip() for s in symbol.split(',') if s.strip()]
            if not symbols:
                return []
            batch = self._db_bridge.get_kline_batch(
                symbols, timeframe, limit, before_time, after_time, adj
            )
            # 合并为统一列表返回（按 symbol 顺序）
            merged = []
            for sym in symbols:
                raw = _strip_cn_prefix(sym)
                bars = batch.get(raw, [])
                merged.extend(bars)
            return merged

        # ── 单只模式 ──
        return self._db_bridge.get_kline(
            symbol, timeframe, limit, before_time, after_time, adj
        )

    # ----------------------------------------------------------
    # 内部: Coordinator 远程拉取（单只）
    # ----------------------------------------------------------

    def _get_kline_remote(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
        adj: str = "qfq",
    ) -> List[Dict[str, Any]]:
        """直接走 Coordinator 远程拉取，不经过 DB 层。

        注意: timeframe 应已由调用方 normalize 过，这里不再重复。
        """
        code = normalize_cn_code(symbol)
        lim = max(int(limit or 300), 1)

        coord_results, failed = get_coordinator().coordinate_kline(
            symbols=[code],
            timeframe=timeframe,
            limit=lim,
            cb=self.circuit_breaker,
            market="CNStock",
            timeout=20,
            adj=adj,
        )

        bars = coord_results.get(code, [])
        if not bars:
            bars = self._try_aggregate_lower(timeframe, code, lim, adj)

        if bars:
            bars = clean_klines(bars, timeframe)

        return self.filter_and_limit(
            bars, limit=lim, before_time=before_time,
            after_time=after_time, truncate=(after_time is None),
        )

    # ----------------------------------------------------------
    # 内部: Coordinator 远程批量拉取
    # ----------------------------------------------------------

    def _get_klines_remote(
        self,
        symbols: List[str],
        timeframe: str,
        limit: int,
        adj: str = "qfq",
    ) -> Dict[str, List[Dict[str, Any]]]:
        """批量走 Coordinator 远程拉取。timeframe 应已 normalize。"""
        if not symbols:
            return {}

        coord_results, failed = get_coordinator().coordinate_kline(
            symbols=[normalize_cn_code(s) for s in symbols],
            timeframe=timeframe,
            limit=limit,
            cb=self.circuit_breaker,
            market="CNStock",
            timeout=20,
            adj=adj,
        )

        result: Dict[str, List[Dict[str, Any]]] = {}
        for sym, bars in coord_results.items():
            raw = _strip_cn_prefix(sym)
            result[raw] = clean_klines(bars, timeframe)

        if failed:
            logger.warning(f"[远程批量] {len(failed)} 只失败: {failed[:5]}...")

        return result

    # ----------------------------------------------------------
    # 内部: 低周期聚合 fallback
    # ----------------------------------------------------------

    _AGG_FALLBACK = {
        '5m':  ('1m',  5),
        '30m': ('15m', 2),
        '1H':  ('30m', 2),
        '2H':  ('1H',  2),
        '4H':  ('1H',  4),
    }

    def _try_aggregate_lower(
        self, tf: str, code: str, limit: int, adj: str
    ) -> List[Dict[str, Any]]:
        """从低周期聚合目标周期（fallback）。tf 应已 normalize。"""
        fallback = self._AGG_FALLBACK.get(tf)
        if not fallback:
            return []
        source_tf, group_size = fallback
        source_limit = limit * group_size + group_size
        try:
            coord_results, _ = get_coordinator().coordinate_kline(
                symbols=[code],
                timeframe=source_tf,
                limit=source_limit,
                cb=self.circuit_breaker,
                market="CNStock",
                timeout=20,
                adj=adj,
            )
            source_bars = coord_results.get(code, [])
        except Exception:
            return []
        if not source_bars:
            return []
        source_bars.sort(key=lambda x: x['time'])
        return self._aggregate_fixed_window(source_bars, group_size, limit)

    @staticmethod
    def _aggregate_fixed_window(
        source_klines: List[Dict], group_size: int, limit: int
    ) -> List[Dict[str, Any]]:
        """将低周期 K 线按固定窗口聚合为高周期。"""
        result = []
        total = len(source_klines)
        for i in range(0, total, group_size):
            chunk = source_klines[i:i + group_size]
            result.append({
                'time': chunk[0]['time'],
                'open': float(chunk[0].get('open', 0)),
                'high': max(float(b.get('high', 0)) for b in chunk),
                'low': min(float(b.get('low', 0)) for b in chunk),
                'close': float(chunk[-1].get('close', 0)),
                'volume': round(sum(float(b.get('volume', 0)) for b in chunk), 2),
            })
        return result[-limit:] if len(result) > limit else result
