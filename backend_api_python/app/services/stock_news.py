#!/usr/bin/env python3
"""
A股个股多渠道新闻聚合工具 — stock_news.py v2.0

功能:
  - 5路数据源并发抓取: 东方财富(公告+新闻)、新浪财经、新浪7x24快讯、腾讯财经、凤凰财经
  - 标题归一化去重, 加权情感分析(0-10分), 时间衰减综合评分
  - 输出标准 SearchResponse(SearchResult), 直接挂载到 search.py 搜索体系

数据模型:
  - 复用 search.py 的 SearchResult / SearchResponse
  - 情感标签: SearchResult.sentiment = "positive" | "negative" | "neutral"
  - 综合评分: SearchResponse.metadata = { composite_score, direction, positive, negative, neutral }

对外接口:
  fetch_stock_news(stock_code, days, stock_name, sources, max_results) -> SearchResponse
  - stock_code: 6位股票代码
  - days: 有效天数 (默认3)
  - stock_name: 可选, 不传则自动查询
  - sources: 指定数据源, None=全部, 可选: eastmoney/sina/sina7x24/qq/ifeng
  - max_results: 限制返回条数, 0=不限

挂载方式 (search.py):
  StockNewsSearchProvider._do_search() 调用本模块 fetch_stock_news()
  SearchService.search_cn_stock_news() 并行调用 Web搜索 + StockNews, 自动去重

去重规则:
  - 标题归一化指纹(md5), 去除空白/标点, 与 search.py 的 _title_key 保持一致
  - stock_news 内部 5 路数据源先去重, search_cn_stock_news 中再与 web 结果去重
  - 优先级: stock_news > web搜索 (国内财经直连数据源优先)

评分机制 (未改变):
  - 每条新闻: 关键词加权情感分析 → sentiment + score
  - 综合评分: 指数时间衰减(每天衰减20%), 非中性消息额外×1.5权重
  - direction: 利好(≥7) / 偏利好(≥6) / 中性 / 偏利空(≤4) / 利空(≤3)

依赖: requests, beautifulsoup4 (bs4)
用法: python stock_news.py <股票代码> [有效天数] [--top N] [--json-only]
"""

import sys
import json
import re
import hashlib
import argparse
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, List

import requests
from bs4 import BeautifulSoup

# ─── 复用 search.py 的数据模型 ──────────────────────────
from app.services.search import SearchResult, SearchResponse

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
import random as _random
import threading as _threading
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

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

# ─── 情感关键词库 (权重1-3, 越强信号权重越高) ───────────
POSITIVE_WORDS = {
    "涨停": 3, "封板": 3, "连板": 3, "一字涨停": 3, "地天板": 3,
    "大涨": 2, "暴涨": 2, "飙升": 2, "强势涨停": 3,
    "创新高": 2, "历史新高": 2, "突破": 2, "放量上涨": 2, "领涨": 2,
    "强势": 1, "反弹": 1, "反转": 2, "金叉": 1, "上涨": 1, "走强": 1,
    "业绩增长": 2, "利润增长": 2, "营收增长": 2, "净利增长": 2,
    "超预期": 3, "超市场预期": 3, "大超预期": 3,
    "业绩预增": 2, "扭亏": 2, "翻倍": 3, "高增长": 2,
    "买入": 2, "增持": 2, "推荐": 2, "强烈推荐": 3, "上调目标价": 2,
    "重大合同": 3, "中标": 2, "签大单": 2, "战略合作": 2,
    "技术突破": 2, "量产": 2, "产能扩张": 2, "获批": 2,
    "分红": 1, "回购": 2, "大股东增持": 3, "并购": 2,
    "政策利好": 3, "政策支持": 2, "行业景气": 2, "涨价": 2,
    "利好": 2, "重大利好": 3, "利好消息": 2,
    "进军": 1, "拓展": 1, "布局": 1, "里程碑": 2,
}
NEGATIVE_WORDS = {
    "跌停": 3, "一字跌停": 3, "连续跌停": 3, "天地板": 3,
    "闪崩": 3, "崩盘": 3, "跳水": 2, "大跌": 2, "暴跌": 2, "重挫": 2,
    "破位": 2, "破发": 2, "新低": 2, "历史新低": 3,
    "放量下跌": 2, "领跌": 2, "弱势": 1, "下跌": 1, "走弱": 1,
    "业绩下滑": 2, "利润下滑": 2, "净利下滑": 2, "净利大跌": 3,
    "不及预期": 2, "业绩暴雷": 3, "暴雷": 3, "业绩变脸": 3,
    "亏损": 2, "巨亏": 3, "大幅亏损": 3, "由盈转亏": 3,
    "商誉减值": 3, "资产减值": 2, "财务造假": 3,
    "卖出": 2, "减持": 2, "抛售": 2, "清仓": 3, "下调目标价": 2,
    "大股东减持": 3, "违规减持": 3, "限售解禁": 2,
    "裁员": 2, "停产": 2, "破产": 3, "清算": 3,
    "质量事故": 3, "安全事故": 3, "产品召回": 2,
    "监管调查": 3, "立案调查": 3, "违规": 2, "违法": 3,
    "退市": 3, "ST": 2, "暂停上市": 3,
    "债务危机": 3, "债务违约": 3, "资金链断裂": 3,
    "诉讼": 2, "巨额索赔": 3,
    "利空": 2, "重大利空": 3, "黑天鹅": 3,
}


# ─── 工具函数 ────────────────────────────────────────────
def _clean_html(text: str) -> str:
    return BeautifulSoup(text, "html.parser").get_text(separator=" ", strip=True)


def _analyze_sentiment(title: str, summary: str = "") -> tuple:
    """加权情感评分 0-10, 返回 (label, score, keywords)"""
    text = f"{title} {summary}"
    pos_weight, neg_weight = 0, 0
    hits = []

    for word, weight in POSITIVE_WORDS.items():
        if word in text:
            pos_weight += weight
            hits.append(word)
    for word, weight in NEGATIVE_WORDS.items():
        if word in text:
            neg_weight += weight
            hits.append(word)

    total = pos_weight + neg_weight
    if total == 0:
        return "neutral", 5.0, []

    raw = (pos_weight - neg_weight) / total
    confidence = min(total / 6, 1.0)
    score = round(raw * 5 * confidence + 5, 1)
    score = max(0.0, min(10.0, score))

    if score >= 7:
        label = "positive"
    elif score <= 3:
        label = "negative"
    else:
        label = "neutral"

    return label, score, list(set(hits))


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


def _title_hash(title: str, source: str = "") -> str:
    """标题归一化指纹, 用于去重 — 与 search.py 的 _title_key 保持一致"""
    norm = re.sub(r'[\s\u3000\uff0c\u3001\u3002\uff01\uff1f\u2014]', '', title)
    return hashlib.md5(norm.encode()).hexdigest()[:16]


# ─── 股票名称/代码互查 ────────────────────────────────────
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
# 数据源 1: 东方财富 — 公告 + 新闻
# ═══════════════════════════════════════════════════════════
def _fetch_eastmoney(code: str, days: int, name: str = "") -> List[SearchResult]:
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
                summary = _summarize(title)
                sentiment, score, skw = _analyze_sentiment(title, summary)
                items.append(SearchResult(
                    title=title, snippet=summary, url=art_url,
                    source="东方财富公告", published_date=pub_time.isoformat(),
                    sentiment=sentiment))
    except Exception as e:
        print(f"  [东方财富公告] 异常: {e}", file=sys.stderr)

    # 新闻 (用名称搜索覆盖更全)
    for kw in keywords:
        try:
            search_url = (
                "https://search-api-web.eastmoney.com/search/jsonp"
                "?cb=jQuery&param=" + json.dumps({
                    "uid": "", "keyword": kw,
                    "type": ["cmsArticleWebOld"],
                    "client": "web", "clientType": "web", "clientVersion": "curr",
                    "param": {"cmsArticleWebOld": {
                        "searchScope": "default", "sort": "default",
                        "pageIndex": 1, "pageSize": MAX_NEWS,
                        "preTag": "", "postTag": ""
                    }}
                }, separators=(',', ':'))
            )
            resp = sess.get(search_url, headers=_rotating_headers(), timeout=TIMEOUT)
            resp.encoding = "utf-8"
            # 提取 JSONP 中的 JSON 部分: 找到第一个 { 对应的完整 JSON
            text = resp.text
            start = text.find('{')
            if start < 0:
                continue
            # 从第一个 { 开始, 用括号计数找到匹配的 }
            depth = 0
            end = -1
            for i in range(start, len(text)):
                if text[i] == '{':
                    depth += 1
                elif text[i] == '}':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end < 0:
                continue
            try:
                data = json.loads(text[start:end])
            except json.JSONDecodeError:
                continue
            articles = data.get("result", {}).get("cmsArticleWebOld", [])
            if isinstance(articles, dict):
                articles = articles.get("list", [])
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
                content = art.get("content", "")
                summary = _summarize(title, abstract=content)
                sentiment, score, skw = _analyze_sentiment(title, summary)
                items.append(SearchResult(
                    title=title, snippet=summary, url=url_link,
                    source="东方财富新闻", published_date=pub_time.isoformat(),
                    sentiment=sentiment))
        except Exception as e:
            print(f"  [东方财富新闻/{kw}] 异常: {e}", file=sys.stderr)

    return items


# ═══════════════════════════════════════════════════════════
# 数据源 2: 新浪财经
# ═══════════════════════════════════════════════════════════
def _fetch_sina(code: str, days: int, name: str = "") -> List[SearchResult]:
    items = []
    cutoff = datetime.now() - timedelta(days=days)
    sess = _get_session()
    try:
        # 沪市: 600/601/603/605/688/689, 深市: 000/001/002/003/300/301
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

            summary = _summarize(title)
            sentiment, score, keywords = _analyze_sentiment(title, summary)
            items.append(SearchResult(
                title=title, snippet=summary, url=href,
                source="新浪财经", published_date=pub_time.isoformat(),
                sentiment=sentiment))
    except Exception as e:
        print(f"  [新浪财经] 异常: {e}", file=sys.stderr)
    return items


# ═══════════════════════════════════════════════════════════
# 数据源 3: 新浪 7x24 财经快讯
# ═══════════════════════════════════════════════════════════
def _fetch_sina7x24(code: str, days: int, name: str = "") -> List[SearchResult]:
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

            summary = _summarize(title, content)
            sentiment, score, skw = _analyze_sentiment(title, summary)
            items.append(SearchResult(
                title=title[:80], snippet=summary,
                url=f"https://zhibo.sina.com.cn/152/{item_data.get('id', '')}.html",
                source="新浪7x24", published_date=pub_time.isoformat(),
                sentiment=sentiment))
    except Exception as e:
        print(f"  [新浪7x24] 异常: {e}", file=sys.stderr)
    return items


# ═══════════════════════════════════════════════════════════
# 数据源 4: 腾讯财经
# ═══════════════════════════════════════════════════════════
def _fetch_qq(code: str, days: int, name: str = "") -> List[SearchResult]:
    items = []
    cutoff = datetime.now() - timedelta(days=days)
    sess = _get_session()
    try:
        keyword = name or code
        url = "https://i.news.qq.com/web_feed/getPCList"
        payload = {
            "qimei36": "", "appver": "2401.01", "devid": "", "os": "2",
            "category": "stock", "ext": {"keyword": keyword},
            "page": 0, "num": 20,
        }
        resp = sess.post(url, headers={**_rotating_headers(), "Content-Type": "application/json"},
                             json=payload, timeout=TIMEOUT)
        data = resp.json() if resp.status_code == 200 else {}
        d = data.get("data") or {}
        news_list = d.get("list", []) if isinstance(d, dict) else (d if isinstance(d, list) else [])
        for item in news_list:
            if isinstance(item, str):
                continue
            title = item.get("title", "") or item.get("brief", "")
            if not title:
                continue
            ctime = item.get("timestamp", "") or item.get("time", "") or item.get("date", "")
            if isinstance(ctime, (int, float)) and ctime > 1e9:
                pub_time = datetime.fromtimestamp(ctime)
            else:
                pub_time = _parse_time(str(ctime))
            if not pub_time or pub_time < cutoff:
                continue
            art_url = item.get("url", "") or item.get("article_url", "")
            if art_url and not art_url.startswith("http"):
                art_url = "https:" + art_url
            summary = _summarize(title, item.get("brief", "") or item.get("abstract", ""))
            sentiment, score, skw = _analyze_sentiment(title, summary)
            items.append(SearchResult(
                title=title, snippet=summary, url=art_url,
                source="腾讯财经", published_date=pub_time.isoformat(),
                sentiment=sentiment))
    except Exception as e:
        print(f"  [腾讯财经] 异常: {e}", file=sys.stderr)
    return items


# ═══════════════════════════════════════════════════════════
# 数据源 5: 凤凰财经
# ═══════════════════════════════════════════════════════════
def _fetch_ifeng(code: str, days: int, name: str = "") -> List[SearchResult]:
    items = []
    cutoff = datetime.now() - timedelta(days=days)
    sess = _get_session()
    try:
        url = "https://so.finance.ifeng.com/api/getSearchNews"
        params = {"q": code, "p": 1, "ps": 20, "type": "news"}
        resp = sess.get(url, headers=_rotating_headers(), params=params, timeout=TIMEOUT)
        if resp.status_code != 200:
            return items
        data = resp.json()
        items_list = (data.get("result", {}) or {}).get("items", []) or data.get("data", []) or []
        for item in items_list:
            title = _clean_html(item.get("title", "") or item.get("name", ""))
            if not title:
                continue
            ctime = item.get("timeStamp", "") or item.get("updateTime", "") or item.get("time", "")
            if isinstance(ctime, (int, float)) and ctime > 1e9:
                pub_time = datetime.fromtimestamp(ctime)
            else:
                pub_time = _parse_time(str(ctime))
            if not pub_time or pub_time < cutoff:
                continue
            art_url = item.get("url", "") or item.get("articleUrl", "")
            if art_url and not art_url.startswith("http"):
                art_url = "https:" + art_url
            summary = _summarize(title, item.get("brief", item.get("digest", "")))
            sentiment, score, keywords = _analyze_sentiment(title, summary)
            items.append(SearchResult(
                title=title, snippet=summary, url=art_url,
                source="凤凰财经", published_date=pub_time.isoformat(),
                sentiment=sentiment))
    except Exception as e:
        print(f"  [凤凰财经] 异常: {e}", file=sys.stderr)
    return items


# ═══════════════════════════════════════════════════════════
# 综合评分 (时间衰减)
# ═══════════════════════════════════════════════════════════
DECAY_PER_DAY = 0.8


def _time_decay(hours_old: float) -> float:
    return DECAY_PER_DAY ** (hours_old / 24)


def _calc_composite(results: List[SearchResult]) -> dict:
    """基于时间衰减的综合评分"""
    now = datetime.now()
    total_weighted = 0.0
    total_weight = 0.0
    pos = neg = neu = 0

    for r in results:
        if not r.published_date:
            continue
        pub = datetime.fromisoformat(r.published_date)
        hours = (now - pub).total_seconds() / 3600
        decay = _time_decay(hours)
        boost = 1.5 if r.sentiment != "neutral" else 1.0
        weight = decay * boost

        score = {"positive": 7.5, "negative": 2.5, "neutral": 5.0}.get(r.sentiment, 5.0)
        total_weighted += score * weight
        total_weight += weight

        if r.sentiment == "positive":
            pos += 1
        elif r.sentiment == "negative":
            neg += 1
        else:
            neu += 1

    composite = round(total_weighted / total_weight, 1) if total_weight > 0 else 5.0
    composite = max(0.0, min(10.0, composite))

    if composite >= 7:
        direction = "利好"
    elif composite <= 3:
        direction = "利空"
    elif composite >= 6:
        direction = "偏利好"
    elif composite <= 4:
        direction = "偏利空"
    else:
        direction = "中性"

    return {
        "composite_score": composite,
        "direction": direction,
        "positive": pos, "negative": neg, "neutral": neu,
    }


# ═══════════════════════════════════════════════════════════
# 去重
# ═══════════════════════════════════════════════════════════
def _dedup(results: List[SearchResult]) -> List[SearchResult]:
    """标题归一化去重 (跨数据源)"""
    seen = set()
    out = []
    for r in results:
        h = _title_hash(r.title)
        if h not in seen:
            seen.add(h)
            out.append(r)
    return out


# ═══════════════════════════════════════════════════════════
# 对外主接口
# ═══════════════════════════════════════════════════════════
ALL_FETCHERS = {
    "eastmoney": _fetch_eastmoney,
    "sina": _fetch_sina,
    "sina7x24": _fetch_sina7x24,
    "qq": _fetch_qq,
    "ifeng": _fetch_ifeng,
}


def fetch_stock_news(
    stock_code: str,
    days: int = 3,
    stock_name: str = "",
    sources: list = None,
    max_results: int = 0,
) -> SearchResponse:
    """
    A股个股新闻聚合 → SearchResponse

    Args:
        stock_code: 6位股票代码
        days: 有效天数
        stock_name: 股票名称 (可选, 不传则自动查询)
        sources: 指定数据源列表, None=全部
        max_results: 限制返回条数, 0=不限

    Returns:
        SearchResponse (metadata 含 composite_score / direction / 正负面统计)
    """
    import time as _time
    t0 = _time.time()

    # 解析代码/名称
    if not stock_name:
        stock_name = _get_stock_name(stock_code)
    query = f"{stock_code} {stock_name}"

    fetchers = {k: v for k, v in ALL_FETCHERS.items() if k in (sources or ALL_FETCHERS)}

    if not fetchers:
        return SearchResponse(
            query=query, results=[], provider="StockNews",
            success=False, search_time=0, metadata={},
            error_message="无可用数据源",
        )

    # 并发抓取
    all_results: List[SearchResult] = []
    with ThreadPoolExecutor(max_workers=len(fetchers)) as pool:
        futures = {pool.submit(fn, stock_code, days, stock_name): name
                   for name, fn in fetchers.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                all_results.extend(future.result())
            except Exception:
                pass

    # 去重 + 按时间降序
    all_results = _dedup(all_results)
    all_results.sort(key=lambda r: r.published_date or "", reverse=True)

    # 综合评分
    composite = _calc_composite(all_results)

    if max_results > 0:
        all_results = all_results[:max_results]

    return SearchResponse(
        query=query,
        results=all_results,
        provider="StockNews",
        success=len(all_results) > 0,
        search_time=round(_time.time() - t0, 2),
        metadata=composite,
    )


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════
_SENTIMENT_ICONS = {"positive": "🟢", "negative": "🔴", "neutral": "⚪"}


def main():
    parser = argparse.ArgumentParser(description="A股个股多渠道新闻聚合工具")
    parser.add_argument("code", help="股票代码或名称")
    parser.add_argument("days", type=int, nargs="?", default=3, help="有效天数 (默认 3)")
    parser.add_argument("--top", type=int, default=0, help="只输出前 N 条")
    parser.add_argument("--json-only", action="store_true", help="只输出 JSON")
    parser.add_argument("--keep-neutral", action="store_true", help="保留中性消息")
    parser.add_argument("--sources", nargs="+",
                        choices=list(ALL_FETCHERS.keys()), default=None)
    args = parser.parse_args()

    resp = fetch_stock_news(args.code.strip(), args.days, sources=args.sources)

    if not args.keep_neutral:
        resp.results = [r for r in resp.results if r.sentiment != "neutral"]
        resp.metadata = _calc_composite(resp.results)

    if args.top > 0:
        resp.results = resp.results[:args.top]

    output = {
        "query": resp.query,
        "provider": resp.provider,
        "success": resp.success,
        "search_time": resp.search_time,
        **resp.metadata,
        "news": [r.to_dict() for r in resp.results],
    }

    if args.json_only:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        sc = output.get("composite_score", 5)
        direction = output.get("direction", "中性")
        icon = "🟢" if sc >= 6 else ("🔴" if sc <= 4 else "⚪")
        bars = "█" * max(1, int(abs(sc - 5) * 2))
        print(f"\n  {icon} 综合评分: {bars} {sc}/10  [{direction}]  "
              f"利好{output.get('positive',0)} 利空{output.get('negative',0)} 中性{output.get('neutral',0)}")
        for i, r in enumerate(resp.results, 1):
            ri = _SENTIMENT_ICONS.get(r.sentiment, "⚪")
            print(f"  {ri} {i:>3}. [{r.source}] {r.title}")
            if r.snippet and r.snippet != r.title:
                print(f"      📝 {r.snippet[:120]}")
        print(f"\n  共 {len(resp.results)} 条, 耗时 {resp.search_time}s")


if __name__ == "__main__":
    main()
