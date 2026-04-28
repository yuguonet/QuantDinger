#!/usr/bin/env python3
"""
财经新闻直接抓取 — 不依赖 AKShare (v2.2)
数据源: 东方财富、新浪财经、央视网 直接爬取
依赖: pip install requests

v2.2 修复:
- 东方财富: API 新增 req_trace 参数, 改用 7x24 快讯接口
- 财联社: content 字段类型变更(str), 适配 brief + ctime
- 新浪财经: feed.mix 大量 403, 改用 zhibo.sina.com.cn 7x24 + top 热榜双通道
- 华尔街见闻: 保持不变 (正常工作)
"""

import requests
import json
import re
import sys
import uuid
import time
import random as _random
import threading as _threading
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def fetch_eastmoney_news(category="财经", max_items=20):
    """
    东方财富财经新闻
    优先: 7x24 快讯 JSON API (需 req_trace)
    备用: newsapi JSONP 接口
    """
    # ── 主接口: 7x24 快讯 ──
    url = "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns"
    params = {
        "client": "web",
        "biz": "web_724",
        "column": "724",
        "order": "1",
        "needInteractData": "0",
        "page_index": "1",
        "page_size": str(max_items),
        "req_trace": str(uuid.uuid4()),
    }
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=10)
        data = resp.json()
        items = data.get("data", {}).get("list", [])
        if items:
            results = []
            for i in items:
                title = i.get("title", "") or i.get("summary", "")
                if title:
                    results.append({
                        "title": title.strip(),
                        "url": i.get("url", "") or i.get("uniqueUrl", ""),
                        "time": i.get("showTime", ""),
                        "source": i.get("mediaName", "东方财富"),
                    })
            return results[:max_items]
    except Exception:
        pass

    # ── 备用: newsapi JSONP ──
    url2 = "https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_50_1_.html"
    try:
        resp = requests.get(url2, headers=HEADERS, timeout=10)
        text = resp.text
        # var ajaxResult={...}
        match = re.search(r'var\s+\w+=\s*(\{.*\})', text, re.DOTALL)
        if match:
            data = json.loads(match.group(1))
            items = data.get("LivesList", [])
            results = []
            for i in items[:max_items]:
                title = i.get("title", "") or i.get("digest", "")
                if title:
                    results.append({
                        "title": title.strip(),
                        "url": i.get("url_w", "") or i.get("url_m", ""),
                        "time": i.get("showtime", ""),
                        "source": "东方财富",
                    })
            return results
    except Exception:
        pass

    return []


def fetch_sina_finance_news(max_items=20):
    """新浪财经新闻 (v2.3 修复)
    主通道: zhibo.sina.com.cn 7x24 直播流 (稳定, 已验证)
    备用: top.finance.sina.com.cn 热榜
    已弃用: feed.mix.sina.com.cn (大量 lid 返回 403)
    """
    sess = _get_session()

    # ── 主通道: zhibo 7x24 ──
    try:
        url = "https://zhibo.sina.com.cn/api/zhibo/feed"
        params = {"page": 1, "page_size": max_items, "zhibo_id": 152, "tag_id": 0, "type": 0}
        resp = sess.get(url, params=params, headers=_rotating_headers(), timeout=TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            feed_list = data.get("result", {}).get("data", {}).get("feed", {}).get("list", [])
            results = []
            for item in feed_list[:max_items]:
                rich = item.get("rich_text", "") or item.get("text_content", "") or item.get("content", "")
                title = _clean_html(rich).strip()[:200]
                if not title:
                    continue
                create_time = item.get("create_time", "")
                try:
                    time_str = datetime.fromtimestamp(int(create_time)).strftime("%Y-%m-%d %H:%M") if create_time else ""
                except (ValueError, TypeError):
                    time_str = str(create_time)
                results.append({
                    "title": title,
                    "url": f"https://finance.sina.com.cn/7x24/{item.get('id', '')}.shtml" if item.get("id") else "",
                    "time": time_str,
                    "source": "新浪财经7x24",
                })
            if results:
                return results
    except Exception as e:
        print(f"  [新浪财经zhibo] 异常: {e}", file=sys.stderr)

    # ── 备用: top 热榜 ──
    try:
        url2 = "https://top.finance.sina.com.cn/ws/GetTopDataList.php"
        today_str = datetime.now().strftime("%Y%m%d")
        params2 = {
            "top_type": "day", "top_cat": "finance_0_suda",
            "top_time": today_str, "top_show_num": str(max_items),
            "top_order": "DESC", "js_var": "all",
        }
        resp = sess.get(url2, params=params2, headers=_rotating_headers(), timeout=TIMEOUT)
        if resp.status_code == 200:
            import re as _re
            match = _re.search(r'var\s+\w+\s*=\s*(\{.*\})', resp.text, _re.DOTALL)
            if match:
                data = json.loads(match.group(1))
                items = data.get("data", [])
                results = []
                for item in items[:max_items]:
                    title = item.get("title", "").strip()
                    if not title:
                        continue
                    results.append({"title": title, "url": item.get("url", ""), "time": "", "source": "新浪财经热榜"})
                if results:
                    return results
    except Exception as e:
        print(f"  [新浪财经top] 异常: {e}", file=sys.stderr)

    return []


def fetch_cls_news(max_items=20):
    """
    财联社电报
    接口: updateTelegraphList (稳定, 无需签名)
    注意: content 字段现在是 str 类型, brief 有摘要, ctime 是 unix 时间戳
    """
    url = "https://www.cls.cn/nodeapi/updateTelegraphList"
    params = {"app": "CailianpressWeb", "os": "web", "sv": "7.7.5", "rn": str(max_items)}
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=10)
        data = resp.json()
        items = data.get("data", {}).get("roll_data", [])
        news = []
        for item in items:
            # brief 是摘要文本, content 现在是 str (非 list)
            brief = item.get("brief", "")
            if not brief:
                # 兜底: 尝试从 content 字段取
                content = item.get("content", "")
                if isinstance(content, str):
                    brief = content
                elif isinstance(content, list):
                    # 兼容旧格式
                    for c in content:
                        brief = c.get("brief", c.get("title", ""))
                        if brief:
                            break
            if not brief:
                continue

            ctime = item.get("ctime", 0)
            try:
                time_str = datetime.fromtimestamp(int(ctime)).strftime("%H:%M") if ctime else ""
            except (ValueError, TypeError):
                time_str = ""

            news.append({
                "title": brief.strip(),
                "time": time_str,
                "source": "财联社",
            })
        return news[:max_items]
    except Exception as e:
        return [{"error": str(e)}]


def fetch_wallstreetcn_news(max_items=20):
    """华尔街见闻快讯 (公开 API) — 无需修改, 原样工作"""
    url = "https://api-one-wscn.awtmt.com/apiv1/content/lives"
    params = {"channel": "global-channel", "limit": max_items}
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=10)
        data = resp.json()
        items = data.get("data", {}).get("items", [])
        results = []
        for item in items:
            title = item.get("content_text", "") or item.get("content_plain", "") or item.get("title", "")
            if not title:
                continue
            display_time = item.get("display_time", 0)
            try:
                time_str = datetime.fromtimestamp(int(display_time)).strftime("%H:%M") if display_time else ""
            except (ValueError, TypeError, OSError):
                time_str = ""
            results.append({
                "title": title.strip(),
                "time": time_str,
                "source": "华尔街见闻",
            })
        return results
    except Exception as e:
        return [{"error": str(e)}]


def fetch_akshare_news(max_items=20):
    """AKShare 财经新闻 (降级备选) — 兼容新旧列结构"""
    try:
        import akshare as ak
        df = ak.stock_news_em(symbol="财经")
        news = []
        for _, row in df.head(max_items).iterrows():
            # 新版列: 0=关键词, 1=新闻标题, 2=新闻内容, 3=发布时间, 4=文章来源, 5=新闻链接
            title = str(row.get('新闻标题', row.iloc[1] if len(row) > 1 else ''))
            time_str = str(row.get('发布时间', row.iloc[3] if len(row) > 3 else ''))
            url = str(row.get('新闻链接', row.iloc[5] if len(row) > 5 else ''))
            if title and title not in ('财经', 'None', ''):
                news.append({"title": title, "time": time_str, "url": url, "source": "AKShare-东方财富"})
        return news
    except Exception as e:
        return [{"error": f"akshare: {e}"}]


def fetch_all_news(max_per_source=10):
    """
    聚合所有新闻源 (供 news.py 调用)
    返回 dict 列表: [{"title", "url", "source", "time"}, ...]
    """
    import logging

    logger = logging.getLogger(__name__)
    logger.info(f"[cn_news] 开始抓取, max_per_source={max_per_source}")

    sources = [
        ("财联社", lambda: fetch_cls_news(max_items=max_per_source)),
        ("华尔街见闻", lambda: fetch_wallstreetcn_news(max_items=max_per_source)),
        ("东方财富", lambda: fetch_eastmoney_news(max_items=max_per_source)),
        ("新浪财经", lambda: fetch_sina_finance_news(max_items=max_per_source)),
        ("AKShare", lambda: fetch_akshare_news(max_items=max_per_source)),
    ]

    t0 = time.time()
    all_news = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fn): name for name, fn in sources}
        for future in as_completed(futures):
            name = futures[future]
            try:
                items = future.result(timeout=15)
                valid = [i for i in items if isinstance(i, dict) and "error" not in i]
                all_news.extend(valid)
                logger.info(f"[cn_news] {name}: {len(valid)} 条")
            except Exception as e:
                logger.warning(f"[cn_news] {name} 异常: {e}")

    elapsed = time.time() - t0
    logger.info(f"[cn_news] 完成, 共 {len(all_news)} 条, 耗时 {elapsed:.2f}s")
    return all_news


# ─── 配置 ───────────────────────────────────────────────
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
]

HEADERS = {
    "User-Agent": _USER_AGENTS[0],
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Connection": "keep-alive",
}
TIMEOUT = 12
MAX_NEWS = 30

# Reusable session with retry logic
_session = None
_session_lock = _threading.Lock()

def _get_session() -> requests.Session:
    """Get a requests.Session with retry and rotating UA. Thread-safe."""
    global _session
    if _session is not None:
        return _session
    with _session_lock:
        if _session is not None:
            return _session
        s = requests.Session()
        retries = Retry(
            total=2,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
        )
        adapter = HTTPAdapter(max_retries=retries, pool_maxsize=10)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _session = s
        return s


def _rotating_headers() -> dict:
    """Return headers with a random User-Agent."""
    h = dict(HEADERS)
    h["User-Agent"] = _random.choice(_USER_AGENTS)
    return h


# ─── 工具函数 ────────────────────────────────────────────
def _clean_html(text: str) -> str:
    return BeautifulSoup(text, "html.parser").get_text(separator=" ", strip=True)



def _summarize(title: str, text: str = "", abstract: str = "", max_len: int = 1000) -> str:
    """摘要提取: 有摘要字段用摘要, 无则截取全文"""
    if abstract and len(abstract.strip()) > 10:
        clean = _clean_html(abstract).strip()
        return clean[:max_len] + ("…" if len(clean) > max_len else "")
    if not text:
        return title[:max_len]
    clean = _clean_html(text)
    return clean[:max_len] + ("…" if len(clean) > max_len else "")


def _parse_time(t: str) -> Optional[datetime]:
    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"]:
        try:
            return datetime.strptime(t.strip(), fmt)
        except ValueError:
            continue
    return None


# ═══════════════════════════════════════════════════════════# ─── 股票名称/代码互查 ────────────────────────────────────
def _get_stock_name(code: str) -> str:
    try:
        sess = _get_session()
        resp = sess.get(
            f"https://suggest3.sinajs.cn/suggest/type=11,12,13,14,15&key={code}",
            headers=_rotating_headers(), timeout=5)
        resp.encoding = "gbk"
        for item in resp.text.split(";"):
            parts = item.split(",")
            if len(parts) >= 5 and code in parts[2]:
                name = parts[4].strip()
                if name:
                    return name
    except Exception:
        pass
    return code


def _resolve_code(input_str: str) -> tuple:
    """输入代码或名称, 返回 (代码, 名称)"""
    s = input_str.strip()
    if re.match(r'^[036]\d{5}$', s):
        return s, _get_stock_name(s)
    m = re.match(r'^(sh|sz|bj)(\d{6})$', s, re.IGNORECASE)
    if m:
        code = m.group(2)
        return code, _get_stock_name(code)
    try:
        sess = _get_session()
        resp = sess.get(
            f"https://suggest3.sinajs.cn/suggest/type=11,12,13,14,15&key={s}",
            headers=_rotating_headers(), timeout=5)
        resp.encoding = "gbk"
        for item in resp.text.split(";"):
            parts = item.split(",")
            if len(parts) >= 5 and re.match(r'^[036]\d{5}$', parts[2]):
                return parts[2], parts[4].strip() or s
    except Exception:
        pass
    return s, s


# ═══════════════════════════════════════════════════════════
# 数据源 1: 东方财富 — 公告 + 新闻 (个股)
# ═══════════════════════════════════════════════════════════
def _fetch_eastmoney(code: str, days: int, name: str = "") -> List[Dict[str, Any]]:
    items = []
    cutoff = datetime.now() - timedelta(days=days)
    keywords = list(set([code, name])) if name else [code]
    sess = _get_session()

    # 公告
    try:
        url = "https://np-anotice-stock.eastmoney.com/api/security/ann"
        params = {"sr": -1, "page_size": MAX_NEWS, "page_index": 1,
                  "ann_type": "A", "client_source": "web", "stock_list": code}
        resp = sess.get(url, headers=_rotating_headers(), params=params, timeout=TIMEOUT)
        if resp.status_code == 200:
            for art in resp.json().get("data", {}).get("list", []):
                title = art.get("title", "")
                if not title:
                    continue
                pub_time = _parse_time(art.get("display_time", art.get("notice_date", ""))[:19])
                if not pub_time or pub_time < cutoff:
                    continue
                art_code = art.get("art_code", "")
                art_url = f"https://data.eastmoney.com/notices/detail/{code}/{art_code}.html"
                items.append({
                    "title": title,
                    "url": art_url,
                    "time": pub_time.strftime("%Y-%m-%d %H:%M"),
                    "source": "东方财富公告",
                })
    except Exception as e:
        print(f"  [东方财富公告] 异常: {e}", file=sys.stderr)

    # 新闻 (用名称搜索覆盖更全)
    for kw in keywords:
        try:
            # v2.3 修复: search-api-web 已返回 400, 改用 listapi+keyword
            listapi_url = "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns"
            listapi_params = {
                "client": "web", "biz": "web_724", "column": "724",
                "order": "1", "needInteractData": "0",
                "page_index": "1", "page_size": str(MAX_NEWS),
                "req_trace": str(uuid.uuid4()),
                "keyword": kw,
            }
            resp = sess.get(listapi_url, params=listapi_params, headers=_rotating_headers(), timeout=TIMEOUT)
            if resp.status_code != 200:
                continue
            listapi_data = resp.json()
            articles = listapi_data.get("data", {}).get("list", [])
            for art in articles:
                title = _clean_html(art.get("title", ""))
                if not title:
                    continue
                pub_time = _parse_time(art.get("date", ""))
                if not pub_time or pub_time < cutoff:
                    continue
                url_link = art.get("url", "")
                if url_link and not url_link.startswith("http"):
                    url_link = "https:" + url_link
                items.append({
                    "title": title,
                    "url": url_link,
                    "time": pub_time.strftime("%Y-%m-%d %H:%M"),
                    "source": "东方财富新闻",
                })
        except Exception as e:
            print(f"  [东方财富新闻/{kw}] 异常: {e}", file=sys.stderr)

    return items


# ═══════════════════════════════════════════════════════════
# 数据源 2: 新浪财经 (个股)
# ═══════════════════════════════════════════════════════════
def _fetch_sina(code: str, days: int, name: str = "") -> List[Dict[str, Any]]:
    items = []
    cutoff = datetime.now() - timedelta(days=days)
    sess = _get_session()
    try:
        market = "sh" if code[:3] in ("600", "601", "603", "605", "688", "689") else "sz"
        url = f"https://finance.sina.com.cn/realstock/company/{market}{code}/news.shtml"
        resp = sess.get(url, headers=_rotating_headers(), timeout=TIMEOUT)
        resp.encoding = "gbk"
        soup = BeautifulSoup(resp.text, "html.parser")

        for a_tag in soup.select("a[href*='/news/'], a[href*='finance.sina.com.cn']"):
            title = a_tag.get_text(strip=True)
            if len(title) < 8 or not re.search(r'[\u4e00-\u9fff]', title):
                continue
            href = a_tag.get("href", "")
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = "https://finance.sina.com.cn" + href

            parent = a_tag.parent
            time_text = parent.get_text() if parent else ""
            tm = re.search(r'(\d{4}[-/]\d{1,2}[-/]\d{1,2})\s*(\d{2}:\d{2})?', time_text)
            if not tm:
                continue
            date_str = tm.group(1) + (" " + tm.group(2) if tm.group(2) else "")
            pub_time = _parse_time(date_str.replace("/", "-"))
            if not pub_time or pub_time < cutoff:
                continue

            items.append({
                "title": title,
                "url": href,
                "time": pub_time.strftime("%Y-%m-%d %H:%M"),
                "source": "新浪财经",
            })
    except Exception as e:
        print(f"  [新浪财经] 异常: {e}", file=sys.stderr)
    return items


# ═══════════════════════════════════════════════════════════
# 数据源 3: 新浪 7x24 财经快讯 (个股)
# ═══════════════════════════════════════════════════════════
def _fetch_sina7x24(code: str, days: int, name: str = "") -> List[Dict[str, Any]]:
    items = []
    cutoff = datetime.now() - timedelta(days=days)
    search_terms = [code]
    if name:
        search_terms.append(name)
    sess = _get_session()
    try:
        url = "https://zhibo.sina.com.cn/api/zhibo/feed"
        params = {"page": 1, "page_size": 50, "zhibo_id": 152, "tag_id": 0, "type": 0}
        resp = sess.get(url, headers=_rotating_headers(), params=params, timeout=TIMEOUT)
        if resp.status_code != 200:
            return items
        data = resp.json()

        for item_data in data.get("result", {}).get("data", {}).get("feed", {}).get("feed_list", []):
            content = item_data.get("rich_text", "") or item_data.get("body", "")
            title = _clean_html(content)
            if not any(t in title for t in search_terms):
                continue
            pub_time = _parse_time(item_data.get("create_time", ""))
            if not pub_time or pub_time < cutoff:
                continue

            items.append({
                "title": title[:80],
                "url": f"https://zhibo.sina.com.cn/152/{item_data.get('id', '')}.html",
                "time": pub_time.strftime("%Y-%m-%d %H:%M"),
                "source": "新浪7x24",
            })
    except Exception as e:
        print(f"  [新浪7x24] 异常: {e}", file=sys.stderr)
    return items


# ═══════════════════════════════════════════════════════════
# 数据源 4: 腾讯财经 (个股)
# ═══════════════════════════════════════════════════════════
def _fetch_qq(code: str, days: int, name: str = "") -> List[Dict[str, Any]]:
    """腾讯财经 (v2.3 修复)
    已知问题: i.news.qq.com 全部接口返回 404 (2026-04 确认)
    """
    return []



def _fetch_ifeng(code: str, days: int, name: str = "") -> List[Dict[str, Any]]:
    """凤凰财经 (v2.3 修复)
    已知问题: so.finance.ifeng.com 域名无法解析 (DNS failure, 2026-04 确认)
    """
    return []



# ─── 数据源 6: 同花顺 (v2.3 新增) ───
def _fetch_ths(code: str, days: int, name: str = "") -> List[Dict[str, Any]]:
    """同花顺个股新闻 (v2.3 新增, 2026-04 验证可用)"""
    items = []
    cutoff = datetime.now() - timedelta(days=days)
    sess = _get_session()
    try:
        url = "https://news.10jqka.com.cn/tapp/news/push/stock/"
        params = {"page": "1", "track": "website", "pagesize": "20"}
        resp = sess.get(url, headers=_rotating_headers(), timeout=TIMEOUT)
        if resp.status_code != 200:
            return items
        data = resp.json()
        news_list = data.get("data", {}).get("list", []) if isinstance(data.get("data"), dict) else []
        for item in news_list:
            title = item.get("title", "").strip()
            if not title:
                continue
            ctime = item.get("ctime", "") or item.get("datetime", "")
            try:
                pub_time = datetime.fromtimestamp(int(ctime)) if ctime else None
            except (ValueError, TypeError):
                pub_time = _parse_time(str(ctime))
            if not pub_time or pub_time < cutoff:
                continue
            items.append({"title": title, "url": item.get("url", ""), "time": pub_time.strftime("%Y-%m-%d %H:%M"), "source": "同花顺"})
    except Exception as e:
        print(f"  [同花顺] 异常: {e}", file=sys.stderr)
    return items


# ── 市场新闻公开接口 ─────────────────────────────────────
def fetch_cls_market(max_items: int = 20, days: int = 1) -> List[Dict[str, Any]]:
    """财联社电报 (市场)"""
    return fetch_cls_news(max_items=max_items)


def fetch_wallstreetcn_market(max_items: int = 20, days: int = 1) -> List[Dict[str, Any]]:
    """华尔街见闻快讯 (市场)"""
    return fetch_wallstreetcn_news(max_items=max_items)


def fetch_eastmoney_market(max_items: int = 20, days: int = 1) -> List[Dict[str, Any]]:
    """东方财富财经新闻 (市场)"""
    return fetch_eastmoney_news(max_items=max_items)


def fetch_sina_market(max_items: int = 20, days: int = 1) -> List[Dict[str, Any]]:
    """新浪财经新闻 (市场)"""
    return fetch_sina_finance_news(max_items=max_items)


def fetch_akshare_market(max_items: int = 20, days: int = 1) -> List[Dict[str, Any]]:
    """AKShare 财经新闻 (市场)"""
    return fetch_akshare_news(max_items=max_items)


# ── 个股新闻公开接口 ─────────────────────────────────────
def fetch_eastmoney_stock(code: str, days: int = 3, name: str = "") -> List[Dict[str, Any]]:
    """东方财富 公告+新闻 (个股)"""
    return _fetch_eastmoney(code, days, name)


def fetch_sina_stock(code: str, days: int = 3, name: str = "") -> List[Dict[str, Any]]:
    """新浪财经个股页"""
    return _fetch_sina(code, days, name)


def fetch_sina7x24_stock(code: str, days: int = 3, name: str = "") -> List[Dict[str, Any]]:
    """新浪7x24 快讯 (按个股关键词过滤)"""
    return _fetch_sina7x24(code, days, name)


def fetch_tencent_stock(code: str, days: int = 3, name: str = "") -> List[Dict[str, Any]]:
    """腾讯财经个股新闻"""
    return _fetch_qq(code, days, name)


def fetch_ifeng_stock(code: str, days: int = 3, name: str = "") -> List[Dict[str, Any]]:
    """凤凰财经个股新闻"""
    return _fetch_ifeng(code, days, name)


# ── 聚合函数 (并行抓取所有子源) ──────────────────────────
def fetch_all_market_news(max_per_source: int = 10, days: int = 1) -> List[Dict[str, Any]]:
    """
    并行抓取所有市场新闻源
    返回: [{"title", "url", "time", "source"}, ...]
    """
    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"[市场新闻] 开始并行抓取, max_per_source={max_per_source}, days={days}")

    sources = [
        ("财联社", lambda: fetch_cls_market(max_items=max_per_source, days=days)),
        ("华尔街见闻", lambda: fetch_wallstreetcn_market(max_items=max_per_source, days=days)),
        ("东方财富", lambda: fetch_eastmoney_market(max_items=max_per_source, days=days)),
        ("新浪财经", lambda: fetch_sina_market(max_items=max_per_source, days=days)),
        ("AKShare", lambda: fetch_akshare_market(max_items=max_per_source, days=days)),
    ]

    all_news = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fn): name for name, fn in sources}
        for future in as_completed(futures):
            name = futures[future]
            try:
                items = future.result(timeout=15)
                valid = [i for i in items if isinstance(i, dict) and "error" not in i and i.get("title")]
                all_news.extend(valid)
                logger.info(f"[市场新闻] {name}: {len(valid)} 条")
            except Exception as e:
                logger.warning(f"[市场新闻] {name} 异常: {e}")

    logger.info(f"[市场新闻] 完成, 共 {len(all_news)} 条")
    return all_news


def fetch_all_stock_news(
    code: str, days: int = 3, name: str = "",
) -> List[Dict[str, Any]]:
    """
    并行抓取所有个股新闻源
    返回: [{"title", "url", "time", "source"}, ...]
    """
    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"[个股新闻] 开始并行抓取: {code}({name}), days={days}")

    sources = [
        ("东方财富", lambda: fetch_eastmoney_stock(code, days, name)),
        ("新浪财经", lambda: fetch_sina_stock(code, days, name)),
        ("新浪7x24", lambda: fetch_sina7x24_stock(code, days, name)),
        ("腾讯财经", lambda: fetch_tencent_stock(code, days, name)),
        ("凤凰财经", lambda: fetch_ifeng_stock(code, days, name)),
        ("同花顺", lambda: _fetch_ths(code, days, name)),
    ]

    all_news = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fn): name for name, fn in sources}
        for future in as_completed(futures):
            src_name = futures[future]
            try:
                items = future.result(timeout=15)
                valid = [i for i in items if isinstance(i, dict) and "error" not in i and i.get("title")]
                all_news.extend(valid)
                logger.info(f"[个股新闻] {src_name}: {len(valid)} 条")
            except Exception as e:
                logger.warning(f"[个股新闻] {src_name} 异常: {e}")

    logger.info(f"[个股新闻] 完成, {code} 共 {len(all_news)} 条")
    return all_news


# ═══════════════════════════════════════════════════
#  测试
# ═══════════════════════════════════════════════════

if __name__ == "__main__":
    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    print(f"\n{'='*60}")
    print(f"  📰 新闻抓取测试 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}\n")

    sources = [
        ("财联社", fetch_cls_news),
        ("华尔街见闻", fetch_wallstreetcn_news),
        ("东方财富", fetch_eastmoney_news),
        ("新浪财经", fetch_sina_finance_news),
        ("AKShare", fetch_akshare_news),
    ]

    # ── 并行抓取 ──
    def _fetch(name, fn, max_items=5):
        try:
            items = fn(max_items=max_items)
            valid = [i for i in items if "error" not in i]
            return name, valid
        except Exception:
            return name, []

    t0 = time.perf_counter()
    all_news = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(_fetch, n, f): n for n, f in sources}
        for future in as_completed(futures):
            name, items = future.result()
            all_news.extend(items)
            print(f"  ✅ {name}: {len(items)} 条")
            for i, item in enumerate(items[:3], 1):
                print(f"     {i}. {item.get('title', '')[:60]}")

    elapsed = time.perf_counter() - t0
    print(f"\n  共 {len(all_news)} 条新闻 | 耗时 {elapsed:.2f}s (并行)")
