"""
中国A股数据源 — 多层 fallback + 超时溢出 + 熔断轮询

Fallback Chain (K线):
  1. 腾讯 fqkline       (免费, 日/周线)
  2. 腾讯 mkline         (免费, 分钟线: m1/m5/m15/m30/m60)
  3. 新浪 JSONP           (免费, 分钟线+日K: scale=1/5/15/30/60/240)
  4. 新浪 fqkline        (免费, 日线)
  5. AkShare             (国内兜底, 分钟线/周线)
  6. 东方财富直接API     (免费, 全周期)
  7. Twelve Data         (付费, 海外降级)

Fallback Chain (实时行情/报价):
  1. 腾讯 qt.gtimg.cn   (免费, 快)
  2. 东方财富 push2     (免费, 全字段)
  3. 新浪 hq.sinajs.cn  (免费, 基础字段)

超时机制:
  - 每个数据源调用封装在 _fetch_with_timeout() 中
  - 单源超时默认 10s（可通过 DATA_SOURCE_TIMEOUT 配置）
  - 超时后立即切换下一数据源，不等待

熔断机制:
  - 引用 circuit_breaker.py 中的全局熔断器
  - 连续失败 N 次（默认2次）后熔断 3 分钟
  - 熔断期间自动跳过该数据源
"""

from __future__ import annotations

import concurrent.futures
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.data_sources.base import BaseDataSource
from app.data_sources.tencent import (
    normalize_cn_code,
    fetch_quote,
    parse_quote_to_ticker,
    fetch_kline,
    tencent_kline_rows_to_dicts,
    fetch_minute_kline,
    tencent_minute_kline_to_dicts,
)
from app.data_sources.asia_stock_kline import (
    normalize_chart_timeframe,
    fetch_twelvedata_klines,
    fetch_yfinance_klines,
    fetch_akshare_minute_klines,
    fetch_akshare_weekly_klines,
)
from app.data_sources.sina import fetch_sina_kline, sina_kline_to_ticker, fetch_sina_minute_kline
from app.data_sources.eastmoney import fetch_eastmoney_kline, eastmoney_kline_to_ticker
from app.data_sources.circuit_breaker import get_realtime_circuit_breaker
from app.data_sources.cache_manager import (
    get_realtime_cache,
    get_kline_cache,
    generate_kline_cache_key,
)
from app.config.data_sources import DataSourceConfig
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ================================================================
# 超时封装工具
# ================================================================

# 共享线程池 — 避免每次调用创建/销毁 executor
_TIMEOUT_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="cnstock-timeout",
)


def _get_timeout() -> float:
    """统一获取超时配置"""
    return float(DataSourceConfig.DEFAULT_TIMEOUT or 10)


def _fetch_with_timeout(
    func: Callable,
    *args,
    timeout: Optional[float] = None,
    source_name: str = "",
    **kwargs,
) -> Tuple[Optional[Any], Optional[str]]:
    """
    在独立线程中执行 func，超时后放弃。

    Returns:
        (result, error)  —— result 非 None 表示成功，error 非 None 表示失败原因。
    """
    if timeout is None:
        timeout = _get_timeout()

    future = _TIMEOUT_EXECUTOR.submit(func, *args, **kwargs)
    try:
        result = future.result(timeout=timeout)
        return result, None
    except concurrent.futures.TimeoutError:
        logger.warning(f"[超时] {source_name} 调用超时 ({timeout}s)")
        future.cancel()
        return None, f"{source_name} timeout ({timeout}s)"
    except Exception as e:
        logger.warning(f"[异常] {source_name} 调用失败: {e}")
        return None, f"{source_name} error: {e}"


# ================================================================
# K线数据校验
# ================================================================

def _validate_kline_result(bars: List[Dict[str, Any]], min_bars: int = 1) -> bool:
    """
    校验K线数据基本合理性。
    返回 True 表示数据可用，False 表示应丢弃。
    """
    if not bars or len(bars) < min_bars:
        return False

    # 检查最近一根K线的基本合理性
    last = bars[-1]
    if not isinstance(last, dict):
        return False

    # time 必须存在且为正整数
    t = last.get("time")
    if not t or not isinstance(t, (int, float)) or t <= 0:
        return False

    # close 不能为 0（停牌/无效数据）
    if last.get("close", 0) <= 0:
        return False

    # high >= low（允许都为0的情况）
    h, low = last.get("high", 0), last.get("low", 0)
    if h > 0 and low > 0 and h < low:
        return False

    return True


def _strip_cn_prefix(code: str) -> str:
    """
    安全剥离 A股代码的 SH/SZ/BJ 前缀。
    不使用 str.lstrip() 避免字符集误伤。
    """
    s = (code or "").strip()
    upper = s.upper()
    if upper.startswith(("SH", "SZ", "BJ")) and len(s) >= 3:
        return s[2:]
    return s


# ================================================================
# 东财批量行情（clist 全市场接口）
# ================================================================

def _fetch_eastmoney_batch_quotes(
    symbols: List[str],
    timeout: int = 15,
) -> Dict[str, Dict[str, Any]]:
    """
    通过东方财富 clist 接口批量获取当日行情。

    一次请求拉全市场（约 5000 只），从中筛选目标股票。
    字段: f2=最新价, f5=成交量, f6=成交额, f7=振幅,
          f12=代码, f15=最高, f16=最低, f17=开盘, f18=昨收

    Returns:
        {symbol: {"time", "open", "high", "low", "close", "volume"}}
    """
    if not symbols:
        return {}

    # 建立纯数字代码 → symbol 映射
    code_set: Dict[str, str] = {}
    for sym in symbols:
        raw = sym.strip()
        for prefix in ("SH", "SZ", "BJ", "sh", "sz", "bj"):
            if raw.startswith(prefix):
                raw = raw[2:]
                break
        if raw.isdigit() and len(raw) == 6:
            code_set[raw] = sym

    if not code_set:
        return {}

    try:
        from app.data_sources.rate_limiter import get_request_headers, get_eastmoney_limiter
        import requests as _req

        limiter = get_eastmoney_limiter()
        limiter.wait()

        # 沪深A股: m:0+t:6(深主板) m:0+t:80(深创业板) m:1+t:2(沪主板) m:1+t:23(科创板)
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": 1, "pz": 6000, "po": 1, "np": 1,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": 2, "invt": 2,
            "fid": "f3",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
            "fields": "f2,f5,f6,f12,f15,f16,f17,f18",
        }
        resp = _req.get(
            url,
            headers=get_request_headers(referer="https://quote.eastmoney.com/"),
            timeout=timeout,
        )
        data = resp.json()
        diff = ((data.get("data") or {}).get("diff")) or []
    except Exception as e:
        logger.warning(f"[东财批量] clist 请求失败: {e}")
        return {}

    now = datetime.now(timezone(timedelta(hours=8)))
    today_ts = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    result: Dict[str, Dict[str, Any]] = {}

    for item in diff:
        code = str(item.get("f12", "")).strip()
        sym = code_set.get(code)
        if not sym:
            continue
        try:
            last = float(item.get("f2", 0))
            if last <= 0:
                continue
            open_p = float(item.get("f17", 0))
            high = float(item.get("f15", 0))
            low = float(item.get("f16", 0))
            vol = float(item.get("f5", 0))
            result[sym] = {
                "time": today_ts,
                "open": round(open_p, 4),
                "high": round(high, 4),
                "low": round(low, 4),
                "close": round(last, 4),
                "volume": round(vol, 2),
            }
        except (ValueError, TypeError):
            continue

    return result


# ================================================================
# 日线聚合辅助（月线批量拉取用）
# ================================================================

def _month_start_from_dt(dt: datetime) -> int:
    """计算给定 datetime 所在月初的 Unix 时间戳"""
    return int(dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())


def _aggregate_daily_to_monthly(daily_bars: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
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
        dt = datetime.fromtimestamp(t, tz=timezone(timedelta(hours=8)))
        ms = _month_start_from_dt(dt)
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
            "open": float(chunk[0].get("open", 0)),
            "high": max(float(b.get("high", 0)) for b in chunk),
            "low": min(float(b.get("low", 0)) for b in chunk),
            "close": float(chunk[-1].get("close", 0)),
            "volume": round(sum(float(b.get("volume", 0)) for b in chunk), 2),
        })
    return result[-limit:] if len(result) > limit else result


# ================================================================
# 数据源类
# ================================================================

class CNStockDataSource(BaseDataSource):
    """A股数据源（TwelveData + Tencent + EastMoney + Sina + yfinance + AkShare）"""

    name = "CNStock/multi-source"

    def __init__(self):
        self.circuit_breaker = get_realtime_circuit_breaker()
        self.realtime_cache = get_realtime_cache()
        self.kline_cache = get_kline_cache()

    # ----------------------------------------------------------
    # 实时行情 / 报价
    # ----------------------------------------------------------

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """
        获取最新报价，fallback chain:
          腾讯 → 东方财富 → 新浪
        """
        code = normalize_cn_code(symbol)  # e.g. "SH600519"
        raw_code = _strip_cn_prefix(code)  # e.g. "600519"

        # 先检查缓存
        cache_key = f"ticker:{code}"
        cached = self.realtime_cache.get(cache_key)
        if cached:
            return cached

        cb = self.circuit_breaker
        timeout = min(_get_timeout(), 8)  # 行价用较短超时

        sources: List[Tuple[str, Callable[[], Optional[Dict[str, Any]]]]] = [
            ("tencent_quote", lambda: self._try_tencent_ticker(code)),
            ("eastmoney_quote", lambda: eastmoney_kline_to_ticker(raw_code)),
            ("sina_quote", lambda: sina_kline_to_ticker(code)),
        ]

        for source_name, fetcher in sources:
            if not cb.is_available(source_name):
                logger.debug(f"[熔断跳过] {source_name}")
                continue

            result, error = _fetch_with_timeout(
                fetcher,
                timeout=timeout,
                source_name=source_name,
            )

            if result is not None and result.get("last", 0) > 0:
                cb.record_success(source_name)
                self.realtime_cache.set(cache_key, result, ttl=600)
                return result

            cb.record_failure(source_name, error or "empty")

        logger.warning(f"[行情] 所有数据源均失败: {symbol}")
        return {"last": 0, "symbol": code}

    def _try_tencent_ticker(self, code: str) -> Optional[Dict[str, Any]]:
        parts = fetch_quote(code)
        if not parts:
            return None
        t = parse_quote_to_ticker(parts)
        last = t.get("last", 0)
        if last <= 0:
            return None
        return {
            "last": last,
            "change": t.get("change", 0),
            "changePercent": t.get("changePercent", 0),
            "high": t.get("high", 0),
            "low": t.get("low", 0),
            "open": t.get("open", 0),
            "previousClose": t.get("previousClose", 0),
            "name": t.get("name", ""),
            "symbol": code,
        }

    # ----------------------------------------------------------
    # K线数据
    # ----------------------------------------------------------

    def get_kline(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        获取K线数据，fallback chain:
          腾讯(日/周) → 腾讯(分钟) → 新浪(分钟) → 新浪(日) → AkShare → 东方财富 → Twelve Data
        """
        code = normalize_cn_code(symbol)
        tf = normalize_chart_timeframe(timeframe)
        lim = max(int(limit or 300), 1)

        # 先检查缓存
        cache_key = generate_kline_cache_key(code, tf, lim, before_time)
        cached = self.kline_cache.get(cache_key)
        if cached:
            return cached

        cb = self.circuit_breaker
        timeout = _get_timeout()

        # ---- 构建 fallback 列表 ----
        sources = self._build_kline_sources(code, tf, lim, before_time)

        # ---- 逐源尝试 ----
        errors: List[str] = []
        t_total_start = time.time()

        for source_name, fetcher in sources:
            if not cb.is_available(source_name):
                errors.append(f"[{source_name}] circuit open")
                continue

            t_source_start = time.time()
            result, error = _fetch_with_timeout(
                fetcher,
                timeout=timeout,
                source_name=source_name,
            )
            elapsed = time.time() - t_source_start

            # 校验结果
            if _validate_kline_result(result):
                cb.record_success(source_name)
                out = self.filter_and_limit(
                    result, limit=lim, before_time=before_time,
                    after_time=after_time, truncate=(after_time is None),
                )
                total_elapsed = time.time() - t_total_start
                logger.info(
                    f"[K线成功] {symbol} tf={tf} 来源={source_name} "
                    f"bars={len(out)} 耗时={total_elapsed:.2f}s"
                )
                # 写入缓存
                kline_ttl = 300.0 if tf in ("1D", "1W") else 120.0
                self.kline_cache.set(cache_key, out, ttl=kline_ttl)
                return out

            cb.record_failure(source_name, error or "empty/invalid")
            errors.append(f"[{source_name}] {error or 'empty'} ({elapsed:.1f}s)")
            logger.info(f"[K线失败] {symbol} tf={tf} {source_name} → 下一数据源")

        total_elapsed = time.time() - t_total_start
        logger.warning(
            f"[K线终止] {symbol} tf={tf} 所有数据源失败 "
            f"(耗时={total_elapsed:.1f}s): {'; '.join(errors)}"
        )
        return []

    def get_kline_batch(
        self,
        symbols: List[str],
        timeframe: str,
        limit: int,
        cached_symbols: Optional[set] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        批量获取多只股票 K 线数据。

        日线优化：
          1. 用腾讯批量行情 API（一个请求）拿所有股票的当日数据
          2. 只对缺失历史的股票逐只调 K 线 API 补数据
          3. 合并：历史 K 线 + 当日行情 → 完整结果

        月线优化：
          1. 先走日线批量路径拉取日线数据
          2. 再从日线聚合为月线

        非日线且非月线 / 不支持批量的市场：并发逐只拉取。
        """
        if not symbols:
            return {}

        tf = normalize_chart_timeframe(timeframe)
        result: Dict[str, List[Dict[str, Any]]] = {}

        # ── 月线：走日线批量 + 聚合 ──
        if tf == "1M":
            daily_limit = min(limit * 21 + 100, 5000)
            daily_result = self.get_kline_batch(
                symbols, "1D", daily_limit, cached_symbols=cached_symbols,
            )
            for sym, daily_bars in daily_result.items():
                if daily_bars:
                    result[sym] = _aggregate_daily_to_monthly(daily_bars, limit)
            return result

        # ── 日线走批量行情优化 ──
        if cached_symbols is None:
            cached_symbols = set()

        # ─── 日线批量优化 ───
        # 1) 批量行情拿当日数据（一个 HTTP 请求），腾讯优先 → 新浪 fallback
        codes = [normalize_cn_code(s).lower() for s in symbols]
        # 腾讯/新浪返回小写代码（sh600519），用小写做 key 匹配
        code_to_sym = {normalize_cn_code(s).lower(): s for s in symbols}
        today_bars: Dict[str, Dict[str, Any]] = {}

        today_ts = int(datetime.now(timezone(timedelta(hours=8))).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp())

        # 尝试腾讯批量行情
        try:
            from app.data_sources.tencent import fetch_quotes_batch
            quotes = fetch_quotes_batch(codes)
            for code, parts in quotes.items():
                sym = code_to_sym.get(code)
                if not sym or len(parts) < 35:
                    continue
                try:
                    last = float(parts[3])
                    open_p = float(parts[5])
                    high = float(parts[33]) if parts[33] else last
                    low = float(parts[34]) if parts[34] else last
                    vol = float(parts[6]) if parts[6] else 0
                    if last <= 0:
                        continue
                    today_bars[sym] = {
                        "time": today_ts,
                        "open": round(open_p, 4),
                        "high": round(high, 4),
                        "low": round(low, 4),
                        "close": round(last, 4),
                        "volume": round(vol, 2),
                    }
                except (ValueError, IndexError):
                    continue
            logger.info(f"[K线批量] 腾讯批量行情获取 {len(today_bars)}/{len(symbols)} 只当日数据")
        except Exception as e:
            logger.warning(f"[K线批量] 腾讯批量行情失败: {e}")

        # 腾讯失败或结果不足 → fallback 新浪批量行情
        if len(today_bars) < len(symbols):
            try:
                from app.data_sources.sina import fetch_sina_quotes_batch
                missing_codes = [c for c, s in code_to_sym.items() if s not in today_bars]
                if missing_codes:
                    sina_quotes = fetch_sina_quotes_batch(missing_codes)
                    for code_str, q in sina_quotes.items():
                        sym = code_to_sym.get(code_str)
                        if not sym:
                            continue
                        last = q.get("last", 0)
                        if last <= 0:
                            continue
                        today_bars[sym] = {
                            "time": today_ts,
                            "open": round(q.get("open", last), 4),
                            "high": round(q.get("high", last), 4),
                            "low": round(q.get("low", last), 4),
                            "close": round(last, 4),
                            "volume": round(q.get("volume", 0), 2),
                        }
                    logger.info(f"[K线批量] 新浪批量行情补充 {len(sina_quotes)} 只，共 {len(today_bars)}/{len(symbols)} 只")
            except Exception as e:
                logger.warning(f"[K线批量] 新浪批量行情失败: {e}")

        # 新浪也不足 → fallback 东方财富（clist 全市场接口，一次拉全部）
        if len(today_bars) < len(symbols):
            try:
                from app.data_sources.eastmoney import _em_secid_from_cn
                missing_syms = [s for s in symbols if s not in today_bars]
                if missing_syms:
                    em_quotes = _fetch_eastmoney_batch_quotes(missing_syms)
                    for sym, bar in em_quotes.items():
                        today_bars[sym] = bar
                    logger.info(f"[K线批量] 东财批量行情补充 {len(em_quotes)} 只，共 {len(today_bars)}/{len(symbols)} 只")
            except Exception as e:
                logger.warning(f"[K线批量] 东财批量行情失败: {e}")

        # 2) 需要补历史的 symbol（缓存中没有的）
        # 统一用 normalize_cn_code 对齐格式，避免 600519 vs SH600519 不匹配
        cached_normalized = {normalize_cn_code(s) for s in cached_symbols}
        need_history = [s for s in symbols if normalize_cn_code(s) not in cached_normalized]
        has_today_only = [s for s in symbols if normalize_cn_code(s) in cached_normalized]

        # 有缓存的：直接从缓存拿历史 + 拼当日行情
        for sym in has_today_only:
            try:
                cached = self.kline_cache.get(
                    generate_kline_cache_key(normalize_cn_code(sym), tf, limit, None)
                )
                if cached:
                    bars = list(cached)
                    # 去掉已有的当日 bar，用批量行情的最新数据替换
                    if sym in today_bars:
                        tb = today_bars[sym]
                        bars = [b for b in bars if b.get("time") != tb["time"]]
                        bars.append(tb)
                        bars.sort(key=lambda x: x["time"])
                    result[sym] = bars[-limit:] if len(bars) > limit else bars
            except Exception as e:
                logger.warning(f"[K线批量] {sym} 缓存读取失败: {e}")

        # 3) 缺失历史的：串行拉取
        if need_history:
            self._serial_fetch_kline_batch(need_history, tf, limit, today_bars, result)

        return result

    def _serial_fetch_kline_batch(
        self,
        symbols: List[str],
        tf: str,
        limit: int,
        today_bars: Dict[str, Dict[str, Any]],
        result: Dict[str, List[Dict[str, Any]]],
    ) -> None:
        """串行拉取多只股票的历史 K 线，合并当日行情后写入 result。"""
        if not symbols:
            return

        success = 0
        for sym in symbols:
            try:
                klines = self.get_kline(sym, tf, limit)
                if klines:
                    bars = list(klines)
                    if sym in today_bars:
                        tb = today_bars[sym]
                        bars = [b for b in bars if b.get("time") != tb["time"]]
                        bars.append(tb)
                    bars.sort(key=lambda x: x["time"])
                    result[sym] = bars[-limit:] if len(bars) > limit else bars
                    success += 1
            except Exception as e:
                logger.warning(f"[K线批量] {sym} {tf} 失败: {e}")

        logger.info(
            f"[K线批量] 历史拉取 {tf} 完成: "
            f"{success}/{len(symbols)} 只成功 (serial)"
        )

    def _build_kline_sources(
        self,
        code: str,
        tf: str,
        lim: int,
        before_time: Optional[int],
    ) -> List[Tuple[str, Callable[[], List[Dict[str, Any]]]]]:
        """
        按数据源和周期构建 fallback 列表。
        用 default-arg 捕获循环变量，避免 lambda 闭包陷阱。

        A 股优先国内源（直连快、免费），海外源降级兜底。
        """
        sources: List[Tuple[str, Callable[[], List[Dict[str, Any]]]]] = []

        # 1) 腾讯日/周线（国内直连）
        if tf in ("1D", "1W"):
            period = "day" if tf == "1D" else "week"
            sources.append((
                "tencent_kline",
                lambda _c=code, _p=period, _l=lim: tencent_kline_rows_to_dicts(
                    fetch_kline(_c, period=_p, count=_l, adj="qfq")
                ),
            ))

        # 1b) 腾讯分钟线（mkline 接口，国内直连）
        if tf in ("1m", "5m", "15m", "30m", "1H"):
            sources.append((
                "tencent_minute_kline",
                lambda _c=code, _t=tf, _l=lim: tencent_minute_kline_to_dicts(
                    fetch_minute_kline(_c, timeframe=_t, count=_l)
                ),
            ))

        # 1c) 新浪分钟线（quotes.sina.cn JSONP 接口）
        if tf in ("1m", "5m", "15m", "30m", "1H"):
            sources.append((
                "sina_minute_kline",
                lambda _c=code, _t=tf, _l=lim: fetch_sina_minute_kline(
                    _c, timeframe=_t, count=_l
                ),
            ))

        # 2) 新浪（国内直连，仅日线）
        if tf == "1D":
            sources.append((
                "sina_kline",
                lambda _c=code, _l=lim: fetch_sina_kline(_c, count=_l, adj="qfq"),
            ))

        # 3) AkShare（国内兜底，分钟线/周线）
        if tf in ("1m", "5m", "15m", "30m", "1H"):
            sources.append((
                "akshare_minute",
                lambda _c=code, _t=tf, _l=lim, _b=before_time: fetch_akshare_minute_klines(
                    is_hk=False, tencent_code=_c,
                    timeframe=_t, limit=_l, before_time=_b,
                ),
            ))
        elif tf == "1W":
            sources.append((
                "akshare_weekly",
                lambda _c=code, _l=lim, _b=before_time: fetch_akshare_weekly_klines(
                    is_hk=False, tencent_code=_c,
                    limit=_l, before_time=_b,
                ),
            ))

        # 4) 东方财富直接API（国内直连，全周期）
        sources.append((
            "eastmoney_kline",
            lambda _c=code, _t=tf, _l=lim: fetch_eastmoney_kline(
                _c, period=_t, count=_l, adj="qfq",
            ),
        ))

        # 5) Twelve Data（海外付费，降级）
        sources.append((
            "twelvedata",
            lambda _c=code, _t=tf, _l=lim, _b=before_time: fetch_twelvedata_klines(
                is_hk=False, tencent_code=_c,
                timeframe=_t, limit=_l, before_time=_b,
            ),
        ))

        return sources


