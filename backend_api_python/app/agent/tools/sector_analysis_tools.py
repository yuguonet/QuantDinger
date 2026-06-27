# -*- coding: utf-8 -*-
"""
Sector Analysis Tools — 桥接 market_cn.china_market 到 agent 工具系统。

所有板块/概念分析统一走 china_market.py（带缓存+自动刷新）。
不直接 import hot_sectors/sector_history 底层模块。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List


logger = logging.getLogger(__name__)


def _to_float(val, default=0.0) -> float:
    """安全转 float，处理 '-' 等异常值。"""
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _to_int(val, default=0) -> int:
    """安全转 int，处理 '-' 等异常值。"""
    if val is None:
        return default
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return default


def _fetch_em_hot_sectors(board_type: str, limit: int = 15) -> List[Dict[str, Any]]:
    """从东方财富直接获取热门板块排名（带 BK 代码）。"""
    fs_filter = _BOARD_TYPE_MAP.get(board_type)
    if not fs_filter:
        return []
    try:
        import requests
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://data.eastmoney.com/",
        }
        params = {
            "pn": 1, "pz": limit, "po": 1, "np": 1, "fltt": 2, "invt": 2,
            "fid": "f3", "fs": fs_filter,
            "fields": "f2,f3,f8,f12,f14,f100,f104,f105,f115,f128",
        }
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        data = resp.json()
        items = (data.get("data") or {}).get("diff") or []
        results = []
        for item in items:
            if not isinstance(item, dict):
                continue
            results.append({
                "code": str(item.get("f12") or ""),
                "name": str(item.get("f14") or ""),
                "change_pct": _to_float(item.get("f3")),
                "limit_up_count": _to_int(item.get("f100")),
                "turnover": _to_float(item.get("f8")),
                "up_count": _to_int(item.get("f104")),
                "down_count": _to_int(item.get("f105")),
                "leading_stock": str(item.get("f128") or ""),
                "leading_stock_pct": _to_float(item.get("f115")),
            })
        return results
    except Exception as e:
        logger.warning("_fetch_em_hot_sectors(%s) 失败: %s", board_type, e)
        return []


def get_hot_sectors(industry_limit: int = 15, concept_limit: int = 15) -> Dict[str, Any]:
    """实时热门板块：返回行业+概念板块的涨跌幅排名、涨停家数、领涨股。

    数据源为东方财富，返回的 code 字段为可直接使用的 BK 板块代码。

    Args:
        industry_limit: 行业板块数量，默认15
        concept_limit: 概念板块数量，默认15
    """
    try:
        industry = _fetch_em_hot_sectors("industry", industry_limit)
        concept = _fetch_em_hot_sectors("concept", concept_limit)

        from app.market_cn.china_market import get_hot_sectors as _get
        result = _get(industry_limit=industry_limit, concept_limit=concept_limit)
        analysis = (result.get("data") or {}).get("analysis") if isinstance(result, dict) else None

        return {
            "code": 1,
            "msg": "success",
            "data": {
                "timestamp": result.get("data", {}).get("timestamp", ""),
                "industry": [{
                    "code": s.get("code", ""),
                    "name": s.get("name", ""),
                    "change_pct": s.get("change_pct", 0),
                    "limit_up_count": s.get("limit_up_count", 0),
                    "leading_stock": s.get("leading_stock", ""),
                } for s in industry],
                "concept": [{
                    "code": s.get("code", ""),
                    "name": s.get("name", ""),
                    "change_pct": s.get("change_pct", 0),
                    "limit_up_count": s.get("limit_up_count", 0),
                    "leading_stock": s.get("leading_stock", ""),
                } for s in concept],
                "analysis": analysis or {},
            },
        }
    except Exception as e:
        logger.warning("get_hot_sectors failed: %s", e)
        return {"error": str(e)}


def get_sector_trend_analysis(board_type: str = "industry") -> Dict[str, Any]:
    """板块趋势：返回近1月涨跌趋势、6个月周期位置、今日预测信号。

    Args:
        board_type: 板块类型，"industry"(行业) 或 "concept"(概念)
    """
    try:
        from app.market_cn.china_market import get_sector_trend as _get
        return _get(board_type=board_type)
    except Exception as e:
        logger.warning("get_sector_trend_analysis failed: %s", e)
        return {"error": str(e)}


def get_sector_history_data(board_type: str = "industry", days: int = 30) -> Dict[str, Any]:
    """板块历史排名：返回板块近N天的每日涨跌幅排名变化。

    Args:
        board_type: 板块类型，"industry"(行业) 或 "concept"(概念)
        days: 获取天数，默认30
    """
    try:
        from app.market_cn.china_market import get_sector_history as _get
        return _get(board_type=board_type, days=days)
    except Exception as e:
        logger.warning("get_sector_history_data failed: %s", e)
        return {"error": str(e)}


def get_sector_prediction() -> Dict[str, Any]:
    """板块预测：基于资金流+情绪+技术面，预测今日可能走强的板块。"""
    try:
        from app.market_cn.china_market import get_sector_prediction as _get
        return _get()
    except Exception as e:
        logger.warning("get_sector_prediction failed: %s", e)
        return {"error": str(e)}


def get_sector_cycle(board_type: str = "industry") -> Dict[str, Any]:
    """板块周期：返回板块6个月内的周期位置（高位/低位/上升/下降）。

    Args:
        board_type: 板块类型，"industry"(行业) 或 "concept"(概念)
    """
    try:
        from app.market_cn.china_market import get_sector_cycle as _get
        return _get(board_type=board_type)
    except Exception as e:
        logger.warning("get_sector_cycle failed: %s", e)
        return {"error": str(e)}


def get_stock_sector_info(codes: str) -> Dict[str, Any]:
    """从本地数据库查询股票所属行业和概念。

    Args:
        codes: 多股用逗号分隔"
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str) -> Dict[str, Any]:
        try:
            from app.utils.basicinfo_db import get_stock_basic_db
            from app.data_sources.normalizer import strip_market_prefix

            db = get_stock_basic_db()
            sym = strip_market_prefix(stock_code)
            stock = db.get_stock(sym)

            if not stock:
                return {"stock_code": stock_code, "error": "未找到该股票信息"}

            result = {"stock_code": sym}
            if stock.get("name"):
                result["name"] = stock["name"]
            if stock.get("industry"):
                result["industry"] = stock["industry"]
            concepts_str = stock.get("concepts", "")
            if concepts_str:
                result["concepts"] = [c.strip() for c in concepts_str.split(",") if c.strip()]
            if stock.get("market_cn"):
                result["market_cn"] = stock["market_cn"]
            if stock.get("list_date"):
                result["list_date"] = stock["list_date"]
            return result
        except Exception as e:
            logger.warning("get_stock_sector_info(%s) failed: %s", stock_code, e)
            return {"stock_code": stock_code, "error": str(e)}

    if len(code_list) == 1:
        return _one(code_list[0])

    results = {}
    for code in code_list:
        try:
            results[code] = _one(code)
        except Exception as e:
            results[code] = {"error": str(e)}
    return {"count": len(results), "data": results}


# 东方财富板块类型与 filter 映射
_BOARD_TYPE_MAP = {
    "industry": "m:90+t:2",
    "concept": "m:90+t:3",
}

# 板块名称 → 代码缓存的线程安全存储
_board_name_cache: Dict[str, Dict[str, str]] = {}


def _build_board_name_cache() -> Dict[str, Dict[str, str]]:
    """构建{板块类型: {板块名称: BK代码}}映射缓存。"""
    if _board_name_cache:
        return _board_name_cache

    try:
        import requests
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://data.eastmoney.com/",
        }
        for board_type, fs_filter in _BOARD_TYPE_MAP.items():
            params = {
                "pn": 1, "pz": 500, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                "fid": "f3", "fs": fs_filter, "fields": "f12,f14",
            }
            try:
                resp = requests.get(url, params=params, headers=headers, timeout=15)
                data = resp.json()
                items = (data.get("data") or {}).get("diff") or []
                mapping = {}
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    code = str(item.get("f12") or "").strip()
                    name = str(item.get("f14") or "").strip()
                    if code and name:
                        mapping[name] = code
                _board_name_cache[board_type] = mapping
                logger.debug("_build_board_name_cache: %s %d 个板块", board_type, len(mapping))
            except Exception as e:
                logger.warning("_build_board_name_cache(%s) 失败: %s", board_type, e)
                _board_name_cache[board_type] = {}
    except Exception as e:
        logger.warning("_build_board_name_cache 初始化失败: %s", e)

    return _board_name_cache


def _resolve_board_code(name_or_code: str) -> str:
    """将板块名称（如 '玻璃行业'）解析为东方财富板块代码（如 'BK0546'）。"""
    if not name_or_code:
        return ""
    # 已经是合法代码格式（BK + 数字）
    if re.match(r'^BK\d+$', name_or_code):
        return name_or_code
    # 遍历行业/概念板块缓存的名称→代码映射
    cache = _build_board_name_cache()
    for mapping in cache.values():
        if name_or_code in mapping:
            return mapping[name_or_code]
    logger.warning("_resolve_board_code: 未找到 '%s' 对应 BK 代码", name_or_code)
    return name_or_code


def get_sector_stocks(board_code: str = "", board_name: str = "", limit: int = 10) -> List[Dict[str, Any]]:
    """获取板块内强势个股列表。

    Args:
        board_code: 板块代码（如 BK0475），与 board_name 二选一
        board_name: 板块名称（如 '玻璃行业'），与 board_code 二选一
        limit: 返回数量，默认10
    """
    try:
        resolved = board_code or _resolve_board_code(board_name)
        from app.market_cn.china_market import get_sector_stocks as _get
        result = _get(board_code=resolved, limit=limit)
        if isinstance(result, dict) and result.get("code") == 1:
            return result.get("data", [])
        logger.warning("get_sector_stocks(%s) 返回异常: %s", resolved, result)
        return []
    except Exception as e:
        logger.warning("get_sector_stocks(%s) failed: %s", board_code or board_name, e)
        return []
