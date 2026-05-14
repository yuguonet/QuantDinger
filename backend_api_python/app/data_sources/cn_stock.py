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

logger = get_logger(__name__)

# DB 支持的原始周期（直接查表，效率最高）
_RAW_TIMEFRAMES = {"15m", "1D"}

# DB 聚合周期（从 15m 或 1D 实时 SQL 聚合）
# 与 MarketKlineWriter._AGG_TARGETS 对齐
_AGG_TIMEFRAMES = {"30m", "1h", "2h", "4h", "1W", "1M"}

# DB 数据新鲜度阈值：最新bar时间距今不超过 N 秒视为有效
# 1天=86400s，A股最长非交易间隔约3天(周末+节假日)，取4天兜底
_DB_FRESHNESS_MAX_AGE = 4 * 86400


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
        从本地 DB 读取 K 线，含最后时间判定。

        返回标准格式列表，失败或数据不够新返回空列表。
        """
        try:
            from app.utils.db_market import get_market_kline_writer
            writer = get_market_kline_writer()
        except Exception:
            return []

        # 计算时间范围
        start_dt = datetime.fromtimestamp(after_time) if after_time else None
        end_dt = datetime.fromtimestamp(before_time) if before_time else None

        try:
            if tf in _RAW_TIMEFRAMES:
                rows = writer.query(
                    "CNStock", symbol, tf,
                    start_time=start_dt, end_time=end_dt,
                    limit=limit,
                )
            else:
                # _AGG_TIMEFRAMES
                rows = writer.aggregate(
                    "CNStock", symbol, tf,
                    start_time=start_dt, end_time=end_dt,
                    limit=limit,
                )
        except Exception:
            return []

        if not rows:
            return []

        # ── 最后时间判定 ──
        latest_db_time = rows[-1].get("time")
        if isinstance(latest_db_time, datetime):
            latest_ts = latest_db_time.timestamp()
        elif isinstance(latest_db_time, (int, float)):
            latest_ts = float(latest_db_time)
        else:
            return []

        now_ts = datetime.now().timestamp()

        if before_time:
            # 回测场景：DB 最新 bar 必须覆盖到 before_time 附近（允许1天误差）
            if latest_ts < before_time - 86400:
                logger.debug(
                    f"[DB] {symbol}/{tf} 数据不够新: "
                    f"最新={datetime.fromtimestamp(latest_ts):%Y-%m-%d} "
                    f"需要>={datetime.fromtimestamp(before_time - 86400):%Y-%m-%d}"
                )
                return []
        else:
            # 非回测场景：DB 最新 bar 不能太旧
            if now_ts - latest_ts > _DB_FRESHNESS_MAX_AGE:
                logger.debug(
                    f"[DB] {symbol}/{tf} 数据过期: "
                    f"最新={datetime.fromtimestamp(latest_ts):%Y-%m-%d} "
                    f"距今{int((now_ts - latest_ts) / 86400)}天"
                )
                return []

        # ── 转换为标准格式（time 转 Unix 秒）──
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

        # ── 数量判定：DB 返回条数不足请求数的一半，视为数据不完整 ──
        min_required = max(limit // 2, 30)
        if len(bars) < min_required:
            logger.debug(
                f"[DB] {symbol}/{tf} 条数不足: "
                f"请求{limit}条, DB返回{len(bars)}条, 最低需要{min_required}条"
            )
            return []

        logger.info(f"[DB] ✅ {symbol}/{tf} 命中 {len(bars)} 条")
        return bars

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

        if failed:
            logger.warning(f"[远程批量] {len(failed)}/{len(symbols)} 只失败: {failed[:10]}")

        logger.info(
            f"[远程批量] 完成 tf={tf} "
            f"成功={len(coord_results)}/{len(symbols)} "
            f"失败={len(failed)} 总bar数={total_bars}"
        )

        return result
