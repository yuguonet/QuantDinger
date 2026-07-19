# -*- coding: utf-8 -*-
"""
Data tools — real-time quotes, K-lines, stock info.
Wraps DataSourceFactory into OpenAI-function-callable tools.
"""
from __future__ import annotations

import json
from app.agent.log import logger
from typing import Any, Dict, List, Optional

from app.agent.tools.finance._analysis_utils import _get_ds

# ── Tool functions ────────────────────────────────────────────

def resolve_stock(keyword: str, market: str = "CNStock", limit: int = 10) -> Dict[str, Any]:
    """股票名称/代码双向解析。输入名称返回代码，输入代码返回名称，支持模糊搜索。

    Args:
        keyword: 搜索关键词（中文名称、代码、简拼等）
        market: 市场，默认 CNStock
        limit: 返回数量上限，默认10

    Returns:
        单只 → {"code": "...", "name": "...", "market": "..."}
        多只 → {"count": N, "data": [{"code": "...", ...}, ...]}
        失败 → {"error": "...", "retriable": false}
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return {"error": "搜索关键词不能为空", "retriable": False}

    limit = min(max(limit, 1), 50)
    try:
        from app.utils.basicinfo_db import get_stock_basic_db
        matches = get_stock_basic_db().search_stocks(keyword, limit=limit)
        items = [
            {"code": m["symbol"], "name": m.get("name", ""), "market": m.get("market_cn", market)}
            for m in matches
        ]
        if len(items) == 1:
            return items[0]
        return {"count": len(items), "data": items}
    except Exception as e:
        logger.error("resolve_stock(%s) failed: %s", keyword, e)
        return {"error": str(e), "retriable": False}


def get_realtime_quote(codes: str) -> Dict[str, Any]:
    """实时行情：价格、涨跌幅、成交量、换手率、量比、PE、PB、总市值等。

    Args:
        codes: 多股用逗号分隔
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    ds = _get_ds("CNStock")

    # 批量接口：一次 coordinate_tickers 拉全部
    try:
        tickers = ds.get_tickers(code_list)
    except Exception as e:
        logger.error("get_realtime_quote batch failed: %s", e)
        tickers = []

    # 按 symbol 索引
    ticker_map: Dict[str, Dict] = {}
    for t in tickers:
        sym = t.get("symbol", "")
        if sym:
            ticker_map[sym] = t

    results: Dict[str, Any] = {}
    for code in code_list:
        t = ticker_map.get(code)
        if t:
            results[code] = {"stock_code": code, "market": "CNStock", **t}
        else:
            results[code] = {"error": "未获取到行情", "stock_code": code}

    if len(code_list) == 1:
        return results[code_list[0]]
    return {"count": len(results), "data": results}

def agent_get_kline(codes: str, timeframe: str = "1D", days: int = 30) -> Dict[str, Any]:
    """K线数据：返回OHLCV，支持 A 股。

    ⚠️ 仅在需要原始数据或自定义计算时调用。趋势/指标/形态/量价/筹码分析已内置K线获取，不要重复调用。

    Args:
        codes: 多股用逗号分隔
        timeframe: 1m/5m/15m/30m/1H/4H/1D/1W，默认1D
        days: 天数，默认30，最大250
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    valid_timeframes = {"1m", "5m", "15m", "30m", "1H", "4H", "1D", "1W"}
    if timeframe not in valid_timeframes:
        return {"error": f"无效周期: {timeframe}，可选: {','.join(sorted(valid_timeframes))}"}
    _days = min(max(days, 1), 250)

    ds = _get_ds("CNStock")

    def _fetch(stock_code: str) -> list:
        try:
            return ds.get_kline(stock_code, timeframe, _days) or []
        except Exception as e:
            logger.error("get_kline(%s, %s, %d) failed: %s", stock_code, timeframe, _days, e)
            return []

    def _ts_to_date(ts) -> str:
        try:
            from datetime import datetime
            return datetime.fromtimestamp(int(ts)).strftime("%m-%d")
        except Exception:
            return str(ts)

    # ── 完整 OHLCV ──
    results: Dict[str, Any] = {}
    for code in code_list:
        klines = _fetch(code)
        results[code] = [{
            "t": k.get("date") or _ts_to_date(k.get("time", 0)),
            "o": round(k.get("open", 0), 2),
            "h": round(k.get("high", 0), 2),
            "l": round(k.get("low", 0), 2),
            "c": round(k.get("close", 0), 2),
            "v": k.get("volume", 0),
        } for k in klines]
    if len(code_list) == 1:
        return results[code_list[0]]
    return {"count": len(results), "data": results}

# ── 核心字段集（Agent 日常分析最常用的 ~15 个字段） ──────────────────────
_STOCK_INFO_CORE_FIELDS = {
    "stock_code", "name", "industry", "concepts",
    "price", "change_pct",
    "pe_ratio", "pe_ttm", "pe_static", "pb_ratio",
    "market_cap", "mcap_yi", "float_market_cap", "float_mcap_yi",
    "roe", "eps", "bvps",
    "total_shares", "circ_shares",
    "turnover_pct", "vol_ratio",
    "list_date", "main_business",
    "source", "market_cn",
}
def _filter_stock_info(info: Dict[str, Any], detail: bool = False) -> Dict[str, Any]:
    """过滤股票信息，去除 None 值；非 detail 模式只保留核心字段。"""
    if not isinstance(info, dict):
        return info
    # error 直接透传，不过滤
    if "error" in info:
        return info
    if detail:
        return {k: v for k, v in info.items() if v is not None}
    return {k: v for k, v in info.items() if v is not None and k in _STOCK_INFO_CORE_FIELDS}

def get_stock_info(codes: str, detail: bool = False) -> Dict[str, Any]:
    """股票基本信息（精简模式，节省 token）。

    默认返回核心字段：名称、行业、价格、PE/PB、市值、ROE、EPS、股本等。
    数据量小，Agent 直接评估更靠谱，不出评分。
    如需完整财务数据（利润表/资产负债表/现金流/股东/杜邦），设置 detail=true。

    Args:
        codes: 多股用逗号分隔
        detail: false=精简（默认），true=完整（50+字段）
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    # 缓存检查已移除：basicinfo_db 本地快读 + HTTP 5s 竞赛替代

    def _one(stock_code: str) -> Dict[str, Any]:
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
        from app.data_sources.normalizer import strip_market_prefix

        sym = strip_market_prefix(stock_code)

        # ── 1) basicinfo_db 本地快读（毫秒级）──
        db_result: Dict[str, Any] = {}
        try:
            from app.utils.basicinfo_db import get_stock_basic_db
            db = get_stock_basic_db()
            stock = db.get_stock(sym)
            if stock:
                db_result = {"stock_code": sym}
                for fld in ("name", "industry", "total_shares", "circ_shares",
                            "pe_ratio", "pb_ratio", "market_cn", "list_date"):
                    val = stock.get(fld)
                    if val is not None and val != "":
                        db_result[fld] = val
                concepts_str = stock.get("concepts", "")
                if concepts_str:
                    db_result["concepts"] = [c.strip() for c in concepts_str.split(",") if c.strip()]
                db_result["source"] = "basicinfo_db"
        except Exception as e:
            logger.warning("get_stock_info(%s) basicinfo_db failed: %s", stock_code, e)

        # ── 2) HTTP 实时拉取（5s 超时竞赛）──
        def _http_fetch() -> Dict[str, Any]:
            # 数据源原生
            try:
                ds = _get_ds("CNStock")
                if hasattr(ds, "get_stock_info"):
                    info = ds.get_stock_info(stock_code)
                    if isinstance(info, dict) and not info.get("error"):
                        return info
                    elif isinstance(info, str):
                        try:
                            parsed = json.loads(info)
                            if isinstance(parsed, dict):
                                return parsed
                        except (ValueError, TypeError):
                            pass
            except (NotImplementedError, Exception) as e:
                logger.debug("get_stock_info(%s) datasource failed: %s", stock_code, e)

            # cn_stock_info 兜底
            try:
                from app.utils.cn_stock_info import get_cn_stock_info
                info = get_cn_stock_info(stock_code)
                if info and not info.get("error"):
                    return info
            except Exception as e:
                logger.debug("get_stock_info(%s) cn_stock_info failed: %s", stock_code, e)
            return {}

        http_result: Dict[str, Any] = {}
        _pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = _pool.submit(_http_fetch)
            http_result = future.result(timeout=5)
        except FuturesTimeout:
            logger.info("get_stock_info(%s) HTTP 5s 超时，使用 basicinfo_db", stock_code)
        except Exception as e:
            logger.debug("get_stock_info(%s) HTTP fetch error: %s", stock_code, e)
        finally:
            _pool.shutdown(wait=False)

        # ── 3) 合并：basicinfo_db 为底，HTTP 补充新字段 ──
        result = {**db_result, **{k: v for k, v in http_result.items() if v is not None}}

        if not result:
            return {"error": f"无法获取 {stock_code} 的基本面信息", "retriable": False}

        if http_result:
            result["source"] = "http"
        # else: 保持 basicinfo_db source

        # ── 4) 腾讯实时估值补全（3s 超时保护）──
        try:
            from app.agent.tools.finance.quote_tools import _tencent_quote_raw
            def _tencent_fetch():
                return _tencent_quote_raw([sym])
            _tpool = ThreadPoolExecutor(max_workers=1)
            try:
                tq = _tpool.submit(_tencent_fetch).result(timeout=3)
            finally:
                _tpool.shutdown(wait=False)
            q = tq.get(sym)
            if q:
                result.setdefault("price", q.get("price"))
                result.setdefault("change_pct", q.get("change_pct"))
                if q.get("pe_ttm"):
                    result["pe_ttm"] = q["pe_ttm"]
                if q.get("pe_static"):
                    result["pe_static"] = q["pe_static"]
                if q.get("pb"):
                    result["pb"] = q["pb"]
                if q.get("mcap_yi"):
                    result["mcap_yi"] = q["mcap_yi"]
                if q.get("float_mcap_yi"):
                    result["float_mcap_yi"] = q["float_mcap_yi"]
                if q.get("turnover_pct") is not None:
                    result["turnover_pct"] = q["turnover_pct"]
                if q.get("vol_ratio") is not None:
                    result["vol_ratio"] = q["vol_ratio"]
                if q.get("amplitude_pct") is not None:
                    result["amplitude_pct"] = q["amplitude_pct"]
                if q.get("amount_wan"):
                    result["amount_wan"] = q["amount_wan"]
                if q.get("limit_up"):
                    result["limit_up"] = q["limit_up"]
                if q.get("limit_down"):
                    result["limit_down"] = q["limit_down"]
        except FuturesTimeout:
            logger.info("get_stock_info(%s) 腾讯估值 3s 超时，跳过补全", stock_code)
        except Exception as e:
            logger.debug("get_stock_info(%s) 腾讯估值补全跳过: %s", stock_code, e)

        return result

    if len(code_list) == 1:
        return _filter_stock_info(_one(code_list[0]), detail)

    results = {}
    for code in code_list:
        try:
            results[code] = _filter_stock_info(_one(code), detail)
        except Exception as e:
            results[code] = {"error": str(e)}
    return {"count": len(results), "data": results}

