"""
中国A股数据源

═══════════════════════════════════════════════════════════════
  核心设计: get_ticker 负责"取"，get_kline 负责"发"
═══════════════════════════════════════════════════════════════

职责分离:
  get_ticker()  → 只管从远程取实时行情，取到后写入 ticker 缓存
  get_kline()   → 只管组装 K 线数据返回给调用方

DB 策略:
  - DB 只存 15m 基线数据（唯一需要持久化的周期）
  - 其余周期（1D/30m/1h/1W/1M）全部从 15m 实时聚合，不单独存
  - 1m/5m 不走 DB，直接远程

盘中 vs 盘后（关键设计）:
  ┌─────────┬──────────────────────────────────────────────────────┐
  │ 盘中     │ 9:15-15:00 交易时段                                  │
  │         │ 1. 强制远程拉取当日 15m 全量，覆盖 DB 中当日部分       │
  │         │ 2. ticker 缓存被动补充最后一根 bar 的实时数据         │
  │         │    （15 分钟间隔内有误差，但够用）                    │
  │         │ 3. 日线/30m/1h 等从已刷新的 15m 聚合，天然包含最新   │
  ├─────────┼──────────────────────────────────────────────────────┤
  │ 盘后     │ 15:15 之后，最后一次 15m 更新完成后                   │
  │         │ 直接读 DB，不折腾。误差交给校准程序处理。              │
  └─────────┴──────────────────────────────────────────────────────┘

为什么不"先出后进"（DB 有旧数据就直接返回）:
  盘中时 DB 里的当日 15m 是过时快照——最后一根 bar 可能还在走。
  如果直接返回 DB，调用方拿到的是 15 分钟前的数据。
  所以盘中必须先拿远程数据覆盖当日部分，再返回。

ticker 缓存的作用:
  远程 15m 数据最多 15 分钟更新一次，但 ticker 是实时的。
  在一根 15m bar 还没结束时，ticker 能提供 high/low/close 的实时值。
  这是"最后一公里"的补充，精度有限但延迟最低。
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
from app.data_sources.coordinator import get_coordinator
from app.data_sources.kline_clean import clean_klines
from app.utils.logger import get_logger

logger = get_logger(__name__)

_TZ_CN = timezone(timedelta(hours=8))

# ticker 缓存最大条目数。每条约 40 字节，2000 条 ≈ 80KB。
_CACHE_MAX_ENTRIES = 2000


# ================================================================
# 工具函数
# ================================================================

def _strip_cn_prefix(code: str) -> str:
    """去掉 A 股代码的市场前缀（SH/SZ/BJ），返回纯 6 位数字代码。"""
    s = (code or "").strip()
    if len(s) == 8 and s[:2].upper() in ("SH", "SZ", "BJ") and s[2:].isdigit():
        return s[2:]
    return s


def _dt_to_ts(dt) -> int:
    """db_market 返回的 datetime → Unix 时间戳（秒）。统一用 naive datetime。"""
    if isinstance(dt, (int, float)):
        return int(dt)
    if isinstance(dt, datetime):
        return int(dt.timestamp())
    return 0


def _is_market_hours() -> bool:
    """判断当前是否为 A 股交易时段。

    范围: 9:15（集合竞价开始）~ 15:00（收盘）。
    午休 11:30-13:00 也算，因为这段时间数据仍有参考价值。
    """
    from app.utils.trading_calendar import is_trading_day_today
    if not is_trading_day_today():
        return False
    now = datetime.now(_TZ_CN)
    t = now.time()
    return dt_time(9, 15) <= t <= dt_time(15, 0)


def _today_str() -> str:
    return datetime.now(_TZ_CN).strftime("%Y-%m-%d")


def _today_ts() -> int:
    """今天 00:00 的 Unix 时间戳（秒）。用于过滤"当日 bar"。"""
    dt = datetime.strptime(_today_str(), "%Y-%m-%d")
    return int(dt.timestamp())


def _day_ts(day_str: str) -> int:
    """'YYYY-MM-DD' → 当天 00:00 的 Unix 时间戳（秒）。"""
    dt = datetime.strptime(day_str, "%Y-%m-%d")
    return int(dt.timestamp())


def _latest_trading_day_ts() -> int:
    """从交易日历获取最近一个交易日的时间戳。"""
    from app.utils.trading_calendar import is_trading_day_today, prev_trading_day
    if is_trading_day_today():
        return _today_ts()
    try:
        return _day_ts(prev_trading_day())
    except Exception:
        return _today_ts()


# ================================================================
# 实时行情缓存
# ================================================================
#
# 每只股票一个缓存行，存 ticker 实时数据（high/low/price/volume）。
# 由 get_ticker() 写入，由 _apply_ticker_to_last_bar() 读取。
#
# "惰性"的含义:
#   不是每次都覆盖，而是只往极端方向写——
#   high 只往更高走，low 只往更低走，price 始终取最新。
#   这样即使 ticker 短暂波动，也不会丢失已观测到的极端值。
#

class _QuoteCacheEntry:
    """单只股票的惰性行情缓存条目。"""
    __slots__ = ('high', 'low', 'price', 'volume', 'ts')

    def __init__(self):
        self.high: float = 0.0
        self.low: float = 0.0
        self.price: float = 0.0
        self.volume: float = 0.0
        self.ts: float = 0.0

    def update_from_ticker(self, ticker: Dict[str, Any]):
        """从 ticker 数据更新缓存（惰性: 只往极端方向写）。"""
        last = float(ticker.get('last', 0) or 0)
        if last <= 0:
            return
        now = time.time()
        high = float(ticker.get('high', 0) or 0)
        low = float(ticker.get('low', 0) or 0)
        vol = float(ticker.get('volume', 0) or ticker.get('baseVolume', 0) or 0)

        # 首次填充 → 直接覆盖
        if self.price <= 0:
            self.high = max(high, last) if high > 0 else last
            self.low = min(low, last) if low > 0 else last
            self.price = last
            self.volume = vol
            self.ts = now
            return

        # 惰性更新: high/low 只往极端方向写，price 始终取最新
        if high > self.high:
            self.high = high
        if low > 0 and (self.low <= 0 or low < self.low):
            self.low = low
        self.price = last
        if vol > 0:
            self.volume = vol
        self.ts = now


class RealtimeQuoteCache:
    """全局 ticker 缓存（线程安全）。

    key 是纯 6 位数字代码（如 "600000"），不含市场前缀。
    由 get_ticker() 写入，由 _apply_ticker_to_last_bar() 读取。

    容量: 最多 _CACHE_MAX_ENTRIES 条，满时淘汰 ts 最老的。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._entries: Dict[str, _QuoteCacheEntry] = {}

    def _evict_if_full(self):
        """容量满时淘汰 ts 最老的条目。调用方需持锁。"""
        if len(self._entries) < _CACHE_MAX_ENTRIES:
            return
        oldest_key = min(self._entries, key=lambda k: self._entries[k].ts or float('inf'))
        del self._entries[oldest_key]

    def _put(self, symbol: str, result: Dict[str, Any]):
        """将 ticker 结果写入缓存。由 get_ticker / _get_tickers 调用。"""
        raw = _strip_cn_prefix(symbol)
        if not result or float(result.get('last', 0) or 0) <= 0:
            return
        with self._lock:
            entry = self._entries.get(raw)
            if entry is None:
                self._evict_if_full()
                entry = _QuoteCacheEntry()
                self._entries[raw] = entry
            entry.update_from_ticker(result)

    def get_or_fetch(self, symbol: str, ds: 'CNStockDataSource') -> Optional[_QuoteCacheEntry]:
        """获取缓存行情，无缓存则实时拉取。"""
        raw = _strip_cn_prefix(symbol)
        with self._lock:
            entry = self._entries.get(raw)
            if entry and entry.price > 0:
                return entry

        try:
            ticker = ds.get_ticker(raw)
            if not ticker or float(ticker.get('last', 0) or 0) <= 0:
                return None
        except Exception:
            return None

        with self._lock:
            entry = self._entries.get(raw)
            if entry is None:
                self._evict_if_full()
                entry = _QuoteCacheEntry()
                self._entries[raw] = entry
            entry.update_from_ticker(ticker)
            return entry

    def batch_fetch(self, symbols: List[str], ds: 'CNStockDataSource') -> Dict[str, _QuoteCacheEntry]:
        """批量获取/刷新行情缓存。一次 batch_quote HTTP 调用覆盖多只。"""
        need_refresh: List[str] = []
        result: Dict[str, _QuoteCacheEntry] = {}

        with self._lock:
            for sym in symbols:
                raw = _strip_cn_prefix(sym)
                entry = self._entries.get(raw)
                if entry and entry.price > 0:
                    result[raw] = entry
                else:
                    need_refresh.append(raw)

        if not need_refresh:
            return result

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
                    self._evict_if_full()
                    entry = _QuoteCacheEntry()
                    self._entries[raw] = entry
                entry.update_from_ticker(ticker)
                result[raw] = entry

        return result


# ================================================================
# DB 行情桥接层
# ================================================================

class DBKlineBridge:
    """DB 行情桥接层 — 封装 db_market.py 的读写逻辑。

    核心职责:
    1. 从 DB 读 15m，聚合成目标周期返回
    2. ticker 补充最后一根 bar（_apply_ticker_to_last_bar）
    3. DB 缺 bar 时触发 backfill_db.run_once() 全盘补齐

    回填策略:
      不管下游怎么完成，只要缺 15m bar 就触发 backfill_db 找回来。
      backfill_db 负责全盘拉取 + 先删后写，桥接层只管触发。
    """

    def __init__(self, ds: 'CNStockDataSource'):
        self._ds = ds
        self._mgr = None
        self._writer = None
        self._quote_cache = RealtimeQuoteCache()
        self._init_lock = threading.Lock()
        self._init_attempted = False

    def _ensure_init(self):
        """惰性初始化 db_market（只尝试一次，失败后不再重试）。"""
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

    # ────────────────────────────────────────────────────────────
    # 主入口: get_kline / get_kline_batch
    # ────────────────────────────────────────────────────────────

    def get_kline(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """取 K 线数据。

        周期路由:
          1m / 5m → 直接远程（DB 不存低周期）
          15m     → DB + ticker 补充
          1D      → 从 15m 聚合
          30m~4h  → 从 15m 聚合
          1W / 1M → 15m → 日 → 周/月，两级聚合

        缺 bar 就触发 backfill_db.run_once()，不管下游怎么补齐。
        """
        adj = "qfq"
        raw = _strip_cn_prefix(symbol)
        tf = normalize_chart_timeframe(timeframe)
        lim = max(int(limit or 300), 1)
        in_trading = _is_market_hours()

        # 低周期直接远程，不走 DB
        if tf in ("1m", "5m"):
            return self._ds._get_kline_remote(
                raw, tf, lim, before_time, after_time, adj
            )

        if not self.available:
            return self._ds._get_kline_remote(
                raw, tf, limit, before_time, after_time, adj
            )

        # ── 缺 bar 就触发全盘回填 ──
        self._trigger_backfill_if_needed(raw, tf)

        # ── 从 DB 取数据 ──
        db_bars = self._fetch_from_db(raw, tf, lim)

        if db_bars:
            if tf in self._INTRADAY_BUCKET_SEC:
                cleaned = db_bars
            else:
                cleaned = clean_klines(db_bars, tf)

            # 盘中 15m: ticker 补充最后一根 bar
            if tf == "15m" and in_trading:
                self._apply_ticker_to_last_bar(raw, cleaned)

            out = self._ds.filter_and_limit(
                cleaned, limit=lim, before_time=before_time,
                after_time=after_time, truncate=(after_time is None),
            )
            if out:
                logger.info(f"[DB桥接] {raw} tf={tf} DB命中 bars={len(out)}")
                return out

        # ── DB 无数据（历史首次查询）→ 远程全量拉取 ──
        return self._remote_with_backfill(raw, tf, lim, before_time, after_time, adj)

    def get_kline_batch(
        self,
        symbols: List[str],
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """批量取 K 线。缺 bar 就触发 backfill_db 补齐。"""
        if not symbols:
            return {}

        tf = normalize_chart_timeframe(timeframe)
        lim = max(int(limit or 300), 1)
        adj = "qfq"
        in_trading = _is_market_hours()

        if tf in ("1m", "5m"):
            return self._ds._get_klines_remote(symbols, tf, lim, adj)

        if not self.available:
            return self._ds._get_klines_remote(symbols, tf, lim, adj)

        # ── 缺 bar 就触发全盘回填 ──
        for sym in symbols:
            self._trigger_backfill_if_needed(_strip_cn_prefix(sym), tf)

        result: Dict[str, List[Dict[str, Any]]] = {}
        need_remote: List[str] = []

        for sym in symbols:
            raw = _strip_cn_prefix(sym)
            db_bars = self._fetch_from_db(raw, tf, lim)

            if db_bars:
                if tf in self._INTRADAY_BUCKET_SEC:
                    cleaned = db_bars
                else:
                    cleaned = clean_klines(db_bars, tf)
                if tf == "15m" and in_trading:
                    self._apply_ticker_to_last_bar(raw, cleaned)
                out = self._ds.filter_and_limit(
                    cleaned, limit=lim, before_time=before_time,
                    after_time=after_time, truncate=(after_time is None),
                )
                if out:
                    result[raw] = out
                    continue

            need_remote.append(raw)

        if need_remote:
            remote_results = self._ds._get_klines_remote(need_remote, tf, lim, adj)
            for raw, bars in remote_results.items():
                out = self._ds.filter_and_limit(
                    bars, limit=lim, before_time=before_time,
                    after_time=after_time, truncate=(after_time is None),
                )
                result[raw] = out
            logger.info(
                f"[DB桥接批量] tf={tf} 总={len(symbols)} "
                f"DB命中={len(result) - len(need_remote)} 远程={len(need_remote)}"
            )

        return result

    # ────────────────────────────────────────────────────────────
    # 缺 bar 检测 + backfill 触发
    # ────────────────────────────────────────────────────────────

    def _trigger_backfill_if_needed(self, raw: str, tf: str):
        """触发后台同步。调度逻辑在 backfill_db.py 中。

        每次调用都触发 trigger_sync()，由 backfill_db 的 _sync_running 锁
        保证同一时间只有一个线程在运行，不阻塞调用方。
        """
        self._backfill_db()

    def _apply_ticker_to_last_bar(self, raw: str, bars: List[Dict[str, Any]]):
        """盘中: 用 ticker 缓存补充最后一根 15m bar 的实时数据。

        场景: 一根 15m bar 还没结束（比如 10:00-10:15 这根，现在是 10:08），
        DB 里这根 bar 的 close 是 10:00 的价格，但 ticker 有 10:08 的实时价格。
        用 ticker 补上。

        技巧: 判断最后一根 bar 是否属于"当前 15 分钟窗口"。
        - 属于当前窗口 → 用 ticker 更新 high/low/close/volume
        - 不属于（ticker 已进入新窗口但还没新 bar）→ 追加一根临时 bar
          下次 backfill 会覆盖它。
        """
        if not bars:
            return

        with self._quote_cache._lock:
            entry = self._quote_cache._entries.get(raw)
            if not entry or entry.price <= 0:
                return

            last_bar = bars[-1]
            bar_ts = last_bar.get("time", 0)

            # 当前 15 分钟窗口的起始时间（向下取整到 900 秒）
            now_ts = int(time.time())
            current_window_start = (now_ts // 900) * 900

            if bar_ts < current_window_start:
                # 最后一根 bar 是上一个窗口的，ticker 已经进入新窗口
                # → 追加一根临时 bar，等下次 backfill 会被全量数据覆盖
                bars.append({
                    "time": current_window_start,
                    "open": entry.price,
                    "high": entry.high,
                    "low": entry.low,
                    "close": entry.price,
                    "volume": entry.volume,
                })
            else:
                # 最后一根 bar 就是当前窗口 → 用 ticker 补充实时值
                if entry.high > last_bar.get("high", 0):
                    last_bar["high"] = round(entry.high, 4)
                if entry.low > 0 and (last_bar.get("low", 0) <= 0 or entry.low < last_bar["low"]):
                    last_bar["low"] = round(entry.low, 4)
                last_bar["close"] = round(entry.price, 4)
                if entry.volume > 0:
                    last_bar["volume"] = round(entry.volume, 2)

    # ────────────────────────────────────────────────────────────
    # DB 读取 + 周期聚合
    # ────────────────────────────────────────────────────────────

    # DB 只存 15m，其余周期全部在 Python 内存中聚合。
    # 统一从 15m 表读取，避免 SQL 聚合的大小写/时区/分区发现问题。
    _AGG_NEED_15M = {"30m", "1H", "2H", "4H", "1D", "1W", "1M"}

    def _fetch_from_db(self, raw: str, tf: str, limit: int) -> List[Dict[str, Any]]:
        """从 db_market 读取 15m K 线，在内存中聚合成目标周期。

        所有非 15m 周期统一走: query 15m → Python 聚合。
        """
        try:
            if tf == "15m":
                rows = self._writer.query("CNStock", raw, "15m", limit=limit)
            elif tf in ("1D", "1W", "1M"):
                rows = self._writer.query("CNStock", raw, "15m", limit=limit * 16 + 200)
            else:
                # 30m / 1h / 2h / 4h → 每天最多 16 根 15m，取够天数
                rows = self._writer.query("CNStock", raw, "15m", limit=limit * 16 + 50)
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

        if tf == "15m":
            return bars
        elif tf == "1D":
            return self._aggregate_15m_to_daily(bars, limit)
        elif tf in ("1W", "1M"):
            daily = self._aggregate_15m_to_daily(bars, limit * 8)
            if tf == "1W":
                return self._aggregate_daily_to_weekly(daily, limit)
            else:
                return self._aggregate_daily_to_monthly(daily, limit)
        else:
            return self._aggregate_15m_to_intraday(bars, tf, limit)

    @staticmethod
    def _aggregate_15m_to_daily(bars: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
        """15m → 日线。按北京时间日期分组，OHLCV 合并。"""
        if not bars:
            return []
        groups: Dict[int, List[Dict]] = {}
        order: List[int] = []
        for b in bars:
            ts = b.get("time", 0)
            if ts <= 0:
                continue
            day_ts = int(datetime.fromtimestamp(ts).replace(
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
        """日线 → 周线。按 ISO 周（年, 周号）分组。"""
        if not daily_bars:
            return []
        groups: Dict[tuple, List[Dict]] = {}
        order: List[tuple] = []
        for b in daily_bars:
            ts = b.get("time", 0)
            if ts <= 0:
                continue
            dt = datetime.fromtimestamp(ts)
            key = dt.isocalendar()[:2]
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
        """日线 → 月线。按年月分组。"""
        if not daily_bars:
            return []
        groups: Dict[int, List[Dict]] = {}
        order: List[int] = []
        for b in daily_bars:
            ts = b.get("time", 0)
            if ts <= 0:
                continue
            dt = datetime.fromtimestamp(ts)
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

    # 15m → 盘中周期聚合（按 bar.time 对齐，天然跳过午休）
    _INTRADAY_BUCKET_SEC = {
        "30m": 1800, "1H": 3600, "2H": 7200, "4H": 14400,
    }

    @staticmethod
    def _aggregate_15m_to_intraday(
        bars: List[Dict[str, Any]], tf: str, limit: int
    ) -> List[Dict[str, Any]]:
        """15m → 30m/1h/2h/4h。按 bar.time 向下取整到 bucket 边界分组。

        因为 15m bar 本身只在交易时段存在（9:30-11:30, 13:00-15:00），
        分桶后自然跳过午休和盘后，不会产生空桶。
        """
        if not bars:
            return []
        sec = DBKlineBridge._INTRADAY_BUCKET_SEC.get(tf)
        if sec is None:
            return bars

        groups: Dict[int, List[Dict]] = {}
        order: List[int] = []
        for b in bars:
            ts = b.get("time", 0)
            if ts <= 0:
                continue
            bucket = ts - (ts % sec)
            if bucket not in groups:
                groups[bucket] = []
                order.append(bucket)
            groups[bucket].append(b)

        result = []
        for bucket_ts in order:
            chunk = groups[bucket_ts]
            if not chunk:
                continue
            result.append({
                "time": bucket_ts,
                "open": float(chunk[0].get("open", 0)),
                "high": max(float(b.get("high", 0)) for b in chunk),
                "low": min(float(b.get("low", 0)) for b in chunk),
                "close": float(chunk[-1].get("close", 0)),
                "volume": round(sum(float(b.get("volume", 0)) for b in chunk), 2),
            })
        return result[-limit:] if len(result) > limit else result

    # ────────────────────────────────────────────────────────────
    # 远程拉取 + DB 回填
    # ────────────────────────────────────────────────────────────

    def _remote_with_backfill(
        self, raw: str, tf: str, limit: int,
        before_time: Optional[int], after_time: Optional[int], adj: str
    ) -> List[Dict[str, Any]]:
        """DB 无数据时的 fallback: 远程全量拉取，同时触发全盘回填补齐其他标的。"""
        remote_bars = self._ds._get_kline_remote(raw, tf, limit, adj=adj)
        if not remote_bars:
            return []

        # 触发全盘回填，让其他缺数据的标的也能补齐
        self._backfill_db()

        out = self._ds.filter_and_limit(
            remote_bars, limit=limit, before_time=before_time,
            after_time=after_time, truncate=(after_time is None),
        )
        logger.info(f"[DB桥接] {raw} tf={tf} 远程补充 bars={len(out)}")
        return out

    def _backfill_db(self):
        """触发后台同步。由 backfill_db._sync_running 锁保证唯一。"""
        try:
            from app.data_sources.backfill_db import trigger_sync
            trigger_sync()
        except Exception as e:
            logger.debug(f"[DB桥接] 触发同步异常: {e}")

    def backfill_all_market(self):
        """全市场回填 — 委托给 backfill_db.run_once()。"""
        from app.data_sources.backfill_db import run_once
        return run_once()


# ================================================================
# 数据源类
# ================================================================

class CNStockDataSource(BaseDataSource):
    """A股数据源。

    职责分离:
      get_ticker → 只管取行情，取到后写入 ticker 缓存
      get_kline  → 只管组装 K 线，盘中刷新 + ticker 补充 + 聚合
    """

    name = "CNStock/multi-source"

    def __init__(self):
        self.circuit_breaker = get_realtime_circuit_breaker()
        self._db_bridge = DBKlineBridge(self)

    # ── get_ticker: 只负责取行情，取到后写入 ticker 缀存 ──

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """获取实时行情。取到后自动写入 ticker 缓存供 get_kline 使用。"""
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
            # 写入 ticker 缓存，供 get_kline 的 _apply_ticker_to_last_bar 使用
            self._db_bridge._quote_cache._put(raw, result)
            return result

        logger.warning(f"[行情] 所有数据源均失败: {symbol}")
        return {"last": 0, "symbol": raw}

    def _get_tickers(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """批量获取实时行情。走 Coordinator 批量行情调度，一次 HTTP 取多只。"""
        from app.data_sources.coordinator import get_realtime_circuit_breaker
        if not symbols:
            return {}
        # coordinate_batch_quotes 内部会加前缀、返回时去前缀，直接透传即可
        raw_result = get_coordinator().coordinate_batch_quotes(
            symbols=symbols,
            cb=get_realtime_circuit_breaker(),
            market="CNStock",
        )
        if not raw_result:
            return {}
        # raw_result key 已是纯数字，写入 ticker 缓存
        for k, v in raw_result.items():
            if isinstance(v, dict):
                self._db_bridge._quote_cache._put(k, v)
        return raw_result

    # ── get_kline: 负责发 K 线数据 ──

    def get_kline(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """获取 K 线数据。盘中刷新 + 盘后直读，支持逗号分隔的批量模式。"""
        # 批量模式
        if ',' in symbol:
            symbols = [s.strip() for s in symbol.split(',') if s.strip()]
            if not symbols:
                return []
            batch = self._db_bridge.get_kline_batch(
                symbols, timeframe, limit, before_time, after_time
            )
            merged = []
            for sym in symbols:
                merged.extend(batch.get(_strip_cn_prefix(sym), []))
            return merged

        # 单只模式
        return self._db_bridge.get_kline(
            symbol, timeframe, limit, before_time, after_time
        )

    # ── 远程拉取（Coordinator 调度）──

    def _get_kline_remote(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
        adj: str = "qfq",
    ) -> List[Dict[str, Any]]:
        """单只远程拉取。不经过 DB 层，直接走 Coordinator。"""
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

    def _get_klines_remote(
        self,
        symbols: List[str],
        timeframe: str,
        limit: int,
        adj: str = "qfq",
    ) -> Dict[str, List[Dict[str, Any]]]:
        """批量远程拉取。"""
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
            result[_strip_cn_prefix(sym)] = clean_klines(bars, timeframe)

        if failed:
            logger.warning(f"[远程批量] {len(failed)} 只失败: {failed[:5]}...")

        return result

    # ── 低周期聚合 fallback ──

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
        """当目标周期远程无数据时，尝试从低周期聚合（fallback）。"""
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
