# -*- coding: utf-8 -*-
"""
Sector Analysis Tools — 桥接 market_cn.china_market 到 agent 工具系统。

所有板块/概念分析统一走 china_market.py（带缓存+自动刷新）。
不直接 import hot_sectors/sector_history 底层模块。
"""
from __future__ import annotations

from app.agent.log import logger
import re
from typing import Any, Dict, List
from app.agent.utils.md_format import _batch_execute, _to_md

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

# 2026-09-15：东财 UA 必须是**完整**的 Chrome UA。截断版（缺 "(KHTML, like Gecko)…"）
# 会被 push2.eastmoney.com 直接掐断连接（RemoteDisconnected，连状态码都不给）——
# 与 market_cn/hot_sectors.py:23-26 的实测结论一致。本文件此前两处都在用截断版，
# 这正是日志里 `Remote end closed connection without response` 的根因。
_EM_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Referer": "https://data.eastmoney.com/",
}
def _fetch_em_hot_sectors(board_type: str, limit: int = 15) -> List[Dict[str, Any]]:
    """从东方财富直接获取热门板块排名（带 BK 代码）。"""
    fs_filter = _BOARD_TYPE_MAP.get(board_type)
    if not fs_filter:
        return []
    try:
        import requests
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        headers = _EM_HEADERS
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
def _merge_em_rows(rows: List[Dict[str, Any]], em_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """用东财结果按**板块名**富化主源行：补 BK 代码与涨停家数。

    新浪主源缺这两项（代码是新浪自有编码、`limit_up_count` 恒为 0），而下游
    `get_sector_detail(board_code)` 需要 BK 代码 ⇒ 东财成功时按名对齐补上；
    东财失败则原样返回——**不再**让主结果变成空数组。
    """
    if not em_rows:
        return rows
    by_name = {str(r.get("name") or "").strip(): r for r in em_rows if isinstance(r, dict)}
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row = dict(row)
        e = by_name.get(str(row.get("name") or "").strip())
        if e:
            if e.get("code"):
                row["code"] = e["code"]                 # 换成 BK 代码，下游可直接用
            if _to_int(e.get("limit_up_count")):
                row["limit_up_count"] = _to_int(e.get("limit_up_count"))
            if e.get("leading_stock"):
                row["leading_stock"] = e["leading_stock"]
        # 新浪字段名是 lead_stock，对外统一成 leading_stock
        row["leading_stock"] = row.get("leading_stock") or row.get("lead_stock", "")
        out.append(row)
    return out


def get_hot_sectors(industry_limit: int = 15, concept_limit: int = 15) -> dict:
    """实时热门板块：返回行业+概念板块的涨跌幅排名、涨停家数、领涨股。

    2026-09-15 主源改为**新浪**（经 market_cn.china_market，其内部已是"新浪优先、
    东财兜底"）。东财退为**富化源**，只负责补 BK 板块代码与涨停家数。

    改动原因（事故）：此前 industry/concept **只**取自东财。东财被反爬掐断后
    （`RemoteDisconnected`；且本文件当时用的是必被掐断的截断 UA）两个列表直接变
    空数组，而同一时刻新浪的数据是好的 ⇒ 数据丢了却静默、无人察觉。
    现在东财挂掉不影响主结果。

    Returns:
        dict: {timestamp, industry:[...], concept:[...], analysis:{}}。板块列表在 hs['industry']/hs['concept']（list，勿用 hs['data']）。

    Args:
        industry_limit: 行业板块数量，默认15
        concept_limit: 概念板块数量，默认15
    """
    try:
        from app.market_cn.china_market import get_hot_sectors as _get

        result = _get(industry_limit=industry_limit, concept_limit=concept_limit)
        data = (result or {}).get("data") or {}

        # ① 主源：新浪优先的行业标准链路
        industry = list(data.get("industry") or [])
        concept = list(data.get("concept") or [])

        # ② 东财兜底：仅当新浪某板块列表为空时才降级调用东财。
        #    —— 主源健康时完全不碰东财，避免 push2 反爬熔断（RemoteDisconnected）。
        #    —— 新浪有数据时不富化 BK 代码（下游 get_sector_stocks 走 board_name 缓存解析）。
        if not industry or not concept:
            em_limit = max(int(industry_limit or 0), int(concept_limit or 0)) or 15
            em = {bt: _fetch_em_hot_sectors(bt, em_limit) for bt in ("industry", "concept")}
            if not industry:
                industry = em.get("industry") or []
            if not concept:
                concept = em.get("concept") or []

        def _slim(rows, limit):
            out = []
            for s in rows[:limit]:
                if not isinstance(s, dict):
                    continue
                out.append({
                    "code": s.get("code", ""),
                    "name": s.get("name", ""),
                    "change_pct": s.get("change_pct", 0),
                    "limit_up_count": _to_int(s.get("limit_up_count")),
                    "leading_stock": s.get("leading_stock") or s.get("lead_stock", ""),
                })
            return out

        return {
            "timestamp": data.get("timestamp", ""),
            "industry": _slim(industry, industry_limit),
            "concept": _slim(concept, concept_limit),
            "analysis": data.get("analysis") or {},
        }
    except Exception as e:
        logger.warning("get_hot_sectors failed: %s", e)
        return {"error": str(e)}
def get_sector_trend_analysis(board_type: str = "industry") -> dict:
    """板块趋势：返回近1月涨跌趋势、6个月周期位置、今日预测信号。

    Returns:
        {"code": 1成功/0失败, "msg": str, "data": {趋势指标 dict}}；失败 → {"error": str}。

    Args:
        board_type: 板块类型，"industry"(行业) 或 "concept"(概念)
    """
    try:
        from app.market_cn.china_market import get_sector_trend as _get
        return _get(board_type=board_type)
    except Exception as e:
        logger.warning("get_sector_trend_analysis failed: %s", e)
        return {"error": str(e)}
def get_sector_history_data(board_type: str = "industry", days: int = 30) -> dict:
    """板块历史排名：返回板块近N天的每日涨跌幅排名变化。

    Returns:
        {"code": 1成功/0失败, "msg": str, "count": N, "data": [每日排名行, ...]}；失败 → {"error": str}。

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
def get_stock_sector_info(codes: str) -> dict:
    """从本地数据库查询股票所属行业和概念。

    Returns:
        单代码 → {stock_code, name?, industry?, concepts[]?, market_cn?, list_date?}；
        多代码 → {"count": N, "data": {代码: 上述dict}}；失败 → {"error", "retriable"}。

    Args:
        codes: 多股用逗号分隔
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str) -> dict:
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

    return _batch_execute(_one, code_list)
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
        headers = _EM_HEADERS
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
def get_sector_stocks(board_code: str = "", board_name: str = "", limit: int = 10) -> dict:
    """获取板块内强势个股列表。

    Returns:
        list: 板块内个股列表，元素 {code, name, price, change_pct, amount, turnover, is_limit_up}；
        无数据/解析失败 → []（**裸列表**，非 dict）。

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
