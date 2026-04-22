"""Commodity price data fetchers — 多源降级版.

数据源优先级:
    新浪 → 东财 → TwelveData → akshare(倒二) → yfinance(垫底)
"""
from __future__ import annotations

import math
import requests
from typing import Any, Dict, List, Optional

from app.utils.logger import get_logger
from app.data_providers import safe_float

logger = get_logger(__name__)

# ============================================================================
# 商品定义 — 各源代码
# ============================================================================

COMMODITIES = [
    {
        "symbol": "GC=F", "name_cn": "黄金", "name_en": "Gold", "unit": "USD/oz",
        "sina": "hf_GC", "eastmoney": "101.GC", "td": "GC",
        "tiingo": "xauusd",
    },
    {
        "symbol": "SI=F", "name_cn": "白银", "name_en": "Silver", "unit": "USD/oz",
        "sina": "hf_SI", "eastmoney": "101.SI", "td": "SI",
        "tiingo": "xagusd",
    },
    {
        "symbol": "CL=F", "name_cn": "原油 WTI", "name_en": "Crude Oil WTI", "unit": "USD/bbl",
        "sina": "hf_CL", "eastmoney": "101.CL", "td": "CL",
        "tiingo": None,
    },
    {
        "symbol": "BZ=F", "name_cn": "原油 Brent", "name_en": "Brent Oil", "unit": "USD/bbl",
        "sina": "hf_BOIL", "eastmoney": None, "td": "BZ",
        "tiingo": None,
    },
    {
        "symbol": "HG=F", "name_cn": "铜", "name_en": "Copper", "unit": "USD/lb",
        "sina": "hf_HG", "eastmoney": None, "td": "HG",
        "tiingo": None,
    },
    {
        "symbol": "NG=F", "name_cn": "天然气", "name_en": "Natural Gas", "unit": "USD/MMBtu",
        "sina": "hf_NG", "eastmoney": "101.NG", "td": "NG",
        "tiingo": None,
    },
    {
        "symbol": "PL=F", "name_cn": "铂金", "name_en": "Platinum", "unit": "USD/oz",
        "sina": "hf_PL", "eastmoney": None, "td": "PL",
        "tiingo": None,
    },
    {
        "symbol": "PA=F", "name_cn": "钯金", "name_en": "Palladium", "unit": "USD/oz",
        "sina": "hf_PA", "eastmoney": None, "td": "PA",
        "tiingo": None,
    },
]

# ============================================================================
# 超时配置
# ============================================================================

_FIRST_TIMEOUT = 5
_FALLBACK_TIMEOUT = 1.5

# ============================================================================
# 可选模块懒加载
# ============================================================================

import threading as _threading

_ak = None
_yf = None
_ak_loaded = False
_yf_loaded = False
_load_lock = _threading.Lock()


def _get_ak():
    global _ak, _ak_loaded
    if not _ak_loaded:
        with _load_lock:
            if not _ak_loaded:
                _ak_loaded = True
                try:
                    import akshare as ak_mod
                    _ak = ak_mod
                    logger.info("akshare loaded successfully")
                except ImportError:
                    _ak = None
                    logger.warning("akshare not installed")
    return _ak


def _get_yf():
    global _yf, _yf_loaded
    if not _yf_loaded:
        with _load_lock:
            if not _yf_loaded:
                _yf_loaded = True
                try:
                    import yfinance as yf_mod
                    _yf = yf_mod
                    logger.info("yfinance loaded successfully")
                except ImportError:
                    _yf = None
                    logger.warning("yfinance not installed")
    return _yf


# ============================================================================
# 工具函数
# ============================================================================

def _safe_float2(text, default=0.0):
    try:
        val = float(str(text).strip())
        return val if val == val else default
    except (ValueError, TypeError, AttributeError):
        return default


def _make_result(c: dict, price: float, change: float) -> dict:
    return {
        "symbol": c["symbol"],
        "name_cn": c["name_cn"],
        "name_en": c["name_en"],
        "price": round(price, 2),
        "change": round(change, 2),
        "unit": c["unit"],
        "category": "commodity",
    }


def _make_default(c: dict) -> dict:
    return _make_result(c, 0, 0)


# ============================================================================
# 新浪源 (批量)
# ============================================================================

def _fetch_sina_batch(commodities: list, timeout: float = 5) -> Dict[str, dict]:
    """批量拉取新浪期货数据。"""
    sina_codes = []
    sina_map = {}
    for c in commodities:
        code = c.get("sina")
        if code:
            sina_codes.append(code)
            sina_map[code] = c

    if not sina_codes:
        return {}

    url = "http://hq.sinajs.cn/list=" + ",".join(sina_codes)
    try:
        r = requests.get(url, timeout=timeout, headers={"Referer": "https://finance.sina.com.cn"})
        r.raise_for_status()
    except Exception as e:
        logger.warning("Sina commodities batch failed: %s", e)
        return {}

    results = {}
    for line in r.text.strip().split("\n"):
        line = line.strip()
        if not line or "=" not in line:
            continue
        try:
            var_part, val_part = line.split("=", 1)
            # var hq_str_hf_GC="黄金,2024,..."
            val_part = val_part.strip().rstrip(";").strip('"')
            if not val_part:
                continue
            parts = val_part.split(",")
            # 新浪期货格式: name, open, prev_close, current, high, low, ...
            if len(parts) < 4:
                continue
            current = _safe_float2(parts[3]) if len(parts) > 3 else 0
            prev_close = _safe_float2(parts[2]) if len(parts) > 2 else 0
            if current <= 0:
                continue
            change = ((current - prev_close) / prev_close * 100) if prev_close else 0

            # 匹配到哪个 commodity
            for code, c in sina_map.items():
                if code in var_part:
                    results[c["symbol"]] = _make_result(c, current, change)
                    break
        except Exception as e:
            logger.debug("Sina commodity parse failed: %s", e)
            continue

    if results:
        logger.info("Fetched %d commodities via Sina", len(results))
    return results


# ============================================================================
# 东财源
# ============================================================================

def _fetch_eastmoney_single(c: dict, timeout: float = 5) -> Optional[dict]:
    secid = c.get("eastmoney")
    if not secid:
        return None
    try:
        r = requests.get(
            "https://push2.eastmoney.com/api/qt/stock/get",
            params={"secid": secid, "fields": "f43,f44,f45,f46,f170,f171,f169"},
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json().get("data")
        if not data:
            return None
        raw_val = data.get("f43")
        if raw_val is None or raw_val == "":
            return None
        val = _safe_float2(str(raw_val))
        change = _safe_float2(str(data.get("f170", 0)))
        if val > 100000:
            val = val / 100
        if val <= 0:
            return None
        return _make_result(c, val, change)
    except Exception as e:
        logger.debug("Eastmoney commodity %s failed: %s", c["symbol"], e)
        return None


# ============================================================================
# TwelveData 源
# ============================================================================

def _fetch_td_single(c: dict, timeout: float = 5) -> Optional[dict]:
    try:
        from app.config import APIKeys
        api_key = (APIKeys.TWELVE_DATA_API_KEY or "").strip()
        if not api_key:
            return None
        td_sym = c.get("td")
        if not td_sym:
            return None
        resp = requests.get("https://api.twelvedata.com/quote", params={
            "symbol": td_sym, "apikey": api_key,
        }, timeout=timeout)
        data = resp.json()
        if data.get("status") == "error" or not data.get("close"):
            return None
        current = float(data.get("close") or 0)
        prev = float(data.get("previous_close") or 0)
        change = ((current - prev) / prev * 100) if prev else 0
        if current <= 0:
            return None
        return _make_result(c, current, change)
    except Exception as e:
        logger.debug("TwelveData commodity %s failed: %s", c["symbol"], e)
        return None


# ============================================================================
# akshare 源 (倒二)
# ============================================================================

def _fetch_akshare_single(c: dict, timeout: float = 5) -> Optional[dict]:
    ak = _get_ak()
    if ak is None:
        return None

    # akshare 期货品种映射
    ak_map = {
        "GC=F": "沪金主力",   # 或用国际金价接口
        "SI=F": "沪银主力",
        "CL=F": "SC原油",     # 上海原油
        "NG=F": "天然气",
    }
    name = ak_map.get(c["symbol"])
    if not name:
        return None
    try:
        # 尝试用 futures_foreign_hist
        sym_map = {"GC=F": "GC", "SI=F": "SI", "CL=F": "CL", "NG=F": "NG"}
        sym = sym_map.get(c["symbol"])
        if sym:
            df = ak.futures_foreign_hist(symbol=sym)
            if df is not None and not df.empty:
                last = df.iloc[-1]
                price = _safe_float2(str(last.get("close", 0)))
                prev = _safe_float2(str(df.iloc[-2].get("close", 0))) if len(df) >= 2 else 0
                change = ((price - prev) / prev * 100) if prev else 0
                if price > 0:
                    return _make_result(c, price, change)
    except Exception as e:
        logger.debug("akshare commodity %s failed: %s", c["symbol"], e)
    return None


# ============================================================================
# yfinance 源 (垫底)
# ============================================================================

def _fetch_yf_single(c: dict, timeout: float = 5) -> Optional[dict]:
    yf = _get_yf()
    if yf is None:
        return None
    try:
        ticker = yf.Ticker(c["symbol"])
        hist = ticker.history(period="2d")
        if len(hist) >= 2:
            prev_close = float(hist["Close"].iloc[-2])
            current = float(hist["Close"].iloc[-1])
            change = ((current - prev_close) / prev_close * 100) if prev_close else 0
        elif len(hist) == 1:
            current = float(hist["Close"].iloc[-1])
            change = 0
        else:
            return None
        if current <= 0:
            return None
        return _make_result(c, current, change)
    except Exception as e:
        logger.debug("yfinance commodity %s failed: %s", c["symbol"], e)
    return None


# ============================================================================
# Tiingo 源 (仅贵金属)
# ============================================================================

def _fetch_tiingo_single(c: dict, timeout: float = 5) -> Optional[dict]:
    from datetime import datetime, timedelta
    try:
        from app.config import TiingoConfig, APIKeys
        api_key = APIKeys.TIINGO_API_KEY
        if not api_key:
            return None
        tiingo_sym = c.get("tiingo")
        if not tiingo_sym:
            return None
        resp = requests.get(f"{TiingoConfig.BASE_URL}/fx/{tiingo_sym}/prices", params={
            "startDate": (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d"),
            "endDate": datetime.now().strftime("%Y-%m-%d"),
            "resampleFreq": "1day",
            "token": api_key,
        }, timeout=TiingoConfig.TIMEOUT)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data or len(data) < 1:
            return None
        current = float(data[-1].get("close", 0) or 0)
        prev = float(data[-2].get("close", 0) or 0) if len(data) >= 2 else 0
        change = ((current - prev) / prev * 100) if prev else 0
        if current <= 0:
            return None
        return _make_result(c, current, change)
    except Exception as e:
        logger.debug("Tiingo commodity %s failed: %s", c["symbol"], e)
        return None


# ============================================================================
# 统一入口
# ============================================================================

# (name, batch_fn_or_none, single_fn, is_batch)
_SOURCES = [
    ("sina",       _fetch_sina_batch,      None,                   True),
    ("eastmoney",  None,                   _fetch_eastmoney_single, False),
    ("twelvedata", None,                   _fetch_td_single,        False),
    ("akshare",    None,                   _fetch_akshare_single,   False),
    ("yfinance",   None,                   _fetch_yf_single,        False),
    ("tiingo",     None,                   _fetch_tiingo_single,    False),
]


def fetch_commodities() -> List[Dict[str, Any]]:
    """Fetch commodity prices. 新浪 → 东财 → TwelveData → akshare → yfinance → Tiingo."""
    result: Dict[str, dict] = {}
    pending = list(COMMODITIES)

    for src_name, batch_fn, single_fn, is_batch in _SOURCES:
        if not pending:
            break

        if is_batch and batch_fn:
            try:
                batch_result = batch_fn(pending)
                for sym, data in batch_result.items():
                    if sym not in result:
                        result[sym] = data
                fetched = set(batch_result.keys())
                pending = [c for c in pending if c["symbol"] not in fetched]
            except Exception as e:
                logger.warning("Batch source %s failed: %s", src_name, e)
        elif single_fn:
            still_pending = []
            for c in pending:
                if c["symbol"] in result:
                    continue
                try:
                    data = single_fn(c)
                    if data and data["price"] > 0:
                        result[c["symbol"]] = data
                        logger.info("Commodity %s: %.2f from %s", c["symbol"], data["price"], src_name)
                    else:
                        still_pending.append(c)
                except Exception as e:
                    logger.debug("Source %s / %s failed: %s", src_name, c["symbol"], e)
                    still_pending.append(c)
            pending = still_pending

    # 半数以上拿到就提前结束（兼容原逻辑）
    output = []
    for c in COMMODITIES:
        if c["symbol"] in result:
            output.append(result[c["symbol"]])
        else:
            logger.warning("Commodity %s: all sources failed, returning default", c["symbol"])
            output.append(_make_default(c))

    return output
