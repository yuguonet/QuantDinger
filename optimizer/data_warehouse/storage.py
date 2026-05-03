"""
storage.py — 统一数据读取层（桥接 db_market）

所有 optimizer 模块通过此模块读取行情数据，不再直接读 CSV。
延迟导入 db_market，避免触发 Flask 依赖（standalone 可用）。

用法:
    from optimizer.data_warehouse.storage import read_local, list_local, get_stats

    # 读取数据
    data = read_local(market="CNStock", timeframe="1D", symbol="000001", limit=500)

    # 列出有数据的股票
    symbols = list_local(market="CNStock", timeframe="1D")

    # 数据仓库统计
    stats = get_stats()
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

# 时间框架 → 目录名映射（与旧 CSV 结构兼容）
_TF_DIR_MAP = {
    "1m":   "1min",
    "5m":   "5min",
    "15m":  "15min",
    "30m":  "30min",
    "1H":   "1hour",
    "2H":   "2hour",
    "4H":   "4hour",
    "1D":   "daily",
    "1W":   "weekly",
}


def _load_env():
    """加载 .env 文件（优先 backend 目录，其次项目根目录）"""
    if os.getenv("_STORAGE_ENV_LOADED"):
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        print("⚠️ python-dotenv 未安装，无法加载 .env 文件 (pip install python-dotenv)")
        return
    _this_dir = os.path.dirname(os.path.abspath(__file__))
    _project_root = os.path.dirname(os.path.dirname(_this_dir))
    _backend_root = os.path.join(_project_root, "backend_api_python")
    for env_path in [
        os.path.join(_backend_root, '.env'),
        os.path.join(_project_root, '.env'),
    ]:
        if os.path.isfile(env_path):
            load_dotenv(env_path, override=False)
            os.environ["_STORAGE_ENV_LOADED"] = "1"
            return
    print(f"⚠️ 未找到 .env 文件（已检查: backend_api_python/.env, 项目根/.env）")


def _get_writer():
    """延迟导入 db_market writer（避免 Flask 依赖）"""
    _load_env()
    from app.utils.db_market import get_market_kline_writer
    return get_market_kline_writer()


def _get_manager():
    """延迟导入 db_market manager"""
    _load_env()
    from app.utils.db_market import get_market_db_manager
    return get_market_db_manager()


def read_local(
    market: str = "CNStock",
    timeframe: str = "1D",
    symbol: str = "000001",
    limit: int = 500,
    before_time: Optional[int] = None,
    after_time: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    从 db_market 读取 K 线数据。

    Args:
        market:    市场标识，如 "CNStock", "us"
        timeframe: K线周期，如 "15m", "1D"
        symbol:    品种代码，如 "000001"
        limit:     返回条数上限
        before_time: 只返回此时间戳之前的数据
        after_time:  只返回此时间戳之后的数据

    Returns:
        K线数据列表: [{"time": int, "open": float, "high": float,
                       "low": float, "close": float, "volume": float}, ...]
    """
    writer = _get_writer()
    return writer.query(
        market=market,
        symbol=symbol,
        timeframe=timeframe,
        start_time=after_time,
        end_time=before_time,
        limit=limit,
    )


def read_clean(
    market: str = "CNStock",
    symbol: str = "000001",
    timeframe: str = "1D",
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """
    通过 kline_clean.MarketDataProvider 获取清洗后的 K 线数据。

    相比 read_local，增加了：交易日过滤、缺失段前向填充、聚合周期支持
    （30m/60m/2H/4H 自动从 15m 聚合）。

    Args:
        market:    市场标识，如 "CNStock"
        symbol:    品种代码，如 "000001"
        timeframe: K线周期，如 "15m", "1D"
        start:     起始时间（datetime），None 则默认一年前
        end:       结束时间（datetime），None 则默认当前时间

    Returns:
        清洗后的 K 线数据列表: [{"time": datetime, "open": float, "high": float,
                                 "low": float, "close": float, "volume": float}, ...]
    """
    from app.data_sources.kline_clean import MarketDataProvider

    if end is None:
        end = datetime.now()
    if start is None:
        start = end - timedelta(days=365)

    writer = _get_writer()
    provider = MarketDataProvider(writer)
    return provider.get_clean_klines(market, symbol, start, end, timeframe)


def list_local(
    market: str = "CNStock",
    timeframe: str = "1D",
) -> List[str]:
    """
    列出指定市场/周期下有数据的品种代码列表。

    Args:
        market:    市场标识
        timeframe: K线周期

    Returns:
        品种代码列表，如 ["000001", "000002", ...]
    """
    writer = _get_writer()
    stats = writer.stats(market)
    return stats.get("symbol_list", [])


def get_stats(market: str = "CNStock") -> Dict[str, Any]:
    """
    获取数据仓库统计信息。

    Returns:
        {"market": str, "db_name": str, "exists": bool, "root": str,
         "tables": [str], "symbols": int, "stocks": int, "symbol_list": [str],
         "total_rows": int, "date_range": {"start": str, "end": str}}
    """
    writer = _get_writer()
    stats = writer.stats(market)
    # 兼容旧字段
    stats.setdefault("root", f"db_market ({stats.get('db_name', 'unknown')})")
    stats.setdefault("stocks", stats.get("symbols", 0))
    return stats
