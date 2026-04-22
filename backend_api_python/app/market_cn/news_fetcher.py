#!/usr/bin/env python3
"""
财经新闻直接抓取 — 不依赖 AKShare
数据源: 东方财富、新浪财经、央视网 直接爬取
依赖: pip install requests beautifulsoup4
"""

import requests
import json
from datetime import datetime
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}


def fetch_eastmoney_news(category="财经", max_items=20):
    """东方财富财经新闻 (JSON API)"""
    url = "https://30.push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": 1,
        "pz": max_items,
        "po": 1,
        "np": 1,
        "fltt": 2,
        "invt": 2,
        "fid": "f3",
        "fs": "b:BK0816",  # 财经要闻板块
        "fields": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,f20,f21,f23,f24,f25,f26,f22,f33,f11,f62,f128,f136,f115,f152",
    }
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=10)
        data = resp.json()
        items = data.get("data", {}).get("diff", [])
        if items:
            return [{"title": i.get("f14", ""), "code": i.get("f12", "")} for i in items]
    except Exception:
        pass  # API 不可用，降级到网页抓取

    # 备用: 新闻列表页
    url2 = "https://finance.eastmoney.com/"
    try:
        resp = requests.get(url2, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        news = []
        for a in soup.select("a[href]")[:max_items]:
            title = a.get_text(strip=True)
            href = a.get("href", "")
            if len(title) > 8 and "http" in href:
                news.append({"title": title, "url": href})
        return news
    except Exception as e:
        return [{"error": str(e)}]


def fetch_sina_finance_news(max_items=20):
    """新浪财经新闻 (API)"""
    url = "https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size=" + str(max_items)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        data = resp.json()
        items = data.get("result", {}).get("data", {}).get("feed", {}).get("feed_list", [])
        return [{"title": item.get("rich_text", "").strip(), "time": item.get("create_time", "")}
                for item in items if item.get("rich_text")]
    except Exception as e:
        return [{"error": str(e)}]


def fetch_cls_news(max_items=20):
    """财联社电报 (公开 API)"""
    url = "https://www.cls.cn/nodeapi/updateTelegraphList"
    params = {"app": "CailianpressWeb", "os": "web", "sv": "7.7.5", "rn": max_items}
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=10)
        data = resp.json()
        items = data.get("data", {}).get("roll_data", [])
        news = []
        for item in items:
            for content in item.get("content", []):
                title = content.get("brief", content.get("title", ""))
                if title:
                    news.append({
                        "title": title,
                        "time": datetime.fromtimestamp(content.get("ctime", 0)).strftime("%H:%M"),
                        "source": "财联社"
                    })
        return news[:max_items]
    except Exception as e:
        return [{"error": str(e)}]


def fetch_wallstreetcn_news(max_items=20):
    """华尔街见闻快讯 (公开 API)"""
    url = "https://api-one-wscn.awtmt.com/apiv1/content/lives"
    params = {"channel": "global-channel", "limit": max_items}
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=10)
        data = resp.json()
        items = data.get("data", {}).get("items", [])
        return [{
            "title": item.get("content_plain", item.get("title", "")),
            "time": datetime.fromtimestamp(item.get("display_time", 0)).strftime("%H:%M"),
            "source": "华尔街见闻"
        } for item in items]
    except Exception as e:
        return [{"error": str(e)}]


def fetch_akshare_news(max_items=20):
    """AKShare 财经新闻 (降级备选)"""
    try:
        import akshare as ak
        df = ak.stock_news_em(symbol="财经")
        news = []
        for _, row in df.head(max_items).iterrows():
            title = str(row.iloc[0])
            time_str = str(row.iloc[1]) if len(row) > 1 else ""
            url = str(row.iloc[2]) if len(row) > 2 else ""
            news.append({"title": title, "time": time_str, "url": url, "source": "AKShare-东方财富"})
        return news
    except Exception as e:
        return [{"error": f"akshare: {e}"}]


def fetch_all_news(max_per_source=10):
    """聚合所有新闻源"""
    all_news = []
    sources = [
        ("财联社", lambda: fetch_cls_news(max_items=max_per_source)),
        ("华尔街见闻", lambda: fetch_wallstreetcn_news(max_items=max_per_source)),
        ("新浪财经", lambda: fetch_sina_finance_news(max_items=max_per_source)),
        ("东方财富", lambda: fetch_eastmoney_news(max_items=max_per_source)),
        ("AKShare", lambda: fetch_akshare_news(max_items=max_per_source)),
    ]
    for name, fetcher in sources:
        try:
            items = fetcher()
            valid = [i for i in items if "error" not in i]
            all_news.extend(valid)
            print(f"  ✅ {name}: {len(valid)} 条")
        except Exception as e:
            print(f"  ⚠️ {name} 失败: {e}")

    return all_news


# ═══════════════════════════════════════════════════
#  测试
# ═══════════════════════════════════════════════════

if __name__ == "__main__":
    print(f"\n{'='*60}")
    print(f"  📰 新闻抓取测试 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}\n")

    news = fetch_all_news(max_per_source=5)
    print(f"\n  共 {len(news)} 条新闻:\n")
    for i, item in enumerate(news[:15], 1):
        title = item.get("title", "")[:60]
        source = item.get("source", "")
        time_str = item.get("time", "")
        print(f"  {i}. [{source}|{time_str}] {title}")
