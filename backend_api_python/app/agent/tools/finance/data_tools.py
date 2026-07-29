# -*- coding: utf-8 -*-
"""
Data tools — real-time quotes, K-lines, stock info, market indices.
Wraps DataSourceFactory into OpenAI-function-callable tools.
"""
from __future__ import annotations

import json
from app.agent.log import logger
from typing import Any, Dict, List, Optional

from app.agent.tools.finance._analysis_utils import _get_ds
from app.agent.utils.md_format import _batch_execute, _to_md

# ── 指数行情 / 市场概览 ──────────────────────────────────────

def get_market_indices() -> dict:
    """指数行情：返回上证/深证/创业板/科创/北证五大指数的价格、涨跌幅、成交量。"""
    from app.market_cn.index import get_index_realtime as _get
    try:
        data = _get()
        return {"count": len(data), "indices": data}
    except Exception as e:
        logger.warning("get_market_indices failed: %s", e)
        return {"error": str(e)}

def get_market_overview() -> dict:
    """市场概览：返回全市场涨跌家数、涨停跌停数、北向资金净买入、市场情绪指数、主力资金流向。"""
    result = {}

    # 指数行情 → 涨跌家数
    try:
        from app.market_cn.index import get_index_realtime
        indices = get_index_realtime(["000001", "399001"])
        up = sum(1 for i in (indices or []) if i.get("change_percent", 0) > 0)
        down = sum(1 for i in (indices or []) if i.get("change_percent", 0) < 0)
        result["up_count"] = up
        result["down_count"] = down
    except Exception:
        pass

    # 北向资金
    try:
        from app.market_cn.index import get_northbound_realtime
        nb = get_northbound_realtime()
        result["north_net_flow"] = round(nb.get("total_latest_yi", 0), 2)
    except Exception:
        pass

    # 情绪指数
    try:
        from app.market_cn.fear_greed_index import fear_greed_index
        fg = fear_greed_index()
        result["emotion"] = int(fg.get("composite_score", 50))
    except Exception:
        pass

    # 主力资金流
    try:
        from app.market_cn.index import get_market_fund_flow_realtime
        mf = get_market_fund_flow_realtime()
        result["main_net_yi"] = round(mf.get("main_net", 0) / 1e8, 2)
        result["main_pct"] = round(mf.get("main_pct", 0), 2)
    except Exception:
        pass

    return result

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

# ── 腾讯财经批量行情（内部共用） ──────────────────────────────

def _strip_prefix(s):
    from app.data_sources.normalizer import strip_market_prefix
    return strip_market_prefix(s)

def _tencent_quote_raw(codes: list) -> dict:
    """腾讯财经批量行情原始接口。返回 {code: {全部字段dict}}。"""
    import urllib.request

    prefixed = []
    for c in codes:
        c = _strip_prefix(c)
        if c.startswith(("6", "9", "5", "000")):
            if c.startswith("000") and not c.startswith(("002", "003")):
                prefixed.append(f"sh{c}")
            elif c.startswith(("6", "9", "5")):
                prefixed.append(f"sh{c}")
            else:
                prefixed.append(f"sz{c}")
        elif c.startswith("8"):
            prefixed.append(f"bj{c}")
        else:
            prefixed.append(f"sz{c}")

    url = "https://qt.gtimg.cn/q=" + ",".join(prefixed)
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    resp = urllib.request.urlopen(req, timeout=10)
    data = resp.read().decode("gbk")

    result = {}
    for line in data.strip().split(";"):
        if not line.strip() or "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]
        vals = line.split('"')[1].split("~")
        if len(vals) < 53:
            continue
        code = key[2:]
        result[code] = {
            "name":           vals[1],
            "price":          float(vals[3]) if vals[3] else 0,
            "last_close":     float(vals[4]) if vals[4] else 0,
            "open":           float(vals[5]) if vals[5] else 0,
            "buy_vol":        float(vals[6]) if vals[6] else 0,
            "sell_vol":       float(vals[7]) if vals[7] else 0,
            "bid1_price":     float(vals[9]) if vals[9] else 0,
            "bid1_vol":       float(vals[10]) if vals[10] else 0,
            "bid2_price":     float(vals[11]) if vals[11] else 0,
            "bid2_vol":       float(vals[12]) if vals[12] else 0,
            "bid3_price":     float(vals[13]) if vals[13] else 0,
            "bid3_vol":       float(vals[14]) if vals[14] else 0,
            "bid4_price":     float(vals[15]) if vals[15] else 0,
            "bid4_vol":       float(vals[16]) if vals[16] else 0,
            "bid5_price":     float(vals[17]) if vals[17] else 0,
            "bid5_vol":       float(vals[18]) if vals[18] else 0,
            "ask1_price":     float(vals[19]) if vals[19] else 0,
            "ask1_vol":       float(vals[20]) if vals[20] else 0,
            "ask2_price":     float(vals[21]) if vals[21] else 0,
            "ask2_vol":       float(vals[22]) if vals[22] else 0,
            "ask3_price":     float(vals[23]) if vals[23] else 0,
            "ask3_vol":       float(vals[24]) if vals[24] else 0,
            "ask4_price":     float(vals[25]) if vals[25] else 0,
            "ask4_vol":       float(vals[26]) if vals[26] else 0,
            "ask5_price":     float(vals[27]) if vals[27] else 0,
            "ask5_vol":       float(vals[28]) if vals[28] else 0,
            "change_amt":     float(vals[31]) if vals[31] else 0,
            "change_pct":     float(vals[32]) if vals[32] else 0,
            "high":           float(vals[33]) if vals[33] else 0,
            "low":            float(vals[34]) if vals[34] else 0,
            "amount_wan":     float(vals[37]) if vals[37] else 0,
            "turnover_pct":   float(vals[38]) if vals[38] else 0,
            "pe_ttm":         float(vals[39]) if vals[39] else 0,
            "amplitude_pct":  float(vals[43]) if vals[43] else 0,
            "mcap_yi":        float(vals[44]) if vals[44] else 0,
            "float_mcap_yi":  float(vals[45]) if vals[45] else 0,
            "pb":             float(vals[46]) if vals[46] else 0,
            "limit_up":       float(vals[47]) if vals[47] else 0,
            "limit_down":     float(vals[48]) if vals[48] else 0,
            "vol_ratio":      float(vals[49]) if vals[49] else 0,
            "pe_static":      float(vals[52]) if vals[52] else 0,
        }
    return result

# ── 五档盘口 ──────────────────────────────────────────────────

def get_order_book(codes: str) -> dict:
    """五档盘口：返回买卖各5档价格和挂单量、涨跌幅、换手率、PE、市值等。

    Args:
        codes: 多股用逗号分隔
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str) -> dict:
        code = _strip_prefix(stock_code)
        try:
            from app.market_cn.tape import get_order_book as _get_order_book
            result = _get_order_book(code)
            if "error" in result:
                return {"stock_code": code, "error": result["error"]}
            return result
        except Exception as e:
            logger.warning("get_order_book(%s) failed: %s", code, e)
            return {"stock_code": code, "error": str(e)}

    return _batch_execute(_one, code_list)

# ── 指数/ETF行情 ──────────────────────────────────────────────

def get_index_etf_quote(codes: str) -> dict:
    """指数/ETF行情：返回价格、涨跌幅、成交量，支持上证/深证/创业板/沪深300及对应ETF。

    Args:
        codes: 逗号分隔的代码，如 "000001,000300,399006,510050"
               指数：000001(上证) 399001(深证) 000300(沪深300) 399006(创业板)
               ETF：510050(上证50) 510300(沪深300) 159919(沪深300) 512880(证券)
    """
    code_list = [_strip_prefix(c.strip()) for c in codes.split(",") if c.strip()]
    if not code_list:
        return {"error": "请提供至少一个代码"}

    try:
        data = _tencent_quote_raw(code_list)
        results = []
        for code in code_list:
            q = data.get(code)
            if q:
                results.append({
                    "code": code,
                    "name": q["name"],
                    "price": q["price"],
                    "change_pct": q["change_pct"],
                    "change_amt": q["change_amt"],
                    "high": q["high"],
                    "low": q["low"],
                    "amount_wan": q["amount_wan"],
                    "open": q["open"],
                    "last_close": q["last_close"],
                })
            else:
                results.append({"code": code, "error": "未获取到数据"})
        _r = {"total": len(results), "quotes": results}
        return _r
    except Exception as e:
        logger.warning("get_index_etf_quote(%s) failed: %s", codes, e)
        return {"error": str(e)}

# ── 批量估值对比 ──────────────────────────────────────────────

def batch_valuation_compare(codes: str) -> dict:
    """估值对比：返回多只股票的PE/PB/市值/营收并排对比表。

    Args:
        codes: 逗号分隔的股票代码，如 "600519,000858,688017"
    """
    code_list = [_strip_prefix(c.strip()) for c in codes.split(",") if c.strip()]
    if not code_list:
        return {"error": "请提供至少一个股票代码"}
    if len(code_list) > 20:
        return {"error": "单次最多对比20只股票"}

    try:
        data = _tencent_quote_raw(code_list)
        results = []
        for code in code_list:
            q = data.get(code)
            if q:
                results.append({
                    "code": code,
                    "name": q["name"],
                    "price": q["price"],
                    "change_pct": q["change_pct"],
                    "pe_ttm": q["pe_ttm"],
                    "pb": q["pb"],
                    "mcap_yi": q["mcap_yi"],
                    "float_mcap_yi": q["float_mcap_yi"],
                    "turnover_pct": q["turnover_pct"],
                })
            else:
                results.append({"code": code, "error": "未获取到数据"})

        valid = [r for r in results if r.get("pe_ttm") and r["pe_ttm"] > 0]
        valid.sort(key=lambda x: x["pe_ttm"])

        _r = {
            "total": len(results),
            "stocks": results,
            "pe_sorted": [r["code"] for r in valid],
        }
        return _r
    except Exception as e:
        logger.warning("batch_valuation_compare(%s) failed: %s", codes, e)
        return {"error": str(e)}

# ═══════════════════════════════════════════════════════════════
# 基本面摘要（从 capital_tools.py 合并）
# ═══════════════════════════════════════════════════════════════

def _safe_float(v, default=0.0):
    from app.data_sources.normalizer import safe_float
    return safe_float(v, default)

def _market_prefix(code: str) -> str:
    if code.startswith(("6", "9")):
        return "sh"
    elif code.startswith("8"):
        return "bj"
    return "sz"

def _get_margin_trading(code: str, days: int = 60) -> Dict[str, Any]:
    """融资融券摘要。提取融资余额趋势+近期变化幅度。"""
    from app.agent.tools.finance.em_utils import em_datacenter
    data = em_datacenter(
        "RPTA_WEB_RZRQ_GGMX",
        filter_str=f'(SCODE="{code}")',
        page_size=days,
        sort_columns="DATE", sort_types="-1",
    )
    if not data:
        return {"signal": "无数据"}

    latest = data[0]
    rz_latest = _safe_float(latest.get("RZYE"))  # 融资余额
    rz_5d_ago = _safe_float(data[4].get("RZYE")) if len(data) > 5 else rz_latest
    rz_change_pct = ((rz_latest / rz_5d_ago - 1) * 100) if rz_5d_ago else 0

    rq_latest = _safe_float(latest.get("RQYE"))  # 融券余额

    signal = "融资净流入" if rz_change_pct > 2 else "融资净流出" if rz_change_pct < -2 else "持平"
    return {
        "rz_balance": round(rz_latest / 1e4, 1),  # 万元
        "rz_5d_change_pct": round(rz_change_pct, 1),
        "rq_balance": round(rq_latest / 1e4, 1),
        "signal": signal,
    }

def _get_block_trades(code: str, page_size: int = 20) -> Dict[str, Any]:
    """大宗交易摘要。提取近期成交笔数、平均溢价率、机构买卖方向。"""
    from app.agent.tools.finance.em_utils import em_datacenter
    data = em_datacenter(
        "RPT_DATA_BLOCKTRADE",
        filter_str=f'(SECURITY_CODE="{code}")',
        page_size=page_size,
        sort_columns="TRADE_DATE", sort_types="-1",
    )
    if not data:
        return {"signal": "无大宗交易记录"}

    premiums = []
    inst_buy = 0
    inst_sell = 0
    for row in data:
        close = _safe_float(row.get("CLOSE_PRICE"))
        deal_price = _safe_float(row.get("DEAL_PRICE"))
        if close:
            premiums.append((deal_price / close - 1) * 100)
        buyer = str(row.get("BUYER", ""))
        seller = str(row.get("SELLER", ""))
        if "机构" in buyer:
            inst_buy += 1
        if "机构" in seller:
            inst_sell += 1

    avg_premium = sum(premiums) / len(premiums) if premiums else 0
    signal = "溢价成交（正面）" if avg_premium > 5 else "折价成交（负面）" if avg_premium < -5 else "中性"
    return {
        "recent_count": len(data),
        "avg_premium_pct": round(avg_premium, 1),
        "inst_buy": inst_buy,
        "inst_sell": inst_sell,
        "signal": signal,
    }

def _get_holder_count(code: str) -> Dict[str, Any]:
    """股东户数摘要。提取最新户数、环比变化趋势。"""
    from app.agent.tools.finance.em_utils import em_datacenter
    data = em_datacenter(
        "RPT_HOLDERNUMLATEST",
        filter_str=f'(SECURITY_CODE="{code}")',
        page_size=10,
        sort_columns="END_DATE", sort_types="-1",
    )
    if not data:
        return {"signal": "无数据"}

    latest = data[0]
    latest_count = _safe_float(latest.get("HOLDER_NUM"))
    prev_count = _safe_float(data[1].get("HOLDER_NUM")) if len(data) > 1 else latest_count
    change_pct = ((latest_count / prev_count - 1) * 100) if prev_count else 0
    avg_amount = _safe_float(latest.get("AVG_AMOUNT"))  # 户均持股

    signal = "筹码集中" if change_pct < -5 else "筹码分散" if change_pct > 5 else "持平"
    return {
        "latest_date": str(latest.get("END_DATE", ""))[:10],
        "holder_count": int(latest_count),
        "change_pct": round(change_pct, 1),
        "avg_amount": round(avg_amount, 0),
        "signal": signal,
    }

def _get_dividend_history(code: str) -> Dict[str, Any]:
    """分红送转摘要。提取累计分红次数、连续分红年数、近期派息水平。"""
    from app.agent.tools.finance.em_utils import em_datacenter
    data = em_datacenter(
        "RPT_SHAREBONUS_DET",
        filter_str=f'(SECURITY_CODE="{code}")',
        page_size=20,
        sort_columns="EX_DIVIDEND_DATE", sort_types="-1",
    )
    if not data:
        return {"signal": "无分红记录"}

    years = set()
    total_bonus = 0.0
    for row in data:
        date_str = str(row.get("EX_DIVIDEND_DATE", ""))[:4]
        if date_str:
            years.add(date_str)
        total_bonus += _safe_float(row.get("PRETAX_BONUS_RMB"))

    return {
        "record_count": len(data),
        "dividend_years": len(years),
        "total_bonus_rmb": round(total_bonus, 3),
        "signal": "持续分红" if len(years) >= 3 else "偶有分红" if years else "无分红",
    }

def _get_financial_statements(code: str) -> Dict[str, Any]:
    """财报关键指标摘要。只提取 Agent 关心的几个数，不返回原始表格。"""
    import requests
    _UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    prefix = _market_prefix(code)
    symbol = f"{prefix}{code}"

    summary: Dict[str, Any] = {}

    try:
        url = f"https://quotes.sina.cn/cn/go.php/vFD_FinancialGuideLine/stockid/{symbol}/ctrl/zcfzb/displaytype/4.phtml"
        headers = {"User-Agent": _UA, "Referer": "https://finance.sina.com.cn/"}
        r = requests.get(url, headers=headers, timeout=15)
        r.encoding = "gbk"

        import pandas as pd
        from io import StringIO
        dfs = pd.read_html(StringIO(r.text))
        if dfs:
            df = dfs[0]
            cols = df.columns.tolist()
            latest_col = cols[1] if len(cols) > 1 else None
            if latest_col:
                for _, row in df.iterrows():
                    item_name = str(row.iloc[0]) if len(row) > 0 else ""
                    val = row[latest_col] if pd.notna(row[latest_col]) else None
                    if val is None:
                        continue
                    # 只保留关键指标
                    if "总资产" in item_name and "负债" not in item_name:
                        summary["total_assets"] = val
                    elif "总负债" in item_name:
                        summary["total_liabilities"] = val
                    elif "股东权益合计" in item_name or "归属.*股东.*权益" in item_name:
                        summary["equity"] = val
                    elif "货币资金" in item_name:
                        summary["cash"] = val
    except Exception as e:
        logger.warning("_get_financial_statements(%s) 资产负债表失败: %s", code, e)

    # 利润表：从财务指标接口取关键增速
    try:
        url2 = f"https://quotes.sina.cn/cn/go.php/vFD_FinancialGuideLine/stockid/{symbol}/ctrl/lrb/displaytype/4.phtml"
        headers = {"User-Agent": _UA, "Referer": "https://finance.sina.com.cn/"}
        r2 = requests.get(url2, headers=headers, timeout=15)
        r2.encoding = "gbk"
        dfs2 = pd.read_html(StringIO(r2.text))
        if dfs2:
            df2 = dfs2[0]
            cols2 = df2.columns.tolist()
            latest_col2 = cols2[1] if len(cols2) > 1 else None
            if latest_col2:
                for _, row in df2.iterrows():
                    item_name = str(row.iloc[0]) if len(row) > 0 else ""
                    val = row[latest_col2] if pd.notna(row[latest_col2]) else None
                    if val is None:
                        continue
                    if "营业总收入" in item_name or "营业收入" in item_name:
                        summary["revenue"] = val
                    elif "净利润" in item_name and "扣非" not in item_name:
                        summary["net_profit"] = val
    except Exception as e:
        logger.warning("_get_financial_statements(%s) 利润表失败: %s", code, e)

    return summary

def get_capital_summary(codes: str) -> dict:
    """基本面摘要：返回营收/利润增速、ROE、PE/PB估值、机构持仓变化等中长线指标。

    一次调用聚合融资融券、大宗交易、股东户数、分红送转、财报三表五大维度数据，
    并生成结构化摘要供中长线持仓决策参考。

    Args:
        codes: 多股用逗号分隔
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str) -> dict:
        code = _strip_prefix(stock_code)

        # ── 并行采集五维数据（单源超时不阻断整体）────────────────────
        def _safe(fn, label):
            try:
                return fn()
            except Exception as e:
                logger.warning("[Capital] %s 超时/失败: %s", label, e)
                return {}

        margin = _safe(lambda: _get_margin_trading(code, days=60), "margin")
        block = _safe(lambda: _get_block_trades(code, page_size=20), "block")
        holders = _safe(lambda: _get_holder_count(code), "holders")
        dividend = _safe(lambda: _get_dividend_history(code), "dividend")
        financials = _safe(lambda: _get_financial_statements(code), "financials")

        # ── 直接组装摘要（子函数已返回浓缩指标，无需二次计算）──
        summary: Dict[str, Any] = {"stock_code": code}

        summary["margin"] = margin
        summary["block_trade"] = block
        summary["holders"] = holders
        summary["dividend"] = dividend
        summary["financials"] = financials

        # ── 综合信号 ─────────────────────────────────────────────
        signals = [
            margin.get("signal", ""),
            block.get("signal", ""),
            holders.get("signal", ""),
            dividend.get("signal", ""),
        ]
        positive = sum(1 for s in signals if "正面" in s or "看多" in s or "集中" in s or "持续" in s or "流入" in s)
        negative = sum(1 for s in signals if "负面" in s or "撤退" in s or "分散" in s or "流出" in s)

        if positive > negative:
            summary["overall_signal"] = "中长线偏多"
        elif negative > positive:
            summary["overall_signal"] = "中长线偏空"
        else:
            summary["overall_signal"] = "中性"

        return {"summary": summary}

    return _batch_execute(_one, code_list)

