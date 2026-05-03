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
  - 统一接口: 所有 Provider 实现相同的 fetch_kline / fetch_quote / fetch_quotes_batch 方法签名，
    编排层可以无差别地调用任何源。

在架构中的位置:
  KlineService → DataSourceFactory → Coordinator → Provider（本层）

关键依赖:
  - app.utils.logger: 日志模块
  - threading.Lock: 注册表线程安全保护
  - pkgutil / importlib: 自动发现机制

已实现 Provider:
  CNStock (A股)  → tencent(10), sina(20), eastmoney(30), akshare(50)
  HKStock (港股) → hk_stock(40), tencent(10)

待实现 Provider (仅预留常量，暂不注册):
  USStock (美股) → yfinance / twelvedata / finnhub
  Crypto (加密)  → ccxt (binance/okx/bybit)
  Forex (外汇)   → twelvedata / tiingo
  Futures (期货) → ccxt / eastmoney
"""

from __future__ import annotations

import importlib
import pkgutil
import threading
from typing import Any, Callable, Dict, List, Optional, Protocol, Set, Tuple, runtime_checkable

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

@runtime_checkable
class BaseDataSource(Protocol):
    """
    A股数据源统一接口（Protocol 类型协议）。

    所有 Provider 必须实现此协议定义的属性和方法。
    使用 @runtime_checkable 装饰器，支持 isinstance() 运行时检查。

    属性:
        name:         Provider 唯一名称（如 "tencent", "sina"）
        priority:     优先级，越小越优先（默认 100）
        capabilities: 能力声明字典，包含:
            - kline: bool        是否支持K线
            - kline_tf: set      支持的K线周期集合
            - kline_batch: bool  是否支持批量K线
            - quote: bool        是否支持单只行情
            - batch_quote: bool  是否支持批量行情
            - hk: bool           是否支持港股
            - markets: set       支持的市场集合
    """

    name: str
    priority: int  # 越小越优先，默认 100
    capabilities: Dict[str, Any]

    def fetch_kline(
        self, code: str, timeframe: str, count: int,
        adj: str = "qfq", timeout: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        获取单只股票K线数据 — 日/周/分钟共用同一接口。

        Args:
            code:      股票代码（如 "SH600519", "600519"）
            timeframe: K线周期（如 "1D", "5m", "1H"）
            count:     请求数据条数
            adj:       复权方式（"qfq" 前复权 / "hfq" 后复权 / "" 不复权）
            timeout:   请求超时秒数

        Returns:
            K线数据列表，每个元素包含 time/open/high/low/close/volume。
            失败或无数据返回空列表。

        Raises:
            不抛出异常，内部捕获所有异常并返回空列表。
        """
        ...

    def fetch_kline_batch(
        self, codes: List[str], timeframe: str, count: int,
        adj: str = "qfq", timeout: int = 15,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        批量K线（单次HTTP或单次API调用）。

        能一次拉全市场的源（如东财 clist）应实现此方法。
        未实现的源自动退化为逐只调用 fetch_kline。

        Args:
            codes:     股票代码列表
            timeframe: K线周期
            count:     每只股票的数据条数
            adj:       复权方式
            timeout:   请求超时秒数

        Returns:
            {code: kline_bars} — 仅包含成功获取的代码
        """
        ...

    def fetch_quote(self, code: str, timeout: int = 8) -> Optional[Dict[str, Any]]:
        """
        获取单只股票实时行情。

        Args:
            code:    股票代码
            timeout: 请求超时秒数

        Returns:
            行情字典，包含 last/change/changePercent/high/low/open/previousClose/name/symbol。
            失败返回 None。
        """
        ...

    def fetch_quotes_batch(self, codes: List[str], timeout: int = 10) -> Dict[str, Dict[str, Any]]:
        """
        批量获取实时行情（单次HTTP请求）。

        Args:
            codes:    股票代码列表
            timeout:  请求超时秒数

        Returns:
            {code: quote_dict} — 仅包含成功获取的代码
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
    获取 Provider 列表，同时标记每个源是否支持批量K线。

    用于 Coordinator 层判断是否可以调用 fetch_kline_batch。

    Args:
        timeframe: 过滤K线周期
        market:    过滤市场类型

    Returns:
        [(provider, has_batch), ...] 按 priority 排序。
        has_batch 为 True 表示该源支持 fetch_kline_batch。
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

    在 app/__init__.py 或启动时调用一次即可。
    调用后，所有 Provider 自动注册到全局 _registry。
    """
    package = importlib.import_module("app.data_sources.provider")
    for importer, modname, ispkg in pkgutil.iter_modules(package.__path__):
        if not modname.startswith("_"):
            try:
                importlib.import_module(f"app.data_sources.provider.{modname}")
            except Exception as e:
                logger.warning("[Provider] 加载 %s 失败: %s", modname, e)
