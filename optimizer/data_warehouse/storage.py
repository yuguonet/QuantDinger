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
    import os
    try:
        from dotenv import load_dotenv
        _this_dir = os.path.dirname(os.path.abspath(__file__))
        _project_root = os.path.dirname(os.path.dirname(_this_dir))
        _backend_root = os.path.join(_project_root, "backend_api_python")
        for env_path in [
            os.path.join(_backend_root, '.env'),
            os.path.join(_project_root, '.env'),
        ]:
            if os.path.isfile(env_path):
                load_dotenv(env_path, override=False)
                break
    except ImportError:
        pass


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
