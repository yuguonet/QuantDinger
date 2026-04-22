"""Stock index data fetcher — 多源降级版.

数据源优先级:
    新浪 → 东财 → 腾讯 → akshare(倒二) → yfinance(垫底)
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

import requests

from app.utils.logger import get_logger

logger = get_logger(__name__)

# ============================================================================
# 指数定义 — 每个 symbol 配置各源的代码
# ============================================================================

INDICES = [
    {
        "symbol": "^GSPC", "name_cn": "标普500", "name_en": "S&P 500",
        "region": "US", "flag": "\U0001f1fa\U0001f1f8",
        "lat": 40.7, "lng": -74.0,
        "sina": "int_sp500", "eastmoney": "100.SPX", "tencent": "usSPX",
        "akshare": "000001",  # akshare index_df 用
    },
    {
        "symbol": "^DJI", "name_cn": "道琼斯", "name_en": "Dow Jones",
        "region": "US", "flag": "\U0001f1fa\U0001f1f8",
        "lat": 38.5, "lng": -77.0,
        "sina": "int_dji", "eastmoney": "100.DJIA", "tencent": "usDJI",
        "akshare": "000001",
    },
    {
        "symbol": "^IXIC", "name_cn": "纳斯达克", "name_en": "NASDAQ",
        "region": "US", "flag": "\U0001f1fa\U0001f1f8",
        "lat": 37.5, "lng": -122.4,
        "sina": "int_nasdaq", "eastmoney": "100.NDX", "tencent": "usNDX",
        "akshare": "000001",
    },
    {
        "symbol": "^GDAXI", "name_cn": "德国DAX", "name_en": "DAX",
        "region": "EU", "flag": "\U0001f1e9\U0001f1ea",
        "lat": 50.1109, "lng": 8.6821,
        "sina": "int_dax", "eastmoney": "100.GDAXI", "tencent": None,
        "akshare": "000001",
    },
    {
        "symbol": "^FTSE", "name_cn": "英国富时100", "name_en": "FTSE 100",
        "region": "EU", "flag": "\U0001f1ec\U0001f1e7",
        "lat": 51.5074, "lng": -0.1278,
        "sina": "int_ftse", "eastmoney": "100.FTSE", "tencent": None,
        "akshare": "000001",
    },
    {
        "symbol": "^FCHI", "name_cn": "法国CAC40", "name_en": "CAC 40",
        "region": "EU", "flag": "\U0001f1eb\U0001f1f7",
        "lat": 48.8566, "lng": 2.3522,
        "sina": "int_cac", "eastmoney": "100.FCHI", "tencent": None,
        "akshare": "000001",
    },
    {
        "symbol": "^N225", "name_cn": "日经225", "name_en": "Nikkei 225",
        "region": "JP", "flag": "\U0001f1ef\U0001f1f5",
        "lat": 35.6762, "lng": 139.6503,
        "sina": "int_nikkei", "eastmoney": "100.N225", "tencent": None,
        "akshare": "000001",
    },
    {
        "symbol": "^KS11", "name_cn": "韩国KOSPI", "name_en": "KOSPI",
        "region": "KR", "flag": "\U0001f1f0\U0001f1f7",
        "lat": 37.5665, "lng": 126.9780,
        "sina": "int_ks11", "eastmoney": None, "tencent": None,
        "akshare": "000001",
    },
    {
        "symbol": "^AXJO", "name_cn": "澳洲ASX200", "name_en": "ASX 200",
        "region": "AU", "flag": "\U0001f1e6\U0001f1fa",
        "lat": -33.8688, "lng": 151.2093,
        "sina": "int_asx", "eastmoney": None, "tencent": None,
        "akshare": "000001",
    },
    {
        "symbol": "^BSESN", "name_cn": "印度SENSEX", "name_en": "SENSEX",
        "region": "IN", "flag": "\U0001f1ee\U0001f1f3",
        "lat": 19.0760, "lng": 72.8777,
        "sina": "int_bsesn", "eastmoney": None, "tencent": None,
        "akshare": "000001",
    },
]

# ============================================================================
# 超时配置
# ============================================================================

_FIRST_TIMEOUT = 5       # 首个源
_FALLBACK_TIMEOUT = 1.5  # 降级源快速跳过

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

def _safe_round(v, n=2):
    f = float(v)
    return 0 if math.isnan(f) or math.isinf(f) else round(f, n)


def _safe_float(text, default=0.0):
    try:
        val = float(str(text).strip())
        return val if val == val else default
    except (ValueError, TypeError, AttributeError):
        return default


def _make_result(idx: dict, price: float, change: float) -> dict:
    return {
        "symbol": idx["symbol"],
        "name_cn": idx["name_cn"],
        "name_en": idx["name_en"],
        "price": _safe_round(price),
        "change": _safe_round(change),
        "region": idx["region"],
        "flag": idx["flag"],
        "lat": idx["lat"],
        "lng": idx["lng"],
        "category": "index",
    }


def _make_default(idx: dict) -> dict:
    return _make_result(idx, 0, 0)


# ============================================================================
# 新浪源
# ============================================================================

def _fetch_sina_batch(indices: list, timeout: float = 5) -> Dict[str, dict]:
    """批量拉取新浪指数数据。"""
    sina_symbols = []
    sina_map = {}  # sina_code -> idx
    for idx in indices:
        code = idx.get("sina")
        if code:
            sina_symbols.append(code)
            sina_map[code] = idx

    if not sina_symbols:
        return {}

    url = "http://hq.sinajs.cn/list=" + ",".join(sina_symbols)
    try:
        r = requests.get(url, timeout=timeout, headers={"Referer": "https://finance.sina.com.cn"})
        r.raise_for_status()
    except Exception as e:
        logger.warning("Sina batch request failed: %s", e)
        return {}

    results = {}
    for line in r.text.strip().split("\n"):
        line = line.strip()
        if not line or "=" not in line:
            continue
        # var hq_str_int_sp500="标普500,2024-01-01,4567.89,...";
        try:
            var_part, val_part = line.split("=", 1)
            sina_code = var_part.split("_", 2)[-1]  # int_sp500
            # 重新拼回完整 key
            for key in sina_map:
                if key in var_part:
                    sina_code = key
                    break
            val_part = val_part.strip().rstrip(";").strip('"')
            if not val_part:
                continue
            parts = val_part.split(",")
            # 新浪国际指数格式: name, date, current, change, change_pct, ...
            if len(parts) < 4:
                continue
            current = _safe_float(parts[2])
            change_pct = _safe_float(parts[3])  # 涨跌幅(%)
            if current <= 0:
                continue
            idx = sina_map.get(sina_code)
            if idx:
                results[idx["symbol"]] = _make_result(idx, current, change_pct)
        except Exception as e:
            logger.debug("Sina parse line failed: %s", e)
            continue

    if results:
        logger.info("Fetched %d indices via Sina", len(results))
    return results


# ============================================================================
# 东财源
# ============================================================================

def _fetch_eastmoney_single(idx: dict, timeout: float = 5) -> Optional[dict]:
    """单个拉取东财指数数据。"""
    secid = idx.get("eastmoney")
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
        val = _safe_float(str(raw_val))
        # f43 可能是放大值，f170 是涨跌幅
        change = _safe_float(str(data.get("f170", 0)))
        # 如果 val 太大可能是放大了的
        if val > 100000:
            val = val / 100
        if val <= 0:
            return None
        return _make_result(idx, val, change)
    except Exception as e:
        logger.debug("Eastmoney %s failed: %s", idx["symbol"], e)
        return None


# ============================================================================
# 腾讯源
# ============================================================================

def _fetch_tencent_single(idx: dict, timeout: float = 5) -> Optional[dict]:
    """单个拉取腾讯指数数据。"""
    code = idx.get("tencent")
    if not code:
        return None
    try:
        r = requests.get(f"https://qt.gtimg.cn/q={code}", timeout=timeout)
        r.raise_for_status()
        parts = r.text.split("~")
        if len(parts) < 5:
            return None
        val = _safe_float(parts[3])
        change_pct = _safe_float(parts[32]) if len(parts) > 32 else 0
        if val <= 0:
            return None
        return _make_result(idx, val, change_pct)
    except Exception as e:
        logger.debug("Tencent %s failed: %s", idx["symbol"], e)
        return None


# ============================================================================
# akshare 源
# ============================================================================

def _fetch_akshare_single(idx: dict, timeout: float = 5) -> Optional[dict]:
    """用 akshare 拉取单个指数（通过 index_name 映射）。"""
    ak = _get_ak()
    if ak is None:
        return None

    # akshare 全球指数映射
    ak_map = {
        "^GSPC": "美国标普500",
        "^DJI": "道琼斯工业平均指数",
        "^IXIC": "纳斯达克综合指数",
        "^GDAXI": "德国DAX指数",
        "^FTSE": "英国富时100指数",
        "^FCHI": "法国CAC40指数",
        "^N225": "日经225指数",
        "^KS11": "韩国综合指数",
        "^AXJO": "澳大利亚标普200指数",
        "^BSESN": "印度孟买SENSEX指数",
    }
    name = ak_map.get(idx["symbol"])
    if not name:
        return None
    try:
        df = ak.index_realtime(symbol=name)
        if df is None or df.empty:
            return None
        last = df.iloc[-1]
        # 列名因版本不同而异，尝试常见列名
        price = 0
        change = 0
        for col in last.index:
            cl = str(col).lower()
            if "最新" in cl or "close" in cl or "收盘" in cl or cl == "price":
                price = _safe_float(str(last[col]))
            elif "涨跌幅" in cl or "change" in cl and "%" in cl:
                change = _safe_float(str(last[col]))
        if price <= 0:
            return None
        return _make_result(idx, price, change)
    except Exception as e:
        logger.debug("akshare %s failed: %s", idx["symbol"], e)
        return None


# ============================================================================
# yfinance 源 (垫底)
# ============================================================================

def _fetch_yf_single(idx: dict, timeout: float = 5) -> Optional[dict]:
    """用 yfinance 拉取单个指数。"""
    yf = _get_yf()
    if yf is None:
        return None
    try:
        ticker = yf.Ticker(idx["symbol"])
        hist = ticker.history(period="5d")
        closes = hist["Close"].dropna() if len(hist) > 0 else []

        if len(closes) >= 2:
            current = float(closes.iloc[-1])
            prev_close = float(closes.iloc[-2])
            change = ((current - prev_close) / prev_close) * 100 if prev_close else 0
        elif len(closes) == 1:
            current = float(closes.iloc[-1])
            change = 0
        else:
            return None

        if current <= 0:
            return None
        return _make_result(idx, current, change)
    except Exception as e:
        logger.debug("yfinance %s failed: %s", idx["symbol"], e)
        return None


# ============================================================================
# 统一入口 — 多源降级
# ============================================================================

# 源定义: (name, batch_fn_or_none, single_fn, can_handle_batch)
_SOURCES = [
    ("sina",      _fetch_sina_batch,      None,                       True),
    ("eastmoney", None,                   _fetch_eastmoney_single,    False),
    ("tencent",   None,                   _fetch_tencent_single,      False),
    ("akshare",   None,                   _fetch_akshare_single,      False),
    ("yfinance",  None,                   _fetch_yf_single,           False),
]


def fetch_stock_indices() -> List[Dict[str, Any]]:
    """Fetch major stock indices with multi-source fallback.

    新浪(批量) → 东财 → 腾讯 → akshare → yfinance
    """
    result: Dict[str, dict] = {}  # symbol -> data
    pending = list(INDICES)  # 待获取的指数

    for src_name, batch_fn, single_fn, is_batch in _SOURCES:
        if not pending:
            break

        if is_batch and batch_fn:
            # 批量源: 一次拉所有
            try:
                batch_result = batch_fn(pending)
                for sym, data in batch_result.items():
                    if sym not in result:
                        result[sym] = data
                # 更新 pending: 还没拿到的
                fetched = set(batch_result.keys())
                pending = [idx for idx in pending if idx["symbol"] not in fetched]
            except Exception as e:
                logger.warning("Batch source %s failed: %s", src_name, e)
        elif single_fn:
            # 逐个源: 一个一个试
            still_pending = []
            for idx in pending:
                if idx["symbol"] in result:
                    continue
                try:
                    data = single_fn(idx)
                    if data and data["price"] > 0:
                        result[idx["symbol"]] = data
                        logger.info("Index %s: %.2f from %s", idx["symbol"], data["price"], src_name)
                    else:
                        still_pending.append(idx)
                except Exception as e:
                    logger.debug("Source %s / %s failed: %s", src_name, idx["symbol"], e)
                    still_pending.append(idx)
            pending = still_pending

    # 剩余未获取的填充默认值
    output = []
    for idx in INDICES:
        if idx["symbol"] in result:
            output.append(result[idx["symbol"]])
        else:
            logger.warning("Index %s: all sources failed, returning default", idx["symbol"])
            output.append(_make_default(idx))

    return output
