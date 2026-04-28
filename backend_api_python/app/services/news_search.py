"""
Search service v2.3 - 搜索引擎调度器
整合多个搜索引擎 + 财经新闻源，支持多源并行 + 聚合去重

支持的搜索引擎（按优先级）：
1. Bocha AI (博查) - 国内 Perplexity 替代，免费额度
2. 百度搜索 - 国内直连，无需代理
3. SerpAPI - Google/Bing/百度结果抓取（需代理）
4. Google CSE - 自定义搜索引擎（需代理）
5. Bing Search API（不稳定）
6. DuckDuckGo - 免费兜底（需代理）

支持的财经新闻源：
7. 财联社电报
8. 华尔街见闻快讯
9. 东方财富财经新闻
10. 新浪财经新闻
11. AKShare 财经新闻 (降级备选)

注意: 新闻缓存、情感评分、search_cn_stock_news 已迁移至 app/data_providers/news.py
"""
import requests
import time
import re
import hashlib
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from itertools import cycle
from urllib.parse import urlparse

from app.utils.logger import get_logger
from app.utils.config_loader import load_addon_config
from app.services.news_provider import (
    # 旧接口 (向后兼容, search.py Provider 类使用)
    fetch_cls_news,
    fetch_wallstreetcn_news,
    fetch_eastmoney_news,
    fetch_sina_finance_news,
    fetch_akshare_news,
    # 新统一接口 (调度层使用)
    fetch_cls_market,
    fetch_wallstreetcn_market,
    fetch_eastmoney_market,
    fetch_sina_market,
    fetch_akshare_market,
    fetch_eastmoney_stock,
    fetch_sina_stock,
    fetch_sina7x24_stock,
    fetch_tencent_stock,
    fetch_ifeng_stock,
    fetch_all_market_news,
    fetch_all_stock_news,
)

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════
# 编码安全工具
# ═══════════════════════════════════════════════════════════════

def _safe_encode(text: str, max_len: int = 0) -> str:
    """
    强制 utf-8 安全清洗:
    - None → ''
    - bytes → decode (errors='replace')
    - str → encode('utf-8', errors='replace') → decode
    - 去除 NUL 字符
    - 可选截断
    """
    if text is None:
        return ''
    if isinstance(text, bytes):
        text = text.decode('utf-8', errors='replace')
    else:
        text = str(text)
        text = text.encode('utf-8', errors='replace').decode('utf-8', errors='replace')
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    if max_len > 0 and len(text) > max_len:
        text = text[:max_len]
    return text



@dataclass
class SearchResult:
    """搜索结果数据类"""
    title: str
    snippet: str  # 摘要
    url: str
    source: str  # 来源网站
    published_date: Optional[str] = None
    sentiment: str = 'neutral'  # 情绪标签
    sentiment_score: Optional[float] = None  # 数值评分 -10 ~ +10, -999=一票否决

    def to_text(self) -> str:
        """转换为文本格式"""
        date_str = f" ({self.published_date})" if self.published_date else ""
        return f"【{self.source}】{self.title}{date_str}\n{self.snippet}"

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'title': self.title,
            'link': self.url,
            'snippet': self.snippet,
            'source': self.source,
            'published': self.published_date or '',
            'sentiment': self.sentiment,
            'sentiment_score': self.sentiment_score,
        }


@dataclass
class SearchResponse:
    """搜索响应

    metadata 附加字段 (由 StockNews 等 provider 填充):
      - composite_score: float  综合评分 -5 ~ +5 (RMS聚合+时间衰减, 0=中性)
      - direction: str          利好/偏利好/中性/偏利空/利空/重大利空
      - positive/negative/neutral: int  情绪分布计数
      - veto: bool              是否触发一票否决
      - stock_news_count: int   stock_news 来源条数 (search_cn_stock_news 填充)
      - web_results_added: int  web 去重后追加条数
      - total_merged: int       合并后总条数
    """
    query: str
    results: List[SearchResult]
    provider: str  # 使用的搜索引擎
    success: bool = True
    error_message: Optional[str] = None
    search_time: float = 0.0  # 搜索耗时（秒）
    metadata: Dict[str, Any] = field(default_factory=dict)  # 附加元数据（如综合评分）

    def to_context(self, max_results: int = 5) -> str:
        """将搜索结果转换为可用于 AI 分析的上下文"""
        if not self.success or not self.results:
            return f"搜索 '{self.query}' 未找到相关结果。"

        lines = [f"【{self.query} 搜索结果】（来源：{self.provider}）"]
        for i, result in enumerate(self.results[:max_results], 1):
            lines.append(f"\n{i}. {result.to_text()}")

        return "\n".join(lines)

    def to_list(self) -> List[Dict[str, Any]]:
        """转换为列表格式（兼容旧接口）"""
        return [r.to_dict() for r in self.results]


class BaseSearchProvider(ABC):
    """搜索引擎基类"""

    def __init__(self, api_keys: List[str], name: str):
        self._api_keys = api_keys
        self._name = name
        self._key_cycle = cycle(api_keys) if api_keys else None
        self._key_usage: Dict[str, int] = {key: 0 for key in api_keys}
        self._key_errors: Dict[str, int] = {key: 0 for key in api_keys}

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_available(self) -> bool:
        """检查是否有可用的 API Key"""
        return bool(self._api_keys)

    def _get_next_key(self) -> Optional[str]:
        if not self._key_cycle:
            return None
        for _ in range(len(self._api_keys)):
            key = next(self._key_cycle)
            if self._key_errors.get(key, 0) < 3:
                return key
        logger.warning(f"[{self._name}] 所有 API Key 都有错误记录，重置错误计数")
        self._key_errors = {key: 0 for key in self._api_keys}
        return self._api_keys[0] if self._api_keys else None

    def _record_success(self, key: str) -> None:
        self._key_usage[key] = self._key_usage.get(key, 0) + 1
        if key in self._key_errors and self._key_errors[key] > 0:
            self._key_errors[key] -= 1

    def _record_error(self, key: str) -> None:
        self._key_errors[key] = self._key_errors.get(key, 0) + 1
        logger.warning(f"[{self._name}] API Key {key[:8]}... 错误计数: {self._key_errors[key]}")

    @abstractmethod
    def _do_search(self, query: str, api_key: str, max_results: int, days: int = 7) -> SearchResponse:
        pass

    def search(self, query: str, max_results: int = 5, days: int = 7) -> SearchResponse:
        api_key = self._get_next_key()
        if not api_key:
            return SearchResponse(
                query=query, results=[], provider=self._name,
                success=False, error_message=f"{self._name} 未配置 API Key"
            )
        start_time = time.time()
        try:
            response = self._do_search(query, api_key, max_results, days=days)
            response.search_time = time.time() - start_time
            if response.success:
                self._record_success(api_key)
                logger.info(f"[{self._name}] 搜索 '{query}' 成功，返回 {len(response.results)} 条结果，耗时 {response.search_time:.2f}s")
            else:
                self._record_error(api_key)
            return response
        except Exception as e:
            self._record_error(api_key)
            elapsed = time.time() - start_time
            logger.error(f"[{self._name}] 搜索 '{query}' 失败: {e}")
            return SearchResponse(
                query=query, results=[], provider=self._name,
                success=False, error_message=str(e), search_time=elapsed
            )

    @staticmethod
    def _extract_domain(url: str) -> str:
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.replace('www.', '')
            return domain or '未知来源'
        except:
            return '未知来源'


class TavilySearchProvider(BaseSearchProvider):
    """Tavily 搜索引擎"""

    def __init__(self, api_keys: List[str]):
        super().__init__(api_keys, "Tavily")

    def _do_search(self, query: str, api_key: str, max_results: int, days: int = 7) -> SearchResponse:
        try:
            from tavily import TavilyClient
        except ImportError:
            return self._do_search_rest(query, api_key, max_results, days)
        try:
            client = TavilyClient(api_key=api_key)
            response = client.search(
                query=query, search_depth="advanced", max_results=max_results,
                include_answer=False, include_raw_content=False, days=days,
            )
            results = []
            for item in response.get('results', []):
                results.append(SearchResult(
                    title=_safe_encode(item.get('title', '')),
                    snippet=_safe_encode(item.get('content', ''))[:500],
                    url=_safe_encode(item.get('url', '')),
                    source=self._extract_domain(item.get('url', '')),
                    published_date=item.get('published_date'),
                ))
            return SearchResponse(query=query, results=results, provider=self.name, success=True)
        except Exception as e:
            error_msg = str(e)
            if 'rate limit' in error_msg.lower() or 'quota' in error_msg.lower():
                error_msg = f"API 配额已用尽: {error_msg}"
            return SearchResponse(query=query, results=[], provider=self.name, success=False, error_message=error_msg)

    def _do_search_rest(self, query: str, api_key: str, max_results: int, days: int = 7) -> SearchResponse:
        try:
            url = "https://api.tavily.com/search"
            headers = {'Content-Type': 'application/json'}
            payload = {
                'api_key': api_key, 'query': query, 'search_depth': 'advanced',
                'max_results': max_results, 'include_answer': False, 'include_raw_content': False,
            }
            response = requests.post(url, headers=headers, json=payload, timeout=15)
            if response.status_code != 200:
                return SearchResponse(query=query, results=[], provider=self.name, success=False,
                                      error_message=f"HTTP {response.status_code}: {response.text}")
            data = response.json()
            results = []
            for item in data.get('results', []):
                results.append(SearchResult(
                    title=_safe_encode(item.get('title', '')),
                    snippet=_safe_encode(item.get('content', ''))[:500],
                    url=_safe_encode(item.get('url', '')),
                    source=self._extract_domain(item.get('url', '')),
                    published_date=item.get('published_date'),
                ))
            return SearchResponse(query=query, results=results, provider=self.name, success=True)
        except Exception as e:
            return SearchResponse(query=query, results=[], provider=self.name, success=False, error_message=str(e))


class SerpAPISearchProvider(BaseSearchProvider):
    """SerpAPI 搜索引擎"""

    def __init__(self, api_keys: List[str]):
        super().__init__(api_keys, "SerpAPI")

    def _do_search(self, query: str, api_key: str, max_results: int, days: int = 7) -> SearchResponse:
        try:
            from serpapi import GoogleSearch
        except ImportError:
            return self._do_search_rest(query, api_key, max_results, days)
        try:
            tbs = "qdr:w"
            if days <= 1:
                tbs = "qdr:d"
            elif days <= 7:
                tbs = "qdr:w"
            elif days <= 30:
                tbs = "qdr:m"
            else:
                tbs = "qdr:y"
            params = {
                "engine": "google", "q": query, "api_key": api_key,
                "google_domain": "google.com.hk", "hl": "zh-cn", "gl": "cn",
                "tbs": tbs, "num": max_results
            }
            search = GoogleSearch(params)
            response = search.get_dict()
            results = []
            for item in response.get('organic_results', [])[:max_results]:
                results.append(SearchResult(
                    title=_safe_encode(item.get('title', '')),
                    snippet=_safe_encode(item.get('snippet', ''))[:500],
                    url=_safe_encode(item.get('link', '')),
                    source=item.get('source', self._extract_domain(item.get('link', ''))),
                    published_date=item.get('date'),
                ))
            return SearchResponse(query=query, results=results, provider=self.name, success=True)
        except Exception as e:
            return SearchResponse(query=query, results=[], provider=self.name, success=False, error_message=str(e))

    def _do_search_rest(self, query: str, api_key: str, max_results: int, days: int = 7) -> SearchResponse:
        try:
            tbs = "qdr:w"
            if days <= 1:
                tbs = "qdr:d"
            elif days <= 7:
                tbs = "qdr:w"
            elif days <= 30:
                tbs = "qdr:m"
            url = "https://serpapi.com/search"
            params = {
                "engine": "google", "q": query, "api_key": api_key,
                "hl": "zh-cn", "gl": "cn", "tbs": tbs, "num": max_results
            }
            response = requests.get(url, params=params, timeout=15)
            if response.status_code != 200:
                return SearchResponse(query=query, results=[], provider=self.name, success=False,
                                      error_message=f"HTTP {response.status_code}")
            data = response.json()
            results = []
            for item in data.get('organic_results', [])[:max_results]:
                results.append(SearchResult(
                    title=_safe_encode(item.get('title', '')),
                    snippet=_safe_encode(item.get('snippet', ''))[:500],
                    url=_safe_encode(item.get('link', '')),
                    source=item.get('source', self._extract_domain(item.get('link', ''))),
                    published_date=item.get('date'),
                ))
            return SearchResponse(query=query, results=results, provider=self.name, success=True)
        except Exception as e:
            return SearchResponse(query=query, results=[], provider=self.name, success=False, error_message=str(e))


class GoogleSearchProvider(BaseSearchProvider):
    """Google Custom Search (CSE) 搜索引擎"""

    def __init__(self, api_key: str, cx: str):
        super().__init__([api_key] if api_key else [], "Google")
        self._cx = cx

    def _do_search(self, query: str, api_key: str, max_results: int, days: int = 7) -> SearchResponse:
        if not self._cx:
            return SearchResponse(query=query, results=[], provider=self.name, success=False,
                                  error_message="Google Search 未配置 CX")
        try:
            url = "https://www.googleapis.com/customsearch/v1"
            params = {'key': api_key, 'cx': self._cx, 'q': query, 'num': min(max_results, 10)}
            if days <= 1:
                params['dateRestrict'] = 'd1'
            elif days <= 7:
                params['dateRestrict'] = 'w1'
            elif days <= 30:
                params['dateRestrict'] = 'm1'
            response = requests.get(url, params=params, timeout=10)
            if response.status_code == 429:
                _google_quota_exhausted = True
                tomorrow = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
                _google_quota_reset_time = tomorrow.timestamp()
                return SearchResponse(query=query, results=[], provider=self.name, success=False,
                                      error_message="Google API 配额已用尽")
            response.raise_for_status()
            data = response.json()
            results = []
            if 'items' in data:
                for item in data['items']:
                    results.append(SearchResult(
                        title=_safe_encode(item.get('title', '')),
                        snippet=_safe_encode(item.get('snippet', '')),
                        url=_safe_encode(item.get('link', '')),
                        source='Google',
                        published_date=item.get('pagemap', {}).get('metatags', [{}])[0].get('article:published_time', ''),
                    ))
            return SearchResponse(query=query, results=results, provider=self.name, success=True)
        except Exception as e:
            return SearchResponse(query=query, results=[], provider=self.name, success=False, error_message=str(e))


class BingSearchProvider(BaseSearchProvider):
    """Bing Search API 搜索引擎"""

    def __init__(self, api_key: str):
        super().__init__([api_key] if api_key else [], "Bing")

    def _do_search(self, query: str, api_key: str, max_results: int, days: int = 7) -> SearchResponse:
        try:
            url = "https://api.bing.microsoft.com/v7.0/search"
            headers = {"Ocp-Apim-Subscription-Key": api_key}
            params = {"q": query, "count": max_results, "textDecorations": True, "textFormat": "HTML"}
            response = requests.get(url, headers=headers, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            results = []
            if 'webPages' in data and 'value' in data['webPages']:
                for item in data['webPages']['value']:
                    results.append(SearchResult(
                        title=_safe_encode(item.get('name', '')),
                        snippet=_safe_encode(item.get('snippet', '')),
                        url=_safe_encode(item.get('url', '')),
                        source='Bing',
                        published_date=item.get('datePublished', ''),
                    ))
            return SearchResponse(query=query, results=results, provider=self.name, success=True)
        except Exception as e:
            return SearchResponse(query=query, results=[], provider=self.name, success=False, error_message=str(e))


class BaiduSearchProvider(BaseSearchProvider):
    """百度搜索 (千帆 AppBuilder API)"""

    def __init__(self, api_key: str):
        super().__init__([api_key] if api_key else [], "Baidu")

    def _do_search(self, query: str, api_key: str, max_results: int, days: int = 7) -> SearchResponse:
        try:
            url = "https://appbuilder.baidu.com/rpc/2.0/cloud_custom/v1/search"
            headers = {
                "Content-Type": "application/json",
                "X-Appbuilder-Authorization": f"Bearer {api_key}",
            }
            payload = {
                "query": query, "search_lang": "zh",
                "search_recency_filter": self._days_to_recency(days),
                "result_num": min(max_results, 10),
            }
            resp = requests.post(url, headers=headers, json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            results = []
            for item in (data.get("result") or {}).get("results", [])[:max_results]:
                results.append(SearchResult(
                    title=_safe_encode(item.get("title", "")),
                    snippet=_safe_encode(item.get("content", ""))[:500],
                    url=_safe_encode(item.get("url", "")),
                    source="百度",
                    published_date=item.get("publish_time"),
                ))
            return SearchResponse(query=query, results=results, provider=self.name,
                                  success=len(results) > 0,
                                  error_message=None if results else "百度搜索无结果")
        except Exception as e:
            return SearchResponse(query=query, results=[], provider=self.name,
                                  success=False, error_message=str(e))

    @staticmethod
    def _days_to_recency(days: int) -> str:
        if days <= 1:
            return "day"
        elif days <= 7:
            return "week"
        elif days <= 30:
            return "month"
        return "all"


class BochaAISearchProvider(BaseSearchProvider):
    """Bocha AI (博查) — 国内 AI 搜索引擎"""

    def __init__(self, api_key: str):
        super().__init__([api_key] if api_key else [], "BochaAI")

    def _do_search(self, query: str, api_key: str, max_results: int, days: int = 7) -> SearchResponse:
        try:
            url = "https://api.bochaai.com/v1/web-search"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            }
            payload = {
                "query": query, "count": min(max_results, 10),
                "search_lang": "zh", "freshness": self._days_to_freshness(days),
            }
            resp = requests.post(url, headers=headers, json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            results = []
            webpages = (data.get("data") or {}).get("webPages", {}).get("value", [])
            for item in webpages[:max_results]:
                results.append(SearchResult(
                    title=_safe_encode(item.get("name", "")),
                    snippet=_safe_encode(item.get("snippet", ""))[:500],
                    url=_safe_encode(item.get("url", "")),
                    source=self._extract_domain(item.get("url", "")),
                    published_date=item.get("datePublished"),
                ))
            return SearchResponse(query=query, results=results, provider=self.name,
                                  success=len(results) > 0,
                                  error_message=None if results else "BochaAI 搜索无结果")
        except Exception as e:
            return SearchResponse(query=query, results=[], provider=self.name,
                                  success=False, error_message=str(e))

    @staticmethod
    def _days_to_freshness(days: int) -> str:
        if days <= 1:
            return "pd"
        elif days <= 7:
            return "pw"
        elif days <= 30:
            return "pm"
        return ""


class DuckDuckGoSearchProvider(BaseSearchProvider):
    """DuckDuckGo 搜索引擎（免费，无需 API Key）"""

    def __init__(self):
        super().__init__(['free'], "DuckDuckGo")

    def _do_search(self, query: str, api_key: str, max_results: int, days: int = 7) -> SearchResponse:
        try:
            url = "https://api.duckduckgo.com/"
            params = {'q': query, 'format': 'json', 'no_html': 1, 'skip_disambig': 1}
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            results = []
            related_topics = data.get('RelatedTopics', [])
            for topic in related_topics[:max_results]:
                if isinstance(topic, dict):
                    if 'FirstURL' in topic:
                        results.append(SearchResult(
                            title=_safe_encode(topic.get('Text', ''))[:100],
                            snippet=_safe_encode(topic.get('Text', '')),
                            url=_safe_encode(topic.get('FirstURL', '')),
                            source='DuckDuckGo',
                        ))
                    elif 'Topics' in topic:
                        for sub_topic in topic['Topics']:
                            if len(results) >= max_results:
                                break
                            if 'FirstURL' in sub_topic:
                                results.append(SearchResult(
                                    title=_safe_encode(sub_topic.get('Text', ''))[:100],
                                    snippet=_safe_encode(sub_topic.get('Text', '')),
                                    url=_safe_encode(sub_topic.get('FirstURL', '')),
                                    source='DuckDuckGo',
                                ))
            if data.get('AbstractURL') and len(results) < max_results:
                results.insert(0, SearchResult(
                    title=_safe_encode(data.get('Heading', query)),
                    snippet=_safe_encode(data.get('AbstractText', '')),
                    url=_safe_encode(data.get('AbstractURL', '')),
                    source='DuckDuckGo',
                ))
            if not results:
                results = self._search_html(query, max_results)
            return SearchResponse(query=query, results=results[:max_results], provider=self.name,
                                  success=len(results) > 0)
        except Exception as e:
            return SearchResponse(query=query, results=[], provider=self.name,
                                  success=False, error_message=str(e))

    def _search_html(self, query: str, max_results: int) -> List[SearchResult]:
        try:
            url = "https://lite.duckduckgo.com/lite/"
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            data = {'q': query}
            response = requests.post(url, headers=headers, data=data, timeout=10)
            response.raise_for_status()
            results = []
            html = response.text
            link_pattern = r'<a[^>]*class="result-link"[^>]*href="([^"]*)"[^>]*>([^<]*)</a>'
            snippet_pattern = r'<td[^>]*class="result-snippet"[^>]*>([^<]*)</td>'
            links = re.findall(link_pattern, html)
            snippets = re.findall(snippet_pattern, html)
            for i, (link, title) in enumerate(links[:max_results]):
                snippet = snippets[i] if i < len(snippets) else ''
                if link and title:
                    results.append(SearchResult(
                        title=_safe_encode(title.strip()),
                        snippet=_safe_encode(snippet.strip()),
                        url=_safe_encode(link),
                        source='DuckDuckGo',
                    ))
            return results
        except Exception as e:
            logger.debug(f"DuckDuckGo HTML search failed: {e}")
            return []


# ═══════════════════════════════════════════════════════════════
# 财经新闻 Providers (从 news_provider 整合)
# ═══════════════════════════════════════════════════════════════

class _BaseNewsProvider(BaseSearchProvider):
    """新闻源基类 — 无需 API Key"""

    def __init__(self, name: str):
        super().__init__(['free'], name)

    def _news_to_results(self, items: List[Dict]) -> List[SearchResult]:
        """将新闻 dict 列表转为 SearchResult 列表"""
        if not items:
            return []
        results = []
        for item in items:
            if not isinstance(item, dict) or "error" in item:
                continue
            title = _safe_encode(item.get("title", ""))
            if not title:
                continue
            results.append(SearchResult(
                title=title,
                snippet=title,
                url=_safe_encode(item.get("url", "")),
                source=item.get("source", self.name),
                published_date=item.get("time", ""),
            ))
        return results

    @abstractmethod
    def _do_search(self, query: str, api_key: str, max_results: int, days: int = 7) -> SearchResponse:
        pass


class CLSNewsProvider(_BaseNewsProvider):
    """财联社电报"""

    def __init__(self):
        super().__init__("财联社")

    def _do_search(self, query: str, api_key: str, max_results: int, days: int = 7) -> SearchResponse:
        try:
            items = fetch_cls_news(max_items=max_results)
            results = self._news_to_results(items)
            return SearchResponse(query=query, results=results, provider=self.name, success=len(results) > 0)
        except Exception as e:
            return SearchResponse(query=query, results=[], provider=self.name, success=False, error_message=str(e))


class WallStreetCNNewsProvider(_BaseNewsProvider):
    """华尔街见闻快讯"""

    def __init__(self):
        super().__init__("华尔街见闻")

    def _do_search(self, query: str, api_key: str, max_results: int, days: int = 7) -> SearchResponse:
        try:
            items = fetch_wallstreetcn_news(max_items=max_results)
            results = self._news_to_results(items)
            return SearchResponse(query=query, results=results, provider=self.name, success=len(results) > 0)
        except Exception as e:
            return SearchResponse(query=query, results=[], provider=self.name, success=False, error_message=str(e))


class EastMoneyNewsProvider(_BaseNewsProvider):
    """东方财富财经新闻"""

    def __init__(self):
        super().__init__("东方财富")

    def _do_search(self, query: str, api_key: str, max_results: int, days: int = 7) -> SearchResponse:
        try:
            items = fetch_eastmoney_news(max_items=max_results)
            results = self._news_to_results(items)
            return SearchResponse(query=query, results=results, provider=self.name, success=len(results) > 0)
        except Exception as e:
            return SearchResponse(query=query, results=[], provider=self.name, success=False, error_message=str(e))


class SinaFinanceNewsProvider(_BaseNewsProvider):
    """新浪财经新闻"""

    def __init__(self):
        super().__init__("新浪财经")

    def _do_search(self, query: str, api_key: str, max_results: int, days: int = 7) -> SearchResponse:
        try:
            items = fetch_sina_finance_news(max_items=max_results)
            results = self._news_to_results(items)
            return SearchResponse(query=query, results=results, provider=self.name, success=len(results) > 0)
        except Exception as e:
            return SearchResponse(query=query, results=[], provider=self.name, success=False, error_message=str(e))


class AKShareNewsProvider(_BaseNewsProvider):
    """AKShare 财经新闻 (降级备选)"""

    def __init__(self):
        super().__init__("AKShare")

    def _do_search(self, query: str, api_key: str, max_results: int, days: int = 7) -> SearchResponse:
        try:
            items = fetch_akshare_news(max_items=max_results)
            results = self._news_to_results(items)
            return SearchResponse(query=query, results=results, provider=self.name, success=len(results) > 0)
        except Exception as e:
            return SearchResponse(query=query, results=[], provider=self.name, success=False, error_message=str(e))


class SearchService:
    """
    搜索服务 v2.2

    功能:
    1. 管理多个搜索引擎 (Web)
    2. 自动故障转移
    3. 结果聚合和格式化

    Provider 架构:
    - _providers: Web 搜索引擎列表 (BochaAI/Baidu/Tavily/SerpAPI/Google/Bing/DuckDuckGo)

    核心方法:
    - search_parallel():      多源并行搜索 + 聚合去重 (默认)
    - search_with_fallback():  串行故障转移 (备用)
    """

    def __init__(self):
        self._providers: List[BaseSearchProvider] = []
        self._config = {}
        self._load_config()
        self._init_providers()

    def _load_config(self):
        config = load_addon_config()
        self._config = config.get('search', {})
        self.provider = self._config.get('provider', 'google')
        self.max_results = int(self._config.get('max_results', 10))

    def _init_providers(self):
        from app.config import APIKeys

        # ── 财经新闻源 (免费, 优先) ──
        self._providers.append(CLSNewsProvider())
        self._providers.append(WallStreetCNNewsProvider())
        self._providers.append(EastMoneyNewsProvider())
        self._providers.append(SinaFinanceNewsProvider())
        self._providers.append(AKShareNewsProvider())
        logger.info("已配置 5 个财经新闻源 (财联社/华尔街见闻/东方财富/新浪财经/AKShare)")

        # ── Web 搜索引擎 ──
        bocha_key = APIKeys.BOCHA_AI_API_KEY
        if bocha_key:
            self._providers.append(BochaAISearchProvider(bocha_key))
            logger.info("已配置 BochaAI 搜索（国内优先）")

        baidu_key = APIKeys.BAIDU_SEARCH_API_KEY
        if baidu_key:
            self._providers.append(BaiduSearchProvider(baidu_key))
            logger.info("已配置百度搜索（国内原生）")

        tavily_keys = APIKeys.TAVILY_API_KEYS
        if tavily_keys:
            self._providers.append(TavilySearchProvider(tavily_keys))
            logger.info(f"已配置 Tavily 搜索，共 {len(tavily_keys)} 个 API Key")

        serpapi_keys = APIKeys.SERPAPI_KEYS
        if serpapi_keys:
            self._providers.append(SerpAPISearchProvider(serpapi_keys))
            logger.info(f"已配置 SerpAPI 搜索，共 {len(serpapi_keys)} 个 API Key")

        google_api_key = self._config.get('google', {}).get('api_key')
        google_cx = self._config.get('google', {}).get('cx')
        if google_api_key and google_cx:
            self._providers.append(GoogleSearchProvider(google_api_key, google_cx))
            logger.info("已配置 Google CSE 搜索")

        bing_api_key = self._config.get('bing', {}).get('api_key')
        if bing_api_key:
            self._providers.append(BingSearchProvider(bing_api_key))
            logger.info("已配置 Bing 搜索")

        self._providers.append(DuckDuckGoSearchProvider())
        logger.info("已配置 DuckDuckGo 搜索（免费兜底）")

        if not any(p.name in ("BochaAI", "Baidu") for p in self._providers):
            logger.warning("未配置国内搜索引擎（BochaAI/百度），建议至少配置一个以保证国内可达性")

    @property
    def is_available(self) -> bool:
        return any(p.is_available for p in self._providers)

    def search(self, query: str, num_results: int = None, date_restrict: str = None, days: int = 7) -> List[Dict[str, Any]]:
        """执行搜索（兼容旧接口，默认走并行）"""
        limit = num_results if num_results else self.max_results
        if date_restrict and not days:
            if date_restrict.startswith('d'):
                days = int(date_restrict[1:])
            elif date_restrict.startswith('w'):
                days = int(date_restrict[1:]) * 7
            elif date_restrict.startswith('m'):
                days = int(date_restrict[1:]) * 30
        response = self.search_parallel(query, limit, days)
        return response.to_list()

    def _search_single_provider(self, provider: BaseSearchProvider, query: str, max_results: int, days: int):
        """单引擎搜索 (带异常隔离)"""
        try:
            if not provider.is_available:
                return provider.name, None
            response = provider.search(query, max_results, days)
            return provider.name, response
        except Exception as e:
            logger.error(f"[{provider.name}] 并行搜索异常: {e}")
            return provider.name, None

    def search_with_fallback(self, query: str, max_results: int = 5, days: int = 7) -> SearchResponse:
        """执行搜索（带自动故障转移）"""
        for provider in self._providers:
            if not provider.is_available:
                continue
            response = provider.search(query, max_results, days)
            if response.success and response.results:
                return response
            else:
                logger.warning(f"{provider.name} 搜索失败: {response.error_message}，尝试下一个引擎")
        return SearchResponse(
            query=query, results=[], provider="None", success=False,
            error_message="所有搜索引擎都不可用或搜索失败"
        )

    def search_parallel(
        self, query: str, max_results: int = 5, days: int = 7,
        max_workers: int = 4, dedup: bool = True, timeout: float = 20.0,
    ) -> SearchResponse:
        """
        多源并行搜索 — 同时请求所有可用引擎, 聚合去重

        Args:
            query:         搜索关键词
            max_results:   每个引擎最多返回条数
            days:          时间范围 (天)
            max_workers:   并行线程数
            dedup:         是否按 URL 去重
            timeout:       单源超时秒数 (防止拖死整体)

        Returns:
            SearchResponse: 聚合后的结果, provider 标记为 "Parallel"
        """
        start_time = time.time()
        available = [p for p in self._providers if p.is_available]

        if not available:
            return SearchResponse(
                query=query, results=[], provider="Parallel",
                success=False, error_message="无可用搜索引擎"
            )

        all_results: List[SearchResult] = []
        errors: List[str] = []

        with ThreadPoolExecutor(max_workers=min(max_workers, len(available))) as executor:
            future_map = {
                executor.submit(self._search_single_provider, p, query, max_results, days): p.name
                for p in available
            }
            for future in as_completed(future_map):
                try:
                    name, response = future.result(timeout=timeout)
                except Exception as e:
                    name = future_map.get(future, "unknown")
                    errors.append(f"{name}: 超时或异常 {e}")
                    logger.warning(f"[{name}] 并行搜索超时/异常: {e}")
                    continue

                if response and response.success and response.results:
                    all_results.extend(response.results)
                    logger.info(f"[{name}] 并行返回 {len(response.results)} 条")
                elif response and response.error_message:
                    errors.append(f"{name}: {response.error_message}")
                    logger.warning(f"[{name}] 并行搜索失败: {response.error_message}")

        # 按 URL 去重 (跳过空 key)
        if dedup:
            seen_urls = set()
            deduped = []
            for r in all_results:
                key = r.url or r.title
                if not key:
                    deduped.append(r)  # 无 key 的保留
                    continue
                if key not in seen_urls:
                    seen_urls.add(key)
                    deduped.append(r)
            all_results = deduped

        elapsed = time.time() - start_time
        logger.info(f"[Parallel] '{query}' 聚合 {len(all_results)} 条, 耗时 {elapsed:.2f}s")

        return SearchResponse(
            query=query,
            results=all_results[:max_results * 2],  # 聚合后适当放宽上限
            provider="Parallel",
            success=len(all_results) > 0,
            error_message="; ".join(errors) if errors and not all_results else None,
            search_time=elapsed,
        )

    # ═══════════════════════════════════════════════════════════════
    # 新闻调度入口 — 根据 symbol/market 类型自动选源
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    def _classify_news_type(symbol: str, market: str, has_symbol: bool = True, has_keywords: bool = False) -> str:
        """
        判断新闻类型:

          symbol=="POLICY"                  → "policy"   政策/宏观 (按 market 区分国家)
          symbol 为空 + market=="all"       → "general"  通用财经
          symbol 为空 + 具体 market         → "market"   该市场新闻
          symbol == market                  → "market"   该市场新闻
          symbol 是股票代码                 → "stock"    个股新闻

          当 symbol 为空 + market 为空 + keywords 非空 → "keyword" 纯关键词搜索
        """
        if symbol == "POLICY":
            return "policy"
        if not has_symbol:
            if market == "all":
                return "keyword" if has_keywords else "general"
            return "market"
        if symbol == market:
            return "market"
        return "stock"

    @staticmethod
    def _is_stock_code(symbol: str) -> bool:
        """判断 symbol 是股票代码还是市场名"""
        if not symbol:
            return False
        if symbol in ("CNStock", "USStock", "Crypto", "Forex", "HKStock", "Futures", "POLICY"):
            return False
        return True

    def _dicts_to_results(self, items: List[Dict[str, Any]]) -> List[SearchResult]:
        """将 news_provider 返回的 dict 列表转为 SearchResult 列表"""
        results = []
        for item in items:
            if not isinstance(item, dict) or not item.get("title"):
                continue
            results.append(SearchResult(
                title=_safe_encode(item.get("title", "")),
                snippet=_safe_encode(item.get("title", "")),
                url=_safe_encode(item.get("url", "")),
                source=item.get("source", ""),
                published_date=item.get("time", ""),
            ))
        return results

    def _web_search_policy(self, market: str, days: int = 1, max_per_query: int = 5) -> List[SearchResult]:
        """政策/宏观 Web 搜索 (按市场区分关键词)"""
        queries_map = {
            "CNStock": [
                "国务院政策 最新", "宏观经济分析 GDP CPI PPI",
                "央行货币政策 降准 降息 LPR", "财政政策 减税 专项债",
                "产业政策 新能源 芯片 制造业", "经济数据 社融 M2 进出口",
            ],
            "USStock": [
                "美联储货币政策 利率决议", "美国经济数据 非农 CPI GDP",
                "美国财政政策 减税 刺激计划", "美国贸易政策 关税 制裁",
            ],
            "Crypto": [
                "加密货币监管政策 各国", "数字货币政策 央行数字货币",
            ],
            "Forex": [
                "外汇政策 央行干预 汇率", "货币政策 利率决议 各国央行",
            ],
        }
        queries = queries_map.get(market, queries_map["CNStock"])
        all_results = []

        def _search_one(query: str) -> List[SearchResult]:
            try:
                resp = self.search_with_fallback(query, max_per_query, days)
                return resp.results if resp.success else []
            except Exception:
                return []

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(_search_one, q): q for q in queries}
            for future in as_completed(futures):
                try:
                    all_results.extend(future.result())
                except Exception:
                    pass

        # 按 URL 去重
        seen = set()
        deduped = []
        for r in all_results:
            key = r.url or r.title
            if key and key not in seen:
                seen.add(key)
                deduped.append(r)
        return deduped

    def _web_search_market(self, market: str, days: int = 1, max_results: int = 5) -> List[SearchResult]:
        """市场新闻 Web 搜索 (备选)"""
        cn_map = {
            "USStock": "美股市场新闻 股票",
            "Crypto": "加密货币新闻 比特币",
            "Forex": "外汇市场分析 汇率",
            "CNStock": "A股市场 财经新闻 股票",
        }
        en_map = {
            "USStock": "US stock market news today",
            "Crypto": "cryptocurrency market news bitcoin",
            "Forex": "forex market analysis news",
            "CNStock": "China A-share stock market news",
        }
        cn_q = cn_map.get(market, f"{market} 市场新闻")
        en_q = en_map.get(market, f"{market} market news")

        all_results = []
        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(self.search_with_fallback, cn_q, max_results, days)
            f2 = pool.submit(self.search_with_fallback, en_q, max_results, days)
            for f in [f1, f2]:
                try:
                    resp = f.result()
                    if resp.success:
                        all_results.extend(resp.results)
                except Exception:
                    pass
        return all_results

    def _web_search_stock(
        self, symbol: str, market: str, name: str = "",
        days: int = 3, max_results: int = 5,
    ) -> List[SearchResult]:
        """个股新闻 Web 搜索 (备选)"""
        cn_q = f"{name} {symbol} A股新闻" if name else f"{symbol} 股票新闻"
        en_q = f"{name or symbol} stock news analysis"

        all_results = []
        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(self.search_with_fallback, cn_q, max_results, days)
            if market != "CNStock":
                f2 = pool.submit(self.search_with_fallback, en_q, max_results, days)
            else:
                f2 = None
            for f in [f1, f2]:
                if f is None:
                    continue
                try:
                    resp = f.result()
                    if resp.success:
                        all_results.extend(resp.results)
                except Exception:
                    pass
        return all_results

    # ── 政策/宏观专用搜索关键词 (按市场区分) ──
    _POLICY_QUERIES: Dict[str, Dict[str, List[str]]] = {
        "CNStock": {
            "cn": [
                "国务院政策 最新",
                "宏观经济分析 GDP CPI PPI",
                "央行货币政策 降准 降息 LPR",
                "财政政策 减税 专项债",
                "产业政策 新能源 芯片 制造业",
                "经济数据 社融 M2 进出口",
            ],
            "en": [
                "China economic policy latest",
                "China central bank monetary policy",
                "China fiscal policy stimulus GDP",
                "China industry policy manufacturing",
            ],
        },
        "USStock": {
            "cn": [
                "美联储货币政策 利率决议",
                "美国经济数据 非农 CPI GDP",
                "美国财政政策 减税 刺激计划",
                "美国贸易政策 关税 制裁",
                "美国产业政策 芯片法案 科技监管",
            ],
            "en": [
                "Federal Reserve interest rate decision",
                "US economic data nonfarm CPI GDP",
                "US fiscal policy tax cut stimulus",
                "US trade policy tariff sanctions",
                "US industrial policy CHIPS act regulation",
            ],
        },
        "Crypto": {
            "cn": [
                "加密货币监管政策 各国",
                "数字货币政策 央行数字货币",
                "区块链监管 Web3 政策",
            ],
            "en": [
                "cryptocurrency regulation policy",
                "digital currency CBDC policy",
                "crypto regulation Web3 government",
            ],
        },
        "Forex": {
            "cn": [
                "外汇政策 央行干预 汇率",
                "货币政策 利率决议 各国央行",
                "国际贸易收支 外汇储备",
            ],
            "en": [
                "forex policy central bank intervention",
                "monetary policy interest rate decision",
                "trade balance foreign exchange reserves",
            ],
        },
    }

    # ── 通用财经搜索关键词 ──
    _GENERAL_QUERIES: Dict[str, List[str]] = {
        "cn": [
            "加密货币新闻", "美联储利率", "美股市场最新消息",
            "外汇市场分析", "全球经济数据", "期货市场动态",
        ],
        "en": [
            "stock market news today", "cryptocurrency bitcoin news",
            "forex market analysis", "federal reserve interest rate",
            "global economic outlook", "S&P 500 market update",
        ],
    }

    def search_news_dispatch(
        self,
        symbol: str,
        market: str = "CNStock",
        lang: str = "all",
        days: int = 3,
        max_web_results: int = 5,
        name: str = "",
        keywords: str = "",
    ) -> SearchResponse:
        """
        新闻统一调度 — 唯一对外接口, 所有路由内部消化

        路由规则:
          symbol=="POLICY"                        → 政策/宏观: Web 政策关键词 + 市场源并行
          symbol==空 + market=="all" + keywords   → 纯关键词: 仅用 keywords 搜索
          symbol==空 + market=="all" + 无keywords → 通用财经: 中英文并行搜索
          symbol==空 + 具体 market                → 市场新闻: 市场源优先, 空则 Web 补充
          symbol==market                          → 市场新闻: 市场源优先, 空则 Web 补充
          symbol 是股票代码                       → 个股新闻: 个股源优先, 空则 Web 补充

        任何路由下 keywords 非空都会追加 Web 搜索 (keyword 路由除外, 它本身就是纯 keywords)。

        Args:
            symbol:    股票代码 ("600519", "AAPL") 或 市场名 ("CNStock") 或 "POLICY" 或 ""
            market:    市场标识 ("CNStock"/"USStock"/"Crypto"/"Forex"/"all")
            lang:      "cn"/"en"/"all" (仅影响 Web 搜索)
            days:      搜索天数
            max_web_results: Web 搜索最大条数
            name:      股票名称 (仅个股)
            keywords:  自定义搜索关键词 (可选, symbol+market 都为空时必须非空)

        Returns:
            SearchResponse, metadata 含:
              - news_type: "policy" / "keyword" / "general" / "market" / "stock"
              - source_count / web_results_added / total_merged
              - from_cache: False (缓存由上层 news.py 管理)
        """
        start_time = time.time()
        news_type = self._classify_news_type(symbol, market, has_symbol=bool(symbol), has_keywords=bool(keywords))
        source_results: List[SearchResult] = []
        web_results: List[SearchResult] = []

        def _title_key(r: SearchResult) -> str:
            norm = re.sub(r'[\s\u3000\uff0c\u3001\u3002\uff01\uff1f\u2014]', '', r.title)
            return hashlib.md5(norm.encode()).hexdigest()[:16]

        # ═══════════════════════════════════════════════
        # 路由 0: 政策/宏观新闻 (symbol == POLICY)
        # ═══════════════════════════════════════════════
        if news_type == "policy":
            logger.info(f"[调度] POLICY({market}): Web 政策关键词 + 市场源并行, days={days}")
            market_queries = self._POLICY_QUERIES.get(market, self._POLICY_QUERIES["CNStock"])
            all_queries = []
            for lk in ("cn", "en"):
                for q in market_queries.get(lk, []):
                    all_queries.append(q)

            with ThreadPoolExecutor(max_workers=8) as pool:
                f_market = pool.submit(fetch_all_market_news, 10, days)
                web_futures = {
                    pool.submit(self.search_with_fallback, q, max_web_results, days): q
                    for q in all_queries
                }
                # 收集市场源
                try:
                    market_dicts = f_market.result(timeout=20)
                    source_results = self._dicts_to_results(market_dicts)
                except Exception as e:
                    logger.warning(f"[调度] POLICY 市场源异常: {e}")
                # 收集 Web 政策搜索
                for future in as_completed(web_futures):
                    try:
                        resp = future.result()
                        if resp and resp.success:
                            web_results.extend(resp.results)
                    except Exception as e:
                        logger.warning(f"[调度] POLICY Web异常: {e}")

        # ═══════════════════════════════════════════════
        # 路由 1: 纯关键词搜索 (symbol 空 + market 空 + keywords 非空)
        # ═══════════════════════════════════════════════
        elif news_type == "keyword":
            logger.info(f"[调度] 纯关键词搜索: '{keywords}', days={days}")
            try:
                resp = self.search_with_fallback(keywords, max_web_results, days)
                if resp and resp.success:
                    web_results.extend(resp.results)
            except Exception as e:
                logger.warning(f"[调度] 关键词搜索异常: {e}")

        # ═══════════════════════════════════════════════
        # 路由 2: 通用财经新闻 (无 symbol, 无 market, 无 keywords)
        # ═══════════════════════════════════════════════
        elif news_type == "general":
            logger.info(f"[调度] 通用财经: 中英文并行搜索, lang={lang}")
            queries = []
            if lang in ("all", "cn"):
                queries.extend(self._GENERAL_QUERIES["cn"])
            if lang in ("all", "en"):
                queries.extend(self._GENERAL_QUERIES["en"])

            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = {
                    pool.submit(self.search_with_fallback, q, max_web_results, 1): q
                    for q in queries
                }
                for future in as_completed(futures):
                    try:
                        resp = future.result()
                        if resp and resp.success:
                            web_results.extend(resp.results)
                    except Exception as e:
                        logger.warning(f"[调度] 通用搜索异常: {e}")

        # ═══════════════════════════════════════════════
        # 路由 2: 市场新闻 (symbol == market)
        # ═══════════════════════════════════════════════
        elif news_type == "market":
            logger.info(f"[调度] 市场新闻({market}): 市场源优先, days={days}")
            try:
                market_dicts = fetch_all_market_news(10, days)
                source_results = self._dicts_to_results(market_dicts)
            except Exception as e:
                logger.warning(f"[调度] 市场源异常: {e}")

            if not source_results:
                logger.info(f"[调度] 市场源无结果, 补充 Web 搜索")
                web_results = self._web_search_market(market, days, max_web_results)

        # ═══════════════════════════════════════════════
        # 路由 3: 个股新闻 (symbol 是股票代码)
        # ═══════════════════════════════════════════════
        else:
            logger.info(f"[调度] 个股新闻({symbol}, {market}): 个股源优先, days={days}")
            try:
                stock_dicts = fetch_all_stock_news(symbol, days, name)
                source_results = self._dicts_to_results(stock_dicts)
            except Exception as e:
                logger.warning(f"[调度] 个股源异常: {e}")

            if not source_results:
                logger.info(f"[调度] 个股源无结果, 补充 Web 搜索")
                web_results = self._web_search_stock(symbol, market, name, days, max_web_results)

        # ═══════════════════════════════════════════════
        # 自定义关键词: 非空时追加 Web 搜索
        # ═══════════════════════════════════════════════
        if keywords:
            logger.info(f"[调度] 自定义关键词: '{keywords}', 追加 Web 搜索")
            try:
                kw_resp = self.search_with_fallback(keywords, max_web_results, days)
                if kw_resp and kw_resp.success:
                    web_results.extend(kw_resp.results)
            except Exception as e:
                logger.warning(f"[调度] 自定义关键词搜索异常: {e}")

        # ── 合并去重: 新闻源 > Web ──
        seen: set = set()
        merged: List[SearchResult] = []

        for r in source_results:
            k = _title_key(r)
            if k not in seen:
                seen.add(k)
                merged.append(r)

        web_added = 0
        for r in web_results:
            k = _title_key(r)
            if k not in seen:
                seen.add(k)
                merged.append(r)
                web_added += 1

        merged.sort(key=lambda r: r.published_date or "", reverse=True)

        elapsed = round(time.time() - start_time, 2)
        provider_label = {
            "policy": "Web+市场源",
            "keyword": "Web(关键词)",
            "general": "Web",
            "market": "市场源" if source_results else "Web(备选)",
            "stock": "个股源" if source_results else "Web(备选)",
        }

        logger.info(
            f"[调度] {news_type}({symbol or market}): "
            f"源={len(source_results)}, Web+{web_added}, 合并={len(merged)}, 耗时={elapsed}s"
        )

        return SearchResponse(
            query=f"{symbol} {name}".strip() if name else (symbol or f"通用财经({market})"),
            results=merged,
            provider=provider_label.get(news_type, "Unknown"),
            success=len(merged) > 0,
            search_time=elapsed,
            metadata={
                "news_type": news_type,
                "source_count": len(source_results),
                "web_results_added": web_added,
                "total_merged": len(merged),
                "from_cache": False,
            },
        )

    def search_stock_news(
        self, stock_code: str, stock_name: str,
        market: str = "USStock", max_results: int = 5
    ) -> SearchResponse:
        """搜索股票相关新闻"""
        today_weekday = datetime.now().weekday()
        if today_weekday == 0:
            search_days = 3
        elif today_weekday >= 5:
            search_days = 2
        else:
            search_days = 1
        if market == "USStock":
            query = f"{stock_name} {stock_code} stock news latest"
        elif market == "Crypto":
            query = f"{stock_name} crypto news price analysis"
        elif market == "Forex":
            query = f"{stock_name} {stock_code} forex news analysis"
        else:
            query = f"{stock_name} {stock_code} latest news"
        logger.info(f"搜索股票新闻: {stock_name}({stock_code}), market={market}, days={search_days}")
        return self.search_parallel(query, max_results, search_days)

    def search_stock_events(
        self, stock_code: str, stock_name: str,
        event_types: Optional[List[str]] = None
    ) -> SearchResponse:
        """搜索股票特定事件（年报预告、减持等）"""
        if event_types is None:
            event_types = ["年报预告", "减持公告", "业绩快报"]
        event_query = " OR ".join(event_types)
        query = f"{stock_name} ({event_query})"
        return self.search_parallel(query, max_results=5, days=30)



# 单例实例
_search_service: Optional[SearchService] = None


def get_search_service() -> SearchService:
    """获取搜索服务单例"""
    global _search_service
    if _search_service is None:
        _search_service = SearchService()
    return _search_service


def reset_search_service() -> None:
    """重置搜索服务（用于测试或配置更新后）"""
    global _search_service
    _search_service = None
