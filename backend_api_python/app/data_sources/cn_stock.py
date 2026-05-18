"""
中国A股数据源

═══════════════════════════════════════════════════════════════
  K 线流程:
    1m/5m   → 直接读远端
    15m/1D  → DB 表 + 远端补满
    30m/1h/2h/4h → 从 15m DB 聚合
    1W      → 计算起止日期，从 1D DB 聚合
    lastbar → 交易时段内用批量快照+TTL(5m)替代，否则 None
═══════════════════════════════════════════════════════════════

设计原则:
  - 15m/1D 有 DB 表，读 DB + 远端增量补满
  - 1m/5m 无 DB，直接走 Coordinator 远程
  - 30m/1h/2h/4h 从 15m DB 实时聚合（内存计算）
  - 1W 计算起止日期，从 1D DB 聚合
  - 交易时段(交易日 9:25~17:00) 用批量快照+TTL(5m) 代替 lastbar
  - 非交易时段 lastbar=None
  - 分时行情 volume 需减去当日 15m 累计量
"""

from __future__ import annotations

import threading
import time as _time
from datetime import datetime, timedelta, time as dtime
from typing import Any, Dict, List, Optional, Tuple

from app.data_sources.base import BaseDataSource
from app.data_sources.asia_stock_kline import normalize_chart_timeframe
from app.data_sources.coordinator import get_coordinator
from app.data_sources.kline_clean import clean_klines
from app.data_sources.normalizer import add_market_prefix, strip_market_prefix
from app.utils.logger import get_logger
from app.utils.trading_calendar import is_trading_day, prev_trading_day

logger = get_logger(__name__)

# ── DB 支持的原始周期（直接查表）──
_RAW_TIMEFRAMES = {"15m", "1D"}

# ── 聚合周期（从 15m 或 1D 在内存中聚合）──
# 15m → 30m/1h/2h/4h/1D; 1D → 1W
_FROM_15M = {"30m", "1h", "2h", "4h"}
_FROM_1D  = {"1W"}

# ── 远端直读周期（不做 DB）──
_REMOTE_ONLY = {"1m", "5m"}

# ── 聚合间隔（秒）──
_INTERVAL_SEC = {
    "15m": 900, "30m": 1800, "1h": 3600,
    "2h": 7200, "4h": 14400, "1D": 86400, "1W": 604800,
}

# ── DB 补满最大条数 ──
_BACKFILL_MAX = {"15m": 2000, "1D": 1000}

# ── 批量快照 TTL（秒）──
_SNAPSHOT_TTL = 300  # 5 分钟


# ================================================================
# 批量快照缓存（进程级单例，5 分钟 TTL）
# ================================================================

class _SnapshotCache:
    """批量行情快照缓存，5 分钟有效期。"""

    def __init__(self):
        self._quotes: List[Dict[str, Any]] = []
        self._ts: float = 0
        self._lock = threading.Lock()

    def get(self, symbol: str) -> Optional[Dict[str, Any]]:
        """获取当前 symbol 的快照行情，过期自动刷新。"""
        now = _time.time()
        with self._lock:
            if now - self._ts < _SNAPSHOT_TTL and self._quotes:
                return self._find(symbol)

        # 需要刷新
        return self._refresh(symbol)

    def _refresh(self, symbol: str) -> Optional[Dict[str, Any]]:
        """从 Coordinator 批量拉取快照。"""
        coord = get_coordinator()
        try:
            quotes = coord.coordinate_batch_quotes(
                symbols=[symbol],
                market="CNStock",
                timeout=10,
            )
        except Exception as e:
            logger.debug(f"[快照] 批量拉取异常: {e}")
            quotes = []

        with self._lock:
            self._quotes = quotes or []
            self._ts = _time.time()
        return self._find(symbol)

    def _find(self, symbol: str) -> Optional[Dict[str, Any]]:
        """从缓存中查找指定 symbol。"""
        pure = strip_market_prefix(symbol) if symbol else symbol
        for q in self._quotes:
            qsym = q.get("symbol", "")
            if strip_market_prefix(qsym) == pure or qsym == pure:
                return q
        return None


_snapshot_cache = _SnapshotCache()


# ================================================================
# 工具函数
# ================================================================

def _is_in_trading_hours() -> bool:
    """交易日 9:25 ~ 17:00。"""
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    if not is_trading_day(today_str):
        return False
    t = now.time()
    return dtime(9, 25) <= t <= dtime(17, 0)


def _compute_tf_time(tf: str, ref_date: str) -> int:
    """计算时间框架的时间点（Unix 秒）。

    对于分时周期：ref_date + 当日时间地板点
    对于 1D：ref_date 00:00
    对于 1W：ref_date 00:00（周一起点）

    Args:
        tf: 时间框架
        ref_date: 参考日期 YYYY-MM-DD
    """
    if tf in ("1D", "1W"):
        return int(datetime.strptime(ref_date, "%Y-%m-%d").timestamp())

    sec = _INTERVAL_SEC.get(tf, 900)
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    # 分时：当日时间地板点
    if ref_date == today_str:
        market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        elapsed = (now - market_open).total_seconds()
        if elapsed < 0:
            elapsed = 0
        floored = int(elapsed) // sec * sec
        dt = market_open + timedelta(seconds=floored)
        return int(dt.timestamp())
    else:
        # 非当日：用最后一根 bar 的时间（收盘 15:00 的地板点）
        dt = datetime.strptime(ref_date, "%Y-%m-%d").replace(hour=15, minute=0)
        return int(dt.timestamp())


def _today_15m_sum(symbol: str, tf: str) -> Tuple[float, float]:
    """读取当日 15m DB 中的 volume/amount 累计和（分时 lastbar 减量用）。

    Returns:
        (volume_sum, amount_sum)
    """
    if tf in ("1D", "1W"):
        return 0.0, 0.0  # 日线及以上不需要减量

    try:
        from app.utils.db_market import get_market_kline_writer
        writer = get_market_kline_writer()
    except Exception:
        return 0.0, 0.0

    db_symbol = strip_market_prefix(symbol) if symbol else symbol
    today_str = datetime.now().strftime("%Y-%m-%d")
    start_dt = datetime.strptime(today_str, "%Y-%m-%d")

    try:
        rows = writer.query(
            "CNStock", db_symbol, "15m",
            start_time=start_dt, end_time=None, limit=50,
        )
    except Exception:
        return 0.0, 0.0

    vol_sum = sum(float(r.get("volume", 0)) for r in rows)
    amt_sum = sum(float(r.get("amount", 0)) for r in rows)
    return vol_sum, amt_sum


# ================================================================
# 主数据源类
# ================================================================

class CNStockDataSource(BaseDataSource):
    """A股数据源 — DB + 远端 + 快照混合架构。"""

    name = "CNStock/multi-source"

    # ── get_ticker: 实时行情（保持不变）──

    def get_ticker(self, symbol) -> Dict[str, Any]:
        """获取实时行情。支持单股 / 逗号拼接 / List[str]。"""
        if isinstance(symbol, list):
            symbols = [s.strip() for s in symbol if s and s.strip()]
        elif isinstance(symbol, str) and ',' in symbol:
            symbols = [s.strip() for s in symbol.split(',') if s.strip()]
        else:
            symbols = [symbol] if symbol else []

        if not symbols:
            return {"last": 0, "symbol": ""}

        coord = get_coordinator()

        if len(symbols) == 1:
            result = coord.coordinate_ticker(
                symbol=symbols[0], market="CNStock", timeout=8,
            )
            if not result:
                logger.warning(f"[行情] 所有数据源均失败: {symbol}")
                return {"last": 0, "symbol": symbols[0]}
            result["symbol"] = symbols[0]
            return result

        quotes_list = coord.coordinate_tickers(
            symbols=symbols, market="CNStock", timeout=8,
        )
        if not quotes_list:
            logger.warning(f"[行情] 所有数据源均失败: {symbol}")
            return {"last": 0, "symbol": symbols[0]}

        result_map: Dict[str, Dict[str, Any]] = {}
        for q in quotes_list:
            sym = q.get("symbol", "")
            if sym:
                result_map[sym] = q
        return result_map

    # ── get_kline: K 线数据（核心重写）──

    def get_kline(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """获取 K 线数据。

        流程:
          1m/5m     → 直接走远端
          15m/1D    → DB 读取 + 远端增量补满 + lastbar
          30m/1h/2h/4h → 从 15m DB 聚合 + lastbar
          1W        → 计算起止日期，从 1D DB 聚合 + lastbar

        lastbar:
          交易日 9:25~17:00 → 批量快照+TTL(5m) - 当日 15m 累计
          其它时间 → None
        """
        tf = normalize_chart_timeframe(timeframe)
        limit = max(int(limit or 300), 1)

        # ── 1m / 5m: 直接走远端 ──
        if tf in _REMOTE_ONLY:
            return self._get_kline_remote(symbol, tf, limit, before_time, after_time)

        # ── 15m / 1D: DB 读取 + 远端增量补满 + lastbar ──
        if tf in _RAW_TIMEFRAMES:
            return self._get_kline_db_flow(symbol, tf, limit)

        # ── 30m / 1h / 2h / 4h: 从 15m 聚合 + lastbar ──
        if tf in _FROM_15M:
            return self._get_kline_agg_from_15m(symbol, tf, limit)

        # ── 1W: 从 1D 聚合（含起止日期计算）+ lastbar ──
        if tf in _FROM_1D:
            return self._get_kline_weekly(symbol, tf, limit)

        # 其它周期走远端
        return self._get_kline_remote(symbol, tf, limit, before_time, after_time)

    # ================================================================
    # 15m / 1D: DB 读取 + 远端增量补满 + lastbar
    # ================================================================

    def _get_kline_db_flow(
        self, symbol: str, tf: str, limit: int,
    ) -> List[Dict[str, Any]]:
        """15m / 1D: DB + 远端增量补满 + lastbar 替换/追加。"""
        in_trading = _is_in_trading_hours()

        # ── 1. 获取 lastbar ──
        lastbar = None
        if in_trading:
            lastbar = self._fetch_lastbar(symbol, tf)

        # ── 2. 读取 DB ──
        db_bars = self._read_db(symbol, tf)

        # ── 3. 判断是否需要远端补满 ──
        if tf == "1D":
            ref_date = datetime.now().strftime("%Y-%m-%d")
            compare_ts = _compute_tf_time(tf, ref_date)
        else:
            prev_td = prev_trading_day(datetime.now().strftime("%Y-%m-%d"))
            compare_ts = _compute_tf_time(tf, prev_td)

        db_last_ts = db_bars[-1]["time"] if db_bars else 0
        if compare_ts > db_last_ts:
            remote_bars = self._fetch_remote_bars(symbol, tf)
            if remote_bars:
                db_bars = self._merge_and_save(symbol, tf, db_bars, remote_bars)

        # ── 4. lastbar 替换/追加 ──
        if lastbar is not None:
            lb_ts = lastbar["time"]
            if db_bars and db_bars[-1]["time"] == lb_ts:
                db_bars[-1] = lastbar
            else:
                db_bars.append(lastbar)

        # ── 5. 返回最新 limit 条 ──
        if len(db_bars) > limit:
            db_bars = db_bars[-limit:]
        return db_bars

    # ================================================================
    # 30m / 1h / 2h / 4h: 从 15m DB 聚合 + lastbar
    # ================================================================

    def _get_kline_agg_from_15m(
        self, symbol: str, tf: str, limit: int,
    ) -> List[Dict[str, Any]]:
        """30m/1h/2h/4h: 从 15m DB 聚合 + lastbar。"""
        in_trading = _is_in_trading_hours()

        # ── 1. 获取 lastbar ──
        lastbar = None
        if in_trading:
            lastbar = self._fetch_lastbar(symbol, tf)

        # ── 2. 读取 15m DB ──
        raw_bars = self._read_db(symbol, "15m")

        # ── 3. 判断是否需要远端补满 15m ──
        prev_td = prev_trading_day(datetime.now().strftime("%Y-%m-%d"))
        compare_ts = _compute_tf_time("15m", prev_td)
        db_last_ts = raw_bars[-1]["time"] if raw_bars else 0
        if compare_ts > db_last_ts:
            remote_bars = self._fetch_remote_bars(symbol, "15m")
            if remote_bars:
                raw_bars = self._merge_and_save(symbol, "15m", raw_bars, remote_bars)

        # ── 4. 聚合 ──
        agg_bars = self._aggregate_bars(raw_bars, tf)

        # ── 5. lastbar 替换/追加 ──
        if lastbar is not None:
            lb_ts = lastbar["time"]
            if agg_bars and agg_bars[-1]["time"] == lb_ts:
                agg_bars[-1] = lastbar
            else:
                agg_bars.append(lastbar)

        # ── 6. 返回最新 limit 条 ──
        if len(agg_bars) > limit:
            agg_bars = agg_bars[-limit:]
        return agg_bars

    # ================================================================
    # 1W: 计算起止日期，从 1D 聚合 + lastbar
    # ================================================================

    def _get_kline_weekly(
        self, symbol: str, tf: str, limit: int,
    ) -> List[Dict[str, Any]]:
        """1W: 计算起止日期，从 1D DB 聚合 + lastbar。"""
        in_trading = _is_in_trading_hours()

        # ── 1. 获取 lastbar ──
        lastbar = None
        if in_trading:
            lastbar = self._fetch_lastbar(symbol, tf)

        # ── 2. 计算起止日期 ──
        # 需要足够多天的日线来聚合出 limit 根周线
        # 每根周线 ≈ 5 个交易日 ≈ 7 个自然日，留余量用 10
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        lookback_days = max(limit * 10, 60)
        start_date = (now - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        # 结束日期：交易中用今天，否则用上一个交易日
        end_date = today_str if in_trading else prev_trading_day(today_str)

        # ── 3. 读取 1D DB（按日期范围）──
        try:
            from app.utils.db_market import get_market_kline_writer
            writer = get_market_kline_writer()
            db_symbol = strip_market_prefix(symbol) if symbol else symbol
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            end_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
            rows = writer.query(
                "CNStock", db_symbol, "1D",
                start_time=start_dt, end_time=end_dt, limit=500,
            )
        except Exception:
            rows = []

        daily_bars = []
        for row in rows:
            t = row.get("time")
            ts = int(t.timestamp()) if isinstance(t, datetime) else int(t)
            daily_bars.append({
                "time": ts,
                "open": float(row.get("open", 0)),
                "high": float(row.get("high", 0)),
                "low": float(row.get("low", 0)),
                "close": float(row.get("close", 0)),
                "volume": float(row.get("volume", 0)),
            })

        # ── 4. 如果 DB 没有足够数据，远端补满 ──
        if not daily_bars:
            remote_bars = self._fetch_remote_bars(symbol, "1D")
            if remote_bars:
                self._save_to_db(symbol, "1D", remote_bars)
                # 重新查询
                try:
                    rows = writer.query(
                        "CNStock", db_symbol, "1D",
                        start_time=start_dt, end_time=end_dt, limit=500,
                    )
                    for row in rows:
                        t = row.get("time")
                        ts = int(t.timestamp()) if isinstance(t, datetime) else int(t)
                        daily_bars.append({
                            "time": ts,
                            "open": float(row.get("open", 0)),
                            "high": float(row.get("high", 0)),
                            "low": float(row.get("low", 0)),
                            "close": float(row.get("close", 0)),
                            "volume": float(row.get("volume", 0)),
                        })
                except Exception:
                    pass

        # ── 5. 聚合为周线 ──
        agg_bars = self._aggregate_bars(daily_bars, "1W")

        # ── 6. lastbar 替换/追加 ──
        if lastbar is not None:
            lb_ts = lastbar["time"]
            if agg_bars and agg_bars[-1]["time"] == lb_ts:
                agg_bars[-1] = lastbar
            else:
                agg_bars.append(lastbar)

        # ── 7. 返回最新 limit 条 ──
        if len(agg_bars) > limit:
            agg_bars = agg_bars[-limit:]
        return agg_bars

    # ================================================================
    # 远端直读（1m / 5m）
    # ================================================================

    def _get_kline_remote(
        self,
        symbol: str,
        tf: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """直接走 Coordinator 远程拉取（1m/5m 专用，不做 DB）。"""
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
    # DB 操作
    # ================================================================

    def _read_db(self, symbol: str, tf: str) -> List[Dict[str, Any]]:
        """从 DB 读取 K 线，按时间升序返回。"""
        try:
            from app.utils.db_market import get_market_kline_writer
            writer = get_market_kline_writer()
        except Exception:
            return []

        db_symbol = strip_market_prefix(symbol) if symbol else symbol
        try:
            rows = writer.query(
                "CNStock", db_symbol, tf,
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

    def _save_to_db(self, symbol: str, tf: str, bars: List[Dict[str, Any]]) -> None:
        """写入 DB（覆盖）。"""
        if not bars:
            return
        try:
            from app.utils.db_market import get_market_kline_writer
            writer = get_market_kline_writer()
            db_symbol = strip_market_prefix(symbol) if symbol else symbol

            # 转为 datetime（DB writer 需要）
            db_bars = []
            for b in bars:
                bar = dict(b)
                t = bar.get("time")
                if isinstance(t, (int, float)):
                    bar["time"] = datetime.fromtimestamp(t)
                db_bars.append(bar)

            writer.upsert("CNStock", db_symbol, tf, db_bars)
            logger.debug(f"[DB写入] {db_symbol}/{tf} 写入 {len(db_bars)} 条")
        except Exception as e:
            logger.debug(f"[DB写入] {symbol}/{tf} 失败: {e}")

    def _merge_and_save(
        self,
        symbol: str,
        tf: str,
        db_bars: List[Dict[str, Any]],
        remote_bars: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """合并 DB + 远端 K 线（远端优先去重），写回 DB，返回合并结果。"""
        merged = self._merge_bars(db_bars, remote_bars)
        self._save_to_db(symbol, tf, merged)
        return merged

    # ================================================================
    # 远端拉取 + 归一化
    # ================================================================

    def _fetch_remote_bars(
        self, symbol: str, tf: str,
    ) -> List[Dict[str, Any]]:
        """从远端拉取 15m 或 1D K 线，归一化 + 去错。"""
        remote_tf = normalize_chart_timeframe(tf)
        max_count = _BACKFILL_MAX.get(tf, 1000)
        coord_result = get_coordinator().coordinate_kline(
            symbol=symbol,
            timeframe=remote_tf,
            limit=max_count,
            market="CNStock",
            timeout=30,
            adj="qfq",
        )
        bars = coord_result.get("bars", []) if coord_result else []
        if not bars:
            return []

        # 归一化 time 为 int 时间戳
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

        # 去错 + 补齐缺失
        bars = clean_klines(bars, tf)
        return bars

    # ================================================================
    # 合并去重
    # ================================================================

    @staticmethod
    def _merge_bars(
        db_bars: List[Dict[str, Any]],
        remote_bars: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """合并 DB + 远端 K 线，远端优先，按时间去重，保留旧数据。"""
        by_time: Dict[int, Dict[str, Any]] = {}
        for bar in db_bars:
            by_time[bar["time"]] = bar
        for bar in remote_bars:
            by_time[bar["time"]] = bar  # 远端覆盖 DB
        merged = sorted(by_time.values(), key=lambda b: b["time"])
        return merged

    # ================================================================
    # 聚合（内存计算）
    # ================================================================

    @staticmethod
    def _aggregate_bars(
        bars: List[Dict[str, Any]], target_tf: str,
    ) -> List[Dict[str, Any]]:
        """将原始 K 线聚合为目标周期（内存计算）。

        15m → 30m/1h/2h/4h: 按时间间隔分桶
        1D → 1W: 按 ISO 周分桶

        聚合规则:
          open   = 桶内第一根的 open
          high   = 桶内所有 high 的最大值
          low    = 桶内所有 low 的最小值
          close  = 桶内最后一根的 close
          volume = 桶内所有 volume 之和
        """
        if not bars:
            return []

        if target_tf == "1W":
            return CNStockDataSource._aggregate_weekly(bars)

        sec = _INTERVAL_SEC.get(target_tf, 3600)
        buckets: Dict[int, List[Dict[str, Any]]] = {}

        for bar in bars:
            bucket_key = bar["time"] // sec * sec
            buckets.setdefault(bucket_key, []).append(bar)

        result = []
        for bucket_ts in sorted(buckets.keys()):
            group = buckets[bucket_ts]
            group.sort(key=lambda b: b["time"])
            result.append({
                "time": bucket_ts,
                "open": group[0]["open"],
                "high": max(b["high"] for b in group),
                "low": min(b["low"] for b in group),
                "close": group[-1]["close"],
                "volume": round(sum(b["volume"] for b in group), 2),
            })
        return result

    @staticmethod
    def _aggregate_weekly(bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """按 ISO 周聚合日线为周线。"""
        if not bars:
            return []

        weeks: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
        for bar in bars:
            dt = datetime.fromtimestamp(bar["time"])
            iso = dt.isocalendar()
            key = (iso[0], iso[1])  # (year, week_number)
            weeks.setdefault(key, []).append(bar)

        result = []
        for key in sorted(weeks.keys()):
            group = weeks[key]
            group.sort(key=lambda b: b["time"])
            # 周一起始时间
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

    # ================================================================
    # lastbar 计算
    # ================================================================

    def _fetch_lastbar(
        self, symbol: str, tf: str,
    ) -> Optional[Dict[str, Any]]:
        """获取当前周期的 lastbar（批量快照 + 减当日 15m 累计）。

        仅在交易时段内调用。
        """
        quote = _snapshot_cache.get(symbol)
        if not quote:
            return None

        # 当前价格
        price = (
            quote.get("last")
            or quote.get("close")
            or quote.get("price")
            or 0
        )
        if not price or float(price) <= 0:
            return None

        price = float(price)

        # 时间地板点
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        if tf in ("1D", "1W"):
            bar_ts = int(datetime.strptime(today_str, "%Y-%m-%d").timestamp())
        else:
            sec = _INTERVAL_SEC.get(tf, 900)
            market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
            elapsed = max(0, (now - market_open).total_seconds())
            floored = int(elapsed) // sec * sec
            dt = market_open + timedelta(seconds=floored)
            bar_ts = int(dt.timestamp())

        # 成交量：快照累计 - 当日 15m 累计
        snap_vol = float(quote.get("volume", 0))
        vol_15m_sum, _ = _today_15m_sum(symbol, tf)
        volume = max(0, snap_vol - vol_15m_sum)

        # 价格字段
        open_p = float(quote.get("open", price))
        high_p = float(quote.get("high", price))
        low_p = float(quote.get("low", price))

        return {
            "time": bar_ts,
            "open": open_p,
            "high": high_p,
            "low": low_p,
            "close": price,
            "volume": round(volume, 2),
        }
