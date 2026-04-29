# -*- coding: utf-8 -*-
"""
本地数据仓库存储层

目录结构:
  data_warehouse/
    CNStock/
      daily/
        000001.SZ.csv
        600000.SH.csv
      weekly/
        000001.SZ.csv
    Crypto/
      4h/
        BTC_USDT.csv
"""
import csv
import os
import sys
import threading
from datetime import datetime
from typing import Dict, List, Any, Optional

# 确保 backend_api_python 在 path 中（app 模块在那里）
_backend_root = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "backend_api_python",
)
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)

from app.utils.logger import get_logger

logger = get_logger(__name__)

# 默认仓库根目录（optimizer 目录下的 data_warehouse/）
_DEFAULT_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data_warehouse",
)

# 内存缓存: "market:timeframe:symbol" → List[Dict]
_cache: Dict[str, List[Dict[str, Any]]] = {}
_cache_lock = threading.Lock()

# 时间周期 → 目录名
_TF_DIR_MAP = {
    "1D": "daily",
    "1W": "weekly",
    "1M": "monthly",
    "1H": "hourly",
    "4H": "4h",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1m": "1min",
}


def _cache_key(market: str, timeframe: str, symbol: str) -> str:
    return f"{market}:{timeframe}:{symbol}"


def get_warehouse_root() -> str:
    """获取仓库根目录，可通过环境变量 DATA_WAREHOUSE_ROOT 覆盖"""
    return os.environ.get("DATA_WAREHOUSE_ROOT", _DEFAULT_ROOT)


def get_file_path(market: str, timeframe: str, symbol: str) -> str:
    """获取某只股票的本地存储路径"""
    root = get_warehouse_root()
    tf_dir = _TF_DIR_MAP.get(timeframe, timeframe.lower())
    # symbol 中的 / 替换为 _（如 BTC/USDT → BTC_USDT）
    safe_symbol = symbol.replace("/", "_").replace(":", "_")
    return os.path.join(root, market, tf_dir, f"{safe_symbol}.csv")


def exists(market: str, timeframe: str, symbol: str) -> bool:
    """检查本地是否已有该股票数据"""
    return os.path.isfile(get_file_path(market, timeframe, symbol))


def read_local(
    market: str,
    timeframe: str,
    symbol: str,
    limit: Optional[int] = None,
    before_time: Optional[int] = None,
    after_time: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    从本地仓库读取 K 线数据。

    Args:
        market: 市场类型 (CNStock, Crypto, ...)
        timeframe: 时间周期 (1D, 1H, ...)
        symbol: 股票代码
        limit: 最多返回条数
        before_time: 只返回 time < before_time 的数据（Unix 秒）
        after_time: 只返回 time >= after_time 的数据（Unix 秒）

    Returns:
        按时间升序排列的 K 线列表
    """
    ck = _cache_key(market, timeframe, symbol)

    # 先查内存缓存
    with _cache_lock:
        if ck in _cache:
            data = _cache[ck]
        else:
            data = _read_from_csv(market, timeframe, symbol)
            if data:
                _cache[ck] = data

    if not data:
        return []

    # 过滤
    filtered = data
    if after_time is not None:
        filtered = [d for d in filtered if d["time"] >= after_time]
    if before_time is not None:
        filtered = [d for d in filtered if d["time"] < before_time]

    # 截断（从尾部取，因为数据按时间升序）
    if limit and limit > 0:
        filtered = filtered[-limit:]

    return filtered


def _read_from_csv(market: str, timeframe: str, symbol: str) -> List[Dict[str, Any]]:
    """从 CSV 文件读取，返回按时间升序排列的数据"""
    filepath = get_file_path(market, timeframe, symbol)
    if not os.path.isfile(filepath):
        return []

    rows: List[Dict[str, Any]] = []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    rows.append({
                        "time": int(row["time"]),
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": float(row["volume"]),
                    })
                except (KeyError, ValueError):
                    continue
    except Exception as e:
        logger.error(f"读取本地数据失败 {filepath}: {e}")
        return []

    # 按时间升序排序
    rows.sort(key=lambda x: x["time"])
    return rows


def write_local(
    market: str,
    timeframe: str,
    symbol: str,
    data: List[Dict[str, Any]],
    mode: str = "overwrite",
) -> int:
    """
    写入 K 线数据到本地仓库。

    Args:
        market: 市场类型
        timeframe: 时间周期
        symbol: 股票代码
        data: K 线数据列表
        mode: "overwrite" 覆盖 / "append" 追加去重

    Returns:
        写入的总条数
    """
    if not data:
        return 0

    filepath = get_file_path(market, timeframe, symbol)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    if mode == "append" and os.path.isfile(filepath):
        existing = _read_from_csv(market, timeframe, symbol)
        existing_times = {d["time"] for d in existing}
        new_data = [d for d in data if d["time"] not in existing_times]
        merged = existing + new_data
        merged.sort(key=lambda x: x["time"])
    else:
        merged = sorted(data, key=lambda x: x["time"])

    # 写入 CSV
    try:
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["time", "open", "high", "low", "close", "volume"])
            writer.writeheader()
            for row in merged:
                writer.writerow({
                    "time": int(row["time"]),
                    "open": round(float(row["open"]), 4),
                    "high": round(float(row["high"]), 4),
                    "low": round(float(row["low"]), 4),
                    "close": round(float(row["close"]), 4),
                    "volume": round(float(row["volume"]), 2),
                })
    except Exception as e:
        logger.error(f"写入本地数据失败 {filepath}: {e}")
        return 0

    # 更新内存缓存
    ck = _cache_key(market, timeframe, symbol)
    with _cache_lock:
        _cache[ck] = merged

    logger.info(f"写入本地仓库: {filepath} ({len(merged)} 条)")
    return len(merged)


def list_local(market: Optional[str] = None, timeframe: Optional[str] = None) -> List[str]:
    """
    列出本地仓库中已有的股票代码。

    Args:
        market: 过滤市场（可选）
        timeframe: 过滤时间周期（可选）

    Returns:
        股票代码列表
    """
    root = get_warehouse_root()
    symbols = []

    markets = [market] if market else _list_dirs(root)
    for mkt in markets:
        mkt_dir = os.path.join(root, mkt)
        if not os.path.isdir(mkt_dir):
            continue

        timeframes = [timeframe] if timeframe else _list_dirs(mkt_dir)
        tf_dir_name = _TF_DIR_MAP.get(timeframe, timeframe) if timeframe else None

        for tf in timeframes:
            tf_dir = os.path.join(mkt_dir, tf_dir_name or tf)
            if not os.path.isdir(tf_dir):
                continue
            for fname in os.listdir(tf_dir):
                if fname.endswith(".csv"):
                    symbol = fname[:-4]  # 去掉 .csv
                    symbols.append(f"{mkt}:{symbol}")

    return sorted(set(symbols))


def clear_cache():
    """清空内存缓存"""
    with _cache_lock:
        _cache.clear()


def _list_dirs(path: str) -> List[str]:
    """列出目录下的子目录"""
    if not os.path.isdir(path):
        return []
    return [d for d in os.listdir(path) if os.path.isdir(os.path.join(path, d))]


def get_stats() -> Dict[str, Any]:
    """获取仓库统计信息"""
    root = get_warehouse_root()
    if not os.path.isdir(root):
        return {"root": root, "exists": False, "stocks": 0, "total_rows": 0}

    total_files = 0
    total_rows = 0
    markets = {}

    for mkt in _list_dirs(root):
        mkt_dir = os.path.join(root, mkt)
        mkt_files = 0
        for tf in _list_dirs(mkt_dir):
            tf_dir = os.path.join(mkt_dir, tf)
            for fname in os.listdir(tf_dir):
                if fname.endswith(".csv"):
                    mkt_files += 1
                    filepath = os.path.join(tf_dir, fname)
                    with open(filepath, "r") as f:
                        total_rows += sum(1 for _ in f) - 1  # 减去 header
        markets[mkt] = mkt_files
        total_files += mkt_files

    return {
        "root": root,
        "exists": True,
        "stocks": total_files,
        "total_rows": total_rows,
        "markets": markets,
    }
