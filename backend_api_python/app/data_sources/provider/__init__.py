# -*- coding: utf-8 -*-
"""
A股数据源 Provider 框架 — 自注册 + 能力声明 + 统一接口

本模块是 Provider 层的核心框架，定义了：
- BaseDataSource Protocol: 所有数据源必须实现的统一接口
- @register 装饰器: Provider 自注册机制（import 即注册）
- get_providers(): 按能力/周期/市场过滤 Provider 列表
- autodiscover(): 自动扫描并导入 provider/ 目录下所有模块

设计原理:
  - 自注册: 每个 Provider 模块在被 import 时，通过 @register 自动注册到全局注册表。
    上层代码不需要硬编码 import 列表，只需调用 get_providers() 获取可用源。
  - 能力声明: 每个 Provider 在 capabilities 字典中声明自己支持的能力（kline/quote/batch_quote）、
    支持的K线周期（kline_tf）、支持的市场（markets）。编排层按声明过滤，避免跨市场误调用。
  - 统一接口: 所有 Provider 实现相同的 fetch_kline / fetch_ticker / fetch_batch_quotes 方法签名，
    编排层可以无差别地调用任何源。

在架构中的位置:
  KlineService → DataSourceFactory → Coordinator → Provider（本层）

关键依赖:
  - app.utils.logger: 日志模块
  - threading.Lock: 注册表线程安全保护
  - pkgutil / importlib: 自动发现机制

已实现 Provider:
  CNStock (A股)  → tencent(10), sina(20), eastmoney(30), akshare(50)
  CNStock (A股) → tencent(10), sina(20), eastmoney(30)

待实现 Provider (仅预留常量，暂不注册):
  USStock (美股) → yfinance / twelvedata / finnhub
  Crypto (加密)  → ccxt (binance/okx/bybit)
  Forex (外汇)   → twelvedata / tiingo
  Futures (期货) → ccxt / eastmoney


===============================================================================
Provider 前置依赖管理方案（重要设计决策）
===============================================================================

背景:
  部分 Provider 在拉取数据前需要前置准备（如 cookie、服务器探测等）。
  目前有两种方案，当前使用【方案 A】。

-------------------------------------------------------------------------------
方案 A — 各源自愈（旧方案，已被方案 B 取代，自愈逻辑保留为兜底）
-------------------------------------------------------------------------------

  思路: 不改上游 Coordinator，各 Provider 在 fetch 内部自动处理前置依赖。
  原则: 前置依赖失败时自动重试，上游无感知。

  已实现的自愈机制:
    - xueqiu:   _get_headers() 中 cookie 为空时自动清除缓存并重试一次。
                cookie TTL=1h，正常情况 0 开销。
    - tdx_ex:   _get_conn() 中若 _live_servers 为空，触发 _discover_servers(force=True)
                重新探测。探测结果缓存，后续请求 0 开销。
    - eastmoney: 无限流器（已移除 get_eastmoney_limiter().wait()）。
    - em_trends2: TDX 除权数据懒加载，不需要预热。只返回当天盘中数据是 API 限制。

  最坏开销:
    - xueqiu cookie 失败: ~700ms/请求，连续 5 次失败后源自动停用，总浪费 ~3.5s
    - tdx_ex 服务器探测: ~5s（首次请求），后续 0 开销
    - 对比 prepare() 方案: 最坏多 ~3s，正常情况 0 差距

  优点:
    - 不改上游 Coordinator，改动面最小
    - 各源独立自治，互不影响
    - 对 market_kline 多源并行场景无额外开销

  缺点:
    - 源首次请求可能有延迟（cookie/探测失败时）
    - 无统一的"就绪状态"查询接口

-------------------------------------------------------------------------------
方案 B — 统一 prepare() 接口（当前方案）
-------------------------------------------------------------------------------

  思路: 在 BaseDataSource Protocol 中定义 prepare() 方法，Coordinator 在批量拉取前
        统一调用，确保所有源就绪。失败的源直接跳过，不浪费请求。

  各源实现:
    - xueqiu:   prepare() 中刷新 cookie，返回 cookie 是否有效
    - tdx_ex:   prepare() 中探测服务器，返回 _live_servers 是否非空
    - 其他源:    继承默认 prepare()，直接返回 True

  Coordinator 调用点:
    - coordinate_market_kline() 中: 过滤可用源后、启动 worker 前调 prepare()

-------------------------------------------------------------------------------
迁移记录（方案 A → 方案 B 已完成）
-------------------------------------------------------------------------------

  已完成:
    1. BaseDataSource Protocol 中添加了 prepare() 方法（默认返回 True）
    2. xueqiu:   prepare() 调 _refresh_cookie()，返回 cookie 是否有效
    3. tdx_ex:   prepare() 调 _discover_servers()，返回 _live_servers 是否非空
    4. Coordinator.coordinate_market_kline() 中启动 worker 前调 prepare() 过滤
    5. 各源保留自愈逻辑作为安全兜底（prepare 失败不影响 fetch 内部重试）

===============================================================================
"""

from __future__ import annotations

import importlib
import pkgutil
import threading
from typing import Any, Callable, Dict, List, Optional, Protocol, Set, Tuple, runtime_checkable

from app.data_sources.normalizer import normalize_cn_code
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ================================================================
# 市场类型常量
# ================================================================
# Provider 在 capabilities["markets"] 中声明支持的市场。
# KlineService 按 market 过滤 Provider，避免跨市场误调用。


# ================================================================
# Provider 协议 — 所有源必须实现
# ================================================================

# ================================================================
# 不支持接口的标准化响应
# ================================================================

NOT_SUPPORTED_REASON = "not_supported"

class NotSupportedResult:
    """
    标准化的"不支持"响应包装。

    当 Provider 不支持某个接口时，返回此对象而非抛出异常。
    Coordinator 可通过 is_not_supported() 快速判断并切换到其他源。

    Attributes:
        source: 不支持该接口的数据源名称
        interface: 不支持的接口名称（如 "fetch_market_kline"）
        reason: 不支持的原因说明
    """

    def __init__(self, source: str, interface: str, reason: str = ""):
        self.source = source
        self.interface = interface
        self.reason = reason or f"{source} does not support {interface}"

    def __bool__(self) -> bool:
        """布尔值为 False，便于 Coordinator 快速判断"""
        return False

    def __repr__(self) -> str:
        return f"NotSupportedResult({self.source}.{self.interface})"


def is_not_supported(result: Any) -> bool:
    """
    判断结果是否为"不支持"响应。

    Coordinator 在获取结果后调用此函数，快速判断是否需要切换数据源。

    Args:
        result: Provider 接口的返回值

    Returns:
        True 表示该 Provider 不支持此接口，Coordinator 应尝试其他源
    """
    return isinstance(result, NotSupportedResult)


# ================================================================
# 批量K线辅助 — 交易日历推算 count
# ================================================================

# 每个交易日的 bar 数量（用于从天数反推 count）
_BARS_PER_DAY = {
    "1m": 240,   # 9:30-11:30 + 13:00-15:00 = 4h = 240min
    "5m": 48,    # 240 / 5
    "15m": 16,   # 240 / 15
    "30m": 8,    # 240 / 30
    "1H": 4,     # 240 / 60
    "1D": 1,
    "1W": 1,     # 近似，实际按周聚合
}


def calc_kline_count(timeframe: str, start_date: str, end_date: str = "") -> int:
    """
    根据交易日历推算需要拉取的 K 线条数。

    用交易日历（非自然日）计算 start_date 到 end_date 之间的交易日数，
    再乘以每个交易日对应的 bar 数量。
    end_date 为今天时，当天按盘中已产生的 bar 数计算（非满 bar）。

    Args:
        timeframe:  K 线周期（"15m", "1D", ...）
        start_date: 起始日期（"YYYY-MM-DD"）
        end_date:   结束日期（"YYYY-MM-DD"），为空则取今天

    Returns:
        需要拉取的 K 线条数
    """
    from app.utils.trading_calendar import trading_days_count
    from datetime import datetime, timezone, timedelta

    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    if not end_date:
        end_date = today

    bars_per_day = _BARS_PER_DAY.get(timeframe, 1)

    # 日线/周线：每天 1 根，直接算天数
    if timeframe in ("1D", "1W"):
        return max(trading_days_count(start_date, end_date), 1)

    # 分钟级 + end_date 是今天 → 最后一天按盘中时间算
    if end_date == today:
        past_days = trading_days_count(start_date, end_date) - 1  # 不含今天
        today_bars = _bars_elapsed_today(timeframe)
        return max(past_days * bars_per_day + today_bars, 1)

    # 分钟级 + end_date 是过去的日期 → 全部满 bar
    days = trading_days_count(start_date, end_date)
    return max(days * bars_per_day, 1)


# ================================================================
# 全市场公共辅助 — 供无原生全市场接口的 Provider 复用
# ================================================================

def _batch_fetch_quotes_by_codes(
    provider,
    batch_size: int = 500,
    timeout: int = 10,
    symbols: Optional[List[str]] = None,
) -> Dict[str, Dict[str, Any]]:
    """
    通过多次调用 fetch_batch_quotes 拼出全市场行情。
    用于不支持原生全市场行情的 Provider（如新浪、腾讯）。
    """
    if not symbols:
        from app.utils.basicinfo_db import get_stock_basic_db
        symbols = get_stock_basic_db().market_all_codes(status="active")
    if not symbols:
        return {}
    result: Dict[str, Dict[str, Any]] = {}
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        try:
            partial = provider.fetch_batch_quotes(batch, timeout=timeout)
            if partial:
                result.update(partial)
        except Exception as e:
            logger.warning("[全市场行情] %s 批次 %d 失败: %s", provider.name, i // batch_size, e)
    return result


def _is_today(date_str: str) -> bool:
    """判断日期字符串是否为今天（支持 YYYY-MM-DD 和 YYYYMMDD）"""
    from datetime import datetime, timezone, timedelta
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    normalized = date_str.replace("-", "") if len(date_str) == 10 else date_str
    if len(normalized) == 8:
        normalized = f"{normalized[:4]}-{normalized[4:6]}-{normalized[6:]}"
    return normalized == today


def _bars_elapsed_today(timeframe: str) -> int:
    """计算今天盘中已产生的 bar 数（按当前时间，不超过每日上限）"""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone(timedelta(hours=8)))
    h, m = now.hour, now.minute

    bars_per_day = _BARS_PER_DAY.get(timeframe, 16)

    # 盘前
    if h < 9 or (h == 9 and m < 30):
        return 0

    # 盘中分钟数（午休跳过）
    if h < 11 or (h == 11 and m <= 30):
        minutes = (h - 9) * 60 + m - 30
    elif h == 11 and m > 30:
        minutes = 120  # 上午盘结束
    elif h == 12:
        minutes = 120
    elif h < 15:
        minutes = 120 + (h - 13) * 60 + m
    else:
        return bars_per_day  # 收盘后返回满 bar

    tf_minutes = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1H": 60}
    step = tf_minutes.get(timeframe, 15)
    return min(minutes // step, bars_per_day)


def _resolve_market_kline_count(
    timeframe: str,
    count: Optional[int],
    start_date: str,
    end_date: str = "",
) -> Optional[int]:
    """
    fetch_market_kline 统一路由：决定 count 值和是否走快照路径。

    路由规则（end_date 是唯一路由依据，count 和 start_date 不影响路径选择）:
      - end_date >= today（或为空，默认今天）→ 锁定走批量行情快照路径（1 HTTP，1 bar/只）
        此时 count 和 start_date 均可推算出 count，但被 end_date>=today 锁定覆盖，
        返回 None 强制走 _all_market_kline_via_quotes 快照。
      - end_date < today → 走并发 fetch_kline 路径
        count 显式传入时直接使用；未传时从 start_date/end_date 推算。

    返回值:
      - None: 走 _all_market_kline_via_quotes 快照路径（end_date>=today 锁定）
      - int:  走并发 fetch_kline 路径，使用此 count 值
    """
    from datetime import datetime, timezone, timedelta
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    effective_end = end_date if end_date else today

    if effective_end >= today:
        # end_date>=today → 走批量行情快照（单次 HTTP 拿全市场最新 bar）
        return None

    # end_date<today → 需要历史数据，推算 count 走并发 fetch_kline
    if count is not None:
        return count
    effective_start = start_date if start_date else effective_end
    return calc_kline_count(timeframe, effective_start, effective_end)


def _all_market_kline_via_quotes(
    provider,
    timeframe: str = "1D",
    timeout: int = 15,
    symbols: Optional[List[str]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    批量行情快照路径（end_date=today 时锁定走此路径）。

    通过 fetch_batch_quotes 一次 HTTP 拿全市场行情，将每只股票的实时行情
    转换成单根 K 线 bar 格式返回。count 和 start_date 虽可推算出 count，
    但被 end_date=today 锁定覆盖，统一走此快照路径。

    返回格式与 fetch_kline 一致：{code: [bar_dict]}，每只股票 1 根 bar。
    """
    if not symbols:
        from app.utils.basicinfo_db import get_stock_basic_db
        symbols = get_stock_basic_db().market_all_codes(status="active")
    quotes = provider.fetch_batch_quotes(symbols, timeout=timeout)
    if not quotes or isinstance(quotes, NotSupportedResult):
        return {}
    result: Dict[str, List[Dict[str, Any]]] = {}
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone(timedelta(hours=8)))
    bar_ts = int(now.timestamp())
    bar_dt = now.strftime("%Y-%m-%d %H:%M:%S")

    for code, q in quotes.items():
        if not isinstance(q, dict):
            continue
        last = q.get("last", 0)
        if not last or last <= 0:
            continue
        bar = {
            "time": bar_ts,
            "open": q.get("open", last),
            "high": q.get("high", last),
            "low": q.get("low", last),
            "close": last,
            "volume": q.get("volume", 0),
            "amount": q.get("amount", 0),
        }
        result[normalize_cn_code(code)] = [bar]
    # 前复权处理
    from app.data_sources.provider.adjustment import apply_fwd_adjust
    for code, bars in result.items():
        result[code] = apply_fwd_adjust(bars, code)
    return result


def _batch_fetch_kline_by_codes(
    provider,
    timeframe: str = "1D",
    count: int = 300,
    adj: str = "qfq",
    timeout: int = 15,
    start_date: str = "",
    end_date: str = "",
    batch_size: int = 500,
    symbols: Optional[List[str]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    通过多次并发调用 fetch_kline 拼出全市场K线。

    用于不支持原生全市场K线的 Provider（如新浪、腾讯，单次HTTP限 ~500 只）。

    Args:
        provider:    Provider 实例（需有 fetch_kline 方法）
        timeframe:   K线周期
        count:       每只股票的数据条数
        adj:         复权方式
        timeout:     单次请求超时秒数
        start_date:  起始日期
        end_date:    结束日期
        batch_size:  每批并发的股票数量
        symbols:     股票代码列表，为 None 时自动从 DB 获取

    Returns:
        全市场K线字典 {code: kline_bars}
    """
    import concurrent.futures
    if not symbols:
        from app.utils.basicinfo_db import get_stock_basic_db
        symbols = get_stock_basic_db().market_all_codes(status="active")
    if not symbols:
        return {}
    result: Dict[str, List[Dict[str, Any]]] = {}
    lock = threading.Lock()

    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]

        def _fetch_one(code: str):
            bars = provider.fetch_kline(
                code, timeframe, count, adj=adj, timeout=timeout,
                start_date=start_date, end_date=end_date,
            )
            if bars:
                with lock:
                    result[normalize_cn_code(code)] = bars

        max_workers = min(len(batch), 8)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_fetch_one, c) for c in batch]
            concurrent.futures.wait(futures, timeout=timeout + 5)

    return result


@runtime_checkable
class BaseDataSource(Protocol):
    """
    A股数据源统一接口（Protocol 类型协议）。

    所有 Provider 必须实现此协议定义的5个标准接口:
      0. prepare              — 下载前准备（cookie、服务器探测等）
      1. fetch_kline          — 单只K线
      2. fetch_market_kline   — 全市场批量K线（无需传入代码列表）
      3. fetch_ticker         — 单只行情
      4. fetch_batch_quotes   — 批量行情

    不支持的接口返回 NotSupportedResult（而非 None 或抛异常），
    以便 Coordinator 快速识别并切换到其他数据源。

    使用 @runtime_checkable 装饰器，支持 isinstance() 运行时检查。

    属性:
        name:         Provider 唯一名称（如 "tencent", "sina"）
        priority:     优先级，越小越优先（默认 100）
        capabilities: 能力声明字典，包含:
            - kline: bool        是否支持K线
            - kline_tf: set      支持的K线周期集合
            - kline_batch: bool  是否支持全市场批量K线（fetch_market_kline）
            - quote: bool        是否支持单只行情
            - batch_quote: bool  是否支持批量行情
            - hk: bool           是否支持港股
            - markets: set       支持的市场集合
    """

    name: str
    priority: int  # 越小越优先，默认 100
    capabilities: Dict[str, Any]

    def prepare(self) -> bool:
        """
        下载前准备 — 由 Coordinator 在派发任务前统一调用。

        各 Provider 在此方法中完成前置依赖（如 cookie 获取、服务器探测等）。
        返回 True 表示就绪，False 表示不可用（Coordinator 跳过该源）。

        默认实现直接返回 True（无需准备的源不需要覆盖）。
        """
        ...

    def fetch_kline(
        self, code: str, timeframe: str, count: int = 300,
        adj: str = "qfq", timeout: int = 10,
        start_date: str = "", end_date: str = "",
    ) -> List[Dict[str, Any]]:
        """
        获取单只股票K线数据 — 日/周/分钟共用同一接口。

        支持两种指定数据量的方式:
          1. count: 直接指定拉取的 bar 数
          2. start_date/end_date: 通过交易日历反推 count（更精确）

        Args:
            code:      股票代码（如 "SH600519", "600519"）
            timeframe: K线周期（如 "1D", "5m", "1H"）
            count:     请求数据条数（start_date 优先时忽略）
            adj:       复权方式（"qfq" 前复权 / "hfq" 后复权 / "" 不复权）
            timeout:   请求超时秒数
            start_date: 起始日期（"YYYY-MM-DD"），提供时用交易日历反推 count
            end_date:   结束日期（"YYYY-MM-DD"），部分数据源支持精确截断

        Returns:
            K线数据列表，每个元素包含 time/open/high/low/close/volume。
            失败或无数据返回空列表。
            不支持返回 NotSupportedResult。

        Raises:
            不抛出异常，内部捕获所有异常并返回空列表。
        """
        ...

    def fetch_market_kline(
        self, timeframe: str, count: int = 300,
        adj: str = "qfq", timeout: int = 15,
        start_date: str = "", end_date: str = "",
        symbols: Optional[List[str]] = None,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        全市场批量K线。

        路由逻辑:
          - end_date 是今天（或为空）→ 走 fetch_batch_quotes（1 HTTP 拿 N 只行情，转单根 bar）
          - end_date 是过去日期 → 并发调用 fetch_kline（每只 1 HTTP）

        Args:
            timeframe: K线周期
            count:     每只股票的数据条数（None 时走批量行情快照路径）
            adj:       复权方式
            timeout:   请求超时秒数
            start_date: 起始日期（"YYYY-MM-DD"），提供时用交易日历反推 count
            end_date:   结束日期（"YYYY-MM-DD"），为空则取今天
            symbols:    股票代码列表，为 None 时自动从 DB 获取全市场 active 股票

        Returns:
            {code: kline_bars} — 仅包含成功获取的代码。
        """
        ...

    def fetch_ticker(self, code: str, timeout: int = 8) -> Optional[Dict[str, Any]]:
        """
        获取单只股票实时行情。

        Args:
            code:    股票代码
            timeout: 请求超时秒数

        Returns:
            行情字典，包含 last/change/changePercent/high/low/open/previousClose/name/symbol。
            失败返回 None。
            不支持返回 NotSupportedResult。
        """
        ...

    def fetch_batch_quotes(self, codes: List[str], timeout: int = 10) -> Dict[str, Dict[str, Any]]:
        """
        批量获取实时行情（单次HTTP请求）。

        Args:
            codes:    股票代码列表
            timeout:  请求超时秒数

        Returns:
            {code: quote_dict} — 仅包含成功获取的代码。
            不支持返回 NotSupportedResult。
        """
        ...

# ================================================================
# 注册表
# ================================================================

# 全局 Provider 注册表，name → provider_instance
# 使用 threading.Lock 保证并发安全（多个模块同时 import 时不会冲突）
_registry: Dict[str, BaseDataSource] = {}
_lock = threading.Lock()


def register(cls=None, *, priority: int = 100):
    """
    Provider 注册装饰器 — 支持两种用法。

    用法1: @register（默认 priority=100）
        @register
        class TencentDataSource: ...

    用法2: @register(priority=10)（指定优先级）
        @register(priority=10)
        class TencentDataSource: ...

    注册过程:
      1. 实例化被装饰的类（无参构造）
      2. 设置 priority（如果类本身未定义）
      3. 将实例写入全局 _registry（加锁保护）
      4. 记录日志

    Args:
        cls:      被装饰的类（@register 直接调用时）
        priority: 优先级数值，越小越优先

    Returns:
        装饰器函数（返回原始类，不修改类本身）
    """
    def _do_register(cls):
        """实际注册逻辑: 实例化 Provider 并加入全局注册表"""
        provider = cls()
        if not hasattr(provider, 'priority'):
            provider.priority = priority
        with _lock:
            _registry[provider.name] = provider
        logger.debug("[Provider] 注册: %s (priority=%s)", provider.name, provider.priority)
        return cls

    if cls is not None:
        return _do_register(cls)
    return _do_register


def get_providers(
    capability: str = None,
    timeframe: str = None,
    market: str = None,
) -> List[BaseDataSource]:
    """
    获取可用 Provider 列表 — 按 priority 排序 + 多维过滤。

    过滤逻辑（AND 关系）:
      1. capability: 过滤支持指定能力的 Provider（如 'kline', 'quote', 'batch_quote'）
      2. timeframe:  过滤支持指定K线周期的 Provider（如 '1D', '5m'）
      3. market:     过滤支持指定市场的 Provider（如 'CNStock', 'HKStock'）

    Args:
        capability: 过滤能力名称
        timeframe:  过滤K线周期
        market:     过滤市场类型

    Returns:
        按 priority 升序排列的 Provider 列表
    """
    with _lock:
        providers = list(_registry.values())

    # 能力过滤: capabilities[capability] 必须为 True
    if capability:
        providers = [
            p for p in providers
            if p.capabilities.get(capability, False)
        ]

    # 周期过滤: timeframe 必须在 capabilities['kline_tf'] 集合中
    if timeframe:
        providers = [
            p for p in providers
            if timeframe in p.capabilities.get('kline_tf', set())
        ]

    # 市场过滤: market 必须在 capabilities['markets'] 集合中
    if market:
        providers = [
            p for p in providers
            if market in p.capabilities.get('markets', set())
        ]

    # 按能力专属优先级排序: {capability}_priority > 全局 priority
    if capability:
        providers.sort(key=lambda p: p.capabilities.get(
            f"{capability}_priority", p.priority
        ))
    else:
        providers.sort(key=lambda p: getattr(p, 'priority', 100))

    return providers


def get_providers_with_batch(
    timeframe: str = None,
    market: str = None,
) -> List[Tuple[BaseDataSource, bool]]:
    """
    获取 Provider 列表，同时标记每个源是否支持全市场批量K线。

    用于 Coordinator 层判断是否可以调用 fetch_market_kline。

    Args:
        timeframe: 过滤K线周期
        market:    过滤市场类型

    Returns:
        [(provider, has_batch), ...] 按 priority 排序。
        has_batch 为 True 表示该源支持 fetch_market_kline。
    """
    providers = get_providers("kline", timeframe=timeframe, market=market)
    result = []
    for p in providers:
        has_batch = p.capabilities.get("kline_batch", False)
        result.append((p, has_batch))
    return result


def get_provider(name: str) -> Optional[BaseDataSource]:
    """
    按名称获取单个 Provider。

    Args:
        name: Provider 名称（如 "tencent", "sina"）

    Returns:
        Provider 实例，未找到返回 None
    """
    return _registry.get(name)


# ================================================================
# 自动发现 — import 时自动注册 provider/ 目录下所有模块
# ================================================================

def autodiscover():
    """
    扫描 app.data_sources.provider 包下所有模块，触发 @register。

    工作原理:
      1. 获取 provider 包的路径
      2. 使用 pkgutil.iter_modules 列出所有子模块
      3. 跳过以 _ 开头的模块（私有模块）
      4. 逐个 import 子模块，触发模块顶部的 @register 装饰器

    在模块加载时自动执行，也可手动调用。
    """
    import sys
    pkg = sys.modules[__name__]
    for importer, modname, ispkg in pkgutil.iter_modules(pkg.__path__):
        if not modname.startswith("_"):
            try:
                importlib.import_module(f"app.data_sources.provider.{modname}")
            except Exception as e:
                logger.warning("[Provider] 加载 %s 失败: %s", modname, e)


# 模块加载时自动扫描并注册所有 Provider
autodiscover()
