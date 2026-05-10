#!/usr/bin/env python3
"""
A股全市场15min K线 — 极速并发拉取
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
push2.eastmoney.com trends2 → 1分钟数据聚合为15min (16bar)
其它源: tencent / sina / baidu / sohu / xueqiu / ...
纯标准库
"""

import csv, json, os, re, ssl, sys, time, random, threading
import urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from queue import Queue, Empty

# TDX (通达信) 数据源
try:
    from pytdx.hq import TdxHq_API
    from pytdx.exhq import TdxExHq_API
    HAS_TDX = True
except ImportError:
    HAS_TDX = False

# ═══════════════ 配置 ═══════════════
TIMEOUT = 10
OUTPUT_DIR = "kline_data"
GROUP_SIZE = 100
THREADS_PER_SOURCE = 3       # 默认线程数 (极速源用)
PER_DOMAIN_CONCURRENT = 50    # push2域名并发上限
PER_DOMAIN_INTERVAL = 0.01    # push2域名最小间隔

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36", "Accept": "*/*"}
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

# ═══════════════ Referer 池 ═══════════════
UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
]

def _rand_ua():
    return random.choice(UA_POOL)

REFERER_POOL = [
    "https://finance.sina.com.cn/",
    "https://gu.qq.com/",
    "https://finance.baidu.com/",
    "https://gushitong.baidu.com/",
    "https://q.stock.sohu.com/",
    "https://xueqiu.com/",
    "https://www.eastmoney.com/",
    "https://quote.eastmoney.com/",
    "https://stockpage.10jqka.com.cn/",
    "https://www.10jqka.com.cn/",
    "https://finance.ifeng.com/",
    "https://money.163.com/",
    "https://stock.hexun.com/",
    "https://www.cls.cn/",
]

def _rand_referer(domain_hint=""):
    """根据域名提示返回最佳 Referer, 否则随机"""
    hint = domain_hint.lower()
    if "gtimg" in hint or "qq" in hint:
        return "https://gu.qq.com/"
    if "sina" in hint:
        return "https://finance.sina.com.cn/"
    if "baidu" in hint:
        return "https://gushitong.baidu.com/"
    if "sohu" in hint:
        return "https://q.stock.sohu.com/"
    if "xueqiu" in hint:
        return "https://xueqiu.com/"
    if "eastmoney" in hint:
        return "https://quote.eastmoney.com/"
    return random.choice(REFERER_POOL)

# ═══════════════ Cookie 管理器 ═══════════════
class CookieJar:
    """按域名管理 Cookie, 支持预热(先访问首页拿 cookie)"""
    def __init__(self):
        self._jar = {}       # domain -> cookie_str
        self._lock = threading.Lock()
        self._warmed = set()

    def get(self, domain):
        with self._lock:
            return self._jar.get(domain, "")

    def set(self, domain, cookie):
        with self._lock:
            old = self._jar.get(domain, "")
            # 合并: 新cookie覆盖旧的同名key
            merged = self._merge(old, cookie)
            self._jar[domain] = merged

    def _merge(self, old, new):
        """简单合并 Set-Cookie: 同名key取新值"""
        if not old: return new
        if not new: return old
        old_d = {}
        for part in old.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                old_d[k.strip()] = v.strip()
        for part in new.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                old_d[k.strip()] = v.strip()
        return "; ".join(f"{k}={v}" for k, v in old_d.items())

    def warm(self, domain, url):
        """预热: GET 访问 url, 收集 Set-Cookie"""
        with self._lock:
            if domain in self._warmed:
                return self._jar.get(domain, "")
        try:
            h = {**HEADERS, "Referer": _rand_referer(domain)}
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=5, context=SSL_CTX) as resp:
                cookies = []
                for k, v in resp.headers.items():
                    if k.lower() == "set-cookie":
                        cookies.append(v.split(";")[0])
                if cookies:
                    self.set(domain, "; ".join(cookies))
        except: pass
        with self._lock:
            self._warmed.add(domain)
        return self.get(domain)

cookie_jar = CookieJar()

# ═══════════════ 域名限流 ═══════════════
class DomainThrottler:
    def __init__(self, max_c=50, interval=0.01):
        self._sems, self._last, self._max, self._interval, self._lock = {}, {}, max_c, interval, threading.Lock()
    def _domain(self, url):
        m = re.search(r'https?://([^/]+)', url)
        return m.group(1) if m else url
    def _sem(self, d):
        with self._lock:
            if d not in self._sems: self._sems[d] = threading.Semaphore(self._max)
            return self._sems[d]
    def acquire(self, url):
        d = self._domain(url); self._sem(d).acquire()
        wait = 0
        with self._lock:
            wait = max(0, self._interval - (time.time() - self._last.get(d, 0)))
            self._last[d] = time.time() + wait
        if wait > 0: time.sleep(wait)
    def release(self, url):
        self._sem(self._domain(url)).release()

throttler = DomainThrottler(PER_DOMAIN_CONCURRENT, PER_DOMAIN_INTERVAL)

# ═══════════════ 源统计 ═══════════════
class SourceStats:
    def __init__(self, name):
        self.name, self.done, self.ok, self.fail = name, 0, 0, 0
        self.start_time, self.lock = time.time(), threading.Lock()
        self.groups_done = 0
    def record(self, success):
        with self.lock:
            self.done += 1
            if success: self.ok += 1
            else: self.fail += 1
    def speed(self):
        e = time.time() - self.start_time
        return self.ok / e if e >= 1 else 0

# ═══════════════ HTTP ═══════════════
# 极速源用持久opener (连接复用), 其它源走限流器
_fast_opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=SSL_CTX))

def http_get(url, headers=None, timeout=TIMEOUT, referer=None, use_cookie=True):
    h = {**HEADERS, **(headers or {})}
    # 轮换 User-Agent
    h["User-Agent"] = _rand_ua()
    # 自动注入 Referer
    if referer:
        h["Referer"] = referer
    elif "Referer" not in h:
        h["Referer"] = _rand_referer(url)
    # 自动注入 Cookie
    if use_cookie:
        m = re.search(r'https?://([^/]+)', url)
        domain = m.group(1) if m else ""
        ck = cookie_jar.get(domain)
        if ck:
            h["Cookie"] = ck
    throttler.acquire(url)
    try:
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
            # 收集 Set-Cookie
            if use_cookie:
                m2 = re.search(r'https?://([^/]+)', url)
                dom = m2.group(1) if m2 else ""
                new_cookies = []
                for k, v in r.headers.items():
                    if k.lower() == "set-cookie":
                        new_cookies.append(v.split(";")[0])
                if new_cookies:
                    cookie_jar.set(dom, "; ".join(new_cookies))
            raw = r.read()
            for enc in ("utf-8","gbk","gb2312","latin-1"):
                try: return raw.decode(enc)
                except: continue
            return raw.decode("utf-8", errors="ignore")
    except: return None
    finally: throttler.release(url)

def http_get_json(url, headers=None, timeout=TIMEOUT, referer=None, use_cookie=True):
    t = http_get(url, headers, timeout, referer=referer, use_cookie=use_cookie)
    if not t: return None
    try:
        m = re.search(r'[=(]\s*(\{[\s\S]*\})\s*[);]*$', t)
        if m: return json.loads(m.group(1))
        return json.loads(t)
    except: return None

# ═══════════════ 代码工具 ═══════════════
def normalize(code):
    c = code.strip().upper().replace(".","").replace("SH","").replace("SZ","").replace("BJ","")
    if c.startswith("6"): return f"sh{c}"
    elif c.startswith(("0","3")): return f"sz{c}"
    elif c.startswith(("8","4")): return f"bj{c}"
    return c

def to_em(code):
    nc = normalize(code)
    return f"1.{nc[2:]}" if nc.startswith("sh") else f"0.{nc[2:]}"

def cn(code): return normalize(code)[2:]
def _k(t,o,h,l,c,v,a=0): return {"time":str(t),"open":float(o),"high":float(h),"low":float(l),"close":float(c),"volume":float(v),"amount":float(a)}

TODAY = datetime.now().strftime("%Y-%m-%d")
BAR_LIMIT = 64
def last_n_bars(klines, n=BAR_LIMIT):
    return klines[-n:] if klines and len(klines) > 0 else None

# ═══════════════ 前复权计算 ═══════════════
# 从 TDX 获取除权除息数据, 计算前复权因子
# 缓存: {code: [(date, factor), ...]}
_xdxr_cache = {}
_xdxr_lock = threading.Lock()

def _fetch_xdxr(code):
    """从TDX获取除权除息数据"""
    if not HAS_TDX or not _tdx_live_servers:
        return []
    nc = normalize(code)
    market = 1 if nc.startswith("sh") else 0
    symbol = nc[2:]
    for host, port in _tdx_live_servers[:3]:
        try:
            api = TdxHq_API()
            api.connect(host, port, time_out=3)
            xdxr = api.get_xdxr_info(market, symbol)
            api.disconnect()
            if xdxr:
                return xdxr
            return []
        except:
            continue
    return []

def _build_fwd_factor(code):
    """构建前复权因子: 返回 [(date_str, cum_factor), ...] 按日期升序"""
    with _xdxr_lock:
        if code in _xdxr_cache:
            return _xdxr_cache[code]

    xdxr = _fetch_xdxr(code)
    if not xdxr:
        with _xdxr_lock:
            _xdxr_cache[code] = []
        return []

    # 只取除权除息事件 (category=1)
    events = []
    for r in xdxr:
        try:
            if int(r.get('category', 0)) != 1:
                continue
            y = int(r.get('year', 0))
            m = int(r.get('month', 0))
            d = int(r.get('day', 0))
            if y < 2000: continue
            date_str = f"{y:04d}-{m:02d}-{d:02d}"
            fenhong = float(r.get('fenhong', 0) or 0)          # 每股分红(元)
            songzhuangu = float(r.get('songzhuangu', 0) or 0)  # 每10股送转
            peigujia = float(r.get('peigujia', 0) or 0)        # 配股价
            peigu = float(r.get('peigu', 0) or 0)              # 每10股配股
            if fenhong == 0 and songzhuangu == 0 and peigu == 0:
                continue
            events.append((date_str, fenhong, songzhuangu/10.0, peigujia, peigu/10.0))
        except:
            continue

    if not events:
        with _xdxr_lock:
            _xdxr_cache[code] = []
        return []

    events.sort(key=lambda x: x[0])

    # 前复权因子: 累乘 1/(1+送转比+配比), 分红用近似比例调整
    cum = 1.0
    result = []
    for date_str, fenhong, sg_ratio, pgj, pg_ratio in events:
        divisor = 1.0 + sg_ratio + pg_ratio
        if divisor > 0:
            cum *= (1.0 / divisor)
        if fenhong > 0:
            cum *= (10.0 - fenhong) / 10.0  # 假设基准价~10元
        result.append((date_str, cum))

    with _xdxr_lock:
        _xdxr_cache[code] = result
    return result

def apply_fwd_adjust(klines, code):
    """对不复权K线数据施加前复权: 将除权日之前的价格乘以累乘因子"""
    if not klines:
        return klines
    factors = _build_fwd_factor(code)
    if not factors:
        return klines  # 无除权数据, 原样返回

    # 构建日期→因子映射 (除权日及之前的数据需要乘因子)
    # 前复权逻辑: 除权日之前的收盘价要下调
    # factors 已按日期升序, 每个 (date, cum_factor) 表示该除权日之前的累乘因子
    adjusted = []
    factor_idx = 0
    current_factor = 1.0

    for bar in klines:
        bar_date = bar["time"][:10]  # "2026-05-08 09:45" → "2026-05-08"
        # 检查是否有新的除权日
        while factor_idx < len(factors) and factors[factor_idx][0] <= bar_date:
            current_factor = factors[factor_idx][1]
            factor_idx += 1

        if current_factor < 1.0:
            # 需要调整
            adjusted.append(_k(
                bar["time"],
                bar["open"] * current_factor,
                bar["high"] * current_factor,
                bar["low"] * current_factor,
                bar["close"] * current_factor,
                bar["volume"],
                bar["amount"],
            ))
        else:
            adjusted.append(bar)
    return adjusted

# ═══════════════ 股票列表 ═══════════════
def get_stock_list():
    cache = os.path.join(OUTPUT_DIR, "_stock_list.json")
    if os.path.exists(cache) and time.time()-os.path.getmtime(cache)<86400:
        with open(cache) as f:
            s = json.load(f)
            if s: return s
    stocks, page = [], 1
    while True:
        data = http_get_json(
            f"https://82.push2.eastmoney.com/api/qt/clist/get?pn={page}&pz=5000&po=1&np=1"
            f"&ut=bd1d9ddb04089700cf9c27f6f7426281&fltt=2&invt=2&fid=f3"
            f"&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048&fields=f12,f14,f13")
        if not data: break
        items = (data.get("data") or {}).get("diff") or []
        if not items: break
        for i in items:
            c, n, m = i.get("f12",""), i.get("f14",""), i.get("f13",0)
            if c: stocks.append({"code": f"{'sh' if m==1 else 'sz'}{c}", "name": n})
        if len(stocks) >= ((data.get("data") or {}).get("total",0)): break
        page += 1
    if stocks:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(cache,"w") as f: json.dump(stocks,f,ensure_ascii=False)
    return stocks

# ═══════════════ 极速源: push2 trends2 → 1min聚合15min ═══════════════
def em_trends2_15m(code, limit=200):
    """push2.eastmoney.com trends2: 今天1分钟数据 → 聚合为15min (16bar)"""
    secid = to_em(code)
    try:
        url = (f"https://push2.eastmoney.com/api/qt/stock/trends2/get?"
               f"secid={secid}&fields1=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13"
               f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58&iscr=0&ndays=1")
        req = urllib.request.Request(url, headers=HEADERS)
        with _fast_opener.open(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "ignore")
        d = json.loads(raw)
        trends = (d.get("data") or {}).get("trends") or []
        if len(trends) < 15: return None
        # "time,open,close,high,low,vol,amount,avg"
        bars = []
        for t in trends:
            p = t.split(",")
            if len(p) < 7: continue
            bars.append({"time":p[0],"open":float(p[1]),"close":float(p[2]),
                         "high":float(p[3]),"low":float(p[4]),
                         "volume":float(p[5]),"amount":float(p[6])})
        # 15根1min → 1根15min
        result = []
        for i in range(0, len(bars)-14, 15):
            c = bars[i:i+15]
            result.append(_k(c[0]["time"], c[0]["open"],
                             max(b["high"] for b in c),
                             min(b["low"] for b in c),
                             c[-1]["close"],
                             sum(b["volume"] for b in c),
                             sum(b["amount"] for b in c)))
        return result if result else None
    except:
        return None

# ═══════════════ 其它15min数据源 (fallback) ═══════════════
def em_15m(code, limit=200):
    data = http_get_json(f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={to_em(code)}&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61&klt=15&fqt=1&end=20500101&lmt={limit}")
    k = (data.get("data") or {}).get("klines") if data else None
    return last_n_bars([_k(*p[:7]) for p in (l.split(",") for l in k)]) if k else None

def tx_15m(code, limit=200):
    tc = normalize(code)
    # 预热: 访问腾讯行情首页拿 cookie
    cookie_jar.warm("web.ifzq.gtimg.cn", "https://web.ifzq.gtimg.cn/")
    data = http_get_json(
        f"https://web.ifzq.gtimg.cn/appstock/app/kline/mkline?param={tc},m15,,{limit}",
        referer="https://gu.qq.com/",
        headers={"Referer": "https://gu.qq.com/", "Origin": "https://gu.qq.com"}
    )
    k = (data.get("data") or {}).get(tc, {}).get("m15", []) if data else []
    return last_n_bars([_k(r[0],r[1],r[3],r[4],r[2],r[5]) for r in k if len(r)>=5]) if k else None

def sina_15m(code, limit=200):
    sc = normalize(code)
    # 预热: 访问新浪财经首页拿 cookie
    cookie_jar.warm("quotes.sina.cn", "https://finance.sina.com.cn/")
    t = http_get(
        f"https://quotes.sina.cn/cn/api/jsonp_v2.php/var _={int(time.time()*1000)}/CN_MarketDataService.getKLineData?symbol={sc}&scale=15&ma=no&datalen={limit}",
        referer="https://finance.sina.com.cn/",
        headers={"Referer": "https://finance.sina.com.cn/", "Origin": "https://finance.sina.com.cn"}
    )
    if not t: return None
    m = re.search(r'\(\s*(\[.*\])\s*\)', t, re.DOTALL)
    if not m: return None
    try: items = json.loads(m.group(1))
    except: return None
    result = [_k(i.get("day",""),i.get("open",0),i.get("high",0),i.get("low",0),i.get("close",0),i.get("volume",0)) for i in items] if items else None
    if result:
        result = apply_fwd_adjust(result, code)
    return last_n_bars(result) if result else None

def baidu_15m(code, limit=200):
    # 预热: 访问百度股市通首页拿 cookie
    cookie_jar.warm("finance.pae.baidu.com", "https://gushitong.baidu.com/")
    data = http_get_json(
        f"https://finance.pae.baidu.com/selfselect/getstockquotation?all=1&code={cn(code)}&is498=1&isBk=false&isBlock=false&isFutures=false&isStock=true&isIndex=false&market_type=ab&newFormat=1&group=quotation_kline_ab&finClientType=pc",
        referer="https://gushitong.baidu.com/",
        headers={"Referer": "https://gushitong.baidu.com/", "Origin": "https://gushitong.baidu.com"}
    )
    if not data: return None
    r = data.get("Result") or []
    if not r: return None
    sd = r[0] if isinstance(r, list) else r
    k = sd.get("kline") or sd.get("dayLine") or []
    return last_n_bars([_k(i.get("date",i.get("time","")),i.get("open",0),i.get("high",0),i.get("low",0),i.get("close",0),i.get("volume",0),i.get("amount",0)) for i in k if isinstance(i,dict)]) if k else None

def sohu_15m(code, limit=200):
    # 预热: 访问搜狐股票首页拿 cookie
    cookie_jar.warm("q.stock.sohu.com", "https://q.stock.sohu.com/")
    data = http_get_json(
        f"https://q.stock.sohu.com/hisHq?code=cn_{cn(code)}&start=20260101&end=20261231&period=15",
        referer="https://q.stock.sohu.com/",
        headers={"Referer": "https://q.stock.sohu.com/", "Origin": "https://q.stock.sohu.com"}
    )
    if not data or not isinstance(data, list): return None
    hq = data[0].get("hq") or []
    result = [_k(r[0],r[1],r[3],r[4],r[2],r[5]) for r in hq if len(r)>=6] if hq else None
    if result:
        result = apply_fwd_adjust(result, code)
    return last_n_bars(result) if result else None

def xq_15m(code, limit=200):
    # 预热: 访问雪球首页拿完整 cookie (xq_a_token 等)
    cookie_jar.warm("xueqiu.com", "https://xueqiu.com/")
    cookie_jar.warm("stock.xueqiu.com", "https://xueqiu.com/")
    sym = normalize(code).upper()
    data = http_get_json(
        f"https://stock.xueqiu.com/v5/stock/chart/kline.json?symbol={sym}&begin={int(time.time()*1000)}&period=15&type=before&count=-{limit}&indicator=kline",
        referer="https://xueqiu.com/",
        headers={"Referer": "https://xueqiu.com/", "Origin": "https://xueqiu.com", "X-Requested-With": "XMLHttpRequest"}
    )
    if not data: return None
    items = (data.get("data") or {}).get("item") or []
    return last_n_bars([_k(datetime.fromtimestamp(r[0]/1000).strftime("%Y-%m-%d %H:%M"),r[2],r[3],r[4],r[5],r[1]) for r in items]) if items else None

# ═══════════════ TDX (通达信) 源 ═══════════════
# 候选服务器列表 (启动时自动探测, 只用能连的)
TDX_CANDIDATE_SERVERS = [
    ("180.153.18.170", 7709), ("60.191.117.167", 7709), ("60.12.136.251", 7709),
    ("60.12.136.250", 7709), ("115.238.90.165", 7709), ("218.75.126.9", 7709),
    ("115.238.56.198", 7709), ("119.147.212.81", 7709), ("112.74.214.43", 7709),
    ("221.231.141.60", 7709), ("101.227.73.20", 7709), ("101.227.77.254", 7709),
    ("14.215.128.18", 7709), ("59.173.18.140", 7709), ("60.28.23.80", 7709),
    ("124.160.88.183", 7709), ("123.125.108.23", 7709), ("119.147.212.76", 7709),
    ("113.105.142.162", 7709), ("218.108.98.244", 7709), ("61.152.107.171", 7709),
    ("61.153.144.66", 7709), ("218.108.47.69", 7709), ("180.153.39.51", 7709),
    ("118.114.77.13", 7709), ("61.135.142.88", 7709), ("218.85.139.19", 7709),
    ("202.108.253.130", 7709), ("202.108.253.131", 7709), ("180.153.18.171", 7709),
]

# 启动时探测, 按延迟排序, 只保留可用的
_tdx_live_servers = []
_tdx_server_lock = threading.Lock()
_tdx_server_idx = [0]

def _tdx_discover():
    """并行探测所有候选服务器, 按延迟排序"""
    global _tdx_live_servers
    import socket
    results = []
    def _probe(host, port):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            t0 = time.time()
            s.connect((host, port))
            lat = time.time() - t0
            s.close()
            # 验证能拉数据
            try:
                api = TdxHq_API()
                api.connect(host, port, time_out=3)
                api.get_security_bars(1, 0, '000001', 0, 1)
                api.disconnect()
                results.append((host, port, lat))
            except:
                pass
        except:
            pass

    threads = [threading.Thread(target=_probe, args=(h,p)) for h,p in TDX_CANDIDATE_SERVERS]
    for t in threads: t.start()
    for t in threads: t.join(timeout=5)
    results.sort(key=lambda x: x[2])
    _tdx_live_servers = [(h,p) for h,p,_ in results]
    return _tdx_live_servers

# TDX连接池: 每个线程持有一个连接, 断了自动切下一个服务器
_tdx_conn_pool = threading.local()

def _tdx_get_conn():
    """获取当前线程的TDX连接, 失败轮询切换"""
    conn = getattr(_tdx_conn_pool, 'conn', None)
    if conn:
        try:
            conn.get_security_count(0)
            return conn
        except:
            try: conn.disconnect()
            except: pass
            _tdx_conn_pool.conn = None

    if not _tdx_live_servers:
        return None

    # 轮询可用服务器
    n = len(_tdx_live_servers)
    for _ in range(n):
        with _tdx_server_lock:
            idx = _tdx_server_idx[0] % n
            _tdx_server_idx[0] += 1
        host, port = _tdx_live_servers[idx]
        try:
            api = TdxHq_API()
            api.connect(host, port, time_out=3)
            _tdx_conn_pool.conn = api
            return api
        except:
            continue
    return None

def tdx_15m(code, limit=200):
    """通达信 Level-1 行情: 15分钟K线"""
    if not HAS_TDX or not _tdx_live_servers:
        return None
    nc = normalize(code)
    if nc.startswith("sh"):
        market, symbol = 1, nc[2:]
    elif nc.startswith("sz"):
        market, symbol = 0, nc[2:]
    else:
        market, symbol = 0, nc[2:]

    api = _tdx_get_conn()
    if not api:
        return None
    try:
        data = api.get_security_bars(1, market, symbol, 0, limit)
        if not data or len(data) == 0:
            return None
        result = []
        for bar in data:
            dt = str(bar.get("datetime", ""))
            if not dt:
                continue
            result.append(_k(dt, bar.get("open",0), bar.get("high",0),
                             bar.get("low",0), bar.get("close",0),
                             bar.get("vol",0), bar.get("amount",0)))
        result = apply_fwd_adjust(result, code)
        return last_n_bars(result) if result else None
    except:
        try: _tdx_conn_pool.conn.disconnect()
        except: pass
        _tdx_conn_pool.conn = None
        return None

# ═══════════════ TDX Extended (通达信扩展行情) 源 ═══════════════
# ExHQ 用不同握手协议, 通常需要 7727 端口 (云服务器可能被屏蔽)
TDX_EX_CANDIDATE_SERVERS = [
    ("112.74.214.43", 7727), ("180.153.18.170", 7727), ("180.153.18.171", 7727),
    ("60.191.117.167", 7727), ("115.238.56.198", 7727), ("115.238.90.165", 7727),
    ("218.75.126.9", 7727), ("60.12.136.251", 7727), ("60.12.136.250", 7727),
    ("119.147.212.81", 7727), ("124.160.88.183", 7727), ("101.227.73.20", 7727),
    ("101.227.77.254", 7727), ("14.215.128.18", 7727), ("59.173.18.140", 7727),
    ("60.28.23.80", 7727), ("221.231.141.60", 7727), ("113.105.142.162", 7727),
    ("218.108.98.244", 7727), ("61.152.107.171", 7727), ("61.153.144.66", 7727),
    ("218.108.47.69", 7727), ("180.153.39.51", 7727), ("118.114.77.13", 7727),
    ("61.135.142.88", 7727), ("218.85.139.19", 7727), ("202.108.253.130", 7727),
    ("202.108.253.131", 7727),
    # 也试 7709 端口 (少数服务器支持 ExHQ 握手)
    ("180.153.18.170", 7709), ("60.12.136.251", 7709), ("60.12.136.250", 7709),
    ("115.238.90.165", 7709), ("218.75.126.9", 7709), ("115.238.56.198", 7709),
]

_tdx_ex_live_servers = []
_tdx_ex_server_lock = threading.Lock()
_tdx_ex_server_idx = [0]

def _tdx_ex_discover():
    """并行探测 ExHQ 服务器, 只保留能连且能拉数据的"""
    global _tdx_ex_live_servers
    import socket
    results = []
    def _probe(host, port):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            t0 = time.time()
            s.connect((host, port))
            lat = time.time() - t0
            s.close()
            # 验证 ExHQ 握手 + 能拉数据
            try:
                api = TdxExHq_API()
                api.connect(host, port, time_out=3)
                # 尝试拉平安银行 15min, market=28(沪A) 或 market=33(深A)
                data = None
                for mkt in [28, 33, 0, 1]:
                    try:
                        data = api.get_instrument_bars(9, mkt, '000001', 0, 1)
                        if data: break
                    except: continue
                api.disconnect()
                if data:
                    results.append((host, port, lat))
            except:
                pass
        except:
            pass

    threads = [threading.Thread(target=_probe, args=(h,p)) for h,p in TDX_EX_CANDIDATE_SERVERS]
    for t in threads: t.start()
    for t in threads: t.join(timeout=8)
    results.sort(key=lambda x: x[2])
    _tdx_ex_live_servers = [(h,p) for h,p,_ in results]
    return _tdx_ex_live_servers

_tdx_ex_conn_pool = threading.local()

def _tdx_ex_get_conn():
    """获取当前线程的 ExHQ 连接"""
    conn = getattr(_tdx_ex_conn_pool, 'conn', None)
    if conn:
        try:
            conn.get_instrument_count(0)
            return conn
        except:
            try: conn.disconnect()
            except: pass
            _tdx_ex_conn_pool.conn = None

    if not _tdx_ex_live_servers:
        return None

    n = len(_tdx_ex_live_servers)
    for _ in range(n):
        with _tdx_ex_server_lock:
            idx = _tdx_ex_server_idx[0] % n
            _tdx_ex_server_idx[0] += 1
        host, port = _tdx_ex_live_servers[idx]
        try:
            api = TdxExHq_API()
            api.connect(host, port, time_out=3)
            _tdx_ex_conn_pool.conn = api
            return api
        except:
            continue
    return None

def tdx_ex_15m(code, limit=200):
    """通达信扩展行情: 15分钟K线 (ExHQ协议, 不同服务器)"""
    if not HAS_TDX or not _tdx_ex_live_servers:
        return None
    nc = normalize(code)
    if nc.startswith("sh"):
        market, symbol = 28, nc[2:]  # 沪A
    elif nc.startswith("sz"):
        market, symbol = 33, nc[2:]  # 深A
    else:
        market, symbol = 33, nc[2:]  # 北交所归深A

    api = _tdx_ex_get_conn()
    if not api:
        return None
    try:
        # category: 0=5min, 8=15min, 1=15min(部分服务器), 试多个
        data = None
        for cat in [8, 1, 9]:
            try:
                data = api.get_instrument_bars(cat, market, symbol, 0, limit)
                if data: break
            except: continue
        if not data or len(data) == 0:
            return None
        result = []
        for bar in data:
            dt = str(bar.get("datetime", ""))
            if not dt:
                continue
            result.append(_k(dt, bar.get("open",0), bar.get("high",0),
                             bar.get("low",0), bar.get("close",0),
                             bar.get("vol",0), bar.get("amount",0)))
        result = apply_fwd_adjust(result, code)
        return last_n_bars(result) if result else None
    except:
        try: _tdx_ex_conn_pool.conn.disconnect()
        except: pass
        _tdx_ex_conn_pool.conn = None
        return None

# ═══════════════ 源注册表 ═══════════════
ALL_SOURCES = [
    ("em_trends2",   em_trends2_15m),   # 极速源: push2 trends2, 默认首选
    ("tdx",          tdx_15m),           # 通达信 HQ: 专用协议, 速度快
    ("tdx_ex",       tdx_ex_15m),        # 通达信 ExHQ: 扩展行情协议
    ("eastmoney",    em_15m),
    ("tencent",      tx_15m),
    ("sina",         sina_15m),
    ("baidu",        baidu_15m),
    ("sohu",         sohu_15m),
    ("xueqiu",       xq_15m),
]

# ═══════════════ 源Worker ═══════════════
def source_worker(name, fetch_fn, queue, stats, out_dir, threads):
    """每个源: 持久线程池, 每次领1组, 完成立即领下一组"""
    subdir = os.path.join(out_dir, "15m")
    os.makedirs(subdir, exist_ok=True)
    header = "time,open,high,low,close,volume,amount\n"

    def fetch_one(stock):
        code = stock["code"]
        try:
            data = fetch_fn(code, 200)
            if data and len(data) > 0:
                fp = os.path.join(subdir, f"{code}.csv")
                with open(fp, "w", encoding="utf-8") as f:
                    f.write(header)
                    for r in data:
                        f.write(f"{r['time']},{r['open']},{r['high']},{r['low']},{r['close']},{r['volume']},{r['amount']}\n")
                stats.record(True)
                return
        except: pass
        stats.record(False)

    with ThreadPoolExecutor(max_workers=threads) as pool:
        while True:
            try:
                _, stocks = queue.get(timeout=5)
            except Empty:
                break
            futs = [pool.submit(fetch_one, s) for s in stocks]
            for f in futs:
                try: f.result()
                except: pass
            stats.groups_done += 1
            queue.task_done()

# ═══════════════ 实时显示 ═══════════════
BAR_LEN = 40

def display(workers, total, stop):
    n = len(workers)
    while not stop.is_set():
        lines = [""]
        lines.append(f"  ⏱ {datetime.now().strftime('%H:%M:%S')} | {total}只 × {n}源")
        lines.append(f"  {'─'*75}")
        tot_ok = tot_fail = 0
        for w in workers:
            s = w["stats"]
            tot_ok += s.ok; tot_fail += s.fail
            done = s.done
            pct = done / total if total else 0
            filled = int(pct * BAR_LEN)
            bar = "█" * filled + "░" * (BAR_LEN - filled)
            alive = "🟢" if w["thread"].is_alive() else "⏹"
            lines.append(f"  {s.name:12s} {bar} {done:>4d}/{total}  ✅{s.ok:>4d} ❌{s.fail:>3d}  {s.speed():>5.1f}只/秒  {s.groups_done}组 {alive}")
        lines.append(f"  {'─'*75}")
        expect = total * n
        tot_done = tot_ok + tot_fail
        pct_all = tot_done / expect if expect else 0
        filled_all = int(pct_all * BAR_LEN)
        bar_all = "█" * filled_all + "░" * (BAR_LEN - filled_all)
        lines.append(f"  {'总计':12s} {bar_all} {tot_done:>4d}/{expect}  ✅{tot_ok:>4d} ❌{tot_fail:>3d}")
        out = "\n".join(lines)
        sys.stdout.write(out + "\n"); sys.stdout.flush()
        if all(not w["thread"].is_alive() for w in workers): break
        sys.stdout.write(f"\033[{len(lines)}A")
        time.sleep(1)
    # 最终
    lines = ["", f"  ✅ {datetime.now().strftime('%H:%M:%S')} 完成", f"  {'─'*75}"]
    tot_ok = tot_fail = 0
    for w in sorted(workers, key=lambda w: -w["stats"].ok):
        s = w["stats"]; tot_ok += s.ok; tot_fail += s.fail
        done = s.done
        pct = done / total if total else 0
        filled = int(pct * BAR_LEN)
        bar = "█" * filled + "░" * (BAR_LEN - filled)
        lines.append(f"  {s.name:12s} {bar} {done:>4d}/{total}  ✅{s.ok:>4d} ❌{s.fail:>3d}  {s.speed():>5.1f}只/秒  {s.groups_done}组")
    lines += [f"  {'─'*75}", f"  合计 ✅{tot_ok} ❌{tot_fail}", ""]
    sys.stdout.write("\n".join(lines) + "\n"); sys.stdout.flush()

# ═══════════════ main ═══════════════
def main():
    import argparse
    p = argparse.ArgumentParser(description="A股15min 极速并发")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--group-size", type=int, default=GROUP_SIZE)
    p.add_argument("--threads", type=int, default=THREADS_PER_SOURCE)
    p.add_argument("--codes", type=str, default="")
    p.add_argument("--sources", type=str, default="", help="源名,逗号分隔. 默认全部. 可选: em_trends2,tdx,tdx_ex,eastmoney,tencent,sina,baidu,sohu,xueqiu")
    args = p.parse_args()

    print("="*65)
    print(f"  A股15min 极速并发 | {datetime.now():%Y-%m-%d %H:%M}")
    print(f"  每组{args.group_size}只 × 每源{args.threads}线程 | 先完成先领组")
    print("="*65)

    if args.sources:
        names = [s.strip() for s in args.sources.split(",")]
        sources = [(n,f) for n,f in ALL_SOURCES if n in names]
    else:
        sources = ALL_SOURCES

    # TDX源: 启动时自动探测可用服务器
    for tdx_name in ("tdx", "tdx_ex"):
        if any(n == tdx_name for n, _ in sources):
            if HAS_TDX:
                label = "TDX" if tdx_name == "tdx" else "TDX ExHQ"
                print(f"\n  🔍 探测{label}服务器...")
                live = _tdx_discover() if tdx_name == "tdx" else _tdx_ex_discover()
                if live:
                    print(f"  ✅ {len(live)}个可用: {live[0][0]}:{live[0][1]} (最快)")
                else:
                    print(f"  ⚠️ 无可用{label}服务器, 跳过")
                    sources = [(n,f) for n,f in sources if n != tdx_name]
            else:
                print(f"  ⚠️ 未安装pytdx, 跳过 (pip install pytdx)")
                sources = [(n,f) for n,f in sources if n != tdx_name]

    print(f"\n  📡 {len(sources)}源: {' | '.join(n for n,_ in sources)}")

    # ═══ Cookie 预热: 并行访问各源首页, 拿到有效 Cookie ═══
    source_names = {n for n, _ in sources}
    warm_targets = []
    if "tencent" in source_names:
        warm_targets.append(("web.ifzq.gtimg.cn", "https://web.ifzq.gtimg.cn/"))
    if "sina" in source_names:
        warm_targets.append(("quotes.sina.cn", "https://finance.sina.com.cn/"))
    if "baidu" in source_names:
        warm_targets.append(("finance.pae.baidu.com", "https://gushitong.baidu.com/"))
    if "sohu" in source_names:
        warm_targets.append(("q.stock.sohu.com", "https://q.stock.sohu.com/"))
    if "xueqiu" in source_names:
        warm_targets.append(("xueqiu.com", "https://xueqiu.com/"))
    if warm_targets:
        print(f"\n  🍪 预热 {len(warm_targets)} 个域名 Cookie...")
        def _warm(domain, url):
            cookie_jar.warm(domain, url)
        warm_threads = [threading.Thread(target=_warm, args=(d, u), daemon=True) for d, u in warm_targets]
        for t in warm_threads: t.start()
        for t in warm_threads: t.join(timeout=10)
        warmed = sum(1 for d, _ in warm_targets if cookie_jar.get(d))
        print(f"  ✅ {warmed}/{len(warm_targets)} 个域名已获取 Cookie")

    print(f"\n  📋 获取股票列表...")
    if args.codes:
        stocks = [{"code":normalize(c.strip()),"name":c.strip()} for c in args.codes.split(",") if c.strip()]
    else:
        stocks = get_stock_list()
    if not stocks: print("  ❌ 获取失败"); return
    if args.limit > 0: stocks = stocks[:args.limit]
    print(f"  ✅ {len(stocks)} 只")

    q = Queue()
    groups = [stocks[i:i+args.group_size] for i in range(0, len(stocks), args.group_size)]
    for idx, g in enumerate(groups): q.put((idx, g))
    print(f"  📦 {len(groups)} 组 → 队列就绪\n  🚀 启动...")

    workers = []
    for name, fn in sources:
        st = SourceStats(name)
        t = threading.Thread(target=source_worker, args=(name, fn, q, st, OUTPUT_DIR, args.threads), daemon=True)
        workers.append({"thread": t, "stats": st})
        t.start()

    stop = threading.Event()
    disp = threading.Thread(target=display, args=(workers, len(stocks), stop), daemon=True)
    t0 = time.time()
    disp.start()

    for w in workers: w["thread"].join()
    stop.set(); disp.join(timeout=3)

    elapsed = time.time() - t0
    tot_ok = sum(w["stats"].ok for w in workers)
    print(f"\n  ⏱ 耗时 {elapsed:.1f}s | 整体 {tot_ok/elapsed:.1f}只/秒" if elapsed > 0 else "")
    print(f"  📁 {os.path.abspath(OUTPUT_DIR)}/15m/")

if __name__ == "__main__":
    main()
