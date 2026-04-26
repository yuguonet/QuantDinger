"""
=============================================
市场情绪指标模块 v2 — 多源降级版 (修复版)
=============================================

每个指标 ≥4 个数据源，按实际响应速度排序：
    启动时探测所有源 → 按延迟排序 → 写入 _SOURCE_PRIORITY
    每个 fetcher 按优先级逐个尝试，失败自动降级

源优先级规则:
    1. 探测最快排最前（国内直连的源通常排前面）
    2. yfinance 固定排最后（垫底）
    3. akshare 固定排倒数第二

指标与 TTL:
    恐贪指数 (Fear & Greed)    4h
    VIX (CBOE 波动率)          5min
    VXN (纳斯达克波动率)        5min
    DXY (美元指数)              10min
    收益率曲线 (10Y-2Y)         10min
    GVZ (黄金波动率)            10min
    VIX 期限结构 (VIX/VIX3M)    5min

依赖:
    - requests   (必须)
    - akshare    (可选，倒数第二降级)
    - yfinance   (可选，最终降级)
"""
from __future__ import annotations

import time
import statistics
import os as _os
import json as _json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

from app.utils.logger import get_logger

logger = get_logger(__name__)

# ============================================================================
# 超时配置
# ============================================================================

_FIRST_TIMEOUT = 5       # 首个源的超时（秒）
_FALLBACK_TIMEOUT = 1.5  # 降级源的超时（秒，快速跳过）

# ============================================================================
# 数据源定义
# ============================================================================

@dataclass
class DataSource:
    """一个数据源的描述。"""
    name: str
    source_type: str        # "api" | "module"
    description: str
    latency_ms: float = 0.0
    available: bool = True
    priority: int = 99

    def __repr__(self):
        status = "✅" if self.available else "❌"
        return f"{status} {self.name} ({self.latency_ms:.0f}ms)"


# ============================================================================
# 源注册表
# ============================================================================

_PROBE_ENDPOINTS: Dict[str, Dict[str, Any]] = {
    "sina":       {"url": "http://hq.sinajs.cn/list=int_vix",           "timeout": 5},
    "tencent":    {"url": "https://qt.gtimg.cn/q=usVIX",                "timeout": 5},
    "eastmoney":  {"url": "https://push2.eastmoney.com/api/qt/stock/get?secid=100.VIX&fields=f43", "timeout": 5},
    "twelvedata": {"url": "https://api.twelvedata.com/quote?symbol=VIX&apikey=test", "timeout": 5},
    "altme":      {"url": "https://api.alternative.me/fng/?limit=1",    "timeout": 5},
    "fx678":      {"url": "https://www.fx678.com/",                     "timeout": 5},
    "akshare":    {"url": None, "timeout": 0},
    "yfinance":   {"url": None, "timeout": 0},
}

_INDICATOR_SOURCES: Dict[str, List[str]] = {
    "fear_greed":  ["altme", "coinglass_scrape", "akshare_a_fear", "self_built"],
    "vix":         ["sina", "tencent", "eastmoney", "twelvedata", "akshare", "yfinance"],
    "vxn":         ["sina", "tencent", "eastmoney", "twelvedata", "akshare", "yfinance"],
    "dxy":         ["sina", "eastmoney", "twelvedata", "tencent", "fx678", "akshare", "yfinance"],
    "yield_curve": ["twelvedata", "sina", "eastmoney", "tencent", "akshare", "yfinance"],
    "gvz":         ["sina", "eastmoney", "twelvedata", "akshare", "yfinance"],
    "vix_term":    ["sina", "eastmoney", "twelvedata", "akshare", "yfinance"],
}

_INDICATOR_CACHE_TTL: Dict[str, int] = {
    "fear_greed":   14400,   # 4h
    "vix":          300,     # 5min
    "dxy":          600,     # 10min
    "yield_curve":  600,     # 10min
    "vxn":          300,     # 5min
    "gvz":          600,     # 10min
    "vix_term":     300,     # 5min
}

_source_descriptions: Dict[str, str] = {
    "sina":       "新浪财经 API (hq.sinajs.cn) — 国内直连",
    "tencent":    "腾讯财经 API (qt.gtimg.cn) — 国内直连",
    "eastmoney":  "东方财富 API (push2.eastmoney.com) — 国内直连",
    "twelvedata": "Twelve Data API — 国内可连，免费 800次/天",
    "altme":      "alternative.me — 加密恐贪指数 API",
    "coinglass_scrape": "CoinGlass 爬取（未实现）",
    "akshare_a_fear": "AkShare A股恐贪指数",
    "self_built": "自建恐贪（未实现）",
    "fx678":      "汇通网 (fx678.com) — 外汇数据",
    "akshare":    "AkShare Python 库 — 国内友好，覆盖广",
    "yfinance":   "yfinance Python 库 — 最后降级，国内可能不稳定",
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
            if not _ak_loaded:  # double-checked locking
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
# 速度探测
# ============================================================================

def _probe_source(name: str) -> Tuple[str, float, bool]:
    """探测单个源的可达性和延迟，返回 (name, latency_ms, available)。"""
    ep = _PROBE_ENDPOINTS.get(name)
    if ep is None or ep.get("url") is None:
        return (name, 5000.0, True)

    url = ep["url"]
    timeout = ep.get("timeout", 5)
    latencies = []

    for _ in range(3):  # 测 3 次取中位数
        try:
            t0 = time.monotonic()
            r = requests.get(url, timeout=timeout)
            elapsed = (time.monotonic() - t0) * 1000
            if r.status_code < 500:
                latencies.append(elapsed)
            else:
                latencies.append(timeout * 1000)
        except Exception:
            latencies.append(timeout * 1000)

    median_ms = statistics.median(latencies) if latencies else 99999
    available = median_ms < (timeout * 1000 * 0.9)
    return (name, median_ms, available)


def probe_all_sources() -> Dict[str, DataSource]:
    """并行探测所有源，返回 {name: DataSource}。"""
    results: Dict[str, DataSource] = {}

    logger.info("Probing all data sources...")
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_probe_source, name): name for name in _PROBE_ENDPOINTS}
        for f in as_completed(futures, timeout=20):
            name = futures[f]
            try:
                _, latency, avail = f.result()
            except Exception:
                latency, avail = 99999, False

            ds = DataSource(
                name=name,
                source_type="api" if _PROBE_ENDPOINTS[name].get("url") else "module",
                description=_source_descriptions.get(name, ""),
                latency_ms=latency,
                available=avail,
            )
            results[name] = ds
            logger.info("  %s: %.0fms %s", name, latency, "✅" if avail else "❌")

    if "akshare" in results:
        results["akshare"].priority = 90
    if "yfinance" in results:
        results["yfinance"].priority = 99

    return results


# ============================================================================
# 源优先级管理
# ============================================================================

_SOURCE_PRIORITY: Dict[str, List[str]] = {}


def build_source_priority(probe_results: Dict[str, DataSource]) -> None:
    global _SOURCE_PRIORITY

    for indicator, sources in _INDICATOR_SOURCES.items():
        scored: List[Tuple[str, float]] = []
        for src in sources:
            ds = probe_results.get(src)
            if ds is None:
                scored.append((src, 88888))
            elif not ds.available:
                scored.append((src, 99999))
            else:
                scored.append((src, ds.latency_ms))

        final: List[Tuple[str, float]] = []
        for src, lat in scored:
            if src == "akshare":
                final.append((src, 90000))
            elif src == "yfinance":
                final.append((src, 99999))
            else:
                final.append((src, lat))

        final.sort(key=lambda x: x[1])
        _SOURCE_PRIORITY[indicator] = [s for s, _ in final]

    logger.info("Source priority built:")
    for ind, srcs in _SOURCE_PRIORITY.items():
        logger.info("  %s: %s", ind, " → ".join(srcs))


def get_source_priority(indicator: str) -> List[str]:
    return _SOURCE_PRIORITY.get(indicator, _INDICATOR_SOURCES.get(indicator, []))


def get_data_source_table() -> Dict[str, Any]:
    table = {}
    for indicator in _INDICATOR_SOURCES:
        srcs = get_source_priority(indicator)
        entries = []
        for i, src in enumerate(srcs):
            ds = _probe_cache.get(src)
            entries.append({
                "rank": i + 1,
                "name": src,
                "description": _source_descriptions.get(src, ""),
                "latency_ms": round(ds.latency_ms, 0) if ds else "N/A",
                "available": ds.available if ds else False,
            })
        table[indicator] = entries
    return table


# ============================================================================
# 探测结果缓存
# ============================================================================

_probe_cache: Dict[str, DataSource] = {}

# 探测结果文件缓存 — 避免每次重启都重新探测所有源
_PROBE_CACHE_DIR = None
_PROBE_CACHE_MAX_AGE = 86400  # 探测结果有效期 1 天


def _probe_cache_dir():
    global _PROBE_CACHE_DIR
    if _PROBE_CACHE_DIR is None:
        _PROBE_CACHE_DIR = _os.path.join(_os.getcwd(), "data", "market_cn_cache")
        _os.makedirs(_PROBE_CACHE_DIR, exist_ok=True)
    return _PROBE_CACHE_DIR


def _probe_cache_path():
    return _os.path.join(_probe_cache_dir(), "sentiment_probe_cache.json")


def _load_probe_cache():
    """从文件加载探测结果，过期返回 None。"""
    path = _probe_cache_path()
    if not _os.path.exists(path):
        return None
    try:
        mtime = _os.path.getmtime(path)
        if time.time() - mtime > _PROBE_CACHE_MAX_AGE:
            return None
        with open(path, "r", encoding="utf-8") as f:
            raw = _json.load(f)
        result = {}
        for name, ds_dict in raw.items():
            result[name] = DataSource(
                name=ds_dict["name"],
                source_type=ds_dict.get("source_type", "api"),
                description=ds_dict.get("description", ""),
                latency_ms=ds_dict.get("latency_ms", 5000),
                available=ds_dict.get("available", False),
                priority=ds_dict.get("priority", 99),
            )
        logger.info("Loaded probe cache from file (%d sources, age %.0fmin)",
                     len(result), (time.time() - mtime) / 60)
        return result
    except Exception as e:
        logger.warning("Failed to load probe cache: %s", e)
        return None


def _save_probe_cache(probe_results: Dict[str, DataSource]):
    """将探测结果写入文件缓存。"""
    path = _probe_cache_path()
    try:
        raw = {}
        for name, ds in probe_results.items():
            raw[name] = {
                "name": ds.name,
                "source_type": ds.source_type,
                "description": ds.description,
                "latency_ms": ds.latency_ms,
                "available": ds.available,
                "priority": ds.priority,
            }
        tmp_path = f"{path}.tmp.{_os.getpid()}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            _json.dump(raw, f, ensure_ascii=False, indent=2)
        _os.replace(tmp_path, path)
        logger.info("Saved probe cache to file (%d sources)", len(raw))
    except Exception as e:
        logger.warning("Failed to save probe cache: %s", e)


def init_sources() -> None:
    """初始化数据源优先级：优先读文件缓存，过期或缺失才重新探测。"""
    global _probe_cache

    cached = _load_probe_cache()
    if cached and len(cached) >= 5:
        _probe_cache = cached
        build_source_priority(_probe_cache)
        logger.info("Data sources initialized from file cache.")
        # 后台异步刷新探测结果
        def _bg_refresh():
            try:
                fresh = probe_all_sources()
                if fresh and len(fresh) >= 5:
                    _probe_cache.update(fresh)
                    build_source_priority(_probe_cache)
                    _save_probe_cache(_probe_cache)
                    logger.info("Probe cache refreshed in background.")
            except Exception as e:
                logger.warning("Background probe refresh failed: %s", e)
        import threading
        threading.Thread(target=_bg_refresh, daemon=True).start()
        return

    # 无缓存：同步探测
    _probe_cache = probe_all_sources()
    build_source_priority(_probe_cache)
    _save_probe_cache(_probe_cache)
    logger.info("Data sources initialized (fresh probe).")


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
        ttl = _INDICATOR_CACHE_TTL.get(name, 300)
        if data is not None and (time.time() - ts) < ttl:
            return data
    except Exception as e:
        logger.warning("Cache read failed for %s: %s", name, e)
    return None


def _set_cached_indicator(name: str, data: Dict[str, Any]) -> None:
    try:
        cm = _cache()
        ttl = _INDICATOR_CACHE_TTL.get(name, 300)
        cm.set(f"{_CK}{name}", {"data": data, "ts": int(time.time())}, ttl=ttl * 2)
    except Exception as e:
        logger.warning("Cache write failed for %s: %s", name, e)


# ============================================================================
# 通用 HTTP 工具
# ============================================================================

def _safe_float(text: str, default: float = 0.0) -> float:
    """安全地将字符串转为 float，失败返回 default。"""
    try:
        val = float(text.strip())
        return val if val == val else default  # NaN 检查
    except (ValueError, TypeError, AttributeError):
        return default


def _safe_get_json(url: str, params: Optional[Dict] = None, timeout: int = 5) -> Dict[str, Any]:
    """安全 HTTP GET，返回 JSON 或空 dict。"""
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
        raise ValueError(f"Sina VIX: unexpected response format ({len(parts)} fields)")
    val = _safe_float(parts[2])
    if val <= 0:
        raise ValueError(f"Sina VIX: invalid value {val}")
    return {"value": val, "source": "sina"}


def _try_sina_vxn(timeout: float = 5) -> Dict[str, Any]:
    r = requests.get("http://hq.sinajs.cn/list=int_vxn", timeout=timeout)
    r.raise_for_status()
    parts = r.text.split(",")
    if len(parts) < 3:
        raise ValueError(f"Sina VXN: unexpected response format")
    val = _safe_float(parts[2])
    if val <= 0:
        raise ValueError(f"Sina VXN: invalid value {val}")
    return {"value": val, "source": "sina"}


def _try_sina_gvz(timeout: float = 5) -> Dict[str, Any]:
    r = requests.get("http://hq.sinajs.cn/list=int_gvz", timeout=timeout)
    r.raise_for_status()
    parts = r.text.split(",")
    if len(parts) < 3:
        raise ValueError(f"Sina GVZ: unexpected response format")
    val = _safe_float(parts[2])
    if val <= 0:
        raise ValueError(f"Sina GVZ: invalid value {val}")
    return {"value": val, "source": "sina"}


def _try_sina_dxy(timeout: float = 5) -> Dict[str, Any]:
    r = requests.get("http://hq.sinajs.cn/list=fx_susdind", timeout=timeout)
    r.raise_for_status()
    parts = r.text.split(",")
    if len(parts) < 2:
        raise ValueError(f"Sina DXY: unexpected response format")
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
        raise ValueError(f"Sina Yield: invalid values y2={y2} y10={y10}")
    return {"yield_10y": y10, "yield_2y": y2, "spread": round(y10 - y2, 3), "source": "sina"}


# ============================================================================
# 腾讯系列
# ============================================================================

def _parse_tencent_vix_response(text: str, label: str) -> float:
    """解析腾讯行情返回的 ~ 分隔字段，取价格字段。"""
    parts = text.split("~")
    if len(parts) < 5:
        raise ValueError(f"Tencent {label}: unexpected format ({len(parts)} fields)")
    val = _safe_float(parts[3])
    if val <= 0:
        raise ValueError(f"Tencent {label}: invalid value {val}")
    return val


def _try_tencent_vix(timeout: float = 5) -> Dict[str, Any]:
    r = requests.get("https://qt.gtimg.cn/q=usVIX", timeout=timeout)
    r.raise_for_status()
    val = _parse_tencent_vix_response(r.text, "VIX")
    return {"value": val, "source": "tencent"}


def _try_tencent_vxn(timeout: float = 5) -> Dict[str, Any]:
    r = requests.get("https://qt.gtimg.cn/q=usVXN", timeout=timeout)
    r.raise_for_status()
    val = _parse_tencent_vix_response(r.text, "VXN")
    return {"value": val, "source": "tencent"}


def _try_tencent_dxy(timeout: float = 5) -> Dict[str, Any]:
    r = requests.get("https://qt.gtimg.cn/q=fx_susdind", timeout=timeout)
    r.raise_for_status()
    val = _parse_tencent_vix_response(r.text, "DXY")
    return {"value": val, "source": "tencent"}


# ============================================================================
# 东方财富系列
# ============================================================================

def _try_eastmoney(symbol: str, timeout: float = 5) -> Dict[str, Any]:
    r = requests.get(
        "https://push2.eastmoney.com/api/qt/stock/get",
        params={"secid": symbol, "fields": "f43,f44,f45,f46,f170,f171"},
        timeout=timeout
    )
    r.raise_for_status()
    data = r.json().get("data")
    if not data:
        raise ValueError(f"Eastmoney {symbol}: empty data in response")
    raw_val = data.get("f43")
    if raw_val is None or raw_val == "":
        raise ValueError(f"Eastmoney {symbol}: f43 field missing")
    # f43 通常是放大100倍的整数（如 VIX=2015 表示 20.15），但也可能是直接值
    val = _safe_float(str(raw_val))
    if val > 1000:
        val = val / 100  # 按东财惯例还原
    if val <= 0:
        raise ValueError(f"Eastmoney {symbol}: invalid value {val}")
    return {"value": val, "source": "eastmoney"}


# ============================================================================
# Twelve Data 系列
# ============================================================================

_TD_KEY: str = ""


def set_twelvedata_key(key: str) -> None:
    """设置 Twelve Data API key。"""
    global _TD_KEY
    _TD_KEY = key


def _get_td_key() -> str:
    return _TD_KEY


def _try_twelvedata(symbol: str, api_key: str = "", timeout: float = 8) -> Dict[str, Any]:
    key = api_key or _get_td_key()
    if not key:
        raise ValueError("TwelveData: API key not set (call set_twelvedata_key())")
    d = _safe_get_json(
        "https://api.twelvedata.com/quote",
        params={"symbol": symbol, "apikey": key},
        timeout=timeout
    )
    if not d or "close" not in d:
        msg = d.get("message", "unknown error") if d else "empty response"
        raise ValueError(f"TwelveData {symbol}: {msg}")
    val = _safe_float(d["close"])
    if val <= 0:
        raise ValueError(f"TwelveData {symbol}: invalid value {val}")
    return {"value": val, "source": "twelvedata"}


def _try_twelvedata_yield(api_key: str = "", timeout: float = 8) -> Dict[str, Any]:
    key = api_key or _get_td_key()
    d10 = _try_twelvedata("US10Y", key, timeout=timeout)
    d2 = _try_twelvedata("US2Y", key, timeout=timeout)
    y10 = d10["value"]
    y2 = d2["value"]
    return {"yield_10y": y10, "yield_2y": y2, "spread": round(y10 - y2, 3), "source": "twelvedata"}


# ============================================================================
# alternative.me
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

def _compute_change(current: float, prev: Optional[float]) -> float:
    if prev and prev != 0:
        return round(((current - prev) / prev) * 100, 2)
    return 0.0


def _vix_level(val: float) -> Tuple[str, str, str]:
    if val < 12:
        return "very_low", "极低波动 - 市场极度乐观", "Very Low - Extreme Optimism"
    elif val < 20:
        return "low", "低波动 - 市场稳定", "Low - Market Stable"
    elif val < 25:
        return "moderate", "中等波动 - 正常水平", "Moderate - Normal Level"
    elif val < 30:
        return "high", "高波动 - 市场担忧", "High - Market Concern"
    else:
        return "very_high", "极高波动 - 市场恐慌", "Very High - Market Panic"


def _dxy_level(val: float) -> Tuple[str, str, str]:
    if val > 105:
        return "strong", "美元强势 - 利空大宗商品/新兴市场", "Strong USD - Bearish commodities/EM"
    elif val > 100:
        return "moderate_strong", "美元偏强 - 关注资金流向", "Moderately Strong"
    elif val > 95:
        return "neutral", "美元中性 - 市场均衡", "Neutral"
    elif val > 90:
        return "moderate_weak", "美元偏弱 - 利多风险资产", "Moderately Weak"
    else:
        return "weak", "美元疲软 - 利多黄金/大宗商品", "Weak USD"


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
    for i, src in enumerate(get_source_priority("vix")):
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
                df = ak.index_vix()
                val = float(df.iloc[-1]["close"])
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
    for i, src in enumerate(get_source_priority("vxn")):
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
            elif src == "akshare":
                raise NotImplementedError("akshare has no VXN interface — skipping to next source")
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
    for i, src in enumerate(get_source_priority("dxy")):
        to = _FIRST_TIMEOUT if i == 0 else _FALLBACK_TIMEOUT
        try:
            if src == "sina":
                val = _try_sina_dxy(timeout=to)["value"]
            elif src == "eastmoney":
                val = _try_eastmoney("133.USDX", timeout=to)["value"]
            elif src == "twelvedata":
                val = _try_twelvedata("USD/IDX", timeout=to)["value"]
            elif src == "tencent":
                val = _try_tencent_dxy(timeout=to)["value"]
            elif src == "fx678":
                raise NotImplementedError("fx678 scraping not yet implemented")
            elif src == "akshare":
                ak = _get_ak()
                if ak is None:
                    raise ImportError("akshare not installed")
                df = ak.futures_foreign_hist(symbol="DINI")
                if df is None or df.empty:
                    raise ValueError("akshare DXY: empty data")
                val = float(df.iloc[-1]["close"])
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
    for i, src in enumerate(get_source_priority("yield_curve")):
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
                    raise ValueError("akshare bond_zh_us_rate: empty data")
                last = df.iloc[-1]
                # akshare bond_zh_us_rate 返回的列名可能因版本不同而变化
                # 常见列名: 中国:国债收益率:10年, 中国:国债收益率:2年 或 10Y, 2Y
                y10 = 0
                y2 = 0
                for col in last.index:
                    col_lower = str(col).lower()
                    if "10" in col_lower and ("年" in col_lower or "y" in col_lower):
                        y10 = _safe_float(str(last[col]))
                    elif "2" in col_lower and ("年" in col_lower or "y" in col_lower):
                        y2 = _safe_float(str(last[col]))
                if y10 <= 0 or y2 <= 0:
                    raise ValueError(f"akshare bond: could not extract 10Y/2Y from columns {list(df.columns)}")
            elif src == "yfinance":
                yf = _get_yf()
                if yf is None:
                    raise ImportError("yfinance not installed")
                tnx_h = yf.Ticker("^TNX").history(period="5d")
                if tnx_h.empty:
                    raise ValueError("yfinance TNX: empty history")
                y10 = float(tnx_h["Close"].iloc[-1])
                y2 = y10 * 0.85  # 粗略估算（yfinance 无稳定 2Y ticker）
            else:
                continue

            if y10 <= 0 or y2 <= 0:
                continue
            spread = round(y10 - y2, 3)
            level, cn, en, signal = _yield_level(spread)
            logger.info("Yield Curve: 10Y=%.2f 2Y=%.2f spread=%.3f from %s", y10, y2, spread, src)
            return {
                "yield_10y": round(y10, 2), "yield_2y": round(y2, 2),
                "spread": spread, "change": 0,
                "level": level, "signal": signal,
                "interpretation": cn, "interpretation_en": en,
                "source": src,
            }
        except Exception as e:
            logger.warning("Yield Curve source %s failed: %s", src, e)
            continue
    return default


def fetch_gvz() -> Dict[str, Any]:
    default = {"value": 0, "change": 0, "level": "unknown", "interpretation": "数据获取失败", "interpretation_en": "Data fetch failed", "source": "N/A"}
    for i, src in enumerate(get_source_priority("gvz")):
        to = _FIRST_TIMEOUT if i == 0 else _FALLBACK_TIMEOUT
        try:
            if src == "sina":
                val = _try_sina_gvz(timeout=to)["value"]
            elif src == "eastmoney":
                val = _try_eastmoney("100.GVZ", timeout=to)["value"]
            elif src == "twelvedata":
                val = _try_twelvedata("GVZ", timeout=to)["value"]
            elif src == "akshare":
                raise NotImplementedError("akshare has no GVZ interface")
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
    for i, src in enumerate(get_source_priority("fear_greed")):
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
            elif src == "coinglass_scrape":
                raise NotImplementedError("CoinGlass scraping not yet implemented")
            elif src == "self_built":
                raise NotImplementedError("Self-built fear/greed not yet implemented")
            else:
                continue

            val = d.get("value", 0)
            if val <= 0:
                continue
            logger.info("Fear & Greed: %d from %s", val, d.get("source", src))
            return {
                "value": val,
                "classification": d.get("classification", ""),
                "timestamp": d.get("timestamp", 0),
                "source": d.get("source", src),
            }
        except Exception as e:
            logger.warning("Fear/Greed source %s failed: %s", src, e)
            continue
    return default


def fetch_put_call_ratio() -> Dict[str, Any]:
    default = {"value": 1.0, "vix": 0, "vix3m": 0, "change": 0, "level": "unknown", "signal": "neutral", "interpretation": "数据获取失败", "interpretation_en": "Data fetch failed", "source": "N/A"}

    for i, src in enumerate(get_source_priority("vix_term")):
        to = _FIRST_TIMEOUT if i == 0 else _FALLBACK_TIMEOUT
        vix_val = 0.0
        vix3m_val = 0.0
        used_source = src
        try:
            if src == "sina":
                r1 = requests.get("http://hq.sinajs.cn/list=int_vix", timeout=to)
                r1.raise_for_status()
                vix_val = _safe_float(r1.text.split(",")[2])
                if vix_val <= 0:
                    raise ValueError(f"Sina VIX for term structure: invalid value")
                # VIX3M — 尝试东财
                try:
                    vix3m_val = _try_eastmoney("100.VIX3M", timeout=to)["value"]
                    used_source = "sina+eastmoney"
                except Exception:
                    vix3m_val = vix_val * 0.85  # 估算 fallback
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
                df = ak.index_vix()
                if df is None or df.empty:
                    raise ValueError("akshare VIX term: empty data")
                vix_val = float(df.iloc[-1]["close"])
                vix3m_val = vix_val * 0.85
                used_source = "akshare(estimated_vix3m)"

            elif src == "yfinance":
                yf = _get_yf()
                if yf is None:
                    raise ImportError("yfinance not installed")
                h1 = yf.Ticker("^VIX").history(period="5d")
                h3 = yf.Ticker("^VIX3M").history(period="5d")
                if h1.empty or h3.empty:
                    raise ValueError("yfinance VIX/VIX3M: empty history")
                vix_val = float(h1["Close"].iloc[-1])
                vix3m_val = float(h3["Close"].iloc[-1])
            else:
                continue

            if vix_val <= 0 or vix3m_val <= 0:
                logger.warning("VIX Term source %s: invalid values vix=%s vix3m=%s", src, vix_val, vix3m_val)
                continue

            ratio = vix_val / vix3m_val

            if ratio > 1.15:
                level, cn, en, signal = "high_fear", "VIX倒挂 - 短期恐慌情绪高涨", "Backwardation - High fear", "bearish"
            elif ratio > 1.0:
                level, cn, en, signal = "elevated", "轻度倒挂 - 市场谨慎", "Slight Backwardation", "neutral"
            elif ratio > 0.9:
                level, cn, en, signal = "normal", "正常结构 - 市场稳定", "Normal Structure", "neutral"
            elif ratio > 0.8:
                level, cn, en, signal = "complacent", "深度正价差 - 市场自满", "Deep Contango - Complacent", "bullish"
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
    timeout = max(1, min(timeout, 60))  # clamp 1~60s

    # 确保源优先级已初始化（首次调用时自动探测）
    if not _SOURCE_PRIORITY:
        try:
            init_sources()
        except Exception as e:
            logger.warning("init_sources() failed, using default order: %s", e)

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
                logger.warning("Total timeout (%ss), %d/%d indicators fetched",
                               timeout, len(results), len(fetchers))
                for fut, key in futures.items():
                    if key not in results:
                        results[key] = None
            # 注意: Python ThreadPoolExecutor 无法强制终止线程，
            # 未完成的线程会在内部 timeout 后自行退出，不会永久泄漏。

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


# ============================================================================
# 数据源表输出
# ============================================================================

def print_source_table() -> str:
    lines = []
    lines.append("=" * 90)
    lines.append(f"{'指标':<16} {'排序':<4} {'数据源':<16} {'延迟(ms)':<10} {'状态':<6} {'说明'}")
    lines.append("-" * 90)

    for indicator in _INDICATOR_SOURCES:
        srcs = get_source_priority(indicator)
        for i, src in enumerate(srcs):
            ds = _probe_cache.get(src)
            lat = f"{ds.latency_ms:.0f}" if ds else "N/A"
            status = "✅" if (ds and ds.available) else "❌"
            desc = _source_descriptions.get(src, "")
            tag = ""
            if i == 0:
                tag = "←主力"
            elif src == "akshare":
                tag = "←倒二"
            elif src == "yfinance":
                tag = "←垫底"
            lines.append(f"{indicator if i == 0 else '':<16} {i+1:<4} {src:<16} {lat:<10} {status:<6} {desc} {tag}")
        lines.append("-" * 90)

    return "\n".join(lines)
