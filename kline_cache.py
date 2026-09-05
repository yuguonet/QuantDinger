"""
kline_cache - 从本地 kline_data.json 读取K线数据
如果本地没有缓存, 则实时从腾讯API下载
"""
import json, os, requests

_CACHE = None
_CACHE_PATH = os.path.join(os.path.dirname(__file__), "kline_data.json")

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})

def _load_cache():
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    if os.path.exists(_CACHE_PATH):
        with open(_CACHE_PATH, "r", encoding="utf-8") as f:
            _CACHE = json.load(f)
        return _CACHE
    _CACHE = {}
    return _CACHE

def _code_to_tencent(code):
    c = code.strip().replace(".", "").replace("SH", "").replace("SZ", "")
    if c.startswith(("6", "5")): return f"sh{c}"
    elif c.startswith(("0", "3", "2")): return f"sz{c}"
    elif c.startswith("68"): return f"sh{c}"
    return ""

def _fetch_remote(code, count=300):
    tc = _code_to_tencent(code)
    if not tc: return []
    try:
        resp = _SESSION.get(
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
            params={"param": f"{tc},day,,,{count},qfq"},
            headers={"Referer": "https://gu.qq.com/"}, timeout=10,
        )
        data = resp.json()
        if not isinstance(data, dict) or int(data.get("code", 0)) != 0: return []
        root = (data.get("data") or {}).get(tc)
        if not isinstance(root, dict): return []
        rows = root.get("qfqday") or root.get("day") or []
        bars = []
        for r in rows:
            if not isinstance(r, (list, tuple)) or len(r) < 6: continue
            try:
                bars.append({
                    "time": str(r[0])[:10], "open": float(r[1]),
                    "high": float(r[3]), "low": float(r[4]),
                    "close": float(r[2]), "volume": float(r[5]) * 100,
                })
            except: continue
        bars.sort(key=lambda x: x["time"])
        return bars
    except:
        return []

def _fetch_sina(code, count=500):
    """Sina K线API (备用源)"""
    c = code.strip().replace(".", "").replace("SH", "").replace("SZ", "")
    if c.startswith(("6", "5")): sym = f"sh{c}"
    elif c.startswith(("0", "3", "2")): sym = f"sz{c}"
    elif c.startswith("68"): sym = f"sh{c}"
    else: return []
    try:
        resp = _SESSION.get(
            "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
            params={"symbol": sym, "scale": "240", "ma": "no", "datalen": str(count)},
            timeout=10,
        )
        rows = json.loads(resp.text)
        bars = [{"time": r["day"][:10], "open": float(r["open"]), "high": float(r["high"]),
                 "low": float(r["low"]), "close": float(r["close"]), "volume": float(r["volume"])} for r in rows]
        bars.sort(key=lambda x: x["time"])
        return bars
    except:
        return []

def fetch_kline(code, count=300):
    """获取K线: 本地缓存 → 腾讯API → SinaAPI"""
    cache = _load_cache()
    code_clean = code.strip().replace(".", "").replace("SH", "").replace("SZ", "")
    
    if code_clean in cache:
        bars = cache[code_clean]
        if count and count < len(bars):
            return bars[-count:]
        return bars
    
    # 缓存没有, 腾讯API
    bars = _fetch_remote(code_clean, count)
    # 腾讯没拿到, 走Sina
    if not bars:
        bars = _fetch_sina(code_clean, count)
    if bars:
        cache[code_clean] = bars
    return bars
