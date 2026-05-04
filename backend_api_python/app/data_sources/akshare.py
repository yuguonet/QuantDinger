"""
AkShare 数据源 — A股龙虎榜 / 热榜 / 涨跌停池 / 炸板池

通过 AkShare 调用多家财经网站数据接口，作为 EastMoney 直连的 fallback。
AkShare 在国内服务器稳定，海外可能受限。

后端源:
- 东财 (stock_*_em): 龙虎榜/涨停池/跌停池/炸板池/人气榜
- 问财/同花顺 (stock_hot_rank_wc): 热榜/人气榜 (不同于东财排行)
- 同花顺 (stock_hot_concept): 热门概念

数据接口:
- 龙虎榜: stock_dragon_tiger_em()
- 热榜(东财): stock_hot_rank_em()
- 热榜(问财): stock_hot_rank_wc()
- 涨停池: stock_zt_pool_em()
- 跌停池: stock_zt_pool_dtgc_em()
- 炸板池: stock_zt_pool_zbgc_em()
"""

from __future__ import annotations

from typing import Any, Dict, List

from app.data_sources.rate_limiter import get_akshare_limiter
from app.data_sources.normalizer import (
    normalize_dragon_tiger_list,
    normalize_hot_rank,
    normalize_zt_pool,
    normalize_dt_pool,
    normalize_broken_board,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _import_akshare():
    """延迟导入 akshare，避免模块加载时的 import 开销"""
    import akshare as ak
    return ak


# ---------- 龙虎榜 ----------

def fetch_akshare_dragon_tiger(start_date: str, end_date: str) -> List[Dict[str, Any]]:
    """通过 AkShare (东财后端) 获取龙虎榜明细，标准化后返回"""
    ak = _import_akshare()
    get_akshare_limiter().wait()

    try:
        df = ak.stock_dragon_tiger_em(start_date=start_date, end_date=end_date)
    except Exception as e:
        logger.debug(f"[AkShare] dragon_tiger failed: {e}")
        return []

    if df is None or df.empty:
        return []

    raw_list = df.to_dict("records")
    result = [r for r in normalize_dragon_tiger_list(raw_list, source="akshare")
              if r.get("stock_code") and r.get("trade_date")]

    logger.info(f"[AkShare] dragon_tiger {start_date}~{end_date}: {len(result)} records")
    return result


# ---------- 热榜 (东财后端) ----------

def fetch_akshare_hot_rank() -> List[Dict[str, Any]]:
    """通过 AkShare (东财后端) 获取人气榜，标准化后返回"""
    ak = _import_akshare()
    get_akshare_limiter().wait()

    try:
        df = ak.stock_hot_rank_em()
    except Exception as e:
        logger.debug(f"[AkShare] hot_rank_em failed: {e}")
        return []

    if df is None or df.empty:
        return []

    result = []
    for _, row in df.iterrows():
        raw = row.to_dict()
        item = normalize_hot_rank(raw, source="akshare")
        if item.get("stock_code"):
            result.append(item)

    logger.info(f"[AkShare] hot_rank(东财): {len(result)} stocks")
    return result


# ---------- 热榜 (问财/同花顺后端) ----------

def fetch_akshare_hot_rank_wc() -> List[Dict[str, Any]]:
    """
    通过 AkShare (问财/同花顺后端) 获取人气榜。

    问财的排行算法与东财不同，可以作为热榜的补充源。
    """
    ak = _import_akshare()
    get_akshare_limiter().wait()

    try:
        df = ak.stock_hot_rank_wc()
    except Exception as e:
        logger.debug(f"[AkShare] hot_rank_wc failed: {e}")
        return []

    if df is None or df.empty:
        return []

    result = []
    for _, row in df.iterrows():
        raw = row.to_dict()
        # 问财的列名: 序号, 股票代码, 股票简称, 最新价, 涨跌幅, 人气排名, 个股热度
        item = normalize_hot_rank(raw, source="akshare")
        if item.get("stock_code"):
            result.append(item)

    logger.info(f"[AkShare] hot_rank(问财): {len(result)} stocks")
    return result


# ---------- 涨停池 ----------

def fetch_akshare_zt_pool(trade_date: str) -> List[Dict[str, Any]]:
    """通过 AkShare (东财后端) 获取涨停池，标准化后返回"""
    ak = _import_akshare()
    get_akshare_limiter().wait()

    try:
        df = ak.stock_zt_pool_em(date=trade_date.replace("-", ""))
    except Exception as e:
        logger.debug(f"[AkShare] zt_pool failed: {e}")
        return []

    if df is None or df.empty:
        return []

    result = []
    for _, row in df.iterrows():
        raw = row.to_dict()
        item = normalize_zt_pool(raw, source="akshare", trade_date=trade_date)
        if item.get("stock_code"):
            result.append(item)

    logger.info(f"[AkShare] zt_pool {trade_date}: {len(result)} stocks")
    return result


# ---------- 跌停池 ----------

def fetch_akshare_dt_pool(trade_date: str) -> List[Dict[str, Any]]:
    """通过 AkShare (东财后端) 获取跌停池，标准化后返回"""
    ak = _import_akshare()
    get_akshare_limiter().wait()

    try:
        df = ak.stock_zt_pool_dtgc_em(date=trade_date.replace("-", ""))
    except Exception as e:
        logger.debug(f"[AkShare] dt_pool failed: {e}")
        return []

    if df is None or df.empty:
        return []

    result = []
    for _, row in df.iterrows():
        raw = row.to_dict()
        item = normalize_dt_pool(raw, source="akshare", trade_date=trade_date)
        if item.get("stock_code"):
            result.append(item)

    logger.info(f"[AkShare] dt_pool {trade_date}: {len(result)} stocks")
    return result


# ---------- 炸板池 ----------

def fetch_akshare_broken_board(trade_date: str) -> List[Dict[str, Any]]:
    """通过 AkShare (东财后端) 获取炸板池，标准化后返回"""
    ak = _import_akshare()
    get_akshare_limiter().wait()

    try:
        df = ak.stock_zt_pool_zbgc_em(date=trade_date.replace("-", ""))
    except Exception as e:
        logger.debug(f"[AkShare] broken_board failed: {e}")
        return []

    if df is None or df.empty:
        return []

    result = []
    for _, row in df.iterrows():
        raw = row.to_dict()
        item = normalize_broken_board(raw, source="akshare", trade_date=trade_date)
        if item.get("stock_code"):
            result.append(item)

    logger.info(f"[AkShare] broken_board {trade_date}: {len(result)} stocks")
    return result
