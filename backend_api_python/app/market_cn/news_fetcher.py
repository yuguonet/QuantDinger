#!/usr/bin/env python3
"""
财经新闻直接抓取 — 不依赖 AKShare (v2.2)
数据源: 东方财富、新浪财经、央视网 直接爬取
依赖: pip install requests

v2.2 修复:
- 东方财富: API 新增 req_trace 参数, 改用 7x24 快讯接口
- 财联社: content 字段类型变更(str), 适配 brief + ctime
- 新浪财经: zhibo API 失效, 改用 feed.mix.sina.com.cn 滚动新闻
- 华尔街见闻: 保持不变 (正常工作)
"""

import requests
import json
import re
import uuid
from datetime import datetime
from typing import List, Dict, Any

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}


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
    """
    新浪财经新闻
    接口: feed.mix.sina.com.cn 滚动新闻 API
    注意: 该 API 有频率限制, 部分 lid 会返回 403, 自动尝试备用 lid
    """
    url = "https://feed.mix.sina.com.cn/api/roll/get"
    # 2516=财经综合, 2509=股票, 2511=国际, 2519=7x24
    lids = ["2516", "2509", "2511", "2519"]
    for lid in lids:
        params = {
            "pageid": "153",
            "lid": lid,
            "k": "",
            "num": str(max_items),
            "page": "1",
        }
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=10)
            if resp.status_code != 200:
                continue
            data = resp.json()
            items = data.get("result", {}).get("data", [])
            if not items:
                continue
            results = []
            for item in items[:max_items]:
                title = item.get("title", "").strip()
                if not title:
                    continue
                ctime = item.get("ctime", "")
                try:
                    time_str = datetime.fromtimestamp(int(ctime)).strftime("%Y-%m-%d %H:%M") if ctime else ""
                except (ValueError, TypeError):
                    time_str = str(ctime)
                results.append({
                    "title": title,
                    "url": item.get("url", ""),
                    "time": time_str,
                    "source": "新浪财经",
                })
            if results:
                return results
        except Exception:
            continue
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
            results.append({
                "title": title.strip(),
                "time": datetime.fromtimestamp(item.get("display_time", 0)).strftime("%H:%M"),
                "source": "华尔街见闻",
            })
        return results
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
        ("东方财富", lambda: fetch_eastmoney_news(max_items=max_per_source)),
        ("新浪财经", lambda: fetch_sina_finance_news(max_items=max_per_source)),
        ("AKShare", lambda: fetch_akshare_news(max_items=max_per_source)),
    ]
    for name, fetcher in sources:
        try:
            items = fetcher()
            valid = [i for i in items if "error" not in i]
            all_news.extend(valid)
        except Exception:
            pass

    return all_news


# ═══════════════════════════════════════════════════
#  测试
# ═══════════════════════════════════════════════════

if __name__ == "__main__":
    print(f"\n{'='*60}")
    print(f"  📰 新闻抓取测试 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}\n")

    sources = [
        ("东方财富", fetch_eastmoney_news),
        ("财联社", fetch_cls_news),
        ("华尔街见闻", fetch_wallstreetcn_news),
        ("新浪财经", fetch_sina_finance_news),
    ]

    total = 0
    for name, fn in sources:
        items = fn(max_items=5)
        valid = [i for i in items if "error" not in i]
        total += len(valid)
        print(f"  ✅ {name}: {len(valid)} 条")
        for i, item in enumerate(valid[:3], 1):
            title = item.get("title", "")[:60]
            print(f"     {i}. {title}")

    print(f"\n  共 {total} 条新闻")
