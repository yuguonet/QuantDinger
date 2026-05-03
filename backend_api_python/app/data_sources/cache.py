# -*- coding: utf-8 -*-
"""
缓存层 — 两层存储，按数据类型自动路由

设计:
  热数据（短TTL）  →  内存 dict     →  丢了30s重取
  温数据（长TTL）  →  feather 文件  →  进程重启还在

目录结构:
  cache/
    kline/
      CN/SH600519_1D.feather
      HK/HK00700_1D.feather
    stock_info/
      CN/SZ000001.feather
"""

from __future__ import annotations

import time
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.utils.logger import get_logger
from app.data_sources.normalizer import detect_market

logger = get_logger(__name__)


# ================================================================
# TTL 策略 — 按数据类型配置
# ================================================================

DEFAULT_TTL = {
    # 行情 — 秒级时效，内存
    "quote":            30,
    "ticker":           30,
    "index_quotes":     30,
    "market_snapshot":  60,

    # K线 — 按周期分级
    "kline:1m":         60,
    "kline:5m":         120,
    "kline:15m":        180,
    "kline:30m":        300,
    "kline:1H":         300,
    "kline:1D":         14400,
    "kline:1W":         86400,
    "kline:1M":         86400,

    # 市场数据
    "zt_pool":          60,
    "dt_pool":          60,
    "broken_board":     60,
    "dragon_tiger":     300,
    "hot_rank":         300,
    "fund_flow":        300,

    # 基础信息
    "stock_info":       86400,
    "stock_codes":      86400,
    "sector_list":      86400,
}


def get_ttl(data_type: str, timeframe: str = None) -> int:
    """
    获取数据类型的 TTL (生存时间)。

    查找逻辑:
      1. 先尝试 "data_type:timeframe" 组合键 (如 "kline:1D")
      2. 未命中则回退到 "data_type" 通用键 (如 "stock_info")
      3. 都没有则使用默认 300 秒

    Args:
        data_type:  数据类型标识 (如 "kline", "quote", "stock_info")
        timeframe:  K线周期 (如 "1D", "5m")，可选

    Returns:
        TTL 秒数
    """
    if timeframe and f"{data_type}:{timeframe}" in DEFAULT_TTL:
        return DEFAULT_TTL[f"{data_type}:{timeframe}"]
    return DEFAULT_TTL.get(data_type, 300)


# ================================================================
# 存储后端分类
# ================================================================

DISK_BASE_TYPES = {"stock_info", "stock_codes", "sector_list"}
KLINE_MEMORY_TIMEFRAMES = {"1m", "5m", "15m", "30m", "1H"}
KLINE_DISK_TIMEFRAMES = {"1D", "1W", "1M"}


def should_use_disk(data_type: str) -> bool:
    """
    判断数据类型是否应使用磁盘缓存。

    路由规则:
      - K线日/周/月线 → 磁盘 (数据量大、TTL长、进程重启需保留)
      - K线分钟线 → 内存 (数据量小、TTL短、丢了可重取)
      - stock_info/stock_codes/sector_list → 磁盘 (基础信息，长期有效)
      - 其他 → 内存

    Args:
        data_type: 数据类型标识，可含周期后缀如 "kline:1D"

    Returns:
        True 表示使用磁盘缓存
    """
    base = data_type.split(":")[0]
    if base == "kline":
        timeframe = data_type.split(":")[1] if ":" in data_type else None
        # 日/周/月线数据量大且TTL长，值得落盘
        if timeframe in KLINE_DISK_TIMEFRAMES:
            return True
        # 分钟线数据量小且TTL短，内存足够
        return False
    # 股票基础信息变更频率低，适合磁盘持久化
    return base in DISK_BASE_TYPES


# ================================================================
# 缓存键生成
# ================================================================

def make_key(data_type: str, *parts) -> str:
    """
    生成缓存键。

    格式: "data_type:part1:part2:..."
    示例: make_key("kline", "SH600519", "1D", 300) → "kline:SH600519:1D:300"

    Args:
        data_type: 数据类型标识
        *parts:    键的组成部分，会被 str() 转换后用 ":" 连接

    Returns:
        缓存键字符串
    """
    return ":".join([data_type] + [str(p) for p in parts])


# ================================================================
# 内存缓存
# ================================================================

class CacheEntry:
    """
    缓存条目 — 存储单个缓存项的数据和元信息。

    使用 __slots__ 优化内存占用（每个实例节省约 200 bytes）。

    Attributes:
        data: 缓存的实际数据
        ts:   写入时间戳 (time.time())
        ttl:  生存时间 (秒)
        key:  缓存键，用于调试
    """
    __slots__ = ("data", "ts", "ttl", "key")

    def __init__(self, data: Any, ttl: float, key: str):
        """初始化"""
        self.data = data
        self.ts = time.time()
        self.ttl = ttl
        self.key = key

    def is_valid(self) -> bool:
        """检查缓存条目是否仍在 TTL 有效期内"""
        return (time.time() - self.ts) < self.ttl


class MemoryCache:
    """
    内存缓存 — 基于 OrderedDict 实现 LRU 淘汰。

    设计要点:
      - LRU 淘汰: 容量满时淘汰最久未访问的条目 (OrderedDict.popitem(last=False))
      - 线程安全: 所有操作使用 threading.Lock 保护
      - 访问提升: get() 时 move_to_end(key) 将条目移到末尾，保持 LRU 顺序
      - 统计内置: 跟踪 hit/miss 计数，用于监控缓存效率

    Args:
        max_size: 最大缓存条目数，默认 10000
    """

    def __init__(self, max_size: int = 10000):
        """初始化"""
        self._store: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = threading.Lock()
        self._max_size = max_size
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Optional[Any]:
        """
        获取缓存条目。

        流程:
          1. 查找 key → 不存在返回 None
          2. 检查 TTL → 过期则删除并返回 None
          3. 访问提升: move_to_end(key) 维护 LRU 顺序
          4. 返回数据

        Args:
            key: 缓存键

        Returns:
            缓存的数据，未命中返回 None
        """
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            if not entry.is_valid():
                del self._store[key]
                self._misses += 1
                return None
            # LRU: 访问后移到末尾，这样 popitem(last=False) 淘汰的是最久未访问的
            self._store.move_to_end(key)
            self._hits += 1
            return entry.data

    def set(self, key: str, data: Any, ttl: float = 300) -> None:
        """
        写入缓存条目。

        流程:
          1. key 已存在 → 更新并移到末尾
          2. 容量满 → 淘汰最久未访问的条目 (LRU)
          3. 写入新条目

        Args:
            key:  缓存键
            data: 要缓存的数据
            ttl:  生存时间 (秒)
        """
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
                self._store[key] = CacheEntry(data, ttl, key)
                return
            # LRU 淘汰: 弹出最前面(最久未访问)的条目
            while len(self._store) >= self._max_size:
                self._store.popitem(last=False)
            self._store[key] = CacheEntry(data, ttl, key)

    def delete(self, key: str) -> None:
        """删除指定缓存条目，不存在时静默忽略"""
        with self._lock:
            self._store.pop(key, None)

    def clear(self, prefix: str = None) -> int:
        """清除缓存，可选按前缀过滤，返回清除条目数"""
        with self._lock:
            if prefix is None:
                count = len(self._store)
                self._store.clear()
                return count
            keys = [k for k in self._store if k.startswith(prefix)]
            for k in keys:
                del self._store[k]
            return len(keys)

    def stats(self) -> Dict[str, Any]:
        """获取内存缓存统计 (大小、命中率等)"""
        total = self._hits + self._misses
        return {
            "size": len(self._store),
            "max_size": self._max_size,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": f"{self._hits/total:.1%}" if total else "0%",
        }


# ================================================================
# 磁盘缓存 — feather 文件
# ================================================================

class DiskCache:
    """
    磁盘缓存 — 基于 feather 文件格式的持久化存储。

    设计要点:
      - feather 格式: 列式存储，读写速度快，适合 DataFrame 数据
      - 元数据文件: 每个 .feather 配套一个 .meta 文件，记录写入时间和 TTL
      - 原子写入: 先写 .tmp 再 rename，避免写入中断导致数据损坏
      - 自动清理: 后台线程定期扫描过期文件并删除
      - 目录按市场分类: cache/kline/CN/SH600519_1D.feather

    Args:
        base_dir:               缓存根目录，默认 "cache"
        max_age_check_interval: 清理线程检查间隔（秒），默认 300
    """

    def __init__(self, base_dir: str = "cache", max_age_check_interval: int = 300):
        """初始化"""
        self._base_dir = Path(base_dir)
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._cleanup_interval = max_age_check_interval
        self._start_cleanup_thread()

    _MARKET_MAP = {"SH": "CN", "SZ": "CN", "BJ": "CN", "HK": "HK"}
    """市场代码 → 目录分类: A股(SH/SZ/BJ)归入 CN，港股归入 HK"""

    def _file_path(self, data_type: str, market: str, symbol: str,
                   timeframe: str = None) -> Path:
        """
        计算 feather 文件路径。

        路径格式: {base_dir}/{data_type}/{market_category}/{market}{symbol}_{timeframe}.feather
        示例: cache/kline/CN/SH600519_1D.feather
        """
        market_category = self._MARKET_MAP.get(market, "OTHER")
        market_dir = self._base_dir / data_type / market_category
        market_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{market}{symbol}_{timeframe}.feather" if timeframe else f"{market}{symbol}.feather"
        return market_dir / filename

    def _meta_path(self, feather_path: Path) -> Path:
        """获取元数据文件路径 (.meta 与 .feather 同目录同名)"""
        return feather_path.with_suffix(".meta")

    def _cleanup_empty_dirs(self, dir_path: Path):
        """向上递归清理空目录，直到 base_dir 为止"""
        dir_path = dir_path.resolve()
        base = self._base_dir.resolve()
        while dir_path != base and dir_path.is_dir():
            try:
                if not any(dir_path.iterdir()):
                    dir_path.rmdir()
                else:
                    break
            except OSError:
                break
            dir_path = dir_path.parent

    def get(self, data_type: str, market: str, symbol: str,
            timeframe: str = None) -> Optional[Any]:
        """
        从磁盘读取缓存。

        流程:
          1. 检查 feather 文件是否存在
          2. 读取 .meta 文件检查 TTL → 过期则删除文件返回 None
          3. 使用 pandas.read_feather 读取数据

        Args:
            data_type:  数据类型 (如 "kline", "stock_info")
            market:     市场代码 (如 "SH", "HK")
            symbol:     数字代码 (如 "600519")
            timeframe:  K线周期 (如 "1D")，可选

        Returns:
            DataFrame 或 None
        """
        import pandas as pd
        fpath = self._file_path(data_type, market, symbol, timeframe)
        mpath = self._meta_path(fpath)
        if not fpath.exists():
            self._misses += 1
            return None
        # 检查元数据中的 TTL
        if mpath.exists():
            try:
                meta = mpath.read_text().split("|")
                write_ts = float(meta[0])
                ttl = float(meta[1])
                if time.time() - write_ts >= ttl:
                    # 已过期，删除文件
                    fpath.unlink(missing_ok=True)
                    mpath.unlink(missing_ok=True)
                    self._misses += 1
                    return None
            except (ValueError, IndexError):
                pass
        try:
            df = pd.read_feather(fpath)
            self._hits += 1
            return df
        except Exception as e:
            logger.warning("[DiskCache] 读取失败 %s: %s", fpath, e)
            self._misses += 1
            return None

    def set(self, data_type: str, market: str, symbol: str,
            data: Any, ttl: float, timeframe: str = None) -> None:
        """
        写入磁盘缓存。

        流程:
          1. 数据校验 (None 或空 DataFrame 跳过)
          2. 先写 .tmp 临时文件
          3. rename 为正式文件 (原子操作，避免写入中断导致数据损坏)
          4. 写入 .meta 元数据文件 (write_ts|ttl)

        Args:
            data_type:  数据类型
            market:     市场代码
            symbol:     数字代码
            data:       要缓存的数据 (DataFrame 或可转为 DataFrame 的数据)
            ttl:        生存时间 (秒)
            timeframe:  K线周期，可选
        """
        import pandas as pd
        if data is None:
            return
        if isinstance(data, pd.DataFrame) and data.empty:
            return
        fpath = self._file_path(data_type, market, symbol, timeframe)
        mpath = self._meta_path(fpath)
        with self._lock:
            try:
                # 原子写入: 先写临时文件再 rename
                tmp_path = fpath.with_suffix(".tmp")
                if isinstance(data, pd.DataFrame):
                    data.to_feather(tmp_path)
                else:
                    pd.DataFrame(data).to_feather(tmp_path)
                tmp_path.replace(fpath)
                # 写入元数据: "写入时间|TTL"
                mpath.write_text(f"{time.time()}|{ttl}")
            except Exception as e:
                logger.warning("[DiskCache] 写入失败 %s: %s", fpath, e)
                try:
                    fpath.with_suffix(".tmp").unlink(missing_ok=True)
                except Exception:
                    pass

    def delete(self, data_type: str, market: str, symbol: str,
               timeframe: str = None) -> None:
        """删除指定缓存文件及其元数据，清理空目录"""
        fpath = self._file_path(data_type, market, symbol, timeframe)
        mpath = self._meta_path(fpath)
        fpath.unlink(missing_ok=True)
        mpath.unlink(missing_ok=True)
        self._cleanup_empty_dirs(fpath.parent)

    def clear(self, data_type: str = None, market: str = None) -> int:
        """清除磁盘缓存，支持按数据类型和市场过滤，返回清除文件数"""
        count = 0
        if data_type:
            target = self._base_dir / data_type
            if market:
                target = target / market
        else:
            target = self._base_dir
        if target.exists():
            for f in target.rglob("*.feather"):
                f.unlink(missing_ok=True)
                meta = f.with_suffix(".meta")
                meta.unlink(missing_ok=True)
                count += 1
        return count

    def cleanup_expired(self) -> int:
        """扫描并清理所有过期的磁盘缓存文件，返回清理数量"""
        count = 0
        now = time.time()
        cleaned_dirs = set()
        for mpath in self._base_dir.rglob("*.meta"):
            try:
                meta = mpath.read_text().split("|")
                write_ts = float(meta[0])
                ttl = float(meta[1])
                if now - write_ts >= ttl:
                    feather = mpath.with_suffix(".feather")
                    feather.unlink(missing_ok=True)
                    mpath.unlink(missing_ok=True)
                    cleaned_dirs.add(mpath.parent)
                    count += 1
            except (ValueError, IndexError, FileNotFoundError):
                pass
        for d in cleaned_dirs:
            self._cleanup_empty_dirs(d)
        if count > 0:
            logger.info("[DiskCache] 清理过期文件 %d 个", count)
        return count

    def stats(self) -> Dict[str, Any]:
        """获取磁盘缓存统计 (文件数、命中率等)"""
        total = self._hits + self._misses
        file_count = len(list(self._base_dir.rglob("*.feather")))
        return {
            "files": file_count,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": f"{self._hits/total:.1%}" if total else "0%",
        }

    def _start_cleanup_thread(self):
        """启动后台清理线程，定期扫描过期文件"""
        def _loop():
            """清理循环: 每隔 cleanup_interval 秒执行一次清理"""
            while True:
                time.sleep(self._cleanup_interval)
                try:
                    self.cleanup_expired()
                except Exception as e:
                    logger.warning("[DiskCache] 清理异常: %s", e)
        t = threading.Thread(target=_loop, daemon=True, name="disk-cache-cleanup")
        t.start()


# ================================================================
# 统一缓存入口
# ================================================================

class TieredCache:
    """
    统一缓存入口 — 两层存储，按数据类型自动路由。

    路由逻辑:
      - should_use_disk(data_type) 为 True → DiskCache (feather 文件)
      - 否则 → MemoryCache (内存 OrderedDict)

    读取流程:
      1. 解析缓存键中的 symbol → detect_market 获取市场和数字代码
      2. 按数据类型路由到对应存储后端
      3. 返回缓存数据或 None

    Args:
        base_dir: 磁盘缓存根目录
    """

    def __init__(self, base_dir: str = "cache"):
        """初始化"""
        self.memory = MemoryCache()
        self.disk = DiskCache(base_dir=base_dir)

    def get(self, key: str, data_type: str) -> Optional[Any]:
        """
        统一缓存读取 — 按数据类型自动路由到内存或磁盘。

        Args:
            key:       缓存键 (如 "kline:SH600519:1D:300")
            data_type: 数据类型 (如 "kline:1D")

        Returns:
            缓存数据，未命中返回 None
        """
        if should_use_disk(data_type):
            # 磁盘缓存: 从 key 中解析 symbol → market + digits
            parts = key.split(":")
            if len(parts) >= 2:
                symbol = parts[1]
                if symbol:
                    market, digits = detect_market(symbol)
                    if digits:
                        timeframe = parts[2] if len(parts) > 2 else None
                        return self.disk.get(data_type.split(":")[0], market, digits, timeframe)
        return self.memory.get(key)

    def set(self, key: str, data: Any, ttl: float, data_type: str) -> None:
        """
        统一缓存写入 — 按数据类型自动路由到内存或磁盘。

        Args:
            key:       缓存键
            data:      要缓存的数据
            ttl:       生存时间 (秒)
            data_type: 数据类型
        """
        if should_use_disk(data_type):
            parts = key.split(":")
            if len(parts) >= 2:
                symbol = parts[1]
                if symbol:
                    market, digits = detect_market(symbol)
                    if digits:
                        timeframe = parts[2] if len(parts) > 2 else None
                        self.disk.set(data_type.split(":")[0], market, digits, data, ttl, timeframe)
                        return
        self.memory.set(key, data, ttl)

    def delete(self, key: str, data_type: str = None) -> None:
        """
        删除缓存条目 — 按数据类型路由到对应存储后端。

        Args:
            key:       缓存键
            data_type: 数据类型，传入时用于路由判断
        """
        if data_type and should_use_disk(data_type):
            parts = key.split(":")
            if len(parts) >= 2:
                symbol = parts[1]
                if symbol:
                    market, digits = detect_market(symbol)
                    if digits:
                        timeframe = parts[2] if len(parts) > 2 else None
                        self.disk.delete(data_type.split(":")[0], market, digits, timeframe)
                        return
        self.memory.delete(key)

    def clear(self, data_type: str = None, market: str = None) -> int:
        """
        清除缓存 — 支持按数据类型和市场过滤。

        Args:
            data_type: 数据类型，不传则清除全部
            market:    市场代码，仅磁盘缓存有效

        Returns:
            清除的条目数
        """
        count = 0
        if data_type:
            if should_use_disk(data_type):
                count = self.disk.clear(data_type, market)
            else:
                count = self.memory.clear(data_type)
        else:
            count = self.memory.clear()
            count += self.disk.clear()
        return count

    def stats(self) -> Dict[str, Any]:
        """获取两层缓存的统计信息 (命中率、条目数等)"""
        return {"memory": self.memory.stats(), "disk": self.disk.stats()}


_cache = TieredCache()
"""全局 TieredCache 单例，整个项目共享同一缓存实例"""


def get_cache() -> TieredCache:
    """获取全局缓存实例，供 KlineService 等上层模块调用"""
    return _cache


def cached(data_type: str, ttl: float = None, key_fn: Callable = None):
    """
    缓存装饰器 — 自动缓存函数返回值。

    用法:
        @cached("stock_info", ttl=86400)
        def get_stock_info(code):
            return fetch_from_api(code)

        @cached("kline", key_fn=lambda code, tf: (code, tf))
        def get_kline(code, tf, limit):
            return fetch_kline(code, tf, limit)

    工作流程:
      1. 根据函数参数生成缓存键
      2. 查缓存 → 命中则直接返回
      3. 未命中 → 调用原函数 → 结果写入缓存 → 返回

    Args:
        data_type: 数据类型标识，用于 TTL 查找和存储路由
        ttl:       自定义 TTL (秒)，不传则使用 DEFAULT_TTL 中的配置
        key_fn:    自定义缓存键生成函数，接收原函数参数，返回元组
                   不传则使用 args[1:] (跳过第一个 self 参数)
    """
    def decorator(fn):
        """装饰器主体"""
        def wrapper(*args, **kwargs):
            """缓存包装: 查缓存→命中返回→未命中调用原函数→写缓存"""
            if key_fn:
                cache_key = make_key(data_type, *key_fn(*args, **kwargs))
            else:
                cache_key = make_key(data_type, *args[1:], **kwargs)
            cached_data = _cache.get(cache_key, data_type)
            if cached_data is not None:
                return cached_data
            result = fn(*args, **kwargs)
            if result is not None and result != [] and result != {}:
                actual_ttl = ttl if ttl is not None else get_ttl(data_type)
                _cache.set(cache_key, result, actual_ttl, data_type)
            return result
        return wrapper
    return decorator
