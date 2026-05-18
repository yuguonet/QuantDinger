"""
中国A股数据源 — 简化版

═══════════════════════════════════════════════════════════════
  设计思路：

  1. 分时线（1m/5m/15m/30m/1h/2h/4h）盘中无法获取 HL 值，
     所以不做 DB 缓存，全部直接走远端。

  2. 只有 1D / 1W 走 DB + TTL 混合流程：
     - 1W 先转成 1D 的 count，走完 1D 流程后再聚合回周线。

  3. Ticker（实时行情）：
     - 盘中（交易日 9:15~15:01）每次必拉，保证实时性
     - 非盘中优先走 TTL 缓存，未命中再拉远端
     - 合并当前股 + TTL 已有 symbols（归一化，最大 500）
     - 拉取结果写入 TTL 内存（无有效期，最旧先丢弃）

  4. Kline（1D / 1W）：
     ① lastbar = TTL 中的实时快照转 OHLCV
     ② 读 DB 中所有 1D bar
     ③ 快速路径：DB 数据够新（≥前一交易日）且有 lastbar
        → 去掉今日旧数据 + 拼 lastbar → 直接返回
     ④ 慢路径：从远端拉 1D
        → 合并 DB + 远端（远端优先覆盖）→ 归一化去重 → 写 DB
        → 根据 source 是否含今日 / lastbar 是否命中，三分支构造 out_kline
     ⑤ 1D 直接返回；1W 用日线聚合为 ISO 周线后返回

═══════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import threading
import time as _time
from datetime import datetime, time as dtime
from typing import Any, Dict, List, Optional, Tuple

from app.data_sources.base import BaseDataSource
from app.data_sources.asia_stock_kline import normalize_chart_timeframe
from app.data_sources.coordinator import get_coordinator
from app.data_sources.kline_clean import clean_klines
from app.data_sources.normalizer import strip_market_prefix
from app.utils.logger import get_logger
from app.utils.trading_calendar import is_trading_day, prev_trading_day

logger = get_logger(__name__)


# ================================================================
# TTL 快照缓存（进程级单例，无有效期）
#
# 为什么需要 TTL：
#   盘中 ticker 是实时价格，但远端接口有调用频率限制。
#   TTL 缓存让同一批 symbols 的行情可以在多次 get_kline 调用间复用，
#   避免每个股票单独拉一次 ticker。
#
# 淘汰策略：
#   最大 500 条，超限按写入时间丢弃最旧的。
#   没有固定有效期——盘中每次 get_tickers 会刷新，非盘中直接用缓存。
# ================================================================

class _SnapshotCache:
    """批量行情快照缓存，无有效期（TTL），最大 500 条，最旧先丢弃。"""

    _MAX_SIZE = 500

    def __init__(self):
        # 同时存 pure symbol 和原始 symbol 两种 key，方便查找
        self._quotes: Dict[str, Dict[str, Any]] = {}
        self._ts: Dict[str, float] = {}  # 每个 key 的写入时间，用于淘汰
        self._lock = threading.Lock()

    def symbols(self) -> List[str]:
        """返回 TTL 中所有去重后的 symbol（去市场前缀的纯代码）。"""
        with self._lock:
            seen = set()
            result = []
            for key in self._quotes:
                pure = strip_market_prefix(key)
                if pure not in seen:
                    seen.add(pure)
                    result.append(pure)
            return result

    def refresh(self, symbols: List[str]) -> None:
        """单股场景下的快捷拉取：调 coordinator_tickers 并写入 TTL。"""
        if not symbols:
            return
        coord = get_coordinator()
        try:
            quotes = coord.coordinate_tickers(
                symbols=symbols, market="CNStock", timeout=10,
            )
        except Exception as e:
            logger.debug(f"[快照] 批量拉取异常: {e}")
            return
        if quotes:
            self.write(quotes)

    def write(self, quotes: List[Dict[str, Any]]) -> None:
        """将行情数据写入 TTL 内存，超 500 条按最旧时间丢弃。"""
        now = _time.time()
        with self._lock:
            for q in quotes:
                sym = q.get("symbol", "")
                if not sym:
                    continue
                pure = strip_market_prefix(sym)
                # 同时写入 pure 和原始两个 key，查哪个都能命中
                for key in (pure, sym):
                    self._quotes[key] = q
                    self._ts[key] = now

            # 超限淘汰：按写入时间排序，最旧的先删
            if len(self._quotes) > self._MAX_SIZE:
                sorted_keys = sorted(self._ts, key=lambda k: self._ts[k])
                to_remove = len(self._quotes) - self._MAX_SIZE
                for k in sorted_keys[:to_remove]:
                    self._quotes.pop(k, None)
                    self._ts.pop(k, None)

    def get(self, symbol: str) -> Optional[Dict[str, Any]]:
        """从 TTL 内存中查找指定 symbol（pure 和原始 key 都尝试）。"""
        pure = strip_market_prefix(symbol) if symbol else symbol
        with self._lock:
            return self._quotes.get(pure) or self._quotes.get(symbol)


# 进程级单例，全局共享同一份 TTL 缓存
_snapshot_cache = _SnapshotCache()


# ================================================================
# 工具函数
# ================================================================

def _is_in_trading_hours() -> bool:
    """判断当前是否在交易时段内（交易日 9:15 < t <= 15:01）。

    为什么是 15:01：
      A 股 15:00 收盘，但收盘瞬间最后一笔成交可能延迟几秒，
      给 1 分钟缓冲确保能抓到收盘价。
    """
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    if not is_trading_day(today_str):
        return False
    t = now.time()
    return dtime(9, 15) < t <= dtime(15, 1)


def _prev_trading_day_ts() -> int:
    """前一交易日的 1D 时间戳（当天 00:00:00）。

    用途：判断 DB 数据是否足够新——如果 DB 最后一条 ≥ 前一交易日，
    说明 DB 已经包含了上一个交易日的收盘数据，可以走快速路径。
    """
    today_str = datetime.now().strftime("%Y-%m-%d")
    prev_td = prev_trading_day(today_str)
    return int(datetime.strptime(prev_td, "%Y-%m-%d").timestamp())


def _today_ts() -> int:
    """今日的 1D 时间戳（00:00:00），用于过滤/拼接今日数据。"""
    today_str = datetime.now().strftime("%Y-%m-%d")
    return int(datetime.strptime(today_str, "%Y-%m-%d").timestamp())


def _normalize_1d_bars(bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """1D 归一化：每天只留一条 bar，按时间排序。

    规则：同一日期出现多条时，后面的覆盖前面的。
    这样当 DB 旧数据和远端新数据合并时，远端（排在后面）会覆盖 DB。
    """
    if not bars:
        return bars
    by_date: Dict[str, Dict[str, Any]] = {}
    for bar in bars:
        dt = datetime.fromtimestamp(bar["time"])
        date_str = dt.strftime("%Y-%m-%d")
        by_date[date_str] = bar
    return sorted(by_date.values(), key=lambda b: b["time"])


def _bar_from_ticker(quote: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """将 ticker/快照数据转为 1D OHLCV bar。

    转换逻辑：
      - close = 最新价（优先 last → close → price）
      - open/high/low 如果快照里有就用，没有就用 close 填充
      - time 固定为今日 00:00（1D 粒度）
      - 价格为 0 或缺失则返回 None（无效数据）
    """
    if not quote:
        return None
    price = (
        quote.get("last")
        or quote.get("close")
        or quote.get("price")
        or 0
    )
    if not price or float(price) <= 0:
        return None

    price = float(price)
    today_str = datetime.now().strftime("%Y-%m-%d")
    bar_ts = int(datetime.strptime(today_str, "%Y-%m-%d").timestamp())

    return {
        "time": bar_ts,
        "open": float(quote.get("open", price)),
        "high": float(quote.get("high", price)),
        "low": float(quote.get("low", price)),
        "close": price,
        "volume": float(quote.get("volume", 0)),
    }


# ================================================================
# 主数据源类
# ================================================================

class CNStockDataSource(BaseDataSource):
    """A股数据源 — 简化版：仅 1D/1W 走 DB，其余直走远端。"""

    name = "CNStock/multi-source"

    # ── get_tickers: 批量行情（自选股列表）──

    def get_tickers(self, symbols: List[str]) -> List[Dict[str, Any]]:
        """批量获取实时行情，写入 TTL 缓存。

        流程：
          盘中 → 每次必拉（保证实时性），合并 TTL 已有一起拉
          非盘中 → 先查 TTL，全命中直接返回；有缺失才拉远端

        拉取时会把当前股 + TTL 已有 symbols 合并（归一化，最大 500），
        一次批量拉取，结果写入 TTL，返回全部结果。

        Args:
            symbols: 自选股列表（已归一化的 symbol）
        Returns:
            行情列表，每项含 symbol/open/high/low/close/volume/last 等
        """
        symbols = [s.strip() for s in symbols if s and s.strip()]
        if not symbols:
            return []

        # ── 非盘中：先查 TTL，全命中直接返回（省一次远端调用）──
        if not _is_in_trading_hours():
            cached_results: List[Dict[str, Any]] = []
            all_hit = True
            for sym in symbols:
                cached = _snapshot_cache.get(sym)
                if cached:
                    cached["symbol"] = sym
                    cached_results.append(cached)
                else:
                    all_hit = False
            if all_hit:
                return cached_results

        # ── 盘中必拉 / 非盘中有缺失 → 合并 TTL 已有 symbols 一起拉 ──
        # 合并的目的是：一次批量请求同时刷新自选股 + 之前缓存过的股票
        all_symbols = list(set(symbols + _snapshot_cache.symbols()))[:500]
        coord = get_coordinator()
        quotes_list = coord.coordinate_tickers(
            symbols=all_symbols, market="CNStock", timeout=10,
        )
        if not quotes_list:
            logger.warning(f"[行情] 批量拉取失败: {symbols}")
            # 非盘中降级：返回已有的缓存（有总比没有好）
            if not _is_in_trading_hours():
                return [q for q in [_snapshot_cache.get(s) for s in symbols] if q]
            return []

        # 写入 TTL（后续 get_kline 会用到 lastbar）
        _snapshot_cache.write(quotes_list)
        return quotes_list

    # ── get_ticker: 单股实时行情 ──

    def get_ticker(self, symbol) -> Dict[str, Any]:
        """获取单股实时行情。

        优先走 TTL 缓存，未命中时合并 TTL 已有 symbols 一起拉（顺带刷新缓存）。
        """
        if not symbol:
            return {"last": 0, "symbol": ""}

        sym = symbol.strip() if isinstance(symbol, str) else str(symbol)
        if not sym:
            return {"last": 0, "symbol": ""}

        # ── 先查 TTL ──
        cached = _snapshot_cache.get(sym)
        if cached:
            cached["symbol"] = sym
            return cached

        # ── TTL 没有，合并 TTL 已有 symbols 一起拉（顺带刷新缓存）──
        all_symbols = list(set([sym] + _snapshot_cache.symbols()))[:500]
        coord = get_coordinator()
        quotes_list = coord.coordinate_tickers(
            symbols=all_symbols, market="CNStock", timeout=10,
        )
        if not quotes_list:
            logger.warning(f"[行情] 所有数据源均失败: {sym}")
            return {"last": 0, "symbol": sym}

        # 写入 TTL
        _snapshot_cache.write(quotes_list)

        # 从 TTL 取当前股（write 时已按 pure/symbol 双 key 存入）
        result = _snapshot_cache.get(sym)
        if result:
            result["symbol"] = sym
            return result

        logger.warning(f"[行情] 拉取成功但未找到当前股: {sym}")
        return {"last": 0, "symbol": sym}

    # ── get_kline: K 线数据入口 ──

    def get_kline(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """获取 K 线数据。

        分流逻辑：
          1D / 1W → 走 DB + TTL 混合流程（有本地缓存，响应快）
          其它    → 直接走远端（分时线盘中无 HL，不值得缓存）
        """
        tf = normalize_chart_timeframe(timeframe)
        limit = max(int(limit or 300), 1)

        if tf == "1D":
            return self._get_kline_1d(symbol, limit)
        if tf == "1W":
            return self._get_kline_weekly(symbol, limit)

        # 1m/5m/15m/30m/1h/2h/4h — 直接走远端，不做 DB
        return self._get_kline_remote(symbol, tf, limit, before_time, after_time)

    # ================================================================
    # 1D 核心流程
    #
    # 两阶段策略：
    #   快速路径（步骤 1-3）：DB 数据够新 + TTL 有实时快照 → 直接拼接返回
    #   慢路径（步骤 4-6）：DB 数据不够或无 lastbar → 拉远端补满 → 合并写 DB
    # ================================================================

    def _get_kline_1d(
        self, symbol: str, limit: int,
    ) -> List[Dict[str, Any]]:
        """1D: DB + 远端补满 + lastbar。"""
        in_trading = _is_in_trading_hours()
        today_ts = _today_ts()
        logger.debug(f"[kline_1d] {symbol} start, in_trading={in_trading}, limit={limit}")

        # ── 步骤 1：尝试获取 lastbar（从 TTL 实时快照转 OHLCV）──
        # 只有盘中才有 lastbar，非盘中 lastbar = None
        lastbar = None
        if in_trading:
            quote = _snapshot_cache.get(symbol)
            if not quote:
                # TTL 没有 → 拉一次（refresh 会写入 TTL）
                _snapshot_cache.refresh([symbol])
                quote = _snapshot_cache.get(symbol)
            if quote:
                lastbar = _bar_from_ticker(quote)
        logger.debug(f"[kline_1d] {symbol} lastbar={'有' if lastbar else 'None'}")

        # ── 步骤 2：读取 DB 中所有 1D bar ──
        db_bars = self._read_db(symbol)
        db_last_ts = db_bars[-1]["time"] if db_bars else 0
        prev_td_ts = _prev_trading_day_ts()
        logger.debug(f"[kline_1d] {symbol} DB读取 {len(db_bars)} 条, "
                      f"db_last_ts={db_last_ts}, prev_td_ts={prev_td_ts}")

        # ── 步骤 3：快速路径判断 ──
        # 条件：DB 最后一条 ≥ 前一交易日（DB 够新）且 lastbar 命中（盘中有实时数据）
        # 满足 → 去掉今日旧 bar + 拼 lastbar → 直接返回，不走远端
        if db_last_ts >= prev_td_ts and lastbar is not None:
            db_bars = [b for b in db_bars if b["time"] < today_ts]
            db_bars.append(lastbar)
            out = db_bars[-limit:] if len(db_bars) > limit else db_bars
            logger.debug(f"[kline_1d] {symbol} 快速路径: 去今日+lastbar, 返回 {len(out)} 条")
            return out

        # ── 步骤 4：慢路径 — 从远端取 1D K 线 ──
        source = self._fetch_remote_1d(symbol, limit)
        source_last_ts = source[-1]["time"] if source else 0
        logger.debug(f"[kline_1d] {symbol} 远端返回 {len(source)} 条, "
                      f"source_last_ts={source_last_ts}")

        # ── 步骤 5：合并 DB + 远端 ──
        # 归一化去重：以 time 为 key，同一时间只保留一条
        # 远端优先覆盖 DB（远端数据排在后面，_normalize_1d_bars 后面覆盖前面）
        # 合并后写回 DB，DB 长度只会 ≥ 远端返回数，旧数据得以保留
        if source:
            db_bars = self._merge_bars(db_bars, source)
            db_bars = _normalize_1d_bars(db_bars)
            self._save_to_db(symbol, db_bars)
            logger.debug(f"[kline_1d] {symbol} 合并后 {len(db_bars)} 条")

        # ── 步骤 6：三分支构造 out_kline ──
        if source_last_ts >= today_ts:
            # 分支 A：远端包含今日数据 → 直接用合并后的 db_kline
            out_kline = db_bars
            logger.debug(f"[kline_1d] {symbol} source含今日, 直接用 db_kline")
        elif lastbar is not None:
            # 分支 B：远端不含今日，但 lastbar 命中 → 追加 lastbar
            out_kline = db_bars + [lastbar]
            logger.debug(f"[kline_1d] {symbol} 追加 lastbar")
        else:
            # 分支 C：远端不含今日，lastbar 也没有 → 尝试取 ticker 转 bar
            quote = _snapshot_cache.get(symbol)
            if not quote:
                _snapshot_cache.refresh([symbol])
                quote = _snapshot_cache.get(symbol)
            ticker_bar = _bar_from_ticker(quote) if quote else None
            if ticker_bar:
                out_kline = db_bars + [ticker_bar]
                logger.debug(f"[kline_1d] {symbol} ticker 转 bar 追加")
            else:
                # 都没有 → 只能用 DB 历史数据
                out_kline = db_bars
                logger.debug(f"[kline_1d] {symbol} ticker 也无数据, 只用 db")

        # 返回最新 limit 条（数量不够就全部返回）
        out = out_kline[-limit:] if len(out_kline) > limit else out_kline
        logger.debug(f"[kline_1d] {symbol} 最终返回 {len(out)} 条")
        return out

    # ================================================================
    # 1W: limit 转 1D count → 走 1D 流程 → ISO 周聚合
    #
    # 为什么先走 1D 再聚合：
    #   1W 的数据本质是 5 个交易日的 OHLCV 聚合。
    #   直接从远端取周线可能不含今日盘中数据，
    #   但走 1D 流程可以利用 lastbar 拼出今日数据，聚合后周线也实时。
    # ================================================================

    def _get_kline_weekly(
        self, symbol: str, limit: int,
    ) -> List[Dict[str, Any]]:
        """1W: 转成 1D count → 走 1D 流程 → ISO 周聚合。"""
        # 每周约 5 个交易日，×7 留冗余保证聚合后数量充足
        daily_count = max(limit * 7, 60)

        # 复用 1D 流程（含 DB + lastbar 逻辑）
        daily_bars = self._get_kline_1d(symbol, daily_count)
        logger.debug(f"[kline_weekly] {symbol} 获取日线 {len(daily_bars)} 条")

        # 按 ISO 周聚合为周线
        out_kline = self._aggregate_weekly(daily_bars)

        out = out_kline[-limit:] if len(out_kline) > limit else out_kline
        logger.debug(f"[kline_weekly] {symbol} 聚合后返回 {len(out)} 条")
        return out

    # ================================================================
    # 远端直读（非 1D/1W 周期）
    #
    # 分时线盘中无法获取完整的 OHLCV（HL 值缺失），
    # 做 DB 缓存没有意义，直接走远端拿最新数据即可。
    # ================================================================

    def _get_kline_remote(
        self,
        symbol: str,
        tf: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """直接走 Coordinator 远程拉取（不做 DB 缓存）。"""
        remote_tf = normalize_chart_timeframe(tf)
        coord_result = get_coordinator().coordinate_kline(
            symbol=symbol,
            timeframe=remote_tf,
            limit=limit,
            market="CNStock",
            timeout=20,
            adj="qfq",
        )
        bars = coord_result.get("bars", []) if coord_result else []
        if bars:
            bars = clean_klines(bars, remote_tf)

        return self.filter_and_limit(
            bars, limit=limit,
            before_time=before_time, after_time=after_time,
            truncate=(after_time is None),
        )

    # ================================================================
    # DB 操作（仅 1D 使用）
    # ================================================================

    def _read_db(self, symbol: str) -> List[Dict[str, Any]]:
        """从 DB 读取 1D K 线，按时间升序返回。"""
        try:
            from app.utils.db_market import get_market_kline_writer
            writer = get_market_kline_writer()
        except Exception:
            return []

        db_symbol = strip_market_prefix(symbol) if symbol else symbol
        try:
            rows = writer.query(
                "CNStock", db_symbol, "1D",
                start_time=None, end_time=None, limit=10000,
            )
        except Exception:
            return []

        bars = []
        for row in rows:
            t = row.get("time")
            ts = int(t.timestamp()) if isinstance(t, datetime) else int(t)
            bars.append({
                "time": ts,
                "open": float(row.get("open", 0)),
                "high": float(row.get("high", 0)),
                "low": float(row.get("low", 0)),
                "close": float(row.get("close", 0)),
                "volume": float(row.get("volume", 0)),
            })
        return bars

    def _save_to_db(self, symbol: str, bars: List[Dict[str, Any]]) -> None:
        """写入 1D DB（upsert 覆盖同时间 bar）。"""
        if not bars:
            return
        try:
            from app.utils.db_market import get_market_kline_writer
            writer = get_market_kline_writer()
            db_symbol = strip_market_prefix(symbol) if symbol else symbol
            db_bars = []
            for b in bars:
                bar = dict(b)
                t = bar.get("time")
                if isinstance(t, (int, float)):
                    bar["time"] = datetime.fromtimestamp(t)
                db_bars.append(bar)
            writer.upsert("CNStock", db_symbol, "1D", db_bars)
            logger.debug(f"[DB写入] {db_symbol}/1D 写入 {len(db_bars)} 条")
        except Exception as e:
            logger.debug(f"[DB写入] {symbol}/1D 失败: {e}")

    def _fetch_remote_1d(self, symbol: str, count: int) -> List[Dict[str, Any]]:
        """从远端拉取 1D K 线，归一化 time 为 int 时间戳。"""
        coord_result = get_coordinator().coordinate_kline(
            symbol=symbol,
            timeframe="1D",
            limit=count,
            market="CNStock",
            timeout=30,
            adj="qfq",
        )
        bars = coord_result.get("bars", []) if coord_result else []
        if not bars:
            return []

        # 归一化 time 为 int 时间戳（远端返回格式不统一，可能是 str/datetime/int）
        for bar in bars:
            t = bar.get("time")
            if isinstance(t, str):
                for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                            "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                    try:
                        bar["time"] = int(datetime.strptime(t.strip(), fmt).timestamp())
                        break
                    except ValueError:
                        continue
            elif isinstance(t, datetime):
                bar["time"] = int(t.timestamp())

        bars.sort(key=lambda b: b["time"])
        bars = clean_klines(bars, "1D")
        bars = _normalize_1d_bars(bars)
        return bars

    # ================================================================
    # 合并去重（远端优先覆盖 DB）
    #
    # 策略：以 time 字段为 key 做 dict 去重
    #   - 先放 DB 数据
    #   - 再放远端数据（同 time 的 bar 会被远端覆盖）
    #   - 这样远端的修正会自动覆盖 DB 中的误差数据
    # ================================================================

    @staticmethod
    def _merge_bars(
        db_bars: List[Dict[str, Any]],
        remote_bars: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """合并 DB + 远端 K 线，远端优先，按时间去重。"""
        by_time: Dict[int, Dict[str, Any]] = {}
        for bar in db_bars:
            by_time[bar["time"]] = bar
        for bar in remote_bars:
            by_time[bar["time"]] = bar  # 远端覆盖 DB
        return sorted(by_time.values(), key=lambda b: b["time"])

    # ================================================================
    # 聚合（周线专用）
    #
    # ISO 周聚合规则：
    #   - 按 isocalendar() 的 (year, week) 分组
    #   - open  = 该周第一条的 open
    #   - high  = 该周所有 high 的最大值
    #   - low   = 该周所有 low 的最小值
    #   - close = 该周最后一条的 close
    #   - volume = 该周所有 volume 之和
    #   - time  = 该周周一 00:00 的时间戳
    # ================================================================

    @staticmethod
    def _aggregate_weekly(bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """按 ISO 周聚合日线为周线。"""
        if not bars:
            return []

        weeks: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
        for bar in bars:
            dt = datetime.fromtimestamp(bar["time"])
            iso = dt.isocalendar()
            key = (iso[0], iso[1])
            weeks.setdefault(key, []).append(bar)

        result = []
        for key in sorted(weeks.keys()):
            group = weeks[key]
            group.sort(key=lambda b: b["time"])
            year, week = key
            monday = datetime.fromisocalendar(year, week, 1)
            monday_ts = int(monday.replace(hour=0, minute=0, second=0).timestamp())
            result.append({
                "time": monday_ts,
                "open": group[0]["open"],
                "high": max(b["high"] for b in group),
                "low": min(b["low"] for b in group),
                "close": group[-1]["close"],
                "volume": round(sum(b["volume"] for b in group), 2),
            })
        return result
