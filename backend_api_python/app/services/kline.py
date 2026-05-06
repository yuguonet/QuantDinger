""" 
K线数据服务（无缓存版）

所有数据直接从远程数据源获取，不使用任何本地缓存。
"""
from datetime import datetime, timezone, timedelta, time as dt_time
from typing import Dict, List, Any, Optional, Tuple

from app.data_sources import DataSourceFactory
from app.utils.trading_calendar import is_trading_day_today
from app.utils.logger import get_logger

logger = get_logger(__name__)

# ─── 常量 ───────────────────────────────────────────────────────────

DAILY_LIMIT = 1500       # 日线最大拉取条数（约 5 年）

# 非日/周/月线的降级映射
_AGGREGATION_FALLBACK: Dict[str, Tuple[str, int]] = {
    '5m':  ('1m',  5),
    '30m': ('15m', 2),
    '1H':  ('30m', 2),
    '2H':  ('1H',  2),
    '4H':  ('1H',  4),
}

# ─── 工具函数 ───────────────────────────────────────────────────────

_TZ_CN = timezone(timedelta(hours=8))


def _dt_from_ts(ts: int) -> datetime:
    """Unix 秒 → 北京时间 datetime"""
    return datetime.fromtimestamp(ts, tz=_TZ_CN)


def _today_str() -> str:
    """返回今日日期字符串 YYYY-MM-DD（北京时间）"""
    return datetime.now(_TZ_CN).strftime("%Y-%m-%d")


def _ts_from_date(date_str: str) -> int:
    """日期字符串 YYYY-MM-DD → Unix 秒（北京时间 0 点）"""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    dt = dt.replace(tzinfo=_TZ_CN)
    return int(dt.timestamp())


def _is_market_hours() -> bool:
    """判断当前是否为 A 股交易时段"""
    if not is_trading_day_today():
        return False
    now = datetime.now(_TZ_CN)
    t = now.time()
    return (dt_time(9, 15) <= t <= dt_time(11, 30)) or (dt_time(13, 0) <= t <= dt_time(15, 0))


def _iso_week_start(ts: int) -> int:
    """给定时间戳所在周的周一 0 点（北京时间）Unix 秒"""
    dt = _dt_from_ts(ts)
    monday = dt - timedelta(days=dt.isoweekday() - 1)
    return int(monday.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def _month_start(ts: int) -> int:
    """给定时间戳所在月初 0 点（北京时间）Unix 秒"""
    dt = _dt_from_ts(ts)
    return int(dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())


def _bar_field(bar: Dict[str, Any], field: str, default: float = 0.0) -> float:
    """安全取 bar 字段值"""
    try:
        return float(bar.get(field, default))
    except (TypeError, ValueError):
        return default


# ─── 聚合函数 ───────────────────────────────────────────────────────

def _aggregate_fixed_window(source_klines, group_size, limit):
    """将低周期 K 线按固定窗口聚合为高周期"""
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


def _aggregate_daily_to_monthly(daily_bars: List[Dict[str, Any]], limit: int = 240) -> List[Dict[str, Any]]:
    """将日线聚合为月线"""
    if not daily_bars:
        return []
    bars = sorted(daily_bars, key=lambda x: x.get("time", 0))
    groups: Dict[int, List[Dict]] = {}
    order: List[int] = []
    for bar in bars:
        t = bar.get("time", 0)
        if not t:
            continue
        ms = _month_start(t)
        if ms not in groups:
            groups[ms] = []
            order.append(ms)
        groups[ms].append(bar)
    result = []
    for ms in order:
        chunk = groups[ms]
        if not chunk:
            continue
        result.append({
            "time": ms,
            "open": _bar_field(chunk[0], "open"),
            "high": max(_bar_field(b, "high") for b in chunk),
            "low": min(_bar_field(b, "low") for b in chunk),
            "close": _bar_field(chunk[-1], "close"),
            "volume": round(sum(_bar_field(b, "volume") for b in chunk), 2),
        })
    return result[-limit:] if len(result) > limit else result


# ─── 盘中当日 K 线合成 ─────────────────────────────────────────────

def _synthesize_today_candle(symbol: str, market: str) -> Optional[Dict[str, Any]]:
    """用分钟线合成盘中当日未完成 K 线，盘外返回 None"""
    if not _is_market_hours():
        return None

    try:
        now = datetime.now(_TZ_CN)
        market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
        elapsed_min = (now - market_open).total_seconds() / 60

        if elapsed_min < 0:
            return None

        # 根据开盘时间选择合适的分钟周期
        if elapsed_min < 5:
            tf, limit = "1m", 100
        elif elapsed_min < 30:
            tf, limit = "5m", 100
        else:
            tf, limit = "15m", 100

        bars = DataSourceFactory.get_kline(market, symbol, tf, limit)
        if not bars:
            return None

        today_ts = _ts_from_date(_today_str())
        today_bars = [b for b in bars if b.get("time", 0) >= today_ts]
        if not today_bars:
            return None

        today_bars.sort(key=lambda x: x.get("time", 0))
        return {
            "time": today_ts,
            "open": _bar_field(today_bars[0], "open"),
            "high": max(_bar_field(b, "high") for b in today_bars),
            "low": min(_bar_field(b, "low") for b in today_bars),
            "close": _bar_field(today_bars[-1], "close"),
            "volume": round(sum(_bar_field(b, "volume") for b in today_bars), 2),
        }
    except Exception as e:
        logger.warning(f"[Kline] 合成当日失败 {market}:{symbol}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════
#  K线数据服务
# ═══════════════════════════════════════════════════════════════════

class KlineService:
    """K线数据服务（无缓存，直接远程获取）"""

    def __init__(self):
        pass

    def get_cache_dir(self) -> str:
        """获取缓存目录路径（兼容接口，返回空字符串）"""
        return ""

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
        adj: str = "qfq",
    ) -> List[Dict[str, Any]]:
        # 周线/月线：从日线实时聚合
        if timeframe == "1W" and not before_time:
            return self._get_weekly_from_daily(market, symbol, limit, adj=adj)
        if timeframe == "1M" and not before_time:
            return self._get_monthly_from_daily(market, symbol, limit, adj=adj)
        # 日线：直接获取 + 合成当日
        if timeframe == "1D" and not before_time:
            return self._get_daily_kline(market, symbol, limit, adj=adj)
        # 其他周期：直接远程获取
        return self._get_remote_kline(market, symbol, timeframe, limit, before_time, adj=adj)

    # ═══════════════════════════════════════════════════════════════════
    #  周线/月线：由日线聚合
    # ═══════════════════════════════════════════════════════════════════

    def _get_weekly_from_daily(
        self, market: str, symbol: str, limit: int, adj: str = "qfq",
    ) -> List[Dict[str, Any]]:
        """周线由日线实时聚合"""
        daily_limit = min(limit * 5 + 50, DAILY_LIMIT)
        daily = self.get_kline(market, symbol, "1D", daily_limit, adj=adj)
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

    def _get_monthly_from_daily(
        self, market: str, symbol: str, limit: int, adj: str = "qfq",
    ) -> List[Dict[str, Any]]:
        """月线由日线实时聚合"""
        daily_limit = min(limit * 22 + 50, DAILY_LIMIT)
        daily = self.get_kline(market, symbol, "1D", daily_limit, adj=adj)
        if not daily:
            return []
        monthly = _aggregate_daily_to_monthly(daily, limit)
        return monthly[-limit:] if len(monthly) > limit else monthly

    # ═══════════════════════════════════════════════════════════════════
    #  日线：直接获取 + 合成当日
    # ═══════════════════════════════════════════════════════════════════

    def _get_daily_kline(
        self, market: str, symbol: str, limit: int, adj: str = "qfq",
    ) -> List[Dict[str, Any]]:
        """日线直接从远程获取，盘中合成当日未完成 K 线"""
        klines = DataSourceFactory.get_kline(market, symbol, "1D", limit, adj=adj) or []

        # DBKlineBridge 内部的 _fill_today_if_needed 已用 15m 聚合 + 实时行情缓存
        # 补充了当日 bar，这里检查是否已存在，避免重复合成导致 OHLC 不一致
        today_ts = _ts_from_date(_today_str())
        has_today = any(int(b.get("time", 0)) == today_ts for b in klines)

        if has_today:
            # 已有当日 bar（由 DB 桥接层补充），直接截断返回
            return klines[-limit:]

        # 无当日 bar → 合成（盘中）或追加已完成 bar（盘后）
        today_candle = _synthesize_today_candle(symbol, market)
        return self._append_current(klines, today_candle, market, symbol, limit, adj)

    def _append_current(self, bars, today_candle, market, symbol, limit, adj="qfq"):
        """追加当日 K 线（盘中合成 / 闭市后取远程已完成 bar）"""
        result = list(bars)
        if today_candle:
            today_ts = today_candle["time"]
            result = [b for b in result if b.get("time") != today_ts]
            result.append(today_candle)
        else:
            # 盘中合成无结果 → 尝试取当日已完成 bar
            completed = self._try_fetch_completed_bar(market, symbol, adj)
            if completed:
                today_ts = completed["time"]
                result = [b for b in result if b.get("time") != today_ts]
                result.append(completed)
        result.sort(key=lambda x: x["time"])
        return result[-limit:]

    def _try_fetch_completed_bar(self, market, symbol, adj="qfq"):
        """闭市后从远程取当日已完成日线"""
        if not market or _is_market_hours() or not is_trading_day_today():
            return None

        # 午休时段不取（数据不完整）
        now = datetime.now(_TZ_CN)
        if dt_time(11, 30) < now.time() < dt_time(13, 0):
            return None

        today_ts = _ts_from_date(_today_str())
        try:
            bars = DataSourceFactory.get_kline(market, symbol, "1D", 2, adj=adj)
            if bars:
                for b in bars:
                    if b.get("time") == today_ts:
                        return b
        except Exception as e:
            logger.debug(f"[Kline] 获取当日已完成 bar 失败 {market}:{symbol}: {e}")
        return None

    # ═══════════════════════════════════════════════════════════════════
    #  分钟/小时线
    # ═══════════════════════════════════════════════════════════════════

    def _get_remote_kline(self, market, symbol, timeframe, limit, before_time, adj="qfq"):
        klines = DataSourceFactory.get_kline(
            market=market, symbol=symbol, timeframe=timeframe,
            limit=limit, before_time=before_time, adj=adj,
        )

        if not klines:
            klines = self._try_aggregate_from_lower_timeframe(
                market, symbol, timeframe, limit, before_time, adj=adj,
            )

        return klines

    def _try_aggregate_from_lower_timeframe(self, market, symbol, target_timeframe, limit, before_time, adj="qfq"):
        fallback = _AGGREGATION_FALLBACK.get(target_timeframe)
        if not fallback:
            return []
        source_tf, group_size = fallback
        source_limit = limit * group_size + group_size
        try:
            source_klines = DataSourceFactory.get_kline(
                market=market, symbol=symbol, timeframe=source_tf,
                limit=source_limit, before_time=None, adj=adj,
            )
        except Exception:
            return []
        if not source_klines:
            return []
        source_klines.sort(key=lambda x: x['time'])
        return _aggregate_fixed_window(source_klines, group_size, limit)

    # ═══════════════════════════════════════════════════════════════════
    #  价格
    # ═══════════════════════════════════════════════════════════════════

    def get_latest_price(self, market: str, symbol: str) -> Optional[Dict[str, Any]]:
        klines = self.get_kline(market, symbol, '1m', 1)
        return klines[-1] if klines else None

    def get_realtime_price(self, market: str, symbol: str, force_refresh: bool = False) -> Dict[str, Any]:
        result = {
            'price': 0, 'change': 0, 'changePercent': 0,
            'high': 0, 'low': 0, 'open': 0, 'previousClose': 0, 'source': 'unknown'
        }

        try:
            ticker = DataSourceFactory.get_ticker(market, symbol)
            if ticker and ticker.get('last', 0) > 0:
                return {
                    'price': ticker.get('last', 0),
                    'change': ticker.get('change', 0),
                    'changePercent': ticker.get('changePercent') or ticker.get('percentage', 0),
                    'high': ticker.get('high', 0), 'low': ticker.get('low', 0),
                    'open': ticker.get('open', 0), 'previousClose': ticker.get('previousClose', 0),
                    'source': 'ticker'
                }
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
                return {
                    'price': price, 'change': chg, 'changePercent': pct,
                    'high': latest.get('high', 0), 'low': latest.get('low', 0),
                    'open': latest.get('open', 0), 'previousClose': prev,
                    'source': 'kline_1m'
                }
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
                return {
                    'price': price, 'change': chg, 'changePercent': pct,
                    'high': latest.get('high', 0), 'low': latest.get('low', 0),
                    'open': latest.get('open', 0), 'previousClose': prev,
                    'source': 'kline_1d'
                }
        except Exception:
            pass

        return result

    # ═══════════════════════════════════════════════════════════════════
    #  兼容接口
    # ═══════════════════════════════════════════════════════════════════

    def prewarm_all(self, symbols: List[str], market: str = "CNStock") -> Dict[str, bool]:
        """预热入口（无缓存模式下为 no-op）"""
        return {"1D": True}
