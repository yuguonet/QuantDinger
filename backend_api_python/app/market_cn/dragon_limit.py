"""
龙虎榜 / 涨跌停池 / 炸板池 — 统一数据层

4 个核心接口:
  - get_dragon_tiger(start_date, end_date)  龙虎榜
  - get_zt_pool(trade_date)                 涨停池
  - get_dt_pool(trade_date)                 跌停池
  - get_broken_board(trade_date)            炸板池

数据源优先级: HTTP 东财搜索 → AkShare 兜底
"""

from __future__ import annotations

from typing import Any, Dict, List

from app.data_sources.rate_limiter import get_akshare_limiter
from app.data_sources.normalizer import _sf, _si, _ss
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════════
#  数据标准化函数 (正本: market_schema.py)
# ══════════════════════════════════════════════════════════════

def _normalize_dragon_tiger(raw: Dict[str, Any], source: str = "") -> Dict[str, Any]:
    """标准化一条龙虎榜记录（内部用）"""
    if source == "akshare" or "代码" in raw:
        return {
            "stock_code": _ss(raw.get("代码", raw.get("code", raw.get("stock_code")))),
            "stock_name": _ss(raw.get("名称", raw.get("name", raw.get("stock_name")))),
            "trade_date": _ss(raw.get("上榜日", raw.get("trade_date", raw.get("stock_code", ""))))[:10],
            "reason": _ss(raw.get("解读", raw.get("reason", raw.get("EXPLANATION"))))[:100],
            "buy_amount": _sf(raw.get("龙虎榜买入额", raw.get("buy_amount", raw.get("BUY")))),
            "sell_amount": _sf(raw.get("龙虎榜卖出额", raw.get("sell_amount", raw.get("SELL")))),
            "net_amount": _sf(raw.get("龙虎榜净额", raw.get("net_amount", raw.get("NET_BUY")))),
            "change_percent": _sf(raw.get("涨跌幅", raw.get("change_percent", raw.get("CHANGE_RATE")))),
            "close_price": _sf(raw.get("收盘价", raw.get("close_price", raw.get("CLOSE_PRICE")))),
            "turnover_rate": _sf(raw.get("换手率", raw.get("turnover_rate", raw.get("TURNOVERRATE")))),
            "amount": _sf(raw.get("成交额", raw.get("amount", raw.get("ACCUM_AMOUNT")))),
            "buy_seat_count": _si(raw.get("买入席位数", raw.get("buy_seat_count", raw.get("BUYER_NUM", 0)))),
            "sell_seat_count": _si(raw.get("卖出席位数", raw.get("sell_seat_count", raw.get("SELLER_NUM", 0)))),
        }
    return {
        "stock_code": _ss(raw.get("stock_code", raw.get("SECURITY_CODE"))),
        "stock_name": _ss(raw.get("stock_name", raw.get("SECURITY_NAME_ABBR"))),
        "trade_date": _ss(raw.get("trade_date", raw.get("TRADE_DATE", "")))[:10],
        "reason": _ss(raw.get("reason", raw.get("EXPLANATION"))),
        "buy_amount": _sf(raw.get("buy_amount", raw.get("BUY"))),
        "sell_amount": _sf(raw.get("sell_amount", raw.get("SELL"))),
        "net_amount": _sf(raw.get("net_amount", raw.get("NET_BUY"))),
        "change_percent": _sf(raw.get("change_percent", raw.get("CHANGE_RATE"))),
        "close_price": _sf(raw.get("close_price", raw.get("CLOSE_PRICE"))),
        "turnover_rate": _sf(raw.get("turnover_rate", raw.get("TURNOVERRATE"))),
        "amount": _sf(raw.get("amount", raw.get("ACCUM_AMOUNT"))),
        "buy_seat_count": _si(raw.get("buy_seat_count", raw.get("BUYER_NUM", 0))),
        "sell_seat_count": _si(raw.get("sell_seat_count", raw.get("SELLER_NUM", 0))),
    }


def _normalize_dragon_tiger_list(raw_list: List[Dict], source: str = "") -> List[Dict[str, Any]]:
    """批量标准化龙虎榜数据（内部用）"""
    return [_normalize_dragon_tiger(r, source) for r in raw_list if isinstance(r, dict)]


def _normalize_hot_rank(raw: Dict[str, Any], source: str = "") -> Dict[str, Any]:
    """标准化一条热榜记录"""
    if source == "akshare" or "股票代码" in raw:
        return {
            "rank": _si(raw.get("当前排名", raw.get("rank", raw.get("RANK")))),
            "stock_code": _ss(raw.get("股票代码", raw.get("code", raw.get("SECURITY_CODE")))),
            "stock_name": _ss(raw.get("股票名称", raw.get("name", raw.get("SECURITY_NAME_ABBR")))),
            "price": _sf(raw.get("最新价", raw.get("price", raw.get("NEWEST_PRICE")))),
            "change_percent": _sf(raw.get("涨跌幅", raw.get("change_percent", raw.get("CHANGE_RATE")))),
            "popularity_score": _sf(raw.get("人气值", raw.get("popularity", raw.get("HOT_NUM", raw.get("SCORE"))))),
            "current_rank_change": _ss(raw.get("排名变化", raw.get("rank_change", raw.get("RANK_CHANGE")))),
        }
    return {
        "rank": _si(raw.get("rank", raw.get("RANK"))),
        "stock_code": _ss(raw.get("stock_code", raw.get("SECURITY_CODE"))),
        "stock_name": _ss(raw.get("stock_name", raw.get("SECURITY_NAME_ABBR"))),
        "price": _sf(raw.get("price", raw.get("NEWEST_PRICE"))),
        "change_percent": _sf(raw.get("change_percent", raw.get("CHANGE_RATE"))),
        "popularity_score": _sf(raw.get("popularity_score", raw.get("HOT_NUM", raw.get("SCORE")))),
        "current_rank_change": _ss(raw.get("current_rank_change", raw.get("RANK_CHANGE"))),
    }


def _normalize_zt_pool(raw: Dict[str, Any], source: str = "", trade_date: str = "") -> Dict[str, Any]:
    """标准化涨停池记录"""
    if source == "akshare" or "代码" in raw:
        return {
            "stock_code": _ss(raw.get("代码", raw.get("code", raw.get("stock_code")))),
            "stock_name": _ss(raw.get("名称", raw.get("name", raw.get("stock_name")))),
            "trade_date": trade_date,
            "price": _sf(raw.get("最新价", raw.get("close", raw.get("price")))),
            "change_percent": _sf(raw.get("涨跌幅", raw.get("pct_chg", raw.get("change_percent")))),
            "continuous_zt_days": _si(raw.get("连板数", raw.get("zt_days", raw.get("continuous_zt_days", 1)))),
            "zt_time": _ss(raw.get("涨停时间", raw.get("zt_time"))),
            "seal_amount": _sf(raw.get("封板资金", raw.get("seal_amount"))),
            "turnover_rate": _sf(raw.get("换手率", raw.get("turnover_rate"))),
            "volume": _sf(raw.get("成交额", raw.get("amount", raw.get("volume")))),
            "amount": _sf(raw.get("成交额", raw.get("amount"))),
            "sector": _ss(raw.get("所属行业", raw.get("sector"))),
            "reason": _ss(raw.get("涨停原因", raw.get("reason"))),
            "open_count": _si(raw.get("炸板次数", raw.get("open_count", 0))),
        }
    return {
        "stock_code": _ss(raw.get("stock_code", raw.get("SECURITY_CODE"))),
        "stock_name": _ss(raw.get("stock_name", raw.get("SECURITY_NAME_ABBR"))),
        "trade_date": _ss(raw.get("trade_date", trade_date)),
        "price": _sf(raw.get("price", raw.get("CLOSE_PRICE"))),
        "change_percent": _sf(raw.get("change_percent", raw.get("CHANGE_RATE"))),
        "continuous_zt_days": _si(raw.get("continuous_zt_days", raw.get("CONTINUOUS_LIMIT_DAYS", raw.get("ZT_DAYS", 1)))),
        "zt_time": _ss(raw.get("zt_time", raw.get("FIRST_ZDT_TIME"))),
        "seal_amount": _sf(raw.get("seal_amount", raw.get("LIMIT_ORDER_AMT"))),
        "turnover_rate": _sf(raw.get("turnover_rate", raw.get("TURNOVERRATE"))),
        "volume": _sf(raw.get("volume", raw.get("VOLUME"))),
        "amount": _sf(raw.get("amount", raw.get("TURNOVER"))),
        "sector": _ss(raw.get("sector", raw.get("BOARD_NAME"))),
        "reason": _ss(raw.get("reason", raw.get("ZT_REASON"))),
        "open_count": _si(raw.get("open_count", raw.get("OPEN_NUM", 0))),
    }


def _normalize_dt_pool(raw: Dict[str, Any], source: str = "", trade_date: str = "") -> Dict[str, Any]:
    """标准化跌停池记录"""
    if source == "akshare" or "代码" in raw:
        return {
            "stock_code": _ss(raw.get("代码", raw.get("code", raw.get("stock_code")))),
            "stock_name": _ss(raw.get("名称", raw.get("name", raw.get("stock_name")))),
            "trade_date": trade_date,
            "price": _sf(raw.get("最新价", raw.get("price"))),
            "change_percent": _sf(raw.get("涨跌幅", raw.get("change_percent"))),
            "seal_amount": _sf(raw.get("封单资金", raw.get("seal_amount"))),
            "turnover_rate": _sf(raw.get("换手率", raw.get("turnover_rate"))),
            "amount": _sf(raw.get("成交额", raw.get("amount"))),
        }
    return {
        "stock_code": _ss(raw.get("stock_code", raw.get("SECURITY_CODE"))),
        "stock_name": _ss(raw.get("stock_name", raw.get("SECURITY_NAME_ABBR"))),
        "trade_date": _ss(raw.get("trade_date", trade_date)),
        "price": _sf(raw.get("price", raw.get("CLOSE_PRICE"))),
        "change_percent": _sf(raw.get("change_percent", raw.get("CHANGE_RATE"))),
        "seal_amount": _sf(raw.get("seal_amount", raw.get("LIMIT_ORDER_AMT"))),
        "turnover_rate": _sf(raw.get("turnover_rate", raw.get("TURNOVERRATE"))),
        "amount": _sf(raw.get("amount", raw.get("TURNOVER"))),
    }


def _normalize_broken_board(raw: Dict[str, Any], source: str = "", trade_date: str = "") -> Dict[str, Any]:
    """标准化炸板池记录"""
    if source == "akshare" or "代码" in raw:
        return {
            "stock_code": _ss(raw.get("代码", raw.get("code", raw.get("stock_code")))),
            "stock_name": _ss(raw.get("名称", raw.get("name", raw.get("stock_name")))),
            "trade_date": trade_date,
            "price": _sf(raw.get("最新价", raw.get("price"))),
            "change_percent": _sf(raw.get("涨跌幅", raw.get("change_percent"))),
            "zt_time": _ss(raw.get("涨停时间", raw.get("zt_time"))),
            "break_time": _ss(raw.get("炸板时间", raw.get("break_time"))),
            "turnover_rate": _sf(raw.get("换手率", raw.get("turnover_rate"))),
            "amount": _sf(raw.get("成交额", raw.get("amount"))),
        }
    return {
        "stock_code": _ss(raw.get("stock_code", raw.get("SECURITY_CODE"))),
        "stock_name": _ss(raw.get("stock_name", raw.get("SECURITY_NAME_ABBR"))),
        "trade_date": _ss(raw.get("trade_date", trade_date)),
        "price": _sf(raw.get("price", raw.get("CLOSE_PRICE"))),
        "change_percent": _sf(raw.get("change_percent", raw.get("CHANGE_RATE"))),
        "zt_time": _ss(raw.get("zt_time", raw.get("FIRST_ZDT_TIME"))),
        "break_time": _ss(raw.get("break_time", raw.get("LAST_ZDT_TIME"))),
        "turnover_rate": _sf(raw.get("turnover_rate", raw.get("TURNOVERRATE"))),
        "amount": _sf(raw.get("amount", raw.get("TURNOVER"))),
    }


# ══════════════════════════════════════════════════════════════
#  HTTP 东财搜索 — 主数据源 (正本: eastmoney_search.py)
# ══════════════════════════════════════════════════════════════

def _em_search(keyword: str, page_size: int = 200) -> List[Dict[str, Any]]:
    """调东财智能选股搜索，返回股票列表（失败返回空列表）"""
    from app.market_cn.eastmoney_search import search_stocks
    try:
        raw = search_stocks(keyword=keyword, page_size=page_size)
        return raw.get("stocks", []) if raw.get("code") == 1 else []
    except Exception as e:
        logger.warning("[东财搜索] '%s' 失败: %s", keyword, e)
        return []


# ══════════════════════════════════════════════════════════════
#  AkShare — 兜底数据源
# ══════════════════════════════════════════════════════════════

def _import_akshare():
    import akshare as ak
    return ak


def _ak_dragon_tiger(start_date: str, end_date: str) -> List[Dict[str, Any]]:
    ak = _import_akshare()
    get_akshare_limiter().wait()
    try:
        df = ak.stock_dragon_tiger_em(start_date=start_date, end_date=end_date)
    except Exception as e:
        logger.debug("[AkShare] dragon_tiger failed: %s", e)
        return []
    if df is None or df.empty:
        return []
    raw_list = df.to_dict("records")
    result = [r for r in _normalize_dragon_tiger_list(raw_list, source="akshare")
              if r.get("stock_code") and r.get("trade_date")]
    logger.info("[AkShare] dragon_tiger %s~%s: %d records", start_date, end_date, len(result))
    return result


def _ak_zt_pool(trade_date: str) -> List[Dict[str, Any]]:
    ak = _import_akshare()
    get_akshare_limiter().wait()
    try:
        df = ak.stock_zt_pool_em(date=trade_date.replace("-", ""))
    except Exception as e:
        logger.debug("[AkShare] zt_pool failed: %s", e)
        return []
    if df is None or df.empty:
        return []
    result = []
    for _, row in df.iterrows():
        raw = row.to_dict()
        item = _normalize_zt_pool(raw, source="akshare", trade_date=trade_date)
        if item.get("stock_code"):
            result.append(item)
    logger.info("[AkShare] zt_pool %s: %d stocks", trade_date, len(result))
    return result


def _ak_dt_pool(trade_date: str) -> List[Dict[str, Any]]:
    ak = _import_akshare()
    get_akshare_limiter().wait()
    try:
        df = ak.stock_zt_pool_dtgc_em(date=trade_date.replace("-", ""))
    except Exception as e:
        logger.debug("[AkShare] dt_pool failed: %s", e)
        return []
    if df is None or df.empty:
        return []
    result = []
    for _, row in df.iterrows():
        raw = row.to_dict()
        item = _normalize_dt_pool(raw, source="akshare", trade_date=trade_date)
        if item.get("stock_code"):
            result.append(item)
    logger.info("[AkShare] dt_pool %s: %d stocks", trade_date, len(result))
    return result


def _ak_broken_board(trade_date: str) -> List[Dict[str, Any]]:
    ak = _import_akshare()
    get_akshare_limiter().wait()
    try:
        df = ak.stock_zt_pool_zbgc_em(date=trade_date.replace("-", ""))
    except Exception as e:
        logger.debug("[AkShare] broken_board failed: %s", e)
        return []
    if df is None or df.empty:
        return []
    result = []
    for _, row in df.iterrows():
        raw = row.to_dict()
        item = _normalize_broken_board(raw, source="akshare", trade_date=trade_date)
        if item.get("stock_code"):
            result.append(item)
    logger.info("[AkShare] broken_board %s: %d stocks", trade_date, len(result))
    return result


def _ak_hot_rank() -> List[Dict[str, Any]]:
    ak = _import_akshare()
    get_akshare_limiter().wait()
    try:
        df = ak.stock_hot_rank_em()
    except Exception as e:
        logger.debug("[AkShare] hot_rank_em failed: %s", e)
        return []
    if df is None or df.empty:
        return []
    result = []
    for _, row in df.iterrows():
        raw = row.to_dict()
        item = _normalize_hot_rank(raw, source="akshare")
        if item.get("stock_code"):
            result.append(item)
    logger.info("[AkShare] hot_rank: %d stocks", len(result))
    return result


# ══════════════════════════════════════════════════════════════
#  统一接口 — HTTP 优先，AkShare 兜底
# ══════════════════════════════════════════════════════════════

def get_dragon_tiger(start_date: str = "", end_date: str = "") -> List[Dict[str, Any]]:
    """获取龙虎榜数据。HTTP 东财搜索优先，AkShare 兜底。"""
    # HTTP 优先
    data = _em_search("龙虎榜", 200)
    if data:
        logger.info("[HTTP] dragon_tiger: %d stocks", len(data))
        return data
    # AkShare 兜底
    logger.info("[HTTP] dragon_tiger 无结果，回退 AkShare")
    return _ak_dragon_tiger(start_date, end_date)


def get_zt_pool(trade_date: str = "") -> List[Dict[str, Any]]:
    """获取涨停池。HTTP 东财搜索优先，AkShare 兜底。"""
    data = _em_search("涨停", 200)
    if data:
        logger.info("[HTTP] zt_pool: %d stocks", len(data))
        return data
    logger.info("[HTTP] zt_pool 无结果，回退 AkShare")
    return _ak_zt_pool(trade_date)


def get_dt_pool(trade_date: str = "") -> List[Dict[str, Any]]:
    """获取跌停池。HTTP 东财搜索优先，AkShare 兜底。"""
    data = _em_search("跌停", 200)
    if data:
        logger.info("[HTTP] dt_pool: %d stocks", len(data))
        return data
    logger.info("[HTTP] dt_pool 无结果，回退 AkShare")
    return _ak_dt_pool(trade_date)


def get_broken_board(trade_date: str = "") -> List[Dict[str, Any]]:
    """获取炸板池。HTTP 东财搜索优先，AkShare 兜底。"""
    data = _em_search("炸板", 200)
    if data:
        logger.info("[HTTP] broken_board: %d stocks", len(data))
        return data
    logger.info("[HTTP] broken_board 无结果，回退 AkShare")
    return _ak_broken_board(trade_date)


def get_hot_rank() -> List[Dict[str, Any]]:
    """获取热榜。HTTP 东财搜索优先，AkShare 兜底。"""
    data = _em_search("热门股票", 100)
    if data:
        logger.info("[HTTP] hot_rank: %d stocks", len(data))
        return data
    logger.info("[HTTP] hot_rank 无结果，回退 AkShare")
    return _ak_hot_rank()
