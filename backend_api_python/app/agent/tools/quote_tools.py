# -*- coding: utf-8 -*-
"""
估值行情工具 — 五档盘口、估值指标、指数/ETF行情、批量估值对比。

数据来源：market_cn.tape（五档盘口）/ 腾讯财经 HTTP API（估值/指数/批量）
"""
from __future__ import annotations
from app.agent.utils.md_format import _to_md
def _strip_prefix(s):
    from app.data_sources.normalizer import strip_market_prefix
    return strip_market_prefix(s)

from app.agent.log import logger
from typing import Any, Dict, List
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# ══════════════════════════════════════════════════════════════
# 腾讯财经批量行情（内部共用）
# ══════════════════════════════════════════════════════════════

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
# ══════════════════════════════════════════════════════════════
# 五档盘口
# ══════════════════════════════════════════════════════════════

def get_order_book(codes: str, output: str = "markdown") -> str:
    """五档盘口：返回买卖各5档价格和挂单量、涨跌幅、换手率、PE、市值等。

    Args:
        codes: 多股用逗号分隔"
        output: "markdown"(默认) | "json"
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()][:20]
    if not code_list:
        return {"error": "codes 不能为空", "retriable": False}

    def _one(stock_code: str, output: str = "markdown") -> str:
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

    if len(code_list) == 1:
        return _one(code_list[0])

    results = {}
    for code in code_list:
        try:
            results[code] = _one(code)
        except Exception as e:
            results[code] = {"error": str(e)}
    return {"count": len(results), "data": results}
# ══════════════════════════════════════════════════════════════
# 指数/ETF行情
# ══════════════════════════════════════════════════════════════

def get_index_etf_quote(codes: str, output: str = "markdown") -> str:
    """指数/ETF行情：返回价格、涨跌幅、成交量，支持上证/深证/创业板/沪深300及对应ETF。

    Args:
        codes: 逗号分隔的代码，如 "000001,000300,399006,510050"
               指数：000001(上证) 399001(深证) 000300(沪深300) 399006(创业板)
               ETF：510050(上证50) 510300(沪深300) 159919(沪深300) 512880(证券)
        output: "markdown"(默认) | "json"
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
        from app.agent.utils.md_format import _to_md
        return json.dumps(_r, ensure_ascii=False) if output == "json" else _to_md(_r)
    except Exception as e:
        logger.warning("get_index_etf_quote(%s) failed: %s", codes, e)
        return {"error": str(e)}
# ══════════════════════════════════════════════════════════════
# 批量估值对比
# ══════════════════════════════════════════════════════════════

def batch_valuation_compare(stock_codes: str, output: str = "markdown") -> str:
    """估值对比：返回多只股票的PE/PB/市值/营收并排对比表。

    Args:
        stock_codes: 逗号分隔的股票代码，如 "600519,000858,688017"
        output: "markdown"(默认) | "json"
    """
    code_list = [_strip_prefix(c.strip()) for c in stock_codes.split(",") if c.strip()]
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
            "total": len(results),
            "stocks": results,
            "stocks": results,
            "pe_sorted": [r["code"] for r in valid],
            "pe_sorted": [r["code"] for r in valid],
        }
        from app.agent.utils.md_format import _to_md
        return json.dumps(_r, ensure_ascii=False) if output == "json" else _to_md(_r)
    except Exception as e:
        logger.warning("batch_valuation_compare(%s) failed: %s", stock_codes, e)
        return {"error": str(e)}
