# -*- coding: utf-8 -*-
"""
估值行情工具 — 五档盘口、估值指标、指数/ETF行情、批量估值对比。

数据来源：market_cn.tape（五档盘口）/ 腾讯财经 HTTP API（估值/指数/批量）
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from app.agent.tools.registry import tool

logger = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def _stock_code_normalize(code: str) -> str:
    code = code.strip().upper()
    for prefix in ("SH", "SZ", "BJ"):
        if code.startswith(prefix):
            code = code[len(prefix):]
    if "." in code:
        code = code.split(".")[0]
    return code


# ══════════════════════════════════════════════════════════════
# 腾讯财经批量行情（内部共用）
# ══════════════════════════════════════════════════════════════

def _tencent_quote_raw(codes: list) -> dict:
    """腾讯财经批量行情原始接口。返回 {code: {全部字段dict}}。"""
    import urllib.request

    prefixed = []
    for c in codes:
        c = _stock_code_normalize(c)
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


# ══════════════════════════════════════════════════════════════
# 五档盘口
# ══════════════════════════════════════════════════════════════

@tool(
    description="[短线] 五档盘口。买一~买五/卖一~卖五价格+挂单量+实时行情。短线盘口语言：买盘挂单大=支撑强，卖盘挂单大=压力大。大单托底可能是诱多，大单压顶可能是洗盘。",
    category="行情数据",
    layer="数据层",
    domain=["finance"],
)
def get_order_book(stock_code: str) -> Dict[str, Any]:
    """获取五档盘口+实时行情。

    Args:
        stock_code: 股票代码（如 600519）
    """
    code = _stock_code_normalize(stock_code)
    try:
        from app.market_cn.tape import get_order_book as _get_order_book
        result = _get_order_book(code)
        if "error" in result:
            return {"stock_code": code, "error": result["error"]}
        return result
    except Exception as e:
        logger.warning("get_order_book(%s) failed: %s", code, e)
        return {"stock_code": code, "error": str(e)}


# ══════════════════════════════════════════════════════════════
# 估值指标
# ══════════════════════════════════════════════════════════════

@tool(
    description="[中线核心] 估值指标（优选数据源：走腾讯财经，PE(TTM)/PB/市值/涨跌停价比 get_stock_info 更准更快，不封IP）。PE(TTM)/PE(静)/PB/总市值/流通市值/换手率/涨跌停价/量比。中线选股核心：PE<行业均值=低估，PB<1=破净，量比>2=异动。配合一致预期算前向PE。",
    category="行情数据",
    layer="数据层",
    domain=["finance"],
)
def get_valuation_metrics(stock_code: str) -> Dict[str, Any]:
    """获取估值指标（腾讯财经）。

    Args:
        stock_code: 股票代码（如 600519）
    """
    code = _stock_code_normalize(stock_code)
    try:
        data = _tencent_quote_raw([code])
        q = data.get(code)
        if not q:
            return {"stock_code": code, "error": "未获取到数据"}
        return {
            "stock_code": code,
            "name": q["name"],
            "price": q["price"],
            "change_pct": q["change_pct"],
            "pe_ttm": q["pe_ttm"],
            "pe_static": q["pe_static"],
            "pb": q["pb"],
            "mcap_yi": q["mcap_yi"],
            "float_mcap_yi": q["float_mcap_yi"],
            "turnover_pct": q["turnover_pct"],
            "amplitude_pct": q["amplitude_pct"],
            "limit_up": q["limit_up"],
            "limit_down": q["limit_down"],
            "vol_ratio": q["vol_ratio"],
            "amount_wan": q["amount_wan"],
        }
    except Exception as e:
        logger.warning("get_valuation_metrics(%s) failed: %s", code, e)
        return {"stock_code": code, "error": str(e)}


# ══════════════════════════════════════════════════════════════
# 指数/ETF行情
# ══════════════════════════════════════════════════════════════

@tool(
    description="[短线+中线] 指数/ETF行情（扩展 get_market_indices：现有工具仅覆盖3大指数，本工具支持任意指数+ETF代码，如510050/510300/159919等）。上证/深证/沪深300/创业板指+主流ETF实时行情。看大盘方向用：指数涨跌判断市场情绪，ETF跟踪行业/宽基趋势。短线看情绪，中线看趋势。",
    category="行情数据",
    layer="数据层",
    domain=["finance"],
)
def get_index_etf_quote(codes: str) -> Dict[str, Any]:
    """获取指数/ETF实时行情（腾讯财经）。

    Args:
        codes: 逗号分隔的代码，如 "000001,000300,399006,510050"
               指数：000001(上证) 399001(深证) 000300(沪深300) 399006(创业板)
               ETF：510050(上证50) 510300(沪深300) 159919(沪深300) 512880(证券)
    """
    code_list = [_stock_code_normalize(c.strip()) for c in codes.split(",") if c.strip()]
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
        return {"total": len(results), "quotes": results}
    except Exception as e:
        logger.warning("get_index_etf_quote(%s) failed: %s", codes, e)
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════
# 批量估值对比
# ══════════════════════════════════════════════════════════════

@tool(
    description="[中线] 批量估值对比。多只股票PE/PB/市值/涨跌幅横向排列。同行业选股用：PE最低+PB最低=潜在低估标的。最多20只同时对比。",
    category="行情数据",
    layer="数据层",
    domain=["finance"],
)
def batch_valuation_compare(stock_codes: str) -> Dict[str, Any]:
    """批量估值对比（腾讯财经）。

    Args:
        stock_codes: 逗号分隔的股票代码，如 "600519,000858,688017"
    """
    code_list = [_stock_code_normalize(c.strip()) for c in stock_codes.split(",") if c.strip()]
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

        return {
            "total": len(results),
            "stocks": results,
            "pe_sorted": [r["code"] for r in valid],
        }
    except Exception as e:
        logger.warning("batch_valuation_compare(%s) failed: %s", stock_codes, e)
        return {"error": str(e)}
