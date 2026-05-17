"""
中国A股数据源

═══════════════════════════════════════════════════════════════
  全远程架构: 所有 K 线数据从 Coordinator 多源获取，不依赖本地 DB
═══════════════════════════════════════════════════════════════

设计原则:
  - 所有周期（1m ~ 1M）统一走 Coordinator 远程拉取
  - 不维护本地 DB，不做周期聚合
  - ticker 实时行情由 Coordinator 统一调度
  - 符号规范化由 Coordinator 统一处理（入口加前缀，出口去前缀）

职责:
  get_ticker()  → 从 Coordinator 取实时行情
  get_kline()   → 从 Coordinator 取 K 线，支持单只/批量
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.data_sources.base import BaseDataSource
from app.data_sources.asia_stock_kline import normalize_chart_timeframe
from app.data_sources.coordinator import get_coordinator
from app.data_sources.kline_clean import clean_klines
from app.utils.logger import get_logger
from app.utils.trading_calendar import prev_trading_day

logger = get_logger(__name__)

# DB 支持的原始周期（直接查表，效率最高）
_RAW_TIMEFRAMES = {"15m", "1D"}

# DB 聚合周期（从 15m 或 1D 实时 SQL 聚合）
# 与 MarketKlineWriter._AGG_TARGETS 对齐
_AGG_TIMEFRAMES = {"30m", "1h", "2h", "4h", "1W", "1M"}

# 周期对应的秒数（用于 DB 数据时效性判断）
_BAR_SECONDS = {
    "15m": 900, "30m": 1800, "1h": 3600,
    "2h": 7200, "4h": 14400, "1D": 86400,
}

# DB 数据新鲜度：最新 bar 的日期必须 >= 上一个交易日
# 使用 trading_calendar 模块精确判断，不再依赖固定天数


# ================================================================
# 数据源类
# ================================================================

class CNStockDataSource(BaseDataSource):
    """A股数据源 — 全远程架构。

    所有 K 线数据通过 Coordinator 从多个远程源获取，
    不依赖本地 DB，不做周期聚合。
    """

    name = "CNStock/multi-source"

    # ── get_ticker: 实时行情 ──

    def get_ticker(self, symbol) -> Dict[str, Any]:
        """获取实时行情。支持单股 / 逗号拼接 / List[str]，均由 Coordinator 统一调度。"""
        if isinstance(symbol, list):
            symbols = [s.strip() for s in symbol if s and s.strip()]
        elif isinstance(symbol, str) and ',' in symbol:
            symbols = [s.strip() for s in symbol.split(',') if s.strip()]
        else:
            symbols = [symbol] if symbol else []

        if not symbols:
            return {"last": 0, "symbol": ""}

        result = get_coordinator().coordinate_ticker(
            symbols=symbols,
            market="CNStock",
            timeout=8,
        )

        if not result:
            logger.warning(f"[行情] 所有数据源均失败: {symbol}")
            return {"last": 0, "symbol": symbols[0] if symbols else ""}

        # 单股模式：返回第一个匹配
        if len(symbols) == 1:
            key = symbols[0]
            if key in result:
                quote = result[key]
                quote["symbol"] = key
                return quote
            logger.warning(f"[行情] 所有数据源均失败: {symbol}")
            return {"last": 0, "symbol": key}

        # 批量模式：返回整个 map
        return result

    # ── get_kline: K 线数据 ──

    def get_kline(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """获取 K 线数据。支持逗号分隔的批量模式。

        回测时优先从本地 DB 读取（15m/1D 直查，其余周期 SQL 聚合），
        DB 无数据或数据不够新时自动降级走远程。
        """
        # 批量模式暂不走 DB，直接远程
        if ',' in symbol:
            symbols = [s.strip() for s in symbol.split(',') if s.strip()]
            if not symbols:
                return []
            batch = self._get_klines_remote(symbols, timeframe, limit)
            merged = []
            for sym in symbols:
                merged.extend(batch.get(sym, []))
            return self.filter_and_limit(
                merged, limit=len(merged),
                before_time=before_time, after_time=after_time,
                truncate=False,
            )

        # 单只：DB 优先
        tf = timeframe.strip()
        if tf in _RAW_TIMEFRAMES or tf in _AGG_TIMEFRAMES:
            bars = self._get_kline_db(
                symbol, tf, limit, before_time, after_time,
            )
            if bars:
                return self.filter_and_limit(
                    bars, limit=limit,
                    before_time=before_time, after_time=after_time,
                    truncate=(after_time is None),
                )

        # 降级：走远程（原路径完全不变）
        return self._get_kline_remote(
            symbol, timeframe, limit, before_time, after_time
        )

    # ── DB 读取（内部方法，不影响外部接口）──

    def _get_kline_db(
        self,
        symbol: str,
        tf: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        从本地 DB 读取 K 线。

        逻辑：
          1. 查 DB → 条数足够且数据新鲜 → 直接返回
          2. DB 空 / 过期 / 条数不足 → 远程补满最大年限 → 写入 DB → 重新查询返回
             15m 最多 2 年，1D 最多 5 年
        """
        try:
            from app.utils.db_market import get_market_kline_writer
            from app.data_sources.normalizer import strip_market_prefix
            writer = get_market_kline_writer()
        except Exception:
            return []

        # DB 存储使用纯数字代码（backfill 写入时 strip_market_prefix），
        # 查询前必须统一格式，否则 "600519.SH" 查不到 "600519"
        db_symbol = strip_market_prefix(symbol) if symbol else symbol

        # ── 最大补满年限：15m 一年，1D 五年 ──
        _BACKFILL_MAX_BARS = {
            "15m": 4000,   # 1年 × 250天 × 16根/天
            "1D":  1000,   # 5年 × 250天
            "30m": 2000,
            "1h":  1000,
            "2h":  500,
            "4h":  250,
            "1W":  260,
            "1M":  60,
        }
        min_required = max(limit // 2, 30)

        def _query_db() -> List[Dict[str, Any]]:
            """查询 DB 并转为标准格式。"""
            start_dt = datetime.fromtimestamp(after_time) if after_time else None
            end_dt = datetime.fromtimestamp(before_time) if before_time else None
            try:
                if tf in _RAW_TIMEFRAMES:
                    rows = writer.query(
                        "CNStock", db_symbol, tf,
                        start_time=start_dt, end_time=end_dt,
                        limit=limit,
                    )
                else:
                    rows = writer.aggregate(
                        "CNStock", db_symbol, tf,
                        start_time=start_dt, end_time=end_dt,
                        limit=limit,
                    )
            except Exception:
                return []
            if not rows:
                return []

            bars = []
            for row in rows:
                t = row.get("time")
                if isinstance(t, datetime):
                    ts = int(t.timestamp())
                elif isinstance(t, (int, float)):
                    ts = int(t)
                else:
                    continue
                bars.append({
                    "time": ts,
                    "open": float(row.get("open", 0)),
                    "high": float(row.get("high", 0)),
                    "low": float(row.get("low", 0)),
                    "close": float(row.get("close", 0)),
                    "volume": float(row.get("volume", 0)),
                })
            return bars

        def _is_fresh(bars: List[Dict[str, Any]]) -> bool:
            """检查 DB 数据是否足够新鲜。"""
            if not bars:
                return False
            latest_ts = bars[-1].get("time", 0)
            now_ts = datetime.now().timestamp()

            if before_time:
                return latest_ts >= before_time - 86400
            else:
                latest_date = datetime.fromtimestamp(latest_ts).strftime("%Y-%m-%d")
                cutoff = prev_trading_day(datetime.now().strftime("%Y-%m-%d"), n=1)
                if latest_date < cutoff:
                    return False
                bar_sec = _BAR_SECONDS.get(tf)
                if bar_sec and (now_ts - latest_ts) > bar_sec * 2:
                    return False
            return True

        def _backfill_from_remote() -> None:
            """
            远程补满最大年限 → 写入 DB。

            条数不足时取最大年限归一化覆盖 DB，不是缺多少取多少。
            每只 symbol 只在首次 miss 时触发一次，后续全走 DB 缓存。
            """
            from app.data_sources.coordinator import get_coordinator
            from app.data_sources.kline_clean import clean_klines
            from app.data_sources.asia_stock_kline import normalize_chart_timeframe

            remote_tf = normalize_chart_timeframe(tf)
            remote_limit = _BACKFILL_MAX_BARS.get(tf, 1000)

            logger.info(
                f"[DB补满] {db_symbol}/{tf} 开始远程补满: "
                f"请求{remote_limit}条 (最大年限)"
            )

            try:
                coord_results, failed = get_coordinator().coordinate_kline(
                    symbols=[symbol],
                    timeframe=remote_tf,
                    limit=remote_limit,
                    market="CNStock",
                    timeout=30,
                    adj="qfq",
                )
            except Exception as e:
                logger.warning(f"[DB补满] {db_symbol}/{tf} 远程拉取异常: {e}")
                return

            bars = coord_results.get(symbol, [])
            if not bars:
                logger.warning(f"[DB补满] {db_symbol}/{tf} 远程返回空")
                return

            bars = clean_klines(bars, remote_tf)
            try:
                writer.upsert("CNStock", db_symbol, tf, bars)
                logger.info(f"[DB补满] {db_symbol}/{tf} 写入 {len(bars)} 条")
            except Exception as e:
                logger.warning(f"[DB补满] {db_symbol}/{tf} 写入 DB 失败: {e}")

        # ── 主流程 ──

        # 1. 先查 DB
        bars = _query_db()

        # 2. 判断是否需要补满：空 / 不新鲜 / 条数不足
        #
        #    补满策略：条数不足时不是缺多少取多少，而是取最大年限归一化覆盖 DB。
        #    这样每只 symbol 只在首次 miss 时慢一次远程拉取，后续全部命中本地 DB。
        #    - 15m 最多补 2 年（8000 条）
        #    - 1D  最多补 5 年（1250 条）
        #
        #    为什么不用"缺多少取多少"：
        #    批量回测时 8 线程并发，每只 symbol 各自缺不同量，会导致大量碎片化远程请求，
        #    远程源扛不住雪崩超时。一次补满最大年限，后续全走 DB，总请求量最小。
        #
        need_backfill = False
        if not bars:
            need_backfill = True
            logger.debug(f"[DB] {db_symbol}/{tf} 无数据，触发补满")
        elif not _is_fresh(bars):
            need_backfill = True
            logger.debug(f"[DB] {db_symbol}/{tf} 数据过期，触发补满")
        elif len(bars) < min_required:
            need_backfill = True
            logger.debug(
                f"[DB] {db_symbol}/{tf} 条数不足: "
                f"{len(bars)} < {min_required}，触发补满"
            )

        # 3. 补满后重新查询
        if need_backfill:
            _backfill_from_remote()
            bars = _query_db()

        if bars:
            logger.info(f"[DB] ✅ {db_symbol}/{tf} 返回 {len(bars)} 条")
        return bars

    # ── 远程拉取（Coordinator 调度）──

    def _cache_to_db(self, symbol: str, timeframe: str, bars: List[Dict[str, Any]]):
        """将 15m/1D K 线写入 DB，供后续请求走本地缓存。"""
        if timeframe not in _RAW_TIMEFRAMES or not bars:
            return
        try:
            from app.utils.db_market import get_market_kline_writer
            from app.data_sources.normalizer import strip_market_prefix
            writer = get_market_kline_writer()
            # DB 统一使用纯数字代码存储（与 backfill_db 一致）
            db_symbol = strip_market_prefix(symbol) if symbol else symbol
            writer.upsert("CNStock", db_symbol, timeframe, bars)
            logger.debug(f"[DB写入] {symbol}→{db_symbol}/{timeframe} 缓存 {len(bars)} 条")
        except Exception as e:
            logger.debug(f"[DB写入] {symbol}/{timeframe} 缓存失败: {e}")

    def _get_kline_remote(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
        adj: str = "qfq",
    ) -> List[Dict[str, Any]]:
        """单只远程拉取。"""
        tf = normalize_chart_timeframe(timeframe)
        lim = max(int(limit or 300), 1)

        coord_results, failed = get_coordinator().coordinate_kline(
            symbols=[symbol],
            timeframe=tf,
            limit=lim,
            market="CNStock",
            timeout=20,
            adj=adj,
        )

        bars = coord_results.get(symbol, [])

        if bars:
            bars = clean_klines(bars, tf)
            # 远程拿到 15m/1D 数据后回写 DB，下次可走本地缓存
            self._cache_to_db(symbol, tf, bars)

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

        tf = normalize_chart_timeframe(timeframe)
        lim = max(int(limit or 300), 1)

        logger.info(
            f"[远程批量] 开始拉取 tf={tf} limit={lim} adj={adj} "
            f"标的数={len(symbols)} 示例={symbols[:3]}"
        )

        coord_results, failed = get_coordinator().coordinate_kline(
            symbols=symbols,
            timeframe=tf,
            limit=lim,
            market="CNStock",
            timeout=20,
            adj=adj,
        )

        result: Dict[str, List[Dict[str, Any]]] = {}
        total_bars = 0
        for sym, bars in coord_results.items():
            cleaned = clean_klines(bars, tf)
            result[sym] = cleaned
            total_bars += len(cleaned)
            # 批量远程也回写 DB（仅 15m/1D）
            self._cache_to_db(sym, tf, cleaned)

        if failed:
            logger.warning(f"[远程批量] {len(failed)}/{len(symbols)} 只失败: {failed[:10]}")

        logger.info(
            f"[远程批量] 完成 tf={tf} "
            f"成功={len(coord_results)}/{len(symbols)} "
            f"失败={len(failed)} 总bar数={total_bars}"
        )

        return result
