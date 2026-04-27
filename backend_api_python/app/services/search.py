"""
Search service v2.1 - 增强版搜索服务 (PostgreSQL 新闻缓存版)
整合多个搜索引擎，支持 API Key 轮换和故障转移

支持的搜索引擎（按优先级）：
1. Bocha AI (博查) - 国内 Perplexity 替代，免费额度
2. 百度搜索 - 国内直连，无需代理
3. SerpAPI - Google/Bing/百度结果抓取（需代理）
4. Google CSE - 自定义搜索引擎（需代理）
5. Bing Search API（不稳定）
6. DuckDuckGo - 免费兜底（需代理）

参考：daily_stock_analysis-main/src/search_service.py

v2.1 变更:
- 新增 sentiment_score REAL 列: 0-10 数值评分, 重大负面 = -999 (一票否决)
- 编码安全: 所有入库文本强制 utf-8 清洗, 替换不可解码字节
- calc_score: 检测 -999 → 综合分直接归零 (一票否决)
- 外部接口参数保持不变
"""
import requests
import json
import time
import re
import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from itertools import cycle
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor

from app.utils.logger import get_logger
from app.utils.config_loader import load_addon_config
from app.utils.db import get_db_connection

logger = get_logger(__name__)

# ── 新闻缓存配置 (顶部统一管理) ─────────────────────────────
NEWS_CACHE_DEDUP_HOURS = 24 # 24小时内不重复搜索

# ── 重大负面新闻一票否决关键词 (命中任一 → sentiment_score = -999) ──
# 这些是 stock_news.py 中 weight=3 的极端负面词, 任一命中即触发一票否决
_VETO_KEYWORDS = {
    "跌停", "一字跌停", "连续跌停", "天地板",
    "闪崩", "崩盘",
    "历史新低",
    "净利大跌",
    "业绩暴雷", "暴雷", "业绩变脸",
    "巨亏", "大幅亏损", "由盈转亏",
    "商誉减值",
    "财务造假",
    "清仓",
    "大股东减持", "违规减持",
    "破产", "清算",
    "质量事故", "安全事故",
    "监管调查", "立案调查", "违法",
    "退市", "暂停上市",
    "债务危机", "债务违约", "资金链断裂",
    "巨额索赔",
    "重大利空", "黑天鹅",
}

# Track Google API quota status
_google_quota_exhausted = False
_google_quota_reset_time = 0


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
        # 双重保险: encode → decode 确保无非法字节
        text = text.encode('utf-8', errors='replace').decode('utf-8', errors='replace')
    # 去除 NUL 和控制字符 (保留换行/回车/制表)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    if max_len > 0 and len(text) > max_len:
        text = text[:max_len]
    return text


def _check_veto(title: str, snippet: str = "") -> bool:
    """检查是否触发重大负面一票否决"""
    text = f"{title} {snippet}"
    for kw in _VETO_KEYWORDS:
        if kw in text:
            return True
    return False


# ═══════════════════════════════════════════════════════════════
# PostgreSQL 新闻缓存管理器
# ═══════════════════════════════════════════════════════════════

class NewsCacheManager:
    """
    新闻缓存管理器 (纯 DB 比对, 无内存状态)

    存储:
    - qd_news_cache_items: 新闻明细 (每条一行, UNIQUE(symbol, market, title))

    策略:
    - 24h去重: 查 DB MAX(created_at) 判断上次搜索时间
    - 自选股: 7:30-9:30 / 12:00-13:00 按最后搜索时间判断是否需要刷新
    - 无结果不写库, 下次会重新搜索 (无额外成本)
    - sentiment_score: 数值评分, 重大负面=-999 一票否决
    """

    def __init__(self):
        pass

    def _get_last_search_time(self, symbol: str, market: str) -> Optional[datetime]:
        """从 DB 查询该股票最后一条缓存的入库时间"""
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT MAX(created_at) AS last_time FROM qd_news_cache_items WHERE symbol = %s AND market = %s",
                    (symbol, market)
                )
                row = cursor.fetchone()
                if row and row.get('last_time'):
                    return row['last_time']
                return None
        except Exception as e:
            logger.warning(f"查询最后搜索时间异常: {e}")
            return None

    def get_items(self, symbol: str, market: str = "CNStock") -> List[Dict[str, Any]]:
        """从 DB 查询该股票的缓存新闻"""
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """SELECT title, snippet, url, source, published_date, sentiment, sentiment_score
                       FROM qd_news_cache_items
                       WHERE symbol = %s AND market = %s
                       ORDER BY published_date DESC""",
                    (symbol, market)
                )
                return [dict(r) for r in cursor.fetchall()] or []
        except Exception as e:
            logger.warning(f"查询新闻缓存异常: {e}")
            return []

    def should_search(self, symbol: str, market: str = "CNStock",
                      is_watchlist: bool = False) -> tuple:
        """
        判断是否需要搜索

        判断顺序:
          1. 自选股 + 开盘前窗口(7:30-9:30) + 今日未搜 → 搜索
          2. 自选股 + 午间窗口(12:00-13:00) + 午间未搜 → 搜索
          3. DB中距上次搜索 <24h → 跳过
          4. 其他 → 搜索

        Returns:
            (should_search: bool, reason: str)
        """
        last_search = self._get_last_search_time(symbol, market)
        now = datetime.now()

        # 自选股特殊策略 (A股 9:30 开盘)
        if is_watchlist and market == "CNStock":
            # 开盘前2小时窗口: 7:30-9:30
            pre_market = (now.hour == 7 and now.minute >= 30) or (now.hour == 8) or (now.hour == 9 and now.minute < 30)
            if pre_market:
                if last_search and last_search.date() == now.date():
                    return False, f"自选股今日已搜({last_search.strftime('%H:%M')}), 开盘前无需重复"
                else:
                    return True, "自选股开盘前刷新(今日未搜索)"

            # 午间窗口: 12:00-13:00
            if now.hour == 12:
                if last_search and last_search.date() == now.date() and last_search.hour >= 12:
                    return False, f"自选股午间已搜({last_search.strftime('%H:%M')})"
                else:
                    return True, "自选股午间刷新"

        # 24小时去重
        if last_search:
            hours_since = (now - last_search).total_seconds() / 3600
            if hours_since < NEWS_CACHE_DEDUP_HOURS:
                return False, f"距上次搜索仅{hours_since:.1f}h(<24h), 使用缓存"

        return True, "需要搜索" if not last_search else f"距上次搜索超过{NEWS_CACHE_DEDUP_HOURS}h"

    def save_items(self, symbol: str, market: str, results: List['SearchResult']) -> bool:
        """
        将搜索结果写入明细表, ON CONFLICT 更新已有标题
        - 写入前强制 utf-8 编码清洗
        - 每条先做情感评分 (sentiment + sentiment_score)
        - 重大负面一票否决 → sentiment_score = -999
        """
        if not symbol or not market or not results:
            return False
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                rows = []
                for r in results:
                    title = _safe_encode(r.title, 500)
                    snippet = _safe_encode(r.snippet, 2000)
                    url = _safe_encode(r.url, 1000)
                    source = _safe_encode(r.source, 100)
                    pub_date = _safe_encode(r.published_date or '', 40)
                    sentiment = _safe_encode(r.sentiment, 20)

                    # 计算数值评分
                    score = r.sentiment_score
                    if score is None:
                        # 从 sentiment 标签推算
                        score = {"positive": 7.5, "negative": 2.5, "neutral": 5.0}.get(sentiment, 5.0)

                    # 重大负面一票否决检测
                    if _check_veto(title, snippet):
                        score = -999.0
                        sentiment = "negative"
                        logger.warning(f"[一票否决] 检测到重大负面: {title[:60]}")

                    rows.append((symbol, market, title, snippet, url, source,
                                 pub_date, sentiment, score))

                cursor.executemany(
                    """INSERT INTO qd_news_cache_items
                       (symbol, market, title, snippet, url, source, published_date, sentiment, sentiment_score)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (symbol, market, title) DO UPDATE SET
                           snippet = EXCLUDED.snippet,
                           url = EXCLUDED.url,
                           source = EXCLUDED.source,
                           published_date = EXCLUDED.published_date,
                           sentiment = EXCLUDED.sentiment,
                           sentiment_score = EXCLUDED.sentiment_score,
                           created_at = NOW()""",
                    rows
                )
                conn.commit()
                logger.info(f"新闻缓存已保存: {symbol}({market}) {len(results)}条")
                return True
        except Exception as e:
            logger.error(f"保存新闻缓存异常: {e}")
            return False

    @staticmethod
    def calc_score(items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        从新闻列表实时计算综合评分 (复用 stock_news.py 的衰减逻辑)

        一票否决规则:
        - 任一条 sentiment_score == -999 → 综合分直接归 0, direction=重大利空
        """
        if not items:
            return {"composite_score": 5.0, "direction": "中性",
                    "positive": 0, "negative": 0, "neutral": 0}

        now = datetime.now()
        pos = neg = neu = 0
        total_weighted = 0.0
        total_weight = 0.0
        has_veto = False

        for item in items:
            s = item.get("sentiment", "neutral")
            raw_score = item.get("sentiment_score", None)

            if s == "positive":
                pos += 1
            elif s == "negative":
                neg += 1
            else:
                neu += 1

            # 一票否决检测
            if raw_score is not None and float(raw_score) == -999.0:
                has_veto = True
                continue  # 不参与加权, 但已标记

            # 时间衰减
            pub = item.get("published_date", "")
            try:
                pub_dt = datetime.fromisoformat(pub) if pub else now
                hours = max(0, (now - pub_dt).total_seconds() / 3600)
            except (ValueError, TypeError):
                hours = 24
            decay = 0.8 ** (hours / 24)
            boost = 1.5 if s != "neutral" else 1.0
            weight = decay * boost
            score = {"positive": 7.5, "negative": 2.5, "neutral": 5.0}.get(s, 5.0)
            total_weighted += score * weight
            total_weight += weight

        # 一票否决: 任何重大负面 → 综合分归零
        if has_veto:
            return {"composite_score": 0.0, "direction": "重大利空",
                    "positive": pos, "negative": neg, "neutral": neu,
                    "veto": True}

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

        return {"composite_score": composite, "direction": direction,
                "positive": pos, "negative": neg, "neutral": neu}


# 全局缓存管理器单例
_news_cache_manager: Optional[NewsCacheManager] = None

def get_news_cache_manager() -> NewsCacheManager:
    """获取新闻缓存管理器单例"""
    global _news_cache_manager
    if _news_cache_manager is None:
        _news_cache_manager = NewsCacheManager()
    return _news_cache_manager


@dataclass
class SearchResult:
    """搜索结果数据类"""
    title: str
    snippet: str  # 摘要
    url: str
    source: str  # 来源网站
    published_date: Optional[str] = None
    sentiment: str = 'neutral'  # 情绪标签
    sentiment_score: Optional[float] = None  # 数值评分 0-10, -999=一票否决

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
      - composite_score: float  综合评分 0-10 (时间衰减加权)
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
        global _google_quota_exhausted, _google_quota_reset_time
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


class StockNewsSearchProvider(BaseSearchProvider):
    """
    A股个股新闻聚合 Provider (v2.1 — 挂载 stock_news.py)

    特点:
    - 5路数据源并发: 东方财富、新浪财经、新浪7x24、腾讯财经、凤凰财经
    - 内置加权情感分析 + 时间衰减综合评分
    - 输出标准 SearchResult, 评分在 metadata 中
    - is_available 永远 True (无需 API Key)
    """

    def __init__(self):
        super().__init__(['free'], "StockNews")

    def _do_search(self, query: str, api_key: str, max_results: int, days: int = 7) -> SearchResponse:
        try:
            from app.services.stock_news import fetch_stock_news
            parts = query.strip().split(None, 1)
            stock_code = parts[0]
            stock_name = parts[1] if len(parts) > 1 else ""
            resp = fetch_stock_news(
                stock_code=stock_code, days=min(days, 30),
                stock_name=stock_name, max_results=max_results or 20,
            )
            direction = resp.metadata.get("direction", "")
            score = resp.metadata.get("composite_score", "")
            resp.provider = f"StockNews({direction} {score}/10)" if direction else "StockNews"
            return resp
        except Exception as e:
            return SearchResponse(query=query, results=[], provider="StockNews",
                                  success=False, error_message=str(e))


class SearchService:
    """
    搜索服务 v2.1

    功能:
    1. 管理多个搜索引擎 (Web) + A股个股新闻 (StockNews)
    2. 自动故障转移
    3. 结果聚合和格式化
    4. PostgreSQL 新闻缓存 (24h去重, 一票否决)

    Provider 架构:
    - _providers: Web 搜索引擎列表 (BochaAI/Baidu/Tavily/SerpAPI/Google/Bing/DuckDuckGo)
    - _stock_news_provider: A股个股新闻聚合 (独立于 _providers, 通过 search_cn_stock_news 调用)

    核心方法:
    - search_with_fallback():  Web 搜索 + 故障转移
    - search_cn_stock_news():  Web + StockNews 并行, 自动去重, stock_news 优先, 带缓存
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

        self._stock_news_provider = StockNewsSearchProvider()
        logger.info("已配置 A股个股新闻聚合 (StockNews)")

        if not any(p.name in ("BochaAI", "Baidu") for p in self._providers):
            logger.warning("未配置国内搜索引擎（BochaAI/百度），建议至少配置一个以保证国内可达性")

    @property
    def is_available(self) -> bool:
        return any(p.is_available for p in self._providers)

    def search(self, query: str, num_results: int = None, date_restrict: str = None, days: int = 7) -> List[Dict[str, Any]]:
        """执行搜索（兼容旧接口）"""
        limit = num_results if num_results else self.max_results
        if date_restrict and not days:
            if date_restrict.startswith('d'):
                days = int(date_restrict[1:])
            elif date_restrict.startswith('w'):
                days = int(date_restrict[1:]) * 7
            elif date_restrict.startswith('m'):
                days = int(date_restrict[1:]) * 30
        response = self.search_with_fallback(query, limit, days)
        return response.to_list()

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
        return self.search_with_fallback(query, max_results, search_days)

    def search_stock_events(
        self, stock_code: str, stock_name: str,
        event_types: Optional[List[str]] = None
    ) -> SearchResponse:
        """搜索股票特定事件（年报预告、减持等）"""
        if event_types is None:
            event_types = ["年报预告", "减持公告", "业绩快报"]
        event_query = " OR ".join(event_types)
        query = f"{stock_name} ({event_query})"
        return self.search_with_fallback(query, max_results=5, days=30)

    def search_cn_stock_news(
        self, stock_code: str, stock_name: str = "",
        days: int = 3, max_web_results: int = 5,
        market: str = "CNStock", is_watchlist: bool = False,
    ) -> SearchResponse:
        """
        A股个股新闻搜索 — 带 PostgreSQL 持久化缓存 v2.1

        缓存策略:
          1. 查 DB 判断是否需要搜索 (24h去重 / 自选股时间窗口)
          2. 需要搜索 → 并行 Web + StockNews → 写入明细表
          3. 不需要搜索 → 从明细表取 items → 实时算评分返回
          4. 无结果不写库, 下次重新搜索
          5. 重大负面一票否决 → sentiment_score=-999 → 综合分归零

        外部接口参数保持不变
        """
        start_time = time.time()
        query = f"{stock_code} {stock_name}".strip()

        # ── 第一步: 判断是否需要搜索 ──
        cache_mgr = get_news_cache_manager()
        should, reason = cache_mgr.should_search(stock_code, market, is_watchlist)

        if not should:
            # 从缓存取 items, 实时算评分
            items = cache_mgr.get_items(stock_code, market)
            if items:
                score_info = cache_mgr.calc_score(items)
                results = [
                    SearchResult(
                        title=i["title"], snippet=i.get("snippet", ""),
                        url=i.get("url", ""), source=i.get("source", ""),
                        published_date=i.get("published_date", ""),
                        sentiment=i.get("sentiment", "neutral"),
                        sentiment_score=i.get("sentiment_score"),
                    ) for i in items
                ]
                score_info["from_cache"] = True
                provider = f"Cache({score_info['direction']} {score_info['composite_score']}/10)"
                logger.info(f"[缓存命中] {stock_code}({market}) {stock_name}: {reason}")
                return SearchResponse(
                    query=query, results=results, provider=provider,
                    success=True, search_time=round(time.time() - start_time, 2),
                    metadata=score_info,
                )
            # DB说不搜但实际没数据 (可能数据被手动删了), 降级为搜索
            logger.info(f"[缓存降级] {stock_code}({market}): {reason}, 但DB无数据, 重新搜索")

        logger.info(f"[需要搜索] {stock_code}({market}) {stock_name}: {reason}")

        # ── 第二步: 并行搜索 ──
        def _title_key(r: SearchResult) -> str:
            norm = re.sub(r'[\s\u3000\uff0c\u3001\u3002\uff01\uff1f\u2014]', '', r.title)
            return hashlib.md5(norm.encode()).hexdigest()[:16]

        web_query = f"{stock_name} {stock_code} A股新闻" if stock_name else f"{stock_code} 股票新闻"

        with ThreadPoolExecutor(max_workers=2) as pool:
            f_web = pool.submit(self.search_with_fallback, web_query, max_web_results, days)
            f_news = pool.submit(self._stock_news_provider.search, query, 20, days)

            try:
                web_resp = f_web.result()
            except Exception as e:
                logger.warning(f"Web搜索异常: {e}")
                web_resp = SearchResponse(query=web_query, results=[], provider="Web",
                                          success=False, error_message=str(e))
            try:
                news_resp = f_news.result()
            except Exception as e:
                logger.warning(f"StockNews搜索异常: {e}")
                news_resp = SearchResponse(query=query, results=[], provider="StockNews",
                                           success=False, error_message=str(e))

        # 合并去重: stock_news 优先
        seen = set()
        merged: List[SearchResult] = []
        for r in news_resp.results:
            k = _title_key(r)
            if k not in seen:
                seen.add(k)
                merged.append(r)

        web_added = 0
        if web_resp.success:
            for r in web_resp.results:
                k = _title_key(r)
                if k not in seen:
                    seen.add(k)
                    merged.append(r)
                    web_added += 1

        merged.sort(key=lambda r: r.published_date or "", reverse=True)

        # ── 第三步: 写入缓存 ──
        if merged:
            cache_mgr.save_items(stock_code, market, merged)

        elapsed = round(time.time() - start_time, 2)
        metadata = dict(news_resp.metadata) if news_resp.metadata else {}
        metadata["web_results_added"] = web_added
        metadata["stock_news_count"] = len(news_resp.results)
        metadata["total_merged"] = len(merged)
        metadata["from_cache"] = False

        return SearchResponse(
            query=query, results=merged, provider="StockNews+Web",
            success=len(merged) > 0, search_time=elapsed, metadata=metadata,
        )


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
    global _search_service, _news_cache_manager
    _search_service = None
    _news_cache_manager = None
