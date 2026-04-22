"""Forex pair data fetchers — 多源降级版.

数据源优先级:
    新浪 → 东财 → TwelveData → akshare(倒二) → yfinance(垫底)
"""
from __future__ import annotations

import requests
from typing import Any, Dict, List, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)

# ============================================================================
# 外汇对定义 — 各源代码
# ============================================================================

FOREX_PAIRS = [
    {
        "symbol": "EURUSD=X", "td": "EUR/USD", "name_cn": "欧元/美元", "name_en": "EUR/USD",
        "base": "EUR", "quote": "USD",
        "sina": "fx_seurusd", "eastmoney": "119.EURUSD", "tencent": "fxEURUSD",
        "tiingo": "eurusd",
    },
    {
        "symbol": "GBPUSD=X", "td": "GBP/USD", "name_cn": "英镑/美元", "name_en": "GBP/USD",
        "base": "GBP", "quote": "USD",
        "sina": "fx_sgbpusd", "eastmoney": "119.GBPUSD", "tencent": "fxGBPUSD",
        "tiingo": "gbpusd",
    },
    {
        "symbol": "USDJPY=X", "td": "USD/JPY", "name_cn": "美元/日元", "name_en": "USD/JPY",
        "base": "USD", "quote": "JPY",
        "sina": "fx_susdjpy", "eastmoney": "119.USDJPY", "tencent": "fxUSDJPY",
        "tiingo": "usdjpy",
    },
    {
        "symbol": "USDCNH=X", "td": "USD/CNH", "name_cn": "美元/离岸人民币", "name_en": "USD/CNH",
        "base": "USD", "quote": "CNH",
        "sina": "fx_susdcnh", "eastmoney": "119.USDCNH", "tencent": None,
        "tiingo": "usdcnh",
    },
    {
        "symbol": "AUDUSD=X", "td": "AUD/USD", "name_cn": "澳元/美元", "name_en": "AUD/USD",
        "base": "AUD", "quote": "USD",
        "sina": "fx_saudusd", "eastmoney": "119.AUDUSD", "tencent": "fxAUDUSD",
        "tiingo": "audusd",
    },
    {
        "symbol": "USDCAD=X", "td": "USD/CAD", "name_cn": "美元/加元", "name_en": "USD/CAD",
        "base": "USD", "quote": "CAD",
        "sina": "fx_susdcad", "eastmoney": "119.USDCAD", "tencent": "fxUSDCAD",
        "tiingo": "usdcad",
    },
    {
        "symbol": "USDCHF=X", "td": "USD/CHF", "name_cn": "美元/瑞郎", "name_en": "USD/CHF",
        "base": "USD", "quote": "CHF",
        "sina": "fx_suschf", "eastmoney": "119.USDCHF", "tencent": "fxUSDCHF",
        "tiingo": "usdchf",
    },
    {
        "symbol": "NZDUSD=X", "td": "NZD/USD", "name_cn": "纽元/美元", "name_en": "NZD/USD",
        "base": "NZD", "quote": "USD",
        "sina": "fx_snzdusd", "eastmoney": "119.NZDUSD", "tencent": "fxNZDUSD",
        "tiingo": "nzdusd",
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


def _make_result(p: dict, price: float, change: float) -> dict:
    return {
        "symbol": p["symbol"],
        "name": p["td"],
        "name_cn": p["name_cn"],
        "name_en": p["name_en"],
        "price": round(price, 5),
        "change": round(change, 2),
        "base": p["base"],
        "quote": p["quote"],
        "category": "forex",
    }


# ============================================================================
# 新浪源 (批量)
# ============================================================================

def _fetch_sina_batch(pairs: list, timeout: float = 5) -> Dict[str, dict]:
    """批量拉取新浪外汇数据。"""
    sina_codes = []
    sina_map = {}
    for p in pairs:
        code = p.get("sina")
        if code:
            sina_codes.append(code)
            sina_map[code] = p

    if not sina_codes:
        return {}

    url = "http://hq.sinajs.cn/list=" + ",".join(sina_codes)
    try:
        r = requests.get(url, timeout=timeout, headers={"Referer": "https://finance.sina.com.cn"})
        r.raise_for_status()
    except Exception as e:
        logger.warning("Sina forex batch failed: %s", e)
        return {}

    results = {}
    for line in r.text.strip().split("\n"):
        line = line.strip()
        if not line or "=" not in line:
            continue
        try:
            var_part, val_part = line.split("=", 1)
            val_part = val_part.strip().rstrip(";").strip('"')
            if not val_part:
                continue
            parts = val_part.split(",")
            # 新浪外汇格式: name, buy, sell, current, high, low, ...
            if len(parts) < 4:
                continue
            current = _safe_float2(parts[3]) if len(parts) > 3 else 0
            if current <= 0:
                continue
            # 变化率在不同字段, 尝试取
            change = _safe_float2(parts[6]) if len(parts) > 6 else 0

            for code, p in sina_map.items():
                if code in var_part:
                    results[p["symbol"]] = _make_result(p, current, change)
                    break
        except Exception as e:
            logger.debug("Sina forex parse failed: %s", e)
            continue

    if results:
        logger.info("Fetched %d forex pairs via Sina", len(results))
    return results


# ============================================================================
# 东财源
# ============================================================================

def _fetch_eastmoney_single(p: dict, timeout: float = 5) -> Optional[dict]:
    secid = p.get("eastmoney")
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
        # 外汇值通常在合理范围, 不需要除100
        if val <= 0:
            return None
        return _make_result(p, val, change)
    except Exception as e:
        logger.debug("Eastmoney forex %s failed: %s", p["symbol"], e)
        return None


# ============================================================================
# 腾讯源
# ============================================================================

def _fetch_tencent_single(p: dict, timeout: float = 5) -> Optional[dict]:
    code = p.get("tencent")
    if not code:
        return None
    try:
        r = requests.get(f"https://qt.gtimg.cn/q={code}", timeout=timeout)
        r.raise_for_status()
        parts = r.text.split("~")
        if len(parts) < 5:
            return None
        val = _safe_float2(parts[3])
        change_pct = _safe_float2(parts[32]) if len(parts) > 32 else 0
        if val <= 0:
            return None
        return _make_result(p, val, change_pct)
    except Exception as e:
        logger.debug("Tencent forex %s failed: %s", p["symbol"], e)
        return None


# ============================================================================
# TwelveData 源
# ============================================================================

def _fetch_td_single(p: dict, timeout: float = 5) -> Optional[dict]:
    try:
        from app.config import APIKeys
        api_key = (APIKeys.TWELVE_DATA_API_KEY or "").strip()
        if not api_key:
            return None
        td_sym = p.get("td")
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
        return _make_result(p, current, change)
    except Exception as e:
        logger.debug("TwelveData forex %s failed: %s", p["symbol"], e)
        return None


# ============================================================================
# akshare 源 (倒二)
# ============================================================================

def _fetch_akshare_single(p: dict, timeout: float = 5) -> Optional[dict]:
    ak = _get_ak()
    if ak is None:
        return None

    # akshare 外汇映射
    ak_map = {
        "EURUSD=X": "欧元美元",
        "GBPUSD=X": "英镑美元",
        "USDJPY=X": "美元日元",
        "USDCNH=X": "美元离岸人民币",
        "AUDUSD=X": "澳元美元",
        "USDCAD=X": "美元加元",
        "USDCHF=X": "美元瑞郎",
        "NZDUSD=X": "新西兰元美元",
    }
    name = ak_map.get(p["symbol"])
    if not name:
        return None
    try:
        df = ak.fx_spot_quote()
        if df is None or df.empty:
            return None
        # 查找匹配行
        row = df[df["名称"].str.contains(name[:2], na=False)]
        if row.empty:
            return None
        last = row.iloc[0]
        price = _safe_float2(str(last.get("最新价", last.get("买报价", 0))))
        change = _safe_float2(str(last.get("涨跌幅", 0)))
        if price <= 0:
            return None
        return _make_result(p, price, change)
    except Exception as e:
        logger.debug("akshare forex %s failed: %s", p["symbol"], e)
    return None


# ============================================================================
# yfinance 源 (垫底)
# ============================================================================

def _fetch_yf_single(p: dict, timeout: float = 5) -> Optional[dict]:
    yf = _get_yf()
    if yf is None:
        return None
    try:
        ticker = yf.Ticker(p["symbol"])
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
        return _make_result(p, current, change)
    except Exception as e:
        logger.debug("yfinance forex %s failed: %s", p["symbol"], e)
        return None


# ============================================================================
# Tiingo 源
# ============================================================================

def _fetch_tiingo_single(p: dict, timeout: float = 5) -> Optional[dict]:
    from datetime import datetime, timedelta
    try:
        from app.config import TiingoConfig, APIKeys
        api_key = APIKeys.TIINGO_API_KEY
        if not api_key:
            return None
        tiingo_sym = p.get("tiingo")
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
        return _make_result(p, current, change)
    except Exception as e:
        logger.debug("Tiingo forex %s failed: %s", p["symbol"], e)
        return None


# ============================================================================
# 统一入口
# ============================================================================

# (name, batch_fn_or_none, single_fn, is_batch)
_SOURCES = [
    ("sina",       _fetch_sina_batch,      None,                    True),
    ("eastmoney",  None,                   _fetch_eastmoney_single, False),
    ("tencent",    None,                   _fetch_tencent_single,   False),
    ("twelvedata", None,                   _fetch_td_single,        False),
    ("akshare",    None,                   _fetch_akshare_single,   False),
    ("yfinance",   None,                   _fetch_yf_single,        False),
    ("tiingo",     None,                   _fetch_tiingo_single,    False),
]


def fetch_forex_pairs() -> List[Dict[str, Any]]:
    """Fetch major forex pairs. 新浪 → 东财 → 腾讯 → TwelveData → akshare → yfinance → Tiingo."""
    result: Dict[str, dict] = {}
    pending = list(FOREX_PAIRS)

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
                pending = [p for p in pending if p["symbol"] not in fetched]
            except Exception as e:
                logger.warning("Batch source %s failed: %s", src_name, e)
        elif single_fn:
            still_pending = []
            for p in pending:
                if p["symbol"] in result:
                    continue
                try:
                    data = single_fn(p)
                    if data and data["price"] > 0:
                        result[p["symbol"]] = data
                        logger.info("Forex %s: %.5f from %s", p["symbol"], data["price"], src_name)
                    else:
                        still_pending.append(p)
                except Exception as e:
                    logger.debug("Source %s / %s failed: %s", src_name, p["symbol"], e)
                    still_pending.append(p)
            pending = still_pending

    output = []
    for p in FOREX_PAIRS:
        if p["symbol"] in result:
            output.append(result[p["symbol"]])
        else:
            logger.warning("Forex %s: all sources failed", p["symbol"])
            output.append({
                "symbol": p["symbol"], "name": p["td"],
                "name_cn": p["name_cn"], "name_en": p["name_en"],
                "price": 0, "change": 0,
                "base": p["base"], "quote": p["quote"],
                "category": "forex",
            })

    return output
