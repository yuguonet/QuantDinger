"""
中国A股数据源 — 多层 fallback + 超时溢出 + 熔断轮询

Fallback Chain (K线):
  1. Twelve Data       (付费, 需 TWELVE_DATA_API_KEY)
  2. 腾讯 fqkline       (免费, 日/周线)
  3. 东方财富直接API    (免费, 全周期)
  4. 新浪财经           (免费, 日线)
  5. yfinance           (海外可用)
  6. AkShare            (国内兜底)

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
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.data_sources.base import BaseDataSource
from app.data_sources.tencent import (
    normalize_cn_code,
    fetch_quote,
    parse_quote_to_ticker,
    fetch_kline,
    tencent_kline_rows_to_dicts,
)
from app.data_sources.asia_stock_kline import (
    normalize_chart_timeframe,
    fetch_twelvedata_klines,
    fetch_yfinance_klines,
    fetch_akshare_minute_klines,
    fetch_akshare_weekly_klines,
)
from app.data_sources.sina import fetch_sina_kline, sina_kline_to_ticker
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
    ) -> List[Dict[str, Any]]:
        """
        获取K线数据，fallback chain（国内优先）:
          东方财富(全周期) → 腾讯(日/周) → 新浪(日) → AkShare(分钟/周) → Twelve Data → yfinance
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
                out = self.filter_and_limit(result, limit=lim, before_time=before_time)
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

        # 1) 东方财富直接API（国内直连，全周期，最稳定）
        sources.append((
            "eastmoney_kline",
            lambda _c=code, _t=tf, _l=lim: fetch_eastmoney_kline(
                _c, period=_t, count=_l, adj="qfq",
            ),
        ))

        # 2) 腾讯日/周线（国内直连）
        if tf in ("1D", "1W"):
            period = "day" if tf == "1D" else "week"
            sources.append((
                "tencent_kline",
                lambda _c=code, _p=period, _l=lim: tencent_kline_rows_to_dicts(
                    fetch_kline(_c, period=_p, count=_l, adj="qfq")
                ),
            ))

        # 3) 新浪（国内直连，仅日线）
        if tf == "1D":
            sources.append((
                "sina_kline",
                lambda _c=code, _l=lim: fetch_sina_kline(_c, count=_l, adj="qfq"),
            ))

        # 4) AkShare（国内兜底，分钟线/周线）
        if tf in ("1m", "5m", "15m", "30m", "1H", "4H"):
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

        # 5) Twelve Data（海外付费，降级）
        sources.append((
            "twelvedata",
            lambda _c=code, _t=tf, _l=lim, _b=before_time: fetch_twelvedata_klines(
                is_hk=False, tencent_code=_c,
                timeframe=_t, limit=_l, before_time=_b,
            ),
        ))

        # 6) yfinance（海外，最后兜底）
        sources.append((
            "yfinance",
            lambda _c=code, _t=tf, _l=lim, _b=before_time: fetch_yfinance_klines(
                is_hk=False, tencent_code=_c,
                timeframe=_t, limit=_l, before_time=_b,
            ),
        ))

        return sources


