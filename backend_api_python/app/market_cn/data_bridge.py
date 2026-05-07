"""
market_cn 数据桥接层 — 对外输出格式与 global_market 完全一致

提供三个函数，返回格式分别对应:
  - get_macro_data()        → market_data_collector._get_macro_data() 的输出格式
  - fetch_sentiment()       → plugin_api._fetch_sentiment_inproc() 的输出格式
  - fetch_overview()        → plugin_api._fetch_overview_inproc() 的输出格式

数据源: market_cn 内部的 get_fear_greed / get_china_macro / get_hot_sectors
缓存:   复用 china_market.py 的文件缓存，不再额外建缓存层
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


# ================================================================
#  1. macro_data — 对应 market_data_collector._get_macro_data()
#     返回: {KEY: {name, description, price, change, changePercent, level?}}
# ================================================================

def get_macro_data() -> Dict[str, Any]:
    """
    A股宏观数据 → 与 global_market.get_sentiment() 同格式输出

    映射关系:
      A股贪恐指数  → FEAR_GREED
      宏观经济     → CN_MACRO
      热门板块     → CN_HOT_SECTORS
    """
    from .china_market import get_fear_greed, get_china_macro, get_hot_sectors

    result: Dict[str, Any] = {}

    # A股贪恐指数 → FEAR_GREED
    try:
        fg_resp = get_fear_greed()
        fg = fg_resp.get("data") or {}
        score = fg.get("composite_score", 0)
        if score > 0:
            result["FEAR_GREED"] = {
                "name": "A股贪恐指数",
                "description": fg.get("label", "中性"),
                "price": score,
                "change": 0,
                "changePercent": 0,
            }
    except Exception as e:
        logger.warning("market_cn fear_greed failed: %s", e)

    # 宏观经济 → CN_MACRO
    try:
        macro_resp = get_china_macro()
        macro = macro_resp.get("data") or {}
        if macro:
            result["CN_MACRO"] = {
                "name": "中国宏观经济",
                "description": "GDP/CPI/PPI/PMI/M2/LPR/社融/进出口",
                "price": 0,
                "change": 0,
                "changePercent": 0,
                "data": macro,
            }
    except Exception as e:
        logger.warning("market_cn china_macro failed: %s", e)

    # 热门板块 → CN_HOT_SECTORS
    try:
        sector_resp = get_hot_sectors()
        sectors = sector_resp.get("data") or {}
        if sectors:
            result["CN_HOT_SECTORS"] = {
                "name": "热门板块",
                "description": "行业/概念板块涨幅排名",
                "price": 0,
                "change": 0,
                "changePercent": 0,
                "data": sectors,
            }
    except Exception as e:
        logger.warning("market_cn hot_sectors failed: %s", e)

    return result


# ================================================================
#  2. fetch_sentiment — 对应 plugin_api._fetch_sentiment_inproc()
#     返回: {fear_greed: {value, classification}, vix: {value, level}, dxy: {value, level}}
# ================================================================

def fetch_sentiment() -> Dict[str, Any]:
    """
    A股情绪数据 → 与 global_market.get_sentiment() 同格式输出

    fear_greed: A股7维度贪恐指数
    vix/dxy:    A股无对应，返回 0 占位
    """
    from .china_market import get_fear_greed

    fallback = {
        "fear_greed": {"value": 50, "classification": "中性", "source": "fallback"},
        "vix":        {"value": 0, "level": "unknown"},
        "dxy":        {"value": 0, "level": "unknown"},
    }

    try:
        fg_resp = get_fear_greed()
        fg = fg_resp.get("data") or {}
        score = fg.get("composite_score", 0)
        return {
            "fear_greed": {
                "value": score if score > 0 else 50,
                "classification": fg.get("label", "中性"),
                "source": "market_cn",
            },
            "vix": {"value": 0, "level": "unknown"},
            "dxy": {"value": 0, "level": "unknown"},
        }
    except Exception as e:
        logger.error("market_cn fetch_sentiment failed: %s", e)
        return fallback


# ================================================================
#  3. fetch_overview — 对应 plugin_api._fetch_overview_inproc()
#     返回: {indices: [...], forex: [...], crypto: [...], commodities: [...]}
# ================================================================

def fetch_overview() -> Dict[str, Any]:
    """
    A股概览数据 → 与 global_market.get_indices() 同格式输出

    indices:      A股主要指数（上证/深证/创业板/沪深300）— 取最新日线收盘
    forex/crypto/commodities: A股无直接对应，返回空列表
    """
    from .china_stock import ChinaData, fallback, ak_index_daily, bs_index_daily

    result: Dict[str, Any] = {
        "indices": [],
        "forex": [],
        "crypto": [],
        "commodities": [],
    }

    # 主要指数 — 取最新日线
    try:
        index_map = [
            ("000001.SH", "sh000001", "sh.000001", "上证指数"),
            ("399001.SZ", "sz399001", "sz.399001", "深证成指"),
            ("399006.SZ", "sz399006", "sz.399006", "创业板指"),
            ("000300.SH", "sh000300", "sh.000300", "沪深300"),
        ]
        data = ChinaData()
        for ts_code, ak_code, bs_code, name in index_map:
            try:
                df = fallback(
                    ("tushare", lambda c=ts_code: data.index_daily(c)),
                    ("akshare", lambda c=ak_code: ak_index_daily(c)),
                    ("baostock", lambda c=bs_code: bs_index_daily(c)),
                )()
                if df is not None and len(df) > 0:
                    last = df.iloc[-1]
                    close = float(last.get("close", last.get("收盘", 0)) or 0)
                    prev = float(df.iloc[-2].get("close", df.iloc[-2].get("收盘", 0)) or 0) if len(df) > 1 else close
                    change = ((close - prev) / prev * 100) if prev else 0
                    result["indices"].append({
                        "symbol": ts_code,
                        "name": name,
                        "name_cn": name,
                        "price": close,
                        "change": round(change, 2),
                    })
            except Exception:
                pass
    except Exception as e:
        logger.warning("market_cn indices failed: %s", e)

    return result
