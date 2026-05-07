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
from app.data_sources.coordinator import get_coordinator, Coordinator_direct_call
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
# 实时行情惰性比对缓存
# ================================================================
#
# 每只股票一个缓存行，存三样东西:
#   1. ticker 实时数据（high/low/price/volume）— 惰性更新
#   2. 最后一根远端 15m bar 的结束时间（remote_ts）— 用于判断是否需要刷新
#   3. 远端数据的时间戳（ts）— 写入时间
#
# 刷新逻辑（纯惰性，无后台线程）:
#   - get_ticker 被调 → 写入 ticker 数据
#   - get_kline 被调 → 检查 remote_ts，过期则调 _refresh_today_15m
#   - 远端 15m bar 的结束时间 = bar.time + 900 秒
#   - 当前时间 > remote_ts → 说明当前窗口有新数据可拉，标记过期
#
# "惰性"的含义:
#   不是每次都覆盖，而是只往极端方向写——
#   high 只往更高走，low 只往更低走，price 始终取最新。
#   这样即使 ticker 短暂波动，也不会丢失已观测到的极端值。
#

class _QuoteCacheEntry:
    """单只股票的惰性行情缓存条目。

    每个 entry 对应一只股票，包含:
    - ticker 实时数据（high/low/price/volume）
    - 最后一根远端 15m bar 的结束时间（remote_ts）
    """
    __slots__ = ('high', 'low', 'price', 'volume', 'remote_ts', 'ts')

    def __init__(self):
        self.high: float = 0.0
        self.low: float = 0.0
        self.price: float = 0.0
        self.volume: float = 0.0
        self.remote_ts: float = 0.0  # 最后一根远端 15m bar 的结束时间（bar.time + 900）
        self.ts: float = 0.0         # 写入时间

    @property
    def is_remote_outdated(self) -> bool:
        """远端 15m 数据是否过期（当前窗口有新数据可拉）。

        判断逻辑: 当前时间 > 最后一根远端 bar 的结束时间。
        remote_ts 是远端返回的 bar.time + 900 秒，不是本地更新时间。
        """
        if self.remote_ts <= 0:
            return True  # 从未拉过远端数据
        return time.time() > self.remote_ts

    def update_from_ticker(self, ticker: Dict[str, Any]):
        """从 ticker 数据更新缓存（惰性: 只往极端方向写）。

        只更新 ticker 字段，不动 remote_ts。
        remote_ts 只由 _refresh_today_15m 更新。
        """
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

    def reset_ticker(self):
        """重置 ticker 字段（进入新 15m 窗口时调用）。

        技巧: 新窗口开始时，旧的 ticker high/low 属于上一个窗口，
        必须清掉，否则会跟新窗口的数据混在一起。
        只清 ticker 字段，remote_ts 不动。
        """
        self.high = 0.0
        self.low = 0.0
        self.price = 0.0
        self.volume = 0.0
        self.ts = 0.0


class RealtimeQuoteCache:
    """全局 ticker 缓存（线程安全）。

    key 是纯 6 位数字代码（如 "600000"），不含市场前缀。
    由 get_ticker() 写入，由 get_kline() 的 _apply_ticker_to_last_bar() 读取。

    容量: 最多 _CACHE_MAX_ENTRIES 条，满时淘汰 remote_ts 最老的（最久没更新远端数据的）。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._entries: Dict[str, _QuoteCacheEntry] = {}

    def _evict_if_full(self):
        """容量满时淘汰 remote_ts 最老的条目。调用方需持锁。"""
        if len(self._entries) < _CACHE_MAX_ENTRIES:
            return
        # 找 remote_ts 最小的（最久没拉过远端数据的）
        oldest_key = min(self._entries, key=lambda k: self._entries[k].remote_ts or float('inf'))
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
        """获取缓存行情，远端过期则惰性刷新（锁外取远程，避免持锁阻塞）。"""
        raw = _strip_cn_prefix(symbol)
        with self._lock:
            entry = self._entries.get(raw)
            if entry and not entry.is_remote_outdated:
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
                if entry and not entry.is_remote_outdated:
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
    1. 盘中强制刷新当日 15m（_refresh_today_15m）
    2. ticker 补充最后一根 bar（_apply_ticker_to_last_bar）
    3. 所有非 15m 周期从 15m 聚合
    4. 远程数据回填 DB（只存 15m）
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
          15m     → DB + 盘中远程覆盖 + ticker 补充
          1D      → 从 15m 聚合（每天约 16 根 15m bar）
          30m~4h  → 从 15m 聚合
          1W / 1M → 15m → 日 → 周/月，两级聚合

        盘中 vs 盘后:
          盘中 → 先 _refresh_today_15m 覆盖当日 DB，再读 DB，再 ticker 补充
          盘后 → 直接读 DB（最后一次 15m 更新约在 15:15 完成）
        """
        adj = "qfq"
        raw = _strip_cn_prefix(symbol)
        tf = normalize_chart_timeframe(timeframe)
        lim = max(int(limit or 300), 1)
        in_trading = _is_market_hours()  # 算一次，后面复用

        # 低周期直接远程，不走 DB
        if tf in ("1m", "5m"):
            return self._ds._get_kline_remote(
                raw, tf, lim, before_time, after_time, adj
            )

        if not self.available:
            return self._ds._get_kline_remote(
                raw, tf, lim, before_time, after_time, adj
            )

        # ── 盘中: 远端 15m 过期则刷新 ──
        # 技巧: 用缓存行的 remote_ts 判断是否需要刷新，不是每次 get_kline 都拉。
        # remote_ts 是远端最后一根 bar 的结束时间（bar.time + 900），
        # 当前时间 > remote_ts 说明当前窗口有新数据可拉。
        if in_trading:
            with self._quote_cache._lock:
                entry = self._quote_cache._entries.get(raw)
                need_refresh = entry is None or entry.is_remote_outdated
            if need_refresh:
                self._refresh_today_15m(raw)

        # ── 从 DB 取数据 ──
        db_bars = self._fetch_from_db(raw, tf, lim)

        if db_bars:
            # 盘中聚合周期（30m/1h/2h/4h）的 bar 已对齐到 bucket 边界，
            # clean_klines 的期望时间点与之不匹配，会错误插入空 bar，跳过。
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
        """批量取 K 线。逻辑与单只一致，盘中按 remote_ts 刷新、盘后直接读 DB。"""
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

        # 盘中: 按 remote_ts 判断哪些需要刷新（不重复拉已刷新的）
        if in_trading:
            for sym in symbols:
                raw = _strip_cn_prefix(sym)
                with self._quote_cache._lock:
                    entry = self._quote_cache._entries.get(raw)
                    need_refresh = entry is None or entry.is_remote_outdated
                if need_refresh:
                    self._refresh_today_15m(raw)

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
                self._backfill_db(raw, tf, bars)
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
    # 盘中刷新: _refresh_today_15m + _apply_ticker_to_last_bar
    # ────────────────────────────────────────────────────────────

    def _refresh_today_15m(self, raw: str):
        """盘中: 从远程拉取当日 15m 全量，覆盖 DB 中当日部分。

        刷新后更新缓存行的 remote_ts（远端最后一根 bar 的结束时间）。
        下次 get_kline 检查 remote_ts，未过期就不再拉。
        """
        today_start = _today_ts()
        try:
            remote_bars = self._ds._get_kline_remote(raw, "15m", 200, adj="qfq")
        except Exception as e:
            logger.debug(f"[盘中刷新] 远程拉取失败 {raw}: {e}")
            return

        if not remote_bars:
            return

        # 只取当日的 bar，过滤掉历史数据
        today_bars = [b for b in remote_bars if b.get("time", 0) >= today_start]
        if not today_bars:
            return

        today_bars.sort(key=lambda b: b["time"])

        # 覆盖 DB 中当日部分
        self._backfill_db(raw, "15m", today_bars)

        # 更新缓存行: remote_ts = 最后一根 bar 的结束时间
        # 技巧: 用 bar.time + 900 作为过期判断依据。
        # 当前时间 > remote_ts 时，说明当前 15m 窗口有新数据可拉。
        last_bar_ts = today_bars[-1].get("time", 0)
        new_remote_ts = last_bar_ts + 900

        with self._quote_cache._lock:
            entry = self._quote_cache._entries.get(raw)
            if entry is None:
                self._quote_cache._evict_if_full()
                entry = _QuoteCacheEntry()
                self._quote_cache._entries[raw] = entry

            # 进入新 15m 窗口时，旧的 ticker high/low 属于上一个窗口，必须清掉
            if entry.remote_ts > 0 and new_remote_ts > entry.remote_ts:
                # 新窗口开始，重置 ticker 字段
                entry.reset_ticker()

            entry.remote_ts = new_remote_ts

        logger.debug(
            f"[盘中刷新] {raw} 当日 15m 覆盖 {len(today_bars)} 根, "
            f"remote_ts={new_remote_ts}"
        )

    def _apply_ticker_to_last_bar(self, raw: str, bars: List[Dict[str, Any]]):
        """盘中: 用 ticker 缓存补充最后一根 15m bar 的实时数据。

        场景: 一根 15m bar 还没结束（比如 10:00-10:15 这根，现在是 10:08），
        远程 15m 数据里这根 bar 的 close 是 10:00 的价格，
        但 ticker 有 10:08 的实时价格。用 ticker 补上。

        技巧: 需要判断最后一根 bar 是否属于"当前 15 分钟窗口"。
        - 属于当前窗口 → 用 ticker 更新 high/low/close/volume
        - 不属于（ticker 已进入新窗口但远程还没返回新 bar）→ 追加一根新 bar
          这根新 bar 的 open=close=price，后续远程数据会覆盖它。

        ticker 有效性: 只看 price > 0（有没有写入过），不设 TTL。
        因为 remote_ts 已经控制了远端刷新频率，ticker 数据在 15m 窗口内不会过期。
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
                # → 追加一根临时 bar，等下次 _refresh_today_15m 会被远程数据覆盖
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
        """DB 无数据时的 fallback: 远程全量拉取，能回填 DB 就回填。"""
        remote_bars = self._ds._get_kline_remote(raw, tf, limit, adj=adj)
        if not remote_bars:
            return []

        self._backfill_db(raw, tf, remote_bars)

        out = self._ds.filter_and_limit(
            remote_bars, limit=limit, before_time=before_time,
            after_time=after_time, truncate=(after_time is None),
        )
        logger.info(f"[DB桥接] {raw} tf={tf} 远程补充 bars={len(out)}")
        return out

    def _backfill_db(self, raw: str, tf: str, bars: List[Dict[str, Any]]):
        """将 K 线数据写入 DB。只存 15m，其余周期不存。

        写入保障:
          1. 时间校准 — 对齐到 15m 边界（整除 900）
          2. 数据清洗 — OHLC > 0, high >= low, volume 缺失填 0
          3. 先删后写 — 删除该 symbol + 时间范围旧数据，再 upsert
          4. 删未来数据 — time > now 的错误数据一律清除
          5. 唯一性 — (symbol, time) 唯一约束 + 先删后写双重保障
          6. 防重复 — 查库中最新时间，数据已足够新则跳过
        """
        if tf != "15m":
            return

        # 防重复: 查库中该 symbol 最新一条的时间，落在当前 15m 窗口内就跳过
        try:
            recent = self._writer.query("CNStock", raw, "15m", limit=1)
            if recent:
                last_ts = recent[-1].get("time")
                if isinstance(last_ts, datetime):
                    last_ts = int(last_ts.timestamp())
                # 最新数据在当前 15m 窗口内 → 不需要重复写
                if last_ts > 0 and (time.time() - last_ts) < 900:
                    return
        except Exception:
            pass  # 查不到就继续写

        _15M_SEC = 900
        now_ts = int(time.time())

        # Step 1: 清洗 + 校验 + 对齐
        seen: Dict[int, Dict] = {}
        for b in bars:
            ts = b.get("time", 0)
            if not isinstance(ts, (int, float)) or ts <= 0:
                continue
            ts = int(ts) - (int(ts) % _15M_SEC)  # 对齐到 15m 边界
            if ts <= 0:
                continue

            try:
                o = float(b.get("open", 0))
                h = float(b.get("high", 0))
                l = float(b.get("low", 0))
                c = float(b.get("close", 0))
                v = b.get("volume", 0)
                v = float(v) if v is not None and str(v).strip() not in ("", "-", "nan") else 0.0
            except (TypeError, ValueError):
                continue

            if o <= 0 or h <= 0 or l <= 0 or c <= 0:
                continue

            if h < l:
                h, l = l, h
            if h < max(o, c):
                h = max(o, c)
            if l > min(o, c):
                l = min(o, c)

            seen[ts] = {
                "time": ts,
                "open": round(o, 4), "high": round(h, 4),
                "low": round(l, 4), "close": round(c, 4),
                "volume": round(max(v, 0), 2),
            }

        if not seen:
            return

        sorted_bars = sorted(seen.values(), key=lambda x: x["time"])
        min_ts = sorted_bars[0]["time"]
        max_ts = sorted_bars[-1]["time"]

        records = [{"time": datetime.fromtimestamp(b["time"]), **{k: b[k] for k in ("open", "high", "low", "close", "volume")}} for b in sorted_bars]

        # Step 2: 删除该时间范围旧数据 + 未来数据
        try:
            pool = self._mgr._get_pool("CNStock")
            start_dt = datetime.fromtimestamp(min_ts)
            end_dt = datetime.fromtimestamp(max_ts + _15M_SEC)
            now_dt = datetime.now()
            with pool.connection() as conn:
                cur = conn.cursor()
                for year in set([start_dt.year, end_dt.year, now_dt.year]):
                    table = f"kline_15m_{year}"
                    try:
                        cur.execute(f'DELETE FROM "{table}" WHERE symbol = %s AND time >= %s AND time < %s', (raw, start_dt, end_dt))
                        cur.execute(f'DELETE FROM "{table}" WHERE symbol = %s AND time > %s', (raw, now_dt))
                    except Exception:
                        pass
                conn.commit()
        except Exception as e:
            logger.debug(f"[DB桥接] 清理旧数据失败 {raw}: {e}")

        # Step 3: upsert 写入
        try:
            result = self._writer.upsert("CNStock", raw, tf, records)
            logger.debug(
                f"[DB桥接] 回填 {raw}/{tf}: "
                f"+{result.get('inserted', 0)} ~{result.get('updated', 0)} "
                f"清洗后={len(records)}"
            )
        except Exception as e:
            logger.debug(f"[DB桥接] 回填失败 {raw}/{tf}: {e}")

    def backfill_all_market(self, batch_size: int = 400, bars_per_stock: int = 32):
        """全市场 15m 后台回填。

        拉取全市场 A 股的 15m K 线，分批写入 DB。
        盘中只写当日 bar，盘后写全量。

        Args:
            batch_size: 每批股票数量（默认 400）
            bars_per_stock: 每只拉取的 15m bar 数量（默认 32 ≈ 2 个交易日）
        """
        self._ensure_init()
        if not self.available:
            logger.error("[全量回填] DB 不可用")
            return

        # 获取全市场代码
        try:
            from app.data_sources.a_stock import AStockDataSource
            ds = AStockDataSource()
            raw_list = ds.get_all_stock_codes()
            codes = [str(it.get("stock_code", "")).strip() for it in raw_list
                     if str(it.get("stock_code", "")).strip() and len(str(it.get("stock_code", "")).strip()) == 6]
        except Exception as e:
            logger.error(f"[全量回填] 获取股票列表失败: {e}")
            return

        if not codes:
            logger.warning("[全量回填] 无股票代码")
            return

        from app.data_sources.circuit_breaker import get_realtime_circuit_breaker
        cb = get_realtime_circuit_breaker()
        in_trading = _is_market_hours()
        today_start = _today_ts() if in_trading else 0

        total = len(codes)
        success = fail = skip = 0
        logger.info(f"[全量回填] 开始: {total} 只, 批次={batch_size}, {'盘中' if in_trading else '盘后'}")

        for i in range(0, total, batch_size):
            batch = codes[i:i + batch_size]
            batch_num = i // batch_size + 1

            normalized = [normalize_cn_code(c) for c in batch]
            coord_results, failed = get_coordinator().coordinate_kline(
                symbols=normalized, timeframe="15m", limit=bars_per_stock,
                cb=cb, market="CNStock", timeout=25, adj="qfq",
            )

            for code in batch:
                bars = coord_results.get(normalize_cn_code(code), [])
                if not bars:
                    skip += 1
                    continue
                if in_trading:
                    bars = [b for b in bars if b.get("time", 0) >= today_start]
                    if not bars:
                        skip += 1
                        continue
                self._backfill_db(code, "15m", bars)
                success += 1

            fail += len(failed)
            logger.info(f"[全量回填] 批次 {batch_num}: 成功={success} 失败={fail} 跳过={skip}")
            if i + batch_size < total:
                time.sleep(1)

        logger.info(f"[全量回填] 完成: 总={total} 成功={success} 失败={fail} 跳过={skip}")


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
        """批量获取实时行情。透传 Provider 层 batch_quote 接口，一次 HTTP 取多只。"""
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
                        self._db_bridge._quote_cache._put(raw_key, v)
                    cleaned[raw_key] = v
                return cleaned
        return {}

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
