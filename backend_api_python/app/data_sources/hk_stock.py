"""
=============================================
港股/H股数据源 (HK Stock Data Source)
=============================================

四级降级:
    有 TWELVE_DATA_API_KEY:
        所有周期 → Twelve Data → 腾讯日/周线 → yfinance → AkShare
    无 API Key:
        分钟/小时 → yfinance → AkShare
        日/周线  → 腾讯 fqkline → yfinance → AkShare

支持功能:
    - K线获取 (get_kline): 1m ~ 1W
    - 实时报价 (get_ticker): 腾讯财经接口

熔断保护: 海外源熔断器 (2次失败 / 15min冷却)
    - 四级降级全部失败才返回空，空结果不触发熔断

依赖:
    - yfinance     (必需)
    - requests     (Twelve Data / 腾讯财经)
    - akshare      (可选, 最后降级)
"""

from __future__ import annotations

from typing import Dict, List, Any, Optional

from app.data_sources.base import BaseDataSource
from app.data_sources.circuit_breaker import get_overseas_circuit_breaker
from app.data_sources.tencent import normalize_hk_code, fetch_quote, parse_quote_to_ticker, fetch_kline, tencent_kline_rows_to_dicts
from app.data_sources.asia_stock_kline import (
    normalize_chart_timeframe,
    fetch_twelvedata_klines,
    fetch_yfinance_klines,
    fetch_akshare_minute_klines,
    fetch_akshare_weekly_klines,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)


class HKStockDataSource(BaseDataSource):
    """港股/H股数据源（TwelveData + Tencent + yfinance + AkShare）"""

    name = "HKStock/multi-source"

    def __init__(self):
        self.cb = get_overseas_circuit_breaker()

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        code = normalize_hk_code(symbol)
        parts = fetch_quote(code)
        if not parts:
            return {"last": 0, "symbol": code}
        t = parse_quote_to_ticker(parts)
        return {
            "last": t.get("last", 0),
            "change": t.get("change", 0),
            "changePercent": t.get("changePercent", 0),
            "high": t.get("high", 0),
            "low": t.get("low", 0),
            "open": t.get("open", 0),
            "previousClose": t.get("previousClose", 0),
            "name": t.get("name", ""),
            "symbol": code,
        }

    def get_kline(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        if not self.cb.is_available(self.name):
            return []

        code = normalize_hk_code(symbol)
        tf = normalize_chart_timeframe(timeframe)
        lim = max(int(limit or 300), 1)

        # Tier 1: Twelve Data (paid, most reliable)
        rows = fetch_twelvedata_klines(
            is_hk=True, tencent_code=code, timeframe=tf, limit=lim, before_time=before_time
        )
        if rows:
            self.cb.record_success(self.name)
            return self.filter_and_limit(rows, limit=lim, before_time=before_time)

        # Tier 2: Tencent for daily/weekly (fast, free)
        if tf in ("1D", "1W"):
            tf_map = {"1D": "day", "1W": "week"}
            period = tf_map.get(tf, "day")
            raw_rows = fetch_kline(code, period=period, count=lim, adj="qfq")
            out = tencent_kline_rows_to_dicts(raw_rows)
            if out:
                self.cb.record_success(self.name)
                return self.filter_and_limit(out, limit=lim, before_time=before_time)

        # Tier 3: yfinance (works when Yahoo not rate-limited)
        rows = fetch_yfinance_klines(
            is_hk=True, tencent_code=code, timeframe=tf, limit=lim, before_time=before_time
        )
        if rows:
            self.cb.record_success(self.name)
            return self.filter_and_limit(rows, limit=lim, before_time=before_time)

        # Tier 4: AkShare (fragile overseas, last resort)
        if tf in ("1m", "5m", "15m", "30m", "1H", "4H"):
            rows = fetch_akshare_minute_klines(
                is_hk=True, tencent_code=code, timeframe=tf, limit=lim, before_time=before_time
            )
        elif tf == "1W":
            rows = fetch_akshare_weekly_klines(
                is_hk=True, tencent_code=code, limit=lim, before_time=before_time
            )
        else:
            rows = []

        if rows:
            self.cb.record_success(self.name)
        # 空结果不触发熔断（可能是合法的：休市、代码不存在）
        return self.filter_and_limit(rows, limit=lim, before_time=before_time)
