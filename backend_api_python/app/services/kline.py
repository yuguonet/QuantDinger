# -*- coding: utf-8 -*-
"""
K线数据服务 — 统一数据层

═══════════════════════════════════════════════════════════════════════
架构概览
═══════════════════════════════════════════════════════════════════════

  KlineService（本层）
      │
      ├─ < 15m 时间框架（1m / 5m）
      │   └─ 走远程 API + 内存缓存
      │       KlineService → Cache → DataSourceFactory → Coordinator → Providers
      │
      └─ 15m+ 时间框架（15m / 30m / 1H / 4H / 1D / 1W）
          ├─ 历史数据（截止昨日）→ kline_clean（PostgreSQL DB）
          └─ 当日数据 → 行情聚合算法
              优先级: 1m(2天) → 5m(5天) → 15m(5天) → ticker 批量行情

═══════════════════════════════════════════════════════════════════════
数据流向
═══════════════════════════════════════════════════════════════════════

  单只K线（15m+）:
    get_kline(market, symbol, tf, limit)
      → _get_historical_from_db()    # DB 查询，截止昨日
      → _get_today_from_lower_tf()   # 当日: 1m→5m→15m→ticker
      → 合并 → adjust_kline()        # 复权后返回

  批量K线（15m+）:
    get_kline_batch(market, symbols, tf, limit)
      → 逐只 _get_historical_from_db()
      → _get_today_batch_from_lower_tf()  # 批量: 低周期K线 + 行情补齐
      → 合并 → adjust_kline()

  单只/批量K线（< 15m）:
    get_kline / get_kline_batch
      → Cache（内存 LRU）→ DataSourceFactory → Coordinator → Providers

  行情:
    get_ticker → Cache（30s TTL）→ DataSourceFactory → race(多源并发)

═══════════════════════════════════════════════════════════════════════
当日行情聚合算法
═══════════════════════════════════════════════════════════════════════

  15m+ 时间框架的当日 bar 不从 DB 读取（DB 数据截止昨日），
  而是从低周期 K 线实时聚合，确保盘中也能拿到当日数据。

  优先级（从高到低）:
    1. 1m K 线（取 2 天 ≈ 480 bar）→ 过滤当日 → 聚合
    2. 5m K 线（取 5 天 ≈ 240 bar）→ 过滤当日 → 聚合
    3. 15m K 线（取 5 天 ≈ 80 bar）→ 过滤当日 → 聚合
    4. ticker 批量行情 → 取最新价构造 bar（open=high=low=close=price）

  聚合公式:
    open  = 当日第一根 bar 的 open
    high  = 所有 bar 的 high 最大值
    low   = 所有 bar 的 low 最小值
    close = 当日最后一根 bar 的 close
    volume = 所有 bar 的 volume 之和

═══════════════════════════════════════════════════════════════════════
1W 周线聚合
═══════════════════════════════════════════════════════════════════════

  1W 不直接查 DB，而是从 1D 日线数据在内存中聚合（最长 5 年 ≈ 1250 个交易日）。
  按 ISO 周分组，每组取最后一个交易日的时间戳作为周 bar 时间。

═══════════════════════════════════════════════════════════════════════
缓存策略
═══════════════════════════════════════════════════════════════════════

  < 15m 数据: 内存 LRU dict（TTL 按周期分级: 1m=60s, 5m=120s）
  15m+ 数据:  无缓存（直接读 DB + 实时聚合，DB 本身即持久化）
  行情:       内存 dict（TTL=30s）
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from datetime import datetime, timedelta, timezone

from app.data_sources.adjustment import adjust_kline
from app.data_sources.cache import get_cache, make_key, get_ttl
from app.data_sources.factory import (
    get_factory, _parse_symbols, _resolve_market, _normalize_symbols,
)
from app.utils.logger import get_logger

# ── 15m+ 时间框架集合，这些周期的历史数据走 DB，当日数据走行情聚合 ──
_HIGH_TIMEFRAMES = {"15m", "30m", "1H", "4H", "1D", "1W"}

# ── 上海时区（A 股交易时间基准） ──
_TZ_SH = timezone(timedelta(hours=8))

logger = get_logger(__name__)


class KlineService:
    """
    K线数据服务 — 统一入口。

    对外暴露 get_kline / get_ticker 等接口，内部根据时间框架自动路由：
      - < 15m: 远程 API + 内存缓存
      - 15m+:  DB 历史 + 当日行情聚合
    """

    def __init__(self):
        """
        初始化服务层。

        获取两个全局单例:
          - _cache:  TieredCache 两层缓存（内存 + 磁盘 feather）
          - _factory: DataSourceFactory 数据源工厂（去重 / fallback / 竞赛）
        """
        self._cache = get_cache()
        self._factory = get_factory()

    # ═══════════════════════════════════════════════════════════════════
    #  内部工具方法
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def _is_high_tf(timeframe: str) -> bool:
        """
        判断是否为 15m 及以上时间框架。

        15m+ 的数据路径与 < 15m 完全不同：
          - 历史: 从 PostgreSQL DB (kline_clean) 读取
          - 当日: 从低周期 K 线实时聚合

        Args:
            timeframe: 时间框架字符串，如 "15m", "1D", "1W"

        Returns:
            True 表示走 DB + 聚合路径，False 表示走远程 API + 缓存路径
        """
        return timeframe in _HIGH_TIMEFRAMES

    @staticmethod
    def _today_cutoff_ts() -> int:
        """
        计算今日 00:00:00 CST 的 Unix 时间戳。

        用于分离"历史数据"和"当日数据"：
          - time < cutoff_ts  → 历史（从 DB 读取）
          - time >= cutoff_ts → 当日（从低周期聚合）

        Returns:
            今日零点的 Unix 时间戳（整数）
        """
        now = datetime.now(_TZ_SH)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return int(today_start.timestamp())

    @staticmethod
    def _bars_to_ts(bars: list) -> list:
        """
        将 kline_clean 返回的 datetime time 字段统一转为 Unix 时间戳(int)。

        kline_clean MarketDataProvider 返回的 bar 格式:
          {"time": datetime(2024,1,15,9,30,tzinfo=...), "open": 10.5, ...}

        本服务统一使用 Unix 时间戳（int），此方法做格式转换。

        Args:
            bars: kline_clean 返回的 bar 列表

        Returns:
            time 字段为 int 的 bar 列表（浅拷贝，不修改原列表）
        """
        result = []
        for b in bars:
            bar = dict(b)  # 浅拷贝，避免修改原数据
            t = bar.get("time")
            if isinstance(t, datetime):
                bar["time"] = int(t.timestamp())
            result.append(bar)
        return result

    @staticmethod
    def _aggregate_bars(bars: list, target_time: int) -> dict:
        """
        将多根小周期 bar 聚合成一根大周期 bar。

        聚合公式:
          open   = 第一根 bar 的 open（时间顺序）
          high   = 所有 bar 的 high 最大值
          low    = 所有 bar 的 low 最小值
          close  = 最后一根 bar 的 close（时间顺序）
          volume = 所有 bar 的 volume 之和

        Args:
            bars:        待聚合的 bar 列表（非空）
            target_time: 聚合后 bar 的目标时间戳（通常为当日零点）

        Returns:
            聚合后的单根 bar dict；bars 为空时返回空 dict
        """
        if not bars:
            return {}
        bars_sorted = sorted(bars, key=lambda b: b.get("time", 0))
        return {
            "time": target_time,
            "open": bars_sorted[0].get("open", 0),
            "high": max(b.get("high", 0) for b in bars_sorted),
            "low": min(b.get("low", 0) for b in bars_sorted),
            "close": bars_sorted[-1].get("close", 0),
            "volume": sum(b.get("volume", 0) for b in bars_sorted),
        }

    # ═══════════════════════════════════════════════════════════════════
    #  DB 数据源（kline_clean）
    # ═══════════════════════════════════════════════════════════════════

    def _get_clean_provider(self):
        """
        获取 kline_clean MarketDataProvider 实例（延迟初始化 + 缓存）。

        MarketDataProvider 封装了 PostgreSQL DB 的 K 线查询，支持：
          - 基础周期直接查: 1m, 5m, 15m
          - 聚合周期从 15m 合成: 30m, 60m, 2H, 4H
          - 日级: 1D, 1W

        延迟初始化: 首次调用时尝试导入和创建，失败则缓存 None 避免重复尝试。
        DB 不可用时返回 None，上层会 fallback 到远程 API。

        Returns:
            MarketDataProvider 实例，或 None（DB 不可用时）
        """
        if not hasattr(self, '_clean_provider'):
            try:
                from app.data_sources.kline_clean import MarketDataProvider
                from app.utils.db_market import get_market_kline_writer
                writer = get_market_kline_writer()
                self._clean_provider = MarketDataProvider(writer)
            except Exception as e:
                logger.warning("[KlineService] kline_clean 不可用: %s", e)
                self._clean_provider = None
        return self._clean_provider

    def _get_historical_from_db(
        self, market: str, symbol: str, timeframe: str, limit: int,
    ) -> list:
        """
        从 kline_clean(DB) 获取历史 K 线数据（截止到昨日 23:59:59）。

        数据路径:
          - 1W: 查 1D 日线 → 在内存中按 ISO 周聚合（最长 5 年 ≈ 1250 个交易日）
          - 15m/30m/1H/4H/1D: 直接查目标周期

        查询时间范围估算:
          - 15m~4H: limit × 1 天（每天约 16 根 15m bar）
          - 1D:     limit × 2 天（含非交易日）
          - 1W:     min(limit × 7, 1250) 天日线后聚合

        Args:
            market:    市场类型，如 "CNStock"
            symbol:    股票代码，如 "SH600519"
            timeframe: 时间框架
            limit:     请求数据条数

        Returns:
            K 线列表（Unix 时间戳），查询失败返回空列表
        """
        provider = self._get_clean_provider()
        if not provider:
            return []
        try:
            now = datetime.now(_TZ_SH)
            # end = 昨日 23:59:59（DB 数据截止到昨日，当日通过聚合补充）
            end = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(seconds=1)

            # ── 1W: 从 1D 日线聚合 ──
            if timeframe == "1W":
                # 查足够多的日线数据，上限 1250 天（≈5 年）
                daily_limit = min(limit * 7, 1250)
                start = end - timedelta(days=daily_limit * 2)
                daily_bars = provider.get_clean_klines(
                    market, symbol, start, end, "1D",
                )
                daily_bars = self._bars_to_ts(daily_bars)
                daily_bars = daily_bars[-daily_limit:]
                if not daily_bars:
                    return []
                return self._aggregate_daily_to_weekly(daily_bars, limit)

            # ── 15m/30m/1H/4H/1D: 直接查目标周期 ──
            _days_per_bar = {
                "15m": 1,   # 每天约 16 根，1 天够用
                "30m": 1,   # 每天约 8 根
                "1H":  1,   # 每天约 4 根
                "4H":  1,   # 每天约 1 根
                "1D":  2,   # 含非交易日，每根约 2 个自然日
            }
            days = limit * _days_per_bar.get(timeframe, 2)
            start = end - timedelta(days=days)
            bars = provider.get_clean_klines(market, symbol, start, end, timeframe)
            bars = self._bars_to_ts(bars)
            return bars[-limit:] if len(bars) > limit else bars
        except Exception as e:
            logger.warning("[KlineService] DB查询失败 %s:%s %s: %s", market, symbol, timeframe, e)
            return []

    @staticmethod
    def _aggregate_daily_to_weekly(daily_bars: list, limit: int) -> list:
        """
        1D → 1W 聚合：将日线按 ISO 周分组，合成周线。

        分组规则:
          - 按 ISO (year, week_number) 分组
          - 跨年的日子（如 12/31）可能属于下一年的 ISO 周，key 会正确反映

        周 bar 时间戳:
          - 取周内最后一个交易日的时间戳（与 tdx_download.py 一致）

        Args:
            daily_bars: 日线列表（必须已按时间排序，Unix 时间戳格式）
            limit:      返回的周线条数上限

        Returns:
            周线列表，最多返回 limit 条
        """
        from collections import OrderedDict
        weeks: OrderedDict[tuple, list] = OrderedDict()
        for bar in daily_bars:
            ts = bar.get("time", 0)
            if not ts:
                continue
            dt = datetime.fromtimestamp(ts, tz=_TZ_SH)
            iso = dt.isocalendar()
            key = (iso[0], iso[1])  # (year, week_number)
            weeks.setdefault(key, []).append(bar)

        result = []
        for bars_in_week in weeks.values():
            bars_in_week.sort(key=lambda b: b.get("time", 0))
            result.append({
                "time": bars_in_week[-1]["time"],  # 周内最后一个交易日
                "open": bars_in_week[0].get("open", 0),
                "high": max(b.get("high", 0) for b in bars_in_week),
                "low": min(b.get("low", 0) for b in bars_in_week),
                "close": bars_in_week[-1].get("close", 0),
                "volume": sum(b.get("volume", 0) for b in bars_in_week),
            })
        return result[-limit:]

    # ═══════════════════════════════════════════════════════════════════
    #  当日行情聚合（单只 + 批量）
    # ═══════════════════════════════════════════════════════════════════

    def _get_today_from_lower_tf(self, market: str, symbol: str) -> Optional[dict]:
        """
        获取单只股票的当日聚合 bar。

        从低周期 K 线实时聚合当日数据，确保盘中也能拿到当日 bar。
        优先级从高到低，拿到即返回：

          1. 1m K 线（取 2 天 ≈ 480 bar）→ 过滤当日 → 聚合
          2. 5m K 线（取 5 天 ≈ 240 bar）→ 过滤当日 → 聚合
          3. 15m K 线（取 5 天 ≈ 80 bar）→ 过滤当日 → 聚合
          4. ticker 实时行情 → 构造 bar（open=high=low=close=last, volume=0）

        Args:
            market: 市场类型
            symbol: 股票代码

        Returns:
            当日聚合 bar dict，无数据时返回 None（如休市日、数据源全挂）
        """
        today_ts = self._today_cutoff_ts()

        # 各周期的请求 bar 数（覆盖足够天数以确保拿到当日数据）
        _tf_limits = {"1m": 480, "5m": 240, "15m": 80}

        for tf in ("1m", "5m", "15m"):
            try:
                # 拉取低周期 K 线（含昨日 + 今日）
                bars = self._factory.fetch_kline_raw(
                    symbol, tf, _tf_limits[tf], market,
                )
                if bars:
                    # 只保留今日及以后的 bar
                    today_bars = [b for b in bars if b.get("time", 0) >= today_ts]
                    if today_bars:
                        return self._aggregate_bars(today_bars, today_ts)
            except Exception:
                pass

        # ── 最终 fallback: ticker 取最新价构造 bar ──
        try:
            ticker = self._factory.fetch_ticker(symbol, market)
            if ticker and ticker.get("last", 0) > 0:
                price = ticker["last"]
                return {
                    "time": today_ts, "open": price, "high": price,
                    "low": price, "close": price, "volume": 0,
                }
        except Exception:
            pass

        return None

    def _get_today_batch_from_lower_tf(
        self, market: str, symbols: list,
    ) -> dict:
        """
        批量获取多只股票的当日聚合 bar。

        与 _get_today_from_lower_tf 逻辑相同，但使用批量接口减少 HTTP 请求数：
          1. 逐级尝试低周期 K 线批量获取（fetch_kline_batch），每级只取未覆盖的 symbol
          2. 仍未覆盖的 symbol 用 fetch_ticker_batch 行情补齐

        Args:
            market:  市场类型
            symbols: 股票代码列表

        Returns:
            {symbol: 当日聚合 bar}，某只股票无数据则不包含在结果中
        """
        today_ts = self._today_cutoff_ts()
        result = {}

        # ── 1. 逐级尝试低周期 K 线，每级只取 missing（避免重复请求） ──
        _tf_limits = {"1m": 480, "5m": 240, "15m": 80}

        for tf in ("1m", "5m", "15m"):
            # 只取还没拿到数据的 symbol
            missing = [s for s in symbols if s not in result]
            if not missing:
                break
            try:
                fetched = self._factory.fetch_kline_batch(
                    missing, tf, _tf_limits[tf], market,
                )
                for sym, bars in (fetched or {}).items():
                    if sym in result:
                        continue
                    today_bars = [b for b in bars if b.get("time", 0) >= today_ts]
                    if today_bars:
                        result[sym] = self._aggregate_bars(today_bars, today_ts)
            except Exception:
                pass

        # ── 2. 行情补齐：用 ticker 批量接口覆盖剩余 symbol ──
        missing = [s for s in symbols if s not in result]
        if missing:
            try:
                tickers = self._factory.fetch_ticker_batch(missing, market)
                for sym, t in (tickers or {}).items():
                    if sym not in result and t and t.get("last", 0) > 0:
                        price = t["last"]
                        result[sym] = {
                            "time": today_ts, "open": price,
                            "high": price, "low": price,
                            "close": price, "volume": 0,
                        }
            except Exception as e:
                logger.warning("[当日聚合] 行情补齐失败: %s", e)

        return result

    # ═══════════════════════════════════════════════════════════════════
    #  K线 — 公开接口
    # ═══════════════════════════════════════════════════════════════════

    def get_kline(
        self,
        market: str,
        symbol: str,
        timeframe: str,
        limit: int = 1000,
        before_time: Optional[int] = None,
        adj: str = "qfq",
    ) -> Any:
        """
        获取 K 线数据 — 最上层统一入口。

        支持单只和批量（symbol 逗号分隔自动路由）。
        根据 timeframe 自动选择数据路径：

          ┌─────────────┬──────────────────────────────────────────────┐
          │ 时间框架     │ 数据路径                                      │
          ├─────────────┼──────────────────────────────────────────────┤
          │ 1m / 5m     │ 远程 API + 内存缓存                          │
          │ 15m ~ 1W    │ DB 历史（截止昨日）+ 当日行情聚合              │
          └─────────────┴──────────────────────────────────────────────┘

        Args:
            market:      市场类型（"CNStock", "Crypto", "USStock" 等）
            symbol:      股票代码，支持逗号分隔批量: "SH600519,SZ000001"
            timeframe:   时间框架（"1m","5m","15m","30m","1H","4H","1D","1W"）
            limit:       数据条数（默认 1000）
            before_time: 分页用，获取此时间戳之前的数据（Unix 时间戳，可选）
            adj:         复权方式（"qfq" 前复权 / "hfq" 后复权 / "" 不复权）

        Returns:
            单只: List[Dict] — K 线列表
            批量: Dict[str, List[Dict]] — {symbol: K 线列表}
        """
        # ── 批量模式: symbol 含逗号 → 委托 get_kline_batch ──
        symbols = _parse_symbols(symbol)
        if len(symbols) > 1:
            resolved_market = _resolve_market(market, symbols[0])
            if not resolved_market:
                raise ValueError(
                    f"批量K线必须传 market 参数，无法从 '{symbols[0]}' 推断市场"
                )
            symbols = _normalize_symbols(symbols, resolved_market)
            return self.get_kline_batch(resolved_market, symbols, timeframe, limit)

        # ── 单只模式 ──
        symbol = symbols[0] if symbols else symbol
        resolved_market = _resolve_market(market, symbol)

        # ══════════════════════════════════════════════════════════════
        #  15m+ 时间框架: DB 历史 + 当日行情聚合
        # ══════════════════════════════════════════════════════════════
        if self._is_high_tf(timeframe):

            # ── before_time 模式（历史翻页）: 直接查 DB ──
            if before_time:
                provider = self._get_clean_provider()
                if provider:
                    try:
                        end_dt = datetime.fromtimestamp(before_time, tz=_TZ_SH)

                        # 1W 翻页: 查 1D 数据后聚合，再按 before_time 过滤
                        if timeframe == "1W":
                            daily_limit = min(limit * 7, 1250)
                            start_dt = end_dt - timedelta(days=daily_limit * 2)
                            daily_bars = provider.get_clean_klines(
                                resolved_market, symbol, start_dt, end_dt, "1D",
                            )
                            daily_bars = self._bars_to_ts(daily_bars)
                            # 只保留 before_time 之前的 bar
                            daily_bars = [
                                b for b in daily_bars
                                if b.get("time", 0) < before_time
                            ]
                            if not daily_bars:
                                return []
                            agg = self._aggregate_daily_to_weekly(daily_bars, limit)
                            return adjust_kline(symbol, agg, adj)

                        # 15m~1D 翻页: 直接查目标周期
                        _days_per_bar = {
                            "15m": 1, "30m": 1, "1H": 1, "4H": 1, "1D": 2,
                        }
                        days = limit * _days_per_bar.get(timeframe, 2)
                        start_dt = end_dt - timedelta(days=days)
                        bars = provider.get_clean_klines(
                            resolved_market, symbol, start_dt, end_dt, timeframe,
                        )
                        bars = self._bars_to_ts(bars)
                        return adjust_kline(symbol, bars[-limit:], adj)
                    except Exception as e:
                        logger.warning("[KlineService] DB翻页失败: %s", e)

                # DB 不可用 → fallback 到远程 API
                raw = self._factory.fetch_kline_raw(
                    symbol, timeframe, limit, resolved_market,
                )
                return adjust_kline(symbol, raw or [], adj)

            # ── 常规模式: DB 历史 + 当日聚合 ──
            # 1. 从 DB 读取截止昨日的历史数据
            historical = self._get_historical_from_db(
                resolved_market, symbol, timeframe, limit,
            )
            # 2. 从低周期 K 线聚合当日 bar
            today_bar = self._get_today_from_lower_tf(resolved_market, symbol)
            # 3. 合并: 历史 + 当日
            combined = list(historical)
            if today_bar:
                combined.append(today_bar)
            # 4. 统一复权后返回
            return adjust_kline(symbol, combined, adj)

        # ══════════════════════════════════════════════════════════════
        #  < 15m 时间框架: 远程 API + 内存缓存
        # ══════════════════════════════════════════════════════════════

        # before_time 模式（历史翻页）: 不缓存，直接穿透
        if before_time:
            raw = self._factory.fetch_kline_raw(
                symbol, timeframe, limit, resolved_market
            )
            return adjust_kline(symbol, raw or [], adj)

        # 1. 查缓存（原始数据，未复权）
        key = make_key("kline", symbol, timeframe, limit)
        data_type = f"kline:{timeframe}"
        cached = self._cache.get(key, data_type)
        if cached is not None:
            # 缓存命中 → 复权后返回
            return adjust_kline(symbol, cached, adj)

        # 2. 缓存未命中 → DataSourceFactory 取原始数据
        raw = self._factory.fetch_kline_raw(
            symbol, timeframe, limit, resolved_market
        )

        # 3. 写缓存（存原始数据，复权在返回时动态计算）
        if raw:
            self._cache.set(key, raw, get_ttl("kline", timeframe), data_type)

        # 4. 复权后返回
        return adjust_kline(symbol, raw or [], adj)

    def get_kline_batch(
        self,
        market: str,
        symbols: List[str],
        timeframe: str,
        limit: int,
        cached_symbols: Optional[set] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        批量获取多只股票的 K 线数据。

        与 get_kline 逻辑对齐，但使用批量接口提升效率：
          - 15m+: 逐只查 DB 历史 + 批量获取当日聚合
          - < 15m: 缓存分离 + DataSourceFactory 批量协调

        Args:
            market:    市场类型
            symbols:   股票代码列表（已去重、已加前缀）
            timeframe: 时间框架
            limit:     数据条数

        Returns:
            {symbol: K 线列表}，仅包含成功获取的 symbol
        """
        if not symbols:
            return {}

        resolved_market = _resolve_market(market, symbols[0] if symbols else "")

        # ══════════════════════════════════════════════════════════════
        #  15m+ 时间框架: DB 历史 + 当日批量聚合
        # ══════════════════════════════════════════════════════════════
        if self._is_high_tf(timeframe):
            result = {}

            # 1. 逐只从 DB 读取历史数据
            for sym in symbols:
                hist = self._get_historical_from_db(
                    resolved_market, sym, timeframe, limit,
                )
                if hist:
                    result[sym] = hist

            # 2. 批量获取当日聚合（低周期 K 线 + ticker 行情补齐）
            today_map = self._get_today_batch_from_lower_tf(
                resolved_market, symbols,
            )

            # 3. 合并: 每只股票的历史 + 当日
            for sym in symbols:
                today_bar = today_map.get(sym)
                if today_bar:
                    hist = result.get(sym, [])
                    hist.append(today_bar)
                    result[sym] = hist

            # 4. 统一复权后返回
            return {
                sym: adjust_kline(sym, bars, "qfq")
                for sym, bars in result.items()
            }

        # ══════════════════════════════════════════════════════════════
        #  < 15m 时间框架: 缓存分离 + DataSourceFactory 批量协调
        # ══════════════════════════════════════════════════════════════
        data_type = f"kline:{timeframe}"
        result = {}
        need_fetch = []

        # 1. 分离: 已缓存的直接用，未缓存的加入待取列表
        for sym in symbols:
            key = make_key("kline", sym, timeframe, limit)
            cached = self._cache.get(key, data_type)
            if cached is not None:
                result[sym] = adjust_kline(sym, cached, "qfq")
            else:
                need_fetch.append(sym)

        if not need_fetch:
            logger.info("[批量K线] 全部 %d 只命中缓存", len(symbols))
            return result

        logger.info(
            f"[批量K线] 缓存命中 {len(result)}/{len(symbols)}，"
            f"需取 {len(need_fetch)}"
        )

        # 2. 批量获取: DataSourceFactory 通过 Coordinator 并发协调
        #    on_raw_data 回调: 获取到原始数据时立即写缓存，避免二次请求
        ttl = get_ttl("kline", timeframe)

        def _write_raw_to_cache(sym: str, raw_bars: List[Dict[str, Any]]):
            """回调: Coordinator 获取到原始数据时写入缓存"""
            key = make_key("kline", sym, timeframe, limit)
            self._cache.set(key, raw_bars, ttl, data_type)

        fetched = self._factory.fetch_kline_batch(
            need_fetch, timeframe, limit, resolved_market,
            on_raw_data=_write_raw_to_cache,
        )

        # 3. 合并结果
        result.update(fetched)

        # 4. 记录失败
        failed = [s for s in need_fetch if s not in fetched]
        if failed:
            logger.warning(
                f"[批量K线] {len(failed)} 只获取失败: "
                f"{failed[:5]}{'...' if len(failed) > 5 else ''}"
            )

        logger.info(
            f"[批量K线] 最终 {len(result)}/{len(symbols)}，失败 {len(failed)}"
        )
        return result

    # ═══════════════════════════════════════════════════════════════════
    #  行情 — 公开接口
    # ═══════════════════════════════════════════════════════════════════

    def get_ticker(self, market: str, symbol: str) -> Any:
        """
        获取实时行情 — 支持单只或批量（symbol 逗号分隔）。

        数据路径: 内存缓存(30s TTL) → DataSourceFactory → race(多源并发竞赛)
        race 模式: 多个 provider 同时请求，第一个有效结果返回。

        Args:
            market: 市场类型
            symbol: 股票代码，支持逗号分隔: "SH600519,SZ000001"

        Returns:
            单只: Dict — {"last": 15.8, "change": 0.3, ...}
            批量: Dict[str, Dict] — {symbol: ticker_dict}
        """
        # ── 批量模式 ──
        symbols = _parse_symbols(symbol)
        if len(symbols) > 1:
            resolved_market = _resolve_market(market, symbols[0])
            if not resolved_market:
                raise ValueError(
                    f"批量行情必须传 market 参数，无法从 '{symbols[0]}' 推断市场"
                )
            symbols = _normalize_symbols(symbols, resolved_market)
            return self._get_ticker_batch(resolved_market, symbols)

        # ── 单只模式 ──
        symbol = symbols[0] if symbols else symbol
        resolved_market = _resolve_market(market, symbol)

        # 1. 查内存缓存（TTL=30s）
        key = make_key("ticker", symbol)
        cached = self._cache.get(key, "ticker")
        if cached is not None:
            return cached

        # 2. 缓存未命中 → DataSourceFactory（内部走 race 多源竞赛）
        result = self._factory.fetch_ticker(symbol, resolved_market)

        # 3. 写缓存（只缓存有效数据）
        if result and result.get("last", 0) > 0:
            self._cache.set(key, result, get_ttl("ticker"), "ticker")

        return result or {"last": 0, "symbol": symbol}

    def _get_ticker_batch(
        self, market: str, symbols: List[str],
    ) -> Dict[str, Dict[str, Any]]:
        """
        批量获取实时行情。

        流程:
          1. 逐只查内存缓存 → 命中直接用
          2. 未命中的 → fetch_ticker_batch（内部走 race 批量接口）
          3. 写缓存 + 合并返回

        Args:
            market:  市场类型
            symbols: 股票代码列表

        Returns:
            {symbol: ticker_dict}，仅包含 last > 0 的有效数据
        """
        result: Dict[str, Dict[str, Any]] = {}
        need_fetch: List[str] = []

        # 1. 分离已缓存和需取的
        for sym in symbols:
            key = make_key("ticker", sym)
            cached = self._cache.get(key, "ticker")
            if cached is not None:
                result[sym] = cached
            else:
                need_fetch.append(sym)

        if not need_fetch:
            logger.info("[批量行情] 全部 %d 只命中缓存", len(symbols))
            return result

        logger.info(
            f"[批量行情] 缓存命中 {len(result)}/{len(symbols)}，"
            f"需取 {len(need_fetch)}"
        )

        # 2. 批量获取（内部走 race: 多源并发，第一个有效结果返回）
        fetched = self._factory.fetch_ticker_batch(need_fetch, market)

        # 3. 写缓存 + 合并结果
        for sym, data in fetched.items():
            if data and data.get("last", 0) > 0:
                key = make_key("ticker", sym)
                self._cache.set(key, data, get_ttl("ticker"), "ticker")
                result[sym] = data

        # 4. 记录失败
        failed = [s for s in need_fetch if s not in result]
        if failed:
            logger.warning(
                f"[批量行情] {len(failed)} 只获取失败: "
                f"{failed[:5]}{'...' if len(failed) > 5 else ''}"
            )

        logger.info(
            f"[批量行情] 最终 {len(result)}/{len(symbols)}，失败 {len(failed)}"
        )
        return result

    # ═══════════════════════════════════════════════════════════════════
    #  便捷方法
    # ═══════════════════════════════════════════════════════════════════

    def get_latest_price(self, market: str, symbol: str) -> Optional[Dict[str, Any]]:
        """
        获取最新价格（便捷方法）。

        内部取 1m K 线最后一条，适用于快速获取价格的场景。

        Args:
            market: 市场类型
            symbol: 股票代码

        Returns:
            最新一条 K 线 bar，无数据返回 None
        """
        klines = self.get_kline(market, symbol, "1m", 1)
        return klines[-1] if klines else None

    def get_realtime_price(
        self, market: str, symbol: str, force_refresh: bool = False,
    ) -> Dict[str, Any]:
        """
        获取实时价格（兼容旧接口）。

        两级 fallback:
          1. ticker 行情（优先，数据最实时）
          2. 1D K 线（fallback，盘后可用）

        Args:
            market:        市场类型
            symbol:        股票代码
            force_refresh: 预留参数（暂未使用）

        Returns:
            价格信息 dict，包含 price/change/changePercent/high/low/open/previousClose/source
        """
        result = {
            'price': 0, 'change': 0, 'changePercent': 0,
            'high': 0, 'low': 0, 'open': 0, 'previousClose': 0,
            'source': 'unknown',
        }

        # ── 优先: ticker 实时行情 ──
        try:
            ticker = self.get_ticker(market, symbol)
            if ticker and ticker.get('last', 0) > 0:
                return {
                    'price': ticker.get('last', 0),
                    'change': ticker.get('change', 0),
                    'changePercent': (
                        ticker.get('changePercent')
                        or ticker.get('percentage', 0)
                    ),
                    'high': ticker.get('high', 0),
                    'low': ticker.get('low', 0),
                    'open': ticker.get('open', 0),
                    'previousClose': ticker.get('previousClose', 0),
                    'source': 'ticker',
                }
        except Exception:
            pass

        # ── fallback: 1D K 线 ──
        try:
            klines = self.get_kline(market, symbol, '1D', 2)
            if klines and len(klines) > 0:
                latest = klines[-1]
                prev = (
                    klines[-2]['close']
                    if len(klines) > 1
                    else latest.get('open', 0)
                )
                price = latest.get('close', 0)
                chg = round(price - prev, 4) if prev else 0
                pct = round(chg / prev * 100, 2) if prev and prev > 0 else 0
                return {
                    'price': price, 'change': chg, 'changePercent': pct,
                    'high': latest.get('high', 0),
                    'low': latest.get('low', 0),
                    'open': latest.get('open', 0),
                    'previousClose': prev,
                    'source': 'kline_1d',
                }
        except Exception:
            pass

        return result

    # ═══════════════════════════════════════════════════════════════════
    #  缓存管理
    # ═══════════════════════════════════════════════════════════════════

    def get_cache_dir(self) -> str:
        """
        获取磁盘缓存目录路径。

        Returns:
            缓存目录绝对路径字符串
        """
        return str(self._cache.disk._base_dir)

    def invalidate(self, symbol: str = None, data_type: str = None) -> int:
        """
        清除缓存。

        用法:
            invalidate()                    # 清全部（内存 + 磁盘）
            invalidate(data_type="kline")   # 清所有 K 线缓存
            invalidate(symbol="SH600519")   # 清某只股票的所有缓存

        注意: 15m+ 数据来自 DB（无缓存），此方法仅清理 <15m 的内存/磁盘缓存。

        Returns:
            清除的缓存条目数
        """
        if symbol:
            from app.data_sources.normalizer import detect_market
            market, digits = detect_market(symbol)
            count = 0

            # 内存: 遍历所有 key，找到包含该 symbol 的删掉
            with self._cache.memory._lock:
                keys_to_delete = [
                    k for k in self._cache.memory._store
                    if f":{symbol}:" in k or k.endswith(f":{symbol}")
                ]
            for k in keys_to_delete:
                self._cache.memory.delete(k)
                count += 1

            # 磁盘: 按 data_type 逐个清
            if digits:
                for dt in ("kline", "stock_info"):
                    self._cache.disk.delete(dt, market, digits)
                    count += 1

            return count
        if data_type:
            return self._cache.clear(data_type)
        return self._cache.clear()

    def cache_stats(self) -> Dict[str, Any]:
        """
        获取缓存统计信息。

        Returns:
            {"memory": {size, hits, misses, hit_rate}, "disk": {files, hits, misses, hit_rate}}
        """
        return self._cache.stats()

    def source_stats(self) -> Dict[str, Any]:
        """
        获取各数据源的吞吐统计。

        委托给 DataSourceFactory，返回每个 provider 的 QPS、成功率、平均延迟。

        Returns:
            {source_name: {qps, success_rate, avg_latency, ...}}
        """
        return self._factory.source_stats()

    # ═══════════════════════════════════════════════════════════════════
    #  预热
    # ═══════════════════════════════════════════════════════════════════

    def prewarm_all(
        self, symbols: List[str], market: str = "CNStock",
    ) -> Dict[str, bool]:
        """
        批量预热缓存入口。

        15m+ 数据来自 DB（已持久化），无需预热。
        < 15m 数据走远程 API + 内存缓存，首次请求时自动填充。

        Args:
            symbols: 股票代码列表
            market:  市场类型

        Returns:
            {timeframe: success_bool} 预热结果
        """
        results = {}
        # 15m+ 来自 DB，不需要预热
        results["1D"] = True
        logger.info("[预热] %s 1D: 跳过（数据来自DB）", market)
        return results
