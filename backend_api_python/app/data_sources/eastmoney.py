"""
东方财富直接数据源 — A股多周期K线 + 实时行情（免费、无需API Key）

数据接口:
- K线: push2his.eastmoney.com/api/qt/stock/kline/get
- 实时行情: push2.eastmoney.com/api/qt/stock/get

特点:
- 国内最稳定的免费数据源之一
- 支持多周期: 1m/5m/15m/30m/1H/4H/1D/1W
- 支持前复权/后复权
- 有频率限制，需要限流 + 随机 UA
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from app.data_sources.rate_limiter import (
    get_request_headers,
    retry_with_backoff,
    get_eastmoney_limiter,
)
from app.data_sources.circuit_breaker import get_realtime_circuit_breaker
from app.data_sources.normalizer import safe_float, safe_int
from app.utils.logger import get_logger

logger = get_logger(__name__)

# ---------- 周期映射 ----------

# 东方财富 K线周期代码 (klt 参数)
_EASTMONEY_KLT = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1H": 60,
    "4H": 240,
    "1D": 101,
    "1W": 102,
}

# 复权方式 (fqt 参数): 0=不复权, 1=前复权, 2=后复权
_EASTMONEY_FQT = {
    "": 0,
    "qfq": 1,
    "hfq": 2,
}


# ---------- 代码转换 ----------

def _em_secid_from_cn(symbol: str) -> str:
    """
    Convert A-share symbol to East Money secid format: 1.600519 (SH) / 0.000001 (SZ).
    Format: <market>.<code>  where 1=上海(含科创板) 0=深圳(含创业板/北交所)
    """
    s = (symbol or "").strip().upper()

    # 剥离前缀: SH600519 / SZ000001 / BJ920748
    if s.startswith(("SH", "SZ", "BJ")) and len(s) >= 3:
        s = s[2:]

    # 剥离后缀: 600519.SH / 000001.SZ / 920748.BJ
    if s.endswith((".SH", ".SS", ".SZ", ".BJ")):
        s = s[:s.rfind(".")]

    if not s.isdigit() or len(s) != 6:
        return ""

    # 判断交易所: 沪市 6/5/9/11 开头, 深市其余
    if s[0] in ("6", "5", "9") or s[:2] == "11":
        return f"1.{s}"
    return f"0.{s}"


# ---------- K线数据 ----------

@retry_with_backoff(max_attempts=1, exceptions=(
    requests.exceptions.RequestException,
    ConnectionError,
    TimeoutError,
))
def fetch_eastmoney_kline(
    code: str,
    period: str = "1D",
    count: int = 300,
    adj: str = "qfq",
    timeout: int = 10,
) -> List[Dict[str, Any]]:
    """
    从东方财富获取K线数据。

    返回字段顺序 (f51~f61):
      f51=日期, f52=开盘, f53=收盘, f54=最高, f55=最低,
      f56=成交量, f57=成交额, f58=振幅, f59=涨跌幅, f60=涨跌额, f61=昨收
    """
    secid = _em_secid_from_cn(code)
    if not secid:
        return []

    klt = _EASTMONEY_KLT.get(period)
    if klt is None:
        logger.warning("EastMoney kline: unsupported period '%s'", period)
        return []

    fqt = _EASTMONEY_FQT.get(adj, 1)

    limiter = get_eastmoney_limiter()
    limiter.wait()

    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": secid,
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": klt,
        "fqt": fqt,
        "end": "20500101",
        "lmt": min(int(count), 5000),
    }

    resp = requests.get(
        url,
        headers=get_request_headers(referer="https://quote.eastmoney.com/"),
        params=params,
        timeout=timeout,
    )

    try:
        data = resp.json()
    except Exception:
        logger.warning("EastMoney kline: invalid JSON for %s", code)
        return []

    if not isinstance(data, dict) or int(data.get("code", -1)) != 0:
        logger.warning("EastMoney kline: API error for %s: %s", code, data.get("msg", ""))
        return []

    klines_data = (data.get("data") or {}).get("klines")
    if not isinstance(klines_data, list):
        return []

    out: List[Dict[str, Any]] = []
    for line in klines_data:
        parts = line.split(",")
        if len(parts) < 7:
            continue
        try:
            # f51=日期, f52=开盘, f53=收盘, f54=最高, f55=最低, f56=成交量
            dt_str = parts[0].strip()
            ts = int(datetime.strptime(dt_str, "%Y-%m-%d").timestamp())
            o = float(parts[1])
            c = float(parts[2])
            h = float(parts[3])
            low = float(parts[4])
            v = float(parts[5])

            # 数据校验: OHLC 不能全为 0, 且 high >= low
            if o == 0 and c == 0:
                continue
            if h > 0 and low > 0 and h < low:
                h, low = low, h  # 修正异常

            out.append({
                "time": ts,
                "open": round(o, 4),
                "high": round(h, 4),
                "low": round(low, 4),
                "close": round(c, 4),
                "volume": round(v, 2),
            })
        except (ValueError, TypeError, IndexError):
            continue

    out.sort(key=lambda x: x["time"])
    if len(out) > count:
        out = out[-count:]
    logger.debug("EastMoney kline: %d bars for %s (klt=%s)", len(out), code, klt)
    return out


# ---------- 实时行情（单股）----------

@retry_with_backoff(max_attempts=1, exceptions=(
    requests.exceptions.RequestException,
    ConnectionError,
    TimeoutError,
))
def fetch_eastmoney_quote(code: str, timeout: int = 8) -> Optional[Dict[str, Any]]:
    """
    从东方财富获取单股实时行情。

    Fields: f43=最新价, f44=最高, f45=最低, f46=开盘,
            f47=成交量, f48=成交额, f57=代码, f58=名称, f60=昨收,
            f170=涨跌幅, f171=振幅
    """
    secid = _em_secid_from_cn(code)
    if not secid:
        return None

    circuit = get_realtime_circuit_breaker()
    if not circuit.is_available("eastmoney_quote"):
        return None

    limiter = get_eastmoney_limiter()
    limiter.wait()

    url = "https://push2.eastmoney.com/api/qt/stock/get"
    params = {
        "secid": secid,
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
        "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f60,f170,f171",
    }

    resp = requests.get(
        url,
        headers=get_request_headers(referer="https://quote.eastmoney.com/"),
        params=params,
        timeout=timeout,
    )

    try:
        data = resp.json()
    except Exception:
        circuit.record_failure("eastmoney_quote", "invalid JSON")
        return None

    if not isinstance(data, dict) or int(data.get("code", -1)) != 0:
        circuit.record_failure("eastmoney_quote", str(data.get("msg", "unknown")))
        return None

    d = data.get("data")
    if not isinstance(d, dict):
        circuit.record_failure("eastmoney_quote", "no data")
        return None

    def _f(key: str, default: float = 0.0) -> float:
        v = d.get(key)
        if v is None or v == "-" or v == "":
            return default
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    last = _f("f43")
    prev = _f("f60")

    # 停牌/无交易: 价格可能为 "-" → 0, 但昨收有值
    if last == 0 and prev == 0:
        circuit.record_failure("eastmoney_quote", "zero prices")
        return None

    change = round(last - prev, 4) if prev else 0.0
    change_pct = round(change / prev * 100, 2) if prev else 0.0

    quote = {
        "symbol": secid,
        "name": str(d.get("f58", "")).strip(),
        "last": last,
        "change": change,
        "changePercent": change_pct,
        "high": _f("f44"),
        "low": _f("f45"),
        "open": _f("f46"),
        "previousClose": prev,
        "volume": _f("f47"),
        "amount": _f("f48"),
    }

    circuit.record_success("eastmoney_quote")
    return quote


# ---------- 便捷接口 ----------

def eastmoney_kline_to_ticker(code: str) -> Optional[Dict[str, Any]]:
    """Fetch East Money quote and return in CNStockDataSource.get_ticker() format."""
    q = fetch_eastmoney_quote(code)
    if not q:
        return None
    return {
        "last": q.get("last", 0),
        "change": q.get("change", 0),
        "changePercent": q.get("changePercent", 0),
        "high": q.get("high", 0),
        "low": q.get("low", 0),
        "open": q.get("open", 0),
        "previousClose": q.get("previousClose", 0),
        "name": q.get("name", ""),
        "symbol": code,
    }


# ---------- 辅助 ----------

def _em_request(report_name: str, params: dict = None, timeout: int = 10) -> list:
    """通用东财 datacenter-web 请求，返回 result.data 列表"""
    limiter = get_eastmoney_limiter()
    limiter.wait()

    default_params = {
        "sortColumns": "TRADE_DATE",
        "sortTypes": "-1",
        "pageSize": 500,
        "pageNumber": 1,
        "reportName": report_name,
        "columns": "ALL",
        "source": "WEB",
        "client": "WEB",
    }
    if params:
        default_params.update(params)

    resp = requests.get(
        "https://datacenter-web.eastmoney.com/api/data/v1/get",
        headers=get_request_headers(referer="https://data.eastmoney.com/"),
        params=default_params,
        timeout=timeout,
    )

    try:
        data = resp.json()
    except Exception:
        return []

    return ((data.get("result") or {}).get("data")) or []


# ---------- 龙虎榜 ----------

def fetch_eastmoney_dragon_tiger(start_date: str, end_date: str) -> List[Dict[str, Any]]:
    """从东财 datacenter 获取龙虎榜明细"""
    items = _em_request(
        "RPT_DAILYBILLBOARD_DETAILSNEW",
        params={"filter": f"(TRADE_DATE>='{start_date}')(TRADE_DATE<='{end_date}')"},
    )
    if not items:
        return []

    result = []
    for item in items:
        try:
            result.append({
                "stock_code": str(item.get("SECURITY_CODE", "")).strip(),
                "stock_name": str(item.get("SECURITY_NAME_ABBR", "")).strip(),
                "trade_date": (str(item.get("TRADE_DATE", ""))[:10]).strip(),
                "reason": str(item.get("EXPLANATION", "") or "").strip()[:100],
                "buy_amount": safe_float(item.get("BUY")),
                "sell_amount": safe_float(item.get("SELL")),
                "net_amount": safe_float(item.get("NET_BUY")),
                "change_percent": safe_float(item.get("CHANGE_RATE")),
                "close_price": safe_float(item.get("CLOSE_PRICE")),
                "turnover_rate": safe_float(item.get("TURNOVERRATE")),
                "amount": safe_float(item.get("ACCUM_AMOUNT")),
                "buy_seat_count": safe_int(item.get("BUYER_NUM") or 0),
                "sell_seat_count": safe_int(item.get("SELLER_NUM") or 0),
            })
        except Exception:
            continue

    logger.info(f"[EastMoney] dragon_tiger {start_date}~{end_date}: {len(result)} records")
    return result


# ---------- 热榜 ----------

def fetch_eastmoney_hot_rank() -> List[Dict[str, Any]]:
    """从东财获取人气榜"""
    items = _em_request(
        "RPT_HOT_STOCK_NEW",
        params={
            "sortColumns": "CHANGE_RATE",
            "sortTypes": "-1",
            "pageSize": 50,
            "filter": "(MARKET_TYPE in (\"沪深A股\"))",
        },
    )
    if not items:
        return []

    result = []
    for i, item in enumerate(items):
        try:
            code = str(item.get("SECURITY_CODE", "")).strip()
            if not code:
                continue
            result.append({
                "rank": i + 1,
                "stock_code": code,
                "stock_name": str(item.get("SECURITY_NAME_ABBR", "")).strip(),
                "price": safe_float(item.get("NEWEST_PRICE", item.get("CLOSE_PRICE"))),
                "change_percent": safe_float(item.get("CHANGE_RATE")),
                "popularity_score": safe_float(item.get("HOT_NUM", item.get("SCORE"))),
                "current_rank_change": str(item.get("RANK_CHANGE", "")),
            })
        except Exception:
            continue

    logger.info(f"[EastMoney] hot_rank: {len(result)} stocks")
    return result


# ---------- 涨停池 ----------

def fetch_eastmoney_zt_pool(trade_date: str) -> List[Dict[str, Any]]:
    """从东财获取涨停池"""
    items = _em_request(
        "RPT_LIMITED_BOARD_POOL",
        params={
            "sortColumns": "TOTAL_MARKET_CAP",
            "sortTypes": "-1",
            "filter": f"(TRADE_DATE='{trade_date}')",
        },
    )
    if not items:
        return []

    result = []
    for item in items:
        try:
            result.append({
                "stock_code": str(item.get("SECURITY_CODE", "")).strip(),
                "stock_name": str(item.get("SECURITY_NAME_ABBR", "")).strip(),
                "trade_date": trade_date,
                "price": safe_float(item.get("CLOSE_PRICE")),
                "change_percent": safe_float(item.get("CHANGE_RATE")),
                "continuous_zt_days": safe_int(item.get("CONTINUOUS_LIMIT_DAYS", item.get("ZT_DAYS", 1)) or 1),
                "zt_time": str(item.get("FIRST_ZDT_TIME", "")),
                "seal_amount": safe_float(item.get("LIMIT_ORDER_AMT")),
                "turnover_rate": safe_float(item.get("TURNOVERRATE")),
                "volume": safe_float(item.get("VOLUME")),
                "amount": safe_float(item.get("TURNOVER")),
                "sector": str(item.get("BOARD_NAME", "")),
                "reason": str(item.get("ZT_REASON", ""))[:80],
                "open_count": safe_int(item.get("OPEN_NUM", 0) or 0),
            })
        except Exception:
            continue

    logger.info(f"[EastMoney] zt_pool {trade_date}: {len(result)} stocks")
    return result


# ---------- 跌停池 ----------

def fetch_eastmoney_dt_pool(trade_date: str) -> List[Dict[str, Any]]:
    """从东财获取跌停池"""
    items = _em_request(
        "RPT_DOWNTREND_LIMIT_POOL",
        params={
            "sortColumns": "TOTAL_MARKET_CAP",
            "sortTypes": "-1",
            "filter": f"(TRADE_DATE='{trade_date}')",
        },
    )
    if not items:
        return []

    result = []
    for item in items:
        try:
            result.append({
                "stock_code": str(item.get("SECURITY_CODE", "")).strip(),
                "stock_name": str(item.get("SECURITY_NAME_ABBR", "")).strip(),
                "trade_date": trade_date,
                "price": safe_float(item.get("CLOSE_PRICE")),
                "change_percent": safe_float(item.get("CHANGE_RATE")),
                "seal_amount": safe_float(item.get("LIMIT_ORDER_AMT")),
                "turnover_rate": safe_float(item.get("TURNOVERRATE")),
                "amount": safe_float(item.get("TURNOVER")),
            })
        except Exception:
            continue

    logger.info(f"[EastMoney] dt_pool {trade_date}: {len(result)} stocks")
    return result


# ---------- 炸板池 ----------

def fetch_eastmoney_broken_board(trade_date: str) -> List[Dict[str, Any]]:
    """从东财获取炸板池"""
    items = _em_request(
        "RPT_LIMITED_BOARD_UNSEALED",
        params={
            "sortColumns": "TOTAL_MARKET_CAP",
            "sortTypes": "-1",
            "filter": f"(TRADE_DATE='{trade_date}')",
        },
    )
    if not items:
        return []

    result = []
    for item in items:
        try:
            result.append({
                "stock_code": str(item.get("SECURITY_CODE", "")).strip(),
                "stock_name": str(item.get("SECURITY_NAME_ABBR", "")).strip(),
                "trade_date": trade_date,
                "price": safe_float(item.get("CLOSE_PRICE")),
                "change_percent": safe_float(item.get("CHANGE_RATE")),
                "zt_time": str(item.get("FIRST_ZDT_TIME", "")),
                "break_time": str(item.get("LAST_ZDT_TIME", "")),
                "turnover_rate": safe_float(item.get("TURNOVERRATE")),
                "amount": safe_float(item.get("TURNOVER")),
            })
        except Exception:
            continue

    logger.info(f"[EastMoney] broken_board {trade_date}: {len(result)} stocks")
    return result
