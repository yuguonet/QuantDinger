"""
市场情绪指标模块 — 多源降级版 (精简版)

7 个宏观情绪指标，每个按固定优先级逐源降级：
    恐贪指数 (Fear & Greed)    4h
    VIX (CBOE 波动率)          5min
    VXN (纳斯达克波动率)        5min
    DXY (美元指数)              10min
    收益率曲线 (10Y-2Y)         10min
    GVZ (黄金波动率)            10min
    VIX 期限结构 (VIX/VIX3M)    5min

优先级: 国内直连(新浪/腾讯/东财) → TwelveData → akshare → yfinance

依赖:
    - requests   (必须)
    - akshare    (可选，倒数第二降级)
    - yfinance   (可选，最终降级)
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, Optional, Tuple

import requests

from app.utils.logger import get_logger

logger = get_logger(__name__)

# ============================================================================
# 超时配置
# ============================================================================

_FIRST_TIMEOUT = 5       # 首个源的超时（秒）
_FALLBACK_TIMEOUT = 1.5  # 降级源的超时（秒）

# ============================================================================
# 源优先级（固定顺序，按国内直连 → 海外 API → Python 库排列）
# ============================================================================

_SOURCE_PRIORITY: Dict[str, list] = {
    "fear_greed":  ["altme", "akshare_a_fear"],
    "vix":         ["sina", "tencent", "eastmoney", "twelvedata", "akshare", "yfinance"],
    "vxn":         ["sina", "tencent", "eastmoney", "twelvedata", "yfinance"],
    "dxy":         ["sina", "eastmoney", "tencent", "twelvedata", "akshare", "yfinance"],
    "yield_curve": ["sina", "twelvedata", "eastmoney", "tencent", "akshare", "yfinance"],
    "gvz":         ["sina", "eastmoney", "twelvedata", "yfinance"],
    "vix_term":    ["sina", "eastmoney", "twelvedata", "akshare", "yfinance"],
}

# ============================================================================
# 缓存 TTL
# ============================================================================

_CACHE_TTL: Dict[str, int] = {
    "fear_greed":  14400,  # 4h
    "vix":         300,    # 5min
    "dxy":         600,    # 10min
    "yield_curve": 600,    # 10min
    "vxn":         300,    # 5min
    "gvz":         600,    # 10min
    "vix_term":    300,    # 5min
}

# ============================================================================
# 可选模块懒加载（线程安全）
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
                except ImportError:
                    _ak = None
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
                except ImportError:
                    _yf = None
    return _yf

# ============================================================================
# 缓存层
# ============================================================================

def _cache() -> Any:
    from app.utils.cache import CacheManager
    return CacheManager()


_CK = "sentiment_"


def _get_cached_indicator(name: str) -> Optional[Dict[str, Any]]:
    try:
        cm = _cache()
        raw = cm.get(f"{_CK}{name}")
        if not raw:
            return None
        data = raw.get("data")
        ts = raw.get("ts", 0)
        ttl = _CACHE_TTL.get(name, 300)
        if data is not None and (time.time() - ts) < ttl:
            return data
    except Exception as e:
        logger.warning("Cache read failed for %s: %s", name, e)
    return None


def _set_cached_indicator(name: str, data: Dict[str, Any]) -> None:
    try:
        cm = _cache()
        ttl = _CACHE_TTL.get(name, 300)
        cm.set(f"{_CK}{name}", {"data": data, "ts": int(time.time())}, ttl=ttl * 2)
    except Exception as e:
        logger.warning("Cache write failed for %s: %s", name, e)

# ============================================================================
# 通用 HTTP 工具
# ============================================================================

def _safe_float(text: str, default: float = 0.0) -> float:
    try:
        val = float(text.strip())
        return val if val == val else default  # NaN 检查
    except (ValueError, TypeError, AttributeError):
        return default


def _safe_get_json(url: str, params: Optional[Dict] = None, timeout: int = 5) -> Dict[str, Any]:
    try:
        r = requests.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.debug("HTTP GET %s failed: %s", url, e)
        return {}

# ============================================================================
# 新浪系列
# ============================================================================

def _try_sina_vix(timeout: float = 5) -> Dict[str, Any]:
    r = requests.get("http://hq.sinajs.cn/list=int_vix", timeout=timeout)
    r.raise_for_status()
    parts = r.text.split(",")
    if len(parts) < 3:
        raise ValueError(f"Sina VIX: unexpected format ({len(parts)} fields)")
    val = _safe_float(parts[2])
    if val <= 0:
        raise ValueError(f"Sina VIX: invalid value {val}")
    return {"value": val, "source": "sina"}


def _try_sina_vxn(timeout: float = 5) -> Dict[str, Any]:
    r = requests.get("http://hq.sinajs.cn/list=int_vxn", timeout=timeout)
    r.raise_for_status()
    parts = r.text.split(",")
    if len(parts) < 3:
        raise ValueError("Sina VXN: unexpected format")
    val = _safe_float(parts[2])
    if val <= 0:
        raise ValueError(f"Sina VXN: invalid value {val}")
    return {"value": val, "source": "sina"}


def _try_sina_gvz(timeout: float = 5) -> Dict[str, Any]:
    r = requests.get("http://hq.sinajs.cn/list=int_gvz", timeout=timeout)
    r.raise_for_status()
    parts = r.text.split(",")
    if len(parts) < 3:
        raise ValueError("Sina GVZ: unexpected format")
    val = _safe_float(parts[2])
    if val <= 0:
        raise ValueError(f"Sina GVZ: invalid value {val}")
    return {"value": val, "source": "sina"}


def _try_sina_dxy(timeout: float = 5) -> Dict[str, Any]:
    r = requests.get("http://hq.sinajs.cn/list=fx_susdind", timeout=timeout)
    r.raise_for_status()
    parts = r.text.split(",")
    if len(parts) < 2:
        raise ValueError("Sina DXY: unexpected format")
    val = _safe_float(parts[1])
    if val <= 0:
        raise ValueError(f"Sina DXY: invalid value {val}")
    return {"value": val, "source": "sina"}


def _try_sina_yield(timeout: float = 5) -> Dict[str, Any]:
    r = requests.get("http://hq.sinajs.cn/list=bond_us02y,bond_us10y", timeout=timeout)
    r.raise_for_status()
    lines = r.text.strip().split("\n")
    if len(lines) < 2:
        raise ValueError(f"Sina Yield: expected 2 lines, got {len(lines)}")
    y2_parts = lines[0].split(",")
    y10_parts = lines[1].split(",")
    y2 = _safe_float(y2_parts[1]) if len(y2_parts) > 1 else 0
    y10 = _safe_float(y10_parts[1]) if len(y10_parts) > 1 else 0
    if y2 <= 0 or y10 <= 0:
        raise ValueError(f"Sina Yield: invalid y2={y2} y10={y10}")
    return {"yield_10y": y10, "yield_2y": y2, "spread": round(y10 - y2, 3), "source": "sina"}

# ============================================================================
# 腾讯系列
# ============================================================================

def _parse_tencent_response(text: str, label: str) -> float:
    parts = text.split("~")
    # 兼容不同格式: VIX有5+字段, VXN/DXY可能只有1-4字段
    if len(parts) >= 5:
        val = _safe_float(parts[3])
    elif len(parts) >= 2:
        # 尝试从第2个字段取值（部分指数格式不同）
        val = _safe_float(parts[1]) if _safe_float(parts[1]) > 0 else _safe_float(parts[0])
    else:
        # 单字段: 尝试直接解析整个响应中的数字
        import re
        nums = re.findall(r'[\d.]+', text)
        val = _safe_float(nums[0]) if nums else 0
    if val <= 0:
        raise ValueError(f"Tencent {label}: invalid value {val}")
    return val


def _try_tencent_vix(timeout: float = 5) -> Dict[str, Any]:
    r = requests.get("https://qt.gtimg.cn/q=usVIX", timeout=timeout)
    r.raise_for_status()
    return {"value": _parse_tencent_response(r.text, "VIX"), "source": "tencent"}


def _try_tencent_vxn(timeout: float = 5) -> Dict[str, Any]:
    r = requests.get("https://qt.gtimg.cn/q=usVXN", timeout=timeout)
    r.raise_for_status()
    return {"value": _parse_tencent_response(r.text, "VXN"), "source": "tencent"}


def _try_tencent_dxy(timeout: float = 5) -> Dict[str, Any]:
    r = requests.get("https://qt.gtimg.cn/q=fx_susdind", timeout=timeout)
    r.raise_for_status()
    return {"value": _parse_tencent_response(r.text, "DXY"), "source": "tencent"}

# ============================================================================
# 东方财富系列
# ============================================================================

def _try_eastmoney(symbol: str, timeout: float = 5) -> Dict[str, Any]:
    r = requests.get(
        "https://push2.eastmoney.com/api/qt/stock/get",
        params={"secid": symbol, "fields": "f43,f44,f45,f46,f170,f171"},
        timeout=timeout,
    )
    r.raise_for_status()
    data = r.json().get("data")
    if not data:
        raise ValueError(f"Eastmoney {symbol}: empty data")
    raw_val = data.get("f43")
    if raw_val is None or raw_val == "":
        raise ValueError(f"Eastmoney {symbol}: f43 missing")
    val = _safe_float(str(raw_val))
    if val > 1000:
        val = val / 100  # 东财惯例：放大100倍
    if val <= 0:
        raise ValueError(f"Eastmoney {symbol}: invalid value {val}")
    return {"value": val, "source": "eastmoney"}

# ============================================================================
# Twelve Data 系列
# ============================================================================

_TD_KEY: str = ""


def set_twelvedata_key(key: str) -> None:
    global _TD_KEY
    _TD_KEY = key


def _try_twelvedata(symbol: str, timeout: float = 8) -> Dict[str, Any]:
    if not _TD_KEY:
        raise ValueError("TwelveData: API key not set")
    d = _safe_get_json(
        "https://api.twelvedata.com/quote",
        params={"symbol": symbol, "apikey": _TD_KEY},
        timeout=timeout,
    )
    if not d or "close" not in d:
        raise ValueError(f"TwelveData {symbol}: {d.get('message', 'empty response')}")
    val = _safe_float(d["close"])
    if val <= 0:
        raise ValueError(f"TwelveData {symbol}: invalid value {val}")
    return {"value": val, "source": "twelvedata"}


def _try_twelvedata_yield(timeout: float = 8) -> Dict[str, Any]:
    d10 = _try_twelvedata("US10Y", timeout=timeout)
    d2 = _try_twelvedata("US2Y", timeout=timeout)
    y10, y2 = d10["value"], d2["value"]
    return {"yield_10y": y10, "yield_2y": y2, "spread": round(y10 - y2, 3), "source": "twelvedata"}

# ============================================================================
# alternative.me (加密恐贪指数)
# ============================================================================

def _try_altme_fear(timeout: float = 10) -> Dict[str, Any]:
    d = _safe_get_json("https://api.alternative.me/fng/?limit=1", timeout=timeout)
    if not d or not d.get("data"):
        raise ValueError("alternative.me: empty response")
    item = d["data"][0]
    val = _safe_float(item.get("value", "50"))
    if val <= 0:
        raise ValueError(f"alternative.me: invalid value {val}")
    return {
        "value": int(val),
        "classification": item.get("value_classification", "Unknown"),
        "timestamp": int(item.get("timestamp", 0)),
        "source": "alternative.me",
    }

# ============================================================================
# 分级解读函数
# ============================================================================

def _vix_level(val: float) -> Tuple[str, str, str]:
    if val < 12:
        return "very_low", "极低波动 - 市场极度乐观", "Very Low"
    elif val < 20:
        return "low", "低波动 - 市场稳定", "Low"
    elif val < 25:
        return "moderate", "中等波动 - 正常水平", "Moderate"
    elif val < 30:
        return "high", "高波动 - 市场担忧", "High"
    else:
        return "very_high", "极高波动 - 市场恐慌", "Very High"


def _dxy_level(val: float) -> Tuple[str, str, str]:
    if val > 105:
        return "strong", "美元强势 - 利空大宗商品/新兴市场", "Strong"
    elif val > 100:
        return "moderate_strong", "美元偏强 - 关注资金流向", "Moderately Strong"
    elif val > 95:
        return "neutral", "美元中性 - 市场均衡", "Neutral"
    elif val > 90:
        return "moderate_weak", "美元偏弱 - 利多风险资产", "Moderately Weak"
    else:
        return "weak", "美元疲软 - 利多黄金/大宗商品", "Weak"


def _gvz_level(val: float) -> Tuple[str, str, str]:
    if val < 12:
        return "very_low", "黄金低波动 - 避险需求低", "Low Gold Vol"
    elif val < 16:
        return "low", "黄金稳定 - 市场平静", "Gold Stable"
    elif val < 20:
        return "moderate", "黄金中等波动 - 关注避险", "Moderate Gold Vol"
    elif val < 25:
        return "high", "黄金高波动 - 避险需求上升", "High Gold Vol"
    else:
        return "very_high", "黄金极高波动 - 市场避险", "Very High Gold Vol"


def _vxn_level(val: float) -> Tuple[str, str, str]:
    if val < 15:
        return "very_low", "科技股极低波动 - 市场乐观", "Very Low Tech Vol"
    elif val < 22:
        return "low", "科技股低波动 - 稳定", "Low Tech Vol"
    elif val < 28:
        return "moderate", "科技股中等波动 - 正常", "Moderate Tech Vol"
    elif val < 35:
        return "high", "科技股高波动 - 谨慎", "High Tech Vol"
    else:
        return "very_high", "科技股极高波动 - 恐慌", "Very High Tech Vol"


def _yield_level(spread: float) -> Tuple[str, str, str, str]:
    if spread < -0.5:
        return "deeply_inverted", "深度倒挂 - 强烈衰退信号", "Deeply Inverted", "bearish"
    elif spread < 0:
        return "inverted", "收益率倒挂 - 衰退预警", "Inverted", "bearish"
    elif spread < 0.5:
        return "flat", "曲线平坦 - 经济放缓信号", "Flat", "neutral"
    elif spread < 1.5:
        return "normal", "正常曲线 - 经济健康", "Normal", "bullish"
    else:
        return "steep", "陡峭曲线 - 经济扩张预期", "Steep", "bullish"

# ============================================================================
# 指标 fetcher — 每个按优先级逐源降级
# ============================================================================

def fetch_vix() -> Dict[str, Any]:
    default = {"value": 0, "change": 0, "level": "unknown", "interpretation": "数据获取失败", "interpretation_en": "Data fetch failed", "source": "N/A"}
    for i, src in enumerate(_SOURCE_PRIORITY["vix"]):
        to = _FIRST_TIMEOUT if i == 0 else _FALLBACK_TIMEOUT
        try:
            if src == "sina":
                val = _try_sina_vix(timeout=to)["value"]
            elif src == "tencent":
                val = _try_tencent_vix(timeout=to)["value"]
            elif src == "eastmoney":
                val = _try_eastmoney("100.VIX", timeout=to)["value"]
            elif src == "twelvedata":
                val = _try_twelvedata("VIX", timeout=to)["value"]
            elif src == "akshare":
                ak = _get_ak()
                if ak is None:
                    raise ImportError("akshare not installed")
                # 兼容不同版本: index_vix → stock_us_vix → index_vix_en
                for fn_name in ("index_vix", "stock_us_vix", "index_vix_en"):
                    fn = getattr(ak, fn_name, None)
                    if fn:
                        df = fn()
                        if df is not None and not df.empty:
                            col = "close" if "close" in df.columns else df.columns[-1]
                            val = float(df.iloc[-1][col])
                            break
                else:
                    raise ValueError("akshare: no VIX function found")
            elif src == "yfinance":
                yf = _get_yf()
                if yf is None:
                    raise ImportError("yfinance not installed")
                h = yf.Ticker("^VIX").history(period="5d")
                if h.empty:
                    raise ValueError("yfinance VIX: empty history")
                val = float(h["Close"].iloc[-1])
            else:
                continue

            if val <= 0:
                continue
            level, cn, en = _vix_level(val)
            logger.info("VIX: %.2f from %s", val, src)
            return {"value": round(val, 2), "change": 0, "level": level, "interpretation": cn, "interpretation_en": en, "source": src}
        except Exception as e:
            logger.warning("VIX source %s failed: %s", src, e)
            continue
    return default


def fetch_vxn() -> Dict[str, Any]:
    default = {"value": 0, "change": 0, "level": "unknown", "interpretation": "数据获取失败", "interpretation_en": "Data fetch failed", "source": "N/A"}
    for i, src in enumerate(_SOURCE_PRIORITY["vxn"]):
        to = _FIRST_TIMEOUT if i == 0 else _FALLBACK_TIMEOUT
        try:
            if src == "sina":
                val = _try_sina_vxn(timeout=to)["value"]
            elif src == "tencent":
                val = _try_tencent_vxn(timeout=to)["value"]
            elif src == "eastmoney":
                val = _try_eastmoney("100.VXN", timeout=to)["value"]
            elif src == "twelvedata":
                val = _try_twelvedata("VXN", timeout=to)["value"]
            elif src == "yfinance":
                yf = _get_yf()
                if yf is None:
                    raise ImportError("yfinance not installed")
                h = yf.Ticker("^VXN").history(period="5d")
                if h.empty:
                    raise ValueError("yfinance VXN: empty history")
                val = float(h["Close"].iloc[-1])
            else:
                continue

            if val <= 0:
                continue
            level, cn, en = _vxn_level(val)
            return {"value": round(val, 2), "change": 0, "level": level, "interpretation": cn, "interpretation_en": en, "source": src}
        except Exception as e:
            logger.warning("VXN source %s failed: %s", src, e)
            continue
    return default


def fetch_dollar_index() -> Dict[str, Any]:
    default = {"value": 0, "change": 0, "level": "unknown", "interpretation": "数据获取失败", "interpretation_en": "Data fetch failed", "source": "N/A"}
    for i, src in enumerate(_SOURCE_PRIORITY["dxy"]):
        to = _FIRST_TIMEOUT if i == 0 else _FALLBACK_TIMEOUT
        try:
            if src == "sina":
                val = _try_sina_dxy(timeout=to)["value"]
            elif src == "eastmoney":
                val = _try_eastmoney("133.USDX", timeout=to)["value"]
            elif src == "tencent":
                val = _try_tencent_dxy(timeout=to)["value"]
            elif src == "twelvedata":
                val = _try_twelvedata("USD/IDX", timeout=to)["value"]
            elif src == "akshare":
                ak = _get_ak()
                if ak is None:
                    raise ImportError("akshare not installed")
                # 兼容不同版本 akshare
                val = 0
                for fn_name, kwargs in [
                    ("futures_foreign_hist", {"symbol": "DINI"}),
                    ("currency_boc_safe", {"symbol": "美元指数", "start_date": "20260101", "end_date": "20261231"}),
                ]:
                    fn = getattr(ak, fn_name, None)
                    if fn:
                        try:
                            df = fn(**kwargs)
                            if df is not None and not df.empty:
                                col = "close" if "close" in df.columns else df.columns[-1]
                                val = float(df.iloc[-1][col])
                                if val > 0:
                                    break
                        except Exception:
                            continue
                if val <= 0:
                    raise ValueError("akshare DXY: no data")
            elif src == "yfinance":
                yf = _get_yf()
                if yf is None:
                    raise ImportError("yfinance not installed")
                h = yf.Ticker("DX-Y.NYB").history(period="5d")
                if h.empty:
                    raise ValueError("yfinance DXY: empty history")
                val = float(h["Close"].iloc[-1])
            else:
                continue

            if val <= 0:
                continue
            level, cn, en = _dxy_level(val)
            logger.info("DXY: %.2f from %s", val, src)
            return {"value": round(val, 2), "change": 0, "level": level, "interpretation": cn, "interpretation_en": en, "source": src}
        except Exception as e:
            logger.warning("DXY source %s failed: %s", src, e)
            continue
    return default


def fetch_yield_curve() -> Dict[str, Any]:
    default = {"yield_10y": 0, "yield_2y": 0, "spread": 0, "change": 0, "level": "unknown", "signal": "neutral", "interpretation": "数据获取失败", "interpretation_en": "Data fetch failed", "source": "N/A"}
    for i, src in enumerate(_SOURCE_PRIORITY["yield_curve"]):
        to = _FIRST_TIMEOUT if i == 0 else _FALLBACK_TIMEOUT
        try:
            if src == "sina":
                d = _try_sina_yield(timeout=to)
                y10, y2 = d["yield_10y"], d["yield_2y"]
            elif src == "twelvedata":
                d = _try_twelvedata_yield(timeout=to)
                y10, y2 = d["yield_10y"], d["yield_2y"]
            elif src == "eastmoney":
                d10 = _try_eastmoney("101.US10Y", timeout=to)
                d2 = _try_eastmoney("101.US02Y", timeout=to)
                y10, y2 = d10["value"], d2["value"]
            elif src == "tencent":
                r = requests.get("https://qt.gtimg.cn/q=usTNX", timeout=to)
                r.raise_for_status()
                parts = r.text.split("~")
                y10 = _safe_float(parts[3]) if len(parts) > 3 else 0
                y2 = y10 * 0.85  # 粗略估算
                if y10 <= 0:
                    raise ValueError(f"Tencent TNX: invalid value {y10}")
            elif src == "akshare":
                ak = _get_ak()
                if ak is None:
                    raise ImportError("akshare not installed")
                df = ak.bond_zh_us_rate()
                if df is None or df.empty:
                    raise ValueError("akshare bond: empty data")
                last = df.iloc[-1]
                y10 = y2 = 0
                # 优先匹配美国国债列（避免误取中国国债收益率）
                for col in last.index:
                    cl = str(col)
                    if "美国" in cl and "10" in cl:
                        y10 = _safe_float(str(last[col]))
                    elif "美国" in cl and "2" in cl:
                        y2 = _safe_float(str(last[col]))
                # 回退: 不含"美国"但含"10Y"/"2Y"的列
                if y10 <= 0 or y2 <= 0:
                    for col in last.index:
                        cl = str(col).lower()
                        if "10" in cl and ("y" in cl or "美国" in cl) and y10 <= 0:
                            y10 = _safe_float(str(last[col]))
                        elif "2" in cl and ("y" in cl or "美国" in cl) and y2 <= 0:
                            y2 = _safe_float(str(last[col]))
                if y10 <= 0 or y2 <= 0:
                    raise ValueError(f"akshare bond: could not extract 10Y/2Y")
            elif src == "yfinance":
                yf = _get_yf()
                if yf is None:
                    raise ImportError("yfinance not installed")
                tnx_h = yf.Ticker("^TNX").history(period="5d")
                if tnx_h.empty:
                    raise ValueError("yfinance TNX: empty history")
                y10 = float(tnx_h["Close"].iloc[-1])
                y2 = y10 * 0.85
            else:
                continue

            if y10 <= 0 or y2 <= 0:
                continue
            spread = round(y10 - y2, 3)
            level, cn, en, signal = _yield_level(spread)
            logger.info("Yield Curve: 10Y=%.2f 2Y=%.2f spread=%.3f from %s", y10, y2, spread, src)
            return {
                "yield_10y": round(y10, 2), "yield_2y": round(y2, 2),
                "spread": spread, "change": 0, "level": level, "signal": signal,
                "interpretation": cn, "interpretation_en": en, "source": src,
            }
        except Exception as e:
            logger.warning("Yield Curve source %s failed: %s", src, e)
            continue
    return default


def fetch_gvz() -> Dict[str, Any]:
    default = {"value": 0, "change": 0, "level": "unknown", "interpretation": "数据获取失败", "interpretation_en": "Data fetch failed", "source": "N/A"}
    for i, src in enumerate(_SOURCE_PRIORITY["gvz"]):
        to = _FIRST_TIMEOUT if i == 0 else _FALLBACK_TIMEOUT
        try:
            if src == "sina":
                val = _try_sina_gvz(timeout=to)["value"]
            elif src == "eastmoney":
                val = _try_eastmoney("100.GVZ", timeout=to)["value"]
            elif src == "twelvedata":
                val = _try_twelvedata("GVZ", timeout=to)["value"]
            elif src == "yfinance":
                yf = _get_yf()
                if yf is None:
                    raise ImportError("yfinance not installed")
                h = yf.Ticker("^GVZ").history(period="5d")
                if h.empty:
                    raise ValueError("yfinance GVZ: empty history")
                val = float(h["Close"].iloc[-1])
            else:
                continue

            if val <= 0:
                continue
            level, cn, en = _gvz_level(val)
            return {"value": round(val, 2), "change": 0, "level": level, "interpretation": cn, "interpretation_en": en, "source": src}
        except Exception as e:
            logger.warning("GVZ source %s failed: %s", src, e)
            continue
    return default


def fetch_fear_greed_index() -> Dict[str, Any]:
    default = {"value": 50, "classification": "Neutral", "timestamp": 0, "source": "N/A"}
    for i, src in enumerate(_SOURCE_PRIORITY["fear_greed"]):
        to = _FIRST_TIMEOUT if i == 0 else _FALLBACK_TIMEOUT
        try:
            if src in ("altme", "alternative.me"):
                d = _try_altme_fear(timeout=to)
            elif src == "akshare_a_fear":
                ak = _get_ak()
                if ak is None:
                    raise ImportError("akshare not installed")
                df = ak.index_fear_greed()
                if df is None or df.empty:
                    raise ValueError("akshare fear_greed: empty data")
                val = int(df.iloc[-1]["fear_greed"])
                d = {"value": val, "classification": "见AkShare", "source": "akshare"}
            else:
                continue

            val = d.get("value", 0)
            if val <= 0:
                continue
            logger.info("Fear & Greed: %d from %s", val, d.get("source", src))
            return {
                "value": val, "classification": d.get("classification", ""),
                "timestamp": d.get("timestamp", 0), "source": d.get("source", src),
            }
        except Exception as e:
            logger.warning("Fear/Greed source %s failed: %s", src, e)
            continue
    return default


def fetch_put_call_ratio() -> Dict[str, Any]:
    """VIX 期限结构 (VIX/VIX3M 比值)"""
    default = {"value": 1.0, "vix": 0, "vix3m": 0, "change": 0, "level": "unknown", "signal": "neutral", "interpretation": "数据获取失败", "interpretation_en": "Data fetch failed", "source": "N/A"}
    for i, src in enumerate(_SOURCE_PRIORITY["vix_term"]):
        to = _FIRST_TIMEOUT if i == 0 else _FALLBACK_TIMEOUT
        vix_val = vix3m_val = 0.0
        used_source = src
        try:
            if src == "sina":
                r1 = requests.get("http://hq.sinajs.cn/list=int_vix", timeout=to)
                r1.raise_for_status()
                vix_val = _safe_float(r1.text.split(",")[2])
                if vix_val <= 0:
                    raise ValueError("Sina VIX: invalid")
                try:
                    vix3m_val = _try_eastmoney("100.VIX3M", timeout=to)["value"]
                    used_source = "sina+eastmoney"
                except Exception:
                    vix3m_val = vix_val * 0.85
                    used_source = "sina(estimated_vix3m)"

            elif src == "eastmoney":
                vix_val = _try_eastmoney("100.VIX", timeout=to)["value"]
                vix3m_val = _try_eastmoney("100.VIX3M", timeout=to)["value"]

            elif src == "twelvedata":
                vix_val = _try_twelvedata("VIX", timeout=to)["value"]
                vix3m_val = _try_twelvedata("VIX3M", timeout=to)["value"]

            elif src == "akshare":
                ak = _get_ak()
                if ak is None:
                    raise ImportError("akshare not installed")
                for fn_name in ("index_vix", "stock_us_vix", "index_vix_en"):
                    fn = getattr(ak, fn_name, None)
                    if fn:
                        df = fn()
                        if df is not None and not df.empty:
                            col = "close" if "close" in df.columns else df.columns[-1]
                            vix_val = float(df.iloc[-1][col])
                            break
                else:
                    raise ValueError("akshare: no VIX function found")
                vix3m_val = vix_val * 0.85
                used_source = "akshare(estimated_vix3m)"

            elif src == "yfinance":
                yf = _get_yf()
                if yf is None:
                    raise ImportError("yfinance not installed")
                h1 = yf.Ticker("^VIX").history(period="5d")
                h3 = yf.Ticker("^VIX3M").history(period="5d")
                if h1.empty or h3.empty:
                    raise ValueError("yfinance VIX/VIX3M: empty")
                vix_val = float(h1["Close"].iloc[-1])
                vix3m_val = float(h3["Close"].iloc[-1])
            else:
                continue

            if vix_val <= 0 or vix3m_val <= 0:
                continue

            ratio = vix_val / vix3m_val
            if ratio > 1.15:
                level, cn, en, signal = "high_fear", "VIX倒挂 - 短期恐慌情绪高涨", "Backwardation", "bearish"
            elif ratio > 1.0:
                level, cn, en, signal = "elevated", "轻度倒挂 - 市场谨慎", "Slight Backwardation", "neutral"
            elif ratio > 0.9:
                level, cn, en, signal = "normal", "正常结构 - 市场稳定", "Normal Structure", "neutral"
            elif ratio > 0.8:
                level, cn, en, signal = "complacent", "深度正价差 - 市场自满", "Deep Contango", "bullish"
            else:
                level, cn, en, signal = "extreme_complacency", "极度自满 - 警惕反转", "Extreme Complacency", "neutral"

            logger.info("VIX Term: ratio=%.3f VIX=%.2f VIX3M=%.2f from %s", ratio, vix_val, vix3m_val, used_source)
            return {
                "value": round(ratio, 3), "vix": round(vix_val, 2), "vix3m": round(vix3m_val, 2),
                "change": 0, "level": level, "signal": signal,
                "interpretation": cn, "interpretation_en": en, "source": used_source,
            }
        except Exception as e:
            logger.warning("VIX Term source %s failed: %s", src, e)
            continue
    return default

# ============================================================================
# 统一入口
# ============================================================================

def get_sentiment_data(timeout: int = 10) -> Dict[str, Any]:
    """返回全部 7 个指标，每个独立缓存 + 独立降级链。"""
    timeout = max(1, min(timeout, 60))

    fetchers = {
        "fear_greed":  fetch_fear_greed_index,
        "vix":         fetch_vix,
        "dxy":         fetch_dollar_index,
        "yield_curve": fetch_yield_curve,
        "vxn":         fetch_vxn,
        "gvz":         fetch_gvz,
        "vix_term":    fetch_put_call_ratio,
    }

    results: Dict[str, Any] = {}
    stale_keys = []

    for key in fetchers:
        cached = _get_cached_indicator(key)
        if cached is not None:
            results[key] = cached
        else:
            stale_keys.append(key)

    if stale_keys:
        logger.info("Fetching %d stale indicators: %s", len(stale_keys), stale_keys)
        with ThreadPoolExecutor(max_workers=min(len(stale_keys), 7)) as ex:
            futures = {ex.submit(fetchers[k]): k for k in stale_keys}
            try:
                for f in as_completed(futures, timeout=timeout):
                    key = futures[f]
                    try:
                        data = f.result(timeout=5)
                        results[key] = data
                        _set_cached_indicator(key, data)
                    except Exception as e:
                        logger.error("Failed to fetch %s: %s", key, e)
                        results[key] = None
            except Exception:
                logger.warning("Total timeout (%ss), %d/%d fetched", timeout, len(results), len(fetchers))
                for fut, key in futures.items():
                    if key not in results:
                        results[key] = None

    now = int(time.time())
    return {
        "fear_greed":  results.get("fear_greed")  or {"value": 50, "classification": "Neutral", "source": "default"},
        "vix":         results.get("vix")         or {"value": 0, "level": "unknown", "source": "default"},
        "dxy":         results.get("dxy")         or {"value": 0, "level": "unknown", "source": "default"},
        "yield_curve": results.get("yield_curve") or {"spread": 0, "level": "unknown", "source": "default"},
        "vxn":         results.get("vxn")         or {"value": 0, "level": "unknown", "source": "default"},
        "gvz":         results.get("gvz")         or {"value": 0, "level": "unknown", "source": "default"},
        "vix_term":    results.get("vix_term")    or {"value": 1.0, "level": "unknown", "source": "default"},
        "fetched_at":  now,
    }
