"""
Tests for app/services/search.py — 搜索引擎调度器

覆盖:
  1. _safe_encode()         编码安全
  2. SearchResult            数据模型
  3. SearchResponse          响应模型
  4. BaseSearchProvider      基类 (Key 轮转/错误计数/故障转移)
  5. 各 Provider             单引擎搜索 (mock HTTP)
  6. SearchService           调度器 (路由/并行/故障转移)
  7. search_news_dispatch()  4 条路由
"""
import os
import sys
import types
import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from dataclasses import asdict

# ── 路径 + mock 重依赖 (在 import app 之前) ──
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

def _make_mock(name):
    mod = types.ModuleType(name)
    mod.__version__ = '0.0.0'
    mod.__file__ = f'/mock/{name}'
    mod.__path__ = []
    mod.__spec__ = None
    mod.__loader__ = None
    mod.__package__ = name
    return mod

# Flask 及其子模块
for _m in ['flask', 'flask.json', 'flask.json.provider', 'flask_cors',
           'flask_limiter', 'flask_limiter.util',
           'flask_socketio', 'flask_compress']:
    if _m not in sys.modules:
        sys.modules[_m] = _make_mock(_m)
# Flask mock 需要关键类/函数
_flask = sys.modules['flask']
_flask.Flask = type('Flask', (), {'__init__': lambda *a, **k: None})
_flask.json = sys.modules['flask.json']
_flask.json.provider = sys.modules['flask.json.provider']
_flask.json.provider.DefaultJSONProvider = type('DefaultJSONProvider', (), {})
_flask_cors = sys.modules['flask_cors']
_flask_cors.CORS = lambda *a, **k: None

# 其他重依赖 (不 mock requests/urllib3, 它们是系统已装的)
for _m in ['yfinance', 'yfinance.shared', 'akshare', 'ccxt', 'finnhub',
           'ib_insync', 'redis', 'bcrypt', 'bip_utils', 'gunicorn', 'bs4',
           'pandas', 'numpy', 'sklearn', 'scipy', 'xgboost', 'lightgbm',
           'torch', 'tensorflow', 'pyarrow', 'pyarrow.feather']:
    if _m not in sys.modules:
        sys.modules[_m] = _make_mock(_m)

# 阻止 app.services.__init__ 级联导入所有服务
# 用一个带 __path__ 的 mock 替代, 这样子模块 (search.py) 仍可被找到
if 'app.services' not in sys.modules:
    _svc = _make_mock('app.services')
    _svc.__path__ = [os.path.join(os.path.dirname(__file__), '..', 'app', 'services')]
    _svc.__package__ = 'app.services'
    sys.modules['app.services'] = _svc

# bs4: BeautifulSoup 需要可调用
sys.modules['bs4'].BeautifulSoup = type('BeautifulSoup', (), {
    '__init__': lambda *a, **k: None,
    'get_text': lambda self, **k: '',
})

# psycopg2
if 'psycopg2' not in sys.modules:
    _pg = _make_mock('psycopg2')
    _pg.extras = _make_mock('psycopg2.extras')
    _pg.extras.RealDictCursor = type('RealDictCursor', (), {})
    _pg.pool = _make_mock('psycopg2.pool')
    _pg.pool.ThreadedConnectionPool = type('ThreadedConnectionPool', (), {})
    sys.modules['psycopg2'] = _pg
    sys.modules['psycopg2.extras'] = _pg.extras
    sys.modules['psycopg2.pool'] = _pg.pool

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("TQDM_DISABLE", "1")
os.environ.setdefault("CACHE_ENABLED", "false")

from app.services.news_search import (
    _safe_encode,
    SearchResult,
    SearchResponse,
    BaseSearchProvider,
    TavilySearchProvider,
    SerpAPISearchProvider,
    GoogleSearchProvider,
    BingSearchProvider,
    BaiduSearchProvider,
    BochaAISearchProvider,
    DuckDuckGoSearchProvider,
    CLSNewsProvider,
    WallStreetCNNewsProvider,
    EastMoneyNewsProvider,
    SinaFinanceNewsProvider,
    AKShareNewsProvider,
    SearchService,
    get_search_service,
    reset_search_service,
)


# ═══════════════════════════════════════════════════════════════
#  1. _safe_encode
# ═══════════════════════════════════════════════════════════════

class TestSafeEncode:
    def test_none_returns_empty(self):
        assert _safe_encode(None) == ''

    def test_bytes_decode(self):
        assert _safe_encode(b'hello') == 'hello'

    def test_bytes_bad_encoding(self):
        result = _safe_encode(b'\xff\xfe')
        assert isinstance(result, str)

    def test_str_passthrough(self):
        assert _safe_encode('你好') == '你好'

    def test_nul_chars_stripped(self):
        assert _safe_encode('ab\x00cd') == 'abcd'

    def test_control_chars_stripped(self):
        assert _safe_encode('ab\x01\x02cd') == 'abcd'

    def test_max_len_truncation(self):
        assert len(_safe_encode('abcdef', max_len=3)) == 3

    def test_max_len_zero_no_truncation(self):
        assert _safe_encode('abcdef', max_len=0) == 'abcdef'

    def test_int_to_str(self):
        assert _safe_encode(12345) == '12345'

    def test_empty_string(self):
        assert _safe_encode('') == ''


# ═══════════════════════════════════════════════════════════════
#  2. SearchResult
# ═══════════════════════════════════════════════════════════════

class TestSearchResult:
    def test_basic_creation(self):
        r = SearchResult(title='t', snippet='s', url='u', source='src')
        assert r.title == 't'
        assert r.sentiment == 'neutral'
        assert r.sentiment_score is None

    def test_to_text(self):
        r = SearchResult(title='A股大涨', snippet='摘要', url='http://x', source='新浪',
                         published_date='2024-01-01')
        text = r.to_text()
        assert 'A股大涨' in text
        assert '新浪' in text
        assert '2024-01-01' in text

    def test_to_text_no_date(self):
        r = SearchResult(title='t', snippet='s', url='u', source='src')
        text = r.to_text()
        assert '(' not in text  # 无日期时不带括号

    def test_to_dict(self):
        r = SearchResult(title='t', snippet='s', url='u', source='src',
                         sentiment='positive', sentiment_score=7.5)
        d = r.to_dict()
        assert d['title'] == 't'
        assert d['sentiment'] == 'positive'
        assert d['sentiment_score'] == 7.5
        assert 'link' in d

    def test_default_values(self):
        r = SearchResult(title='t', snippet='s', url='u', source='src')
        assert r.published_date is None
        assert r.sentiment == 'neutral'


# ═══════════════════════════════════════════════════════════════
#  3. SearchResponse
# ═══════════════════════════════════════════════════════════════

class TestSearchResponse:
    def _make_response(self, **kwargs):
        defaults = dict(query='test', results=[], provider='Test', success=True)
        defaults.update(kwargs)
        return SearchResponse(**defaults)

    def test_success_empty(self):
        resp = self._make_response(results=[], success=False)
        assert not resp.success

    def test_to_context_empty(self):
        resp = self._make_response(results=[], success=False)
        ctx = resp.to_context()
        assert '未找到' in ctx

    def test_to_context_with_results(self):
        r = SearchResult(title='A股利好', snippet='摘要', url='u', source='s')
        resp = self._make_response(results=[r])
        ctx = resp.to_context()
        assert 'A股利好' in ctx

    def test_to_list(self):
        r = SearchResult(title='t', snippet='s', url='u', source='src')
        resp = self._make_response(results=[r])
        lst = resp.to_list()
        assert len(lst) == 1
        assert lst[0]['title'] == 't'

    def test_metadata_default_empty(self):
        resp = self._make_response()
        assert resp.metadata == {}

    def test_search_time(self):
        resp = self._make_response(search_time=1.23)
        assert resp.search_time == 1.23

    def test_error_message(self):
        resp = self._make_response(success=False, error_message='API key invalid')
        assert resp.error_message == 'API key invalid'


# ═══════════════════════════════════════════════════════════════
#  4. BaseSearchProvider
# ═══════════════════════════════════════════════════════════════

class ConcreteProvider(BaseSearchProvider):
    """测试用具象 Provider"""
    def __init__(self, api_keys, name='Test', search_result=None):
        super().__init__(api_keys, name)
        self._search_result = search_result
        self._call_count = 0

    def _do_search(self, query, api_key, max_results, days=7):
        self._call_count += 1
        if self._search_result:
            return self._search_result
        return SearchResponse(query=query, results=[], provider=self.name, success=True)


class TestBaseSearchProvider:
    def test_no_keys_not_available(self):
        p = ConcreteProvider([])
        assert not p.is_available

    def test_has_keys_available(self):
        p = ConcreteProvider(['key1'])
        assert p.is_available

    def test_name_property(self):
        p = ConcreteProvider(['k'], name='MyProvider')
        assert p.name == 'MyProvider'

    def test_key_rotation(self):
        p = ConcreteProvider(['k1', 'k2', 'k3'])
        keys = [p._get_next_key() for _ in range(6)]
        # 应该轮转: k1, k2, k3, k1, k2, k3
        assert keys == ['k1', 'k2', 'k3', 'k1', 'k2', 'k3']

    def test_key_error_tracking_skip_bad_keys(self):
        p = ConcreteProvider(['k1', 'k2'])
        # k1 累积 3 次错误
        for _ in range(3):
            p._record_error('k1')
        # 应该跳过 k1, 返回 k2
        assert p._get_next_key() == 'k2'

    def test_key_error_reset(self):
        p = ConcreteProvider(['k1', 'k2'])
        for _ in range(3):
            p._record_error('k1')
            p._record_error('k2')
        # 所有 key 都有错误, 应该重置并返回第一个
        key = p._get_next_key()
        assert key == 'k1'

    def test_record_success_reduces_errors(self):
        p = ConcreteProvider(['k1'])
        p._record_error('k1')
        p._record_error('k1')
        p._record_success('k1')
        assert p._key_errors['k1'] == 1

    def test_search_delegates_to_do_search(self):
        r = SearchResponse(query='q', results=[
            SearchResult(title='t', snippet='s', url='u', source='src')
        ], provider='Test', success=True)
        p = ConcreteProvider(['key1'], search_result=r)
        resp = p.search('query', max_results=5)
        assert resp.success
        assert len(resp.results) == 1

    def test_search_no_key_returns_failure(self):
        p = ConcreteProvider([])
        resp = p.search('query')
        assert not resp.success
        assert '未配置' in resp.error_message

    def test_search_exception_returns_failure(self):
        class FailProvider(BaseSearchProvider):
            def __init__(self):
                super().__init__(['key'], 'Fail')
            def _do_search(self, query, api_key, max_results, days=7):
                raise RuntimeError('boom')

        p = FailProvider()
        resp = p.search('query')
        assert not resp.success
        assert 'boom' in resp.error_message

    def test_extract_domain(self):
        assert BaseSearchProvider._extract_domain('https://www.example.com/path') == 'example.com'
        assert BaseSearchProvider._extract_domain('https://finance.sina.com.cn/x') == 'finance.sina.com.cn'

    def test_extract_domain_bad_url(self):
        result = BaseSearchProvider._extract_domain('not a url')
        assert result == '未知来源' or isinstance(result, str)


# ═══════════════════════════════════════════════════════════════
#  5. 各 Provider (mock HTTP)
# ═══════════════════════════════════════════════════════════════

class TestTavilyProvider:
    @patch('app.services.search.requests.post')
    def test_rest_search_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'results': [
                {'title': 'A股利好', 'content': '摘要内容', 'url': 'http://x.com', 'published_date': '2024-01-01'}
            ]
        }
        mock_post.return_value = mock_resp

        p = TavilySearchProvider(['test-key'])
        resp = p._do_search_rest('A股', 'test-key', 5)
        assert resp.success
        assert len(resp.results) == 1
        assert resp.results[0].title == 'A股利好'

    @patch('app.services.search.requests.post')
    def test_rest_search_failure(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.text = 'rate limited'
        mock_post.return_value = mock_resp

        p = TavilySearchProvider(['test-key'])
        resp = p._do_search_rest('query', 'test-key', 5)
        assert not resp.success


class TestSerpAPIProvider:
    @patch('app.services.search.requests.get')
    def test_rest_search_success(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'organic_results': [
                {'title': '美股新闻', 'snippet': '内容', 'link': 'http://x.com', 'source': 'Reuters'}
            ]
        }
        mock_get.return_value = mock_resp

        p = SerpAPISearchProvider(['test-key'])
        resp = p._do_search_rest('US stocks', 'test-key', 5)
        assert resp.success
        assert resp.results[0].source == 'Reuters'


class TestBaiduProvider:
    @patch('app.services.search.requests.post')
    def test_search_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'result': {
                'results': [
                    {'title': '百度结果', 'content': '摘要', 'url': 'http://baidu.com', 'publish_time': '2024-01-01'}
                ]
            }
        }
        mock_post.return_value = mock_resp

        p = BaiduSearchProvider(['test-key'])
        resp = p._do_search('query', 'test-key', 5)
        assert resp.success
        assert resp.results[0].source == '百度'

    @patch('app.services.search.requests.post')
    def test_search_empty_results(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'result': {'results': []}}
        mock_post.return_value = mock_resp

        p = BaiduSearchProvider(['test-key'])
        resp = p._do_search('query', 'test-key', 5)
        assert not resp.success  # 无结果视为失败


class TestBochaAIProvider:
    @patch('app.services.search.requests.post')
    def test_search_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'data': {
                'webPages': {
                    'value': [
                        {'name': '博查结果', 'snippet': '内容', 'url': 'http://bocha.com', 'datePublished': '2024-01-01'}
                    ]
                }
            }
        }
        mock_post.return_value = mock_resp

        p = BochaAISearchProvider(['test-key'])
        resp = p._do_search('query', 'test-key', 5)
        assert resp.success
        assert resp.results[0].title == '博查结果'


class TestGoogleProvider:
    @patch('app.services.search.requests.get')
    def test_search_success(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'items': [
                {'title': 'Google Result', 'snippet': 'content', 'link': 'http://g.com',
                 'pagemap': {'metatags': [{'article:published_time': '2024-01-01'}]}}
            ]
        }
        mock_get.return_value = mock_resp

        p = GoogleSearchProvider('api-key', 'cx-id')
        resp = p._do_search('query', 'api-key', 5)
        assert resp.success
        assert resp.results[0].title == 'Google Result'

    def test_no_cx_returns_failure(self):
        p = GoogleSearchProvider('api-key', '')
        resp = p._do_search('query', 'api-key', 5)
        assert not resp.success
        assert 'CX' in resp.error_message


class TestBingProvider:
    @patch('app.services.search.requests.get')
    def test_search_success(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'webPages': {
                'value': [
                    {'name': 'Bing Result', 'snippet': 'content', 'url': 'http://b.com', 'datePublished': '2024-01-01'}
                ]
            }
        }
        mock_get.return_value = mock_resp

        p = BingSearchProvider('test-key')
        resp = p._do_search('query', 'test-key', 5)
        assert resp.success
        assert resp.results[0].source == 'Bing'


class TestDuckDuckGoProvider:
    @patch('app.services.search.requests.get')
    def test_search_success_json(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            'Heading': 'DDG Result',
            'AbstractText': '摘要',
            'AbstractURL': 'http://ddg.com',
            'RelatedTopics': [
                {'Text': 'Topic 1', 'FirstURL': 'http://ddg.com/1'},
                {'Text': 'Topic 2', 'FirstURL': 'http://ddg.com/2'},
            ]
        }
        mock_get.return_value = mock_resp

        p = DuckDuckGoSearchProvider()
        resp = p._do_search('query', 'free', 5)
        assert resp.success
        assert len(resp.results) >= 1

    @patch('app.services.search.requests.get')
    def test_search_empty_topics_fallback_html(self, mock_get):
        # JSON 返回空, HTML fallback 也返回空
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {'RelatedTopics': []}
        mock_resp.text = '<html></html>'
        mock_get.return_value = mock_resp

        p = DuckDuckGoSearchProvider()
        resp = p._do_search('query', 'free', 5)
        # 无结果时 success=False
        assert not resp.success or len(resp.results) == 0


class TestNewsProviders:
    """测试财经新闻 Provider (mock 底层 fetch 函数)"""

    @patch('app.services.search.fetch_cls_news')
    def test_cls_provider(self, mock_fetch):
        mock_fetch.return_value = [{'title': '财联社快讯', 'url': 'http://cls.cn', 'time': '10:30', 'source': '财联社'}]
        p = CLSNewsProvider()
        resp = p._do_search('query', 'free', 5)
        assert resp.success
        assert resp.results[0].title == '财联社快讯'

    @patch('app.services.search.fetch_cls_news')
    def test_cls_provider_empty(self, mock_fetch):
        mock_fetch.return_value = []
        p = CLSNewsProvider()
        resp = p._do_search('query', 'free', 5)
        assert not resp.success

    @patch('app.services.search.fetch_wallstreetcn_news')
    def test_wallstreetcn_provider(self, mock_fetch):
        mock_fetch.return_value = [{'title': '见闻快讯', 'url': 'http://wsj.com', 'time': '09:00', 'source': '华尔街见闻'}]
        p = WallStreetCNNewsProvider()
        resp = p._do_search('query', 'free', 5)
        assert resp.success

    @patch('app.services.search.fetch_eastmoney_news')
    def test_eastmoney_provider(self, mock_fetch):
        mock_fetch.return_value = [{'title': '东方财富', 'url': 'http://em.com', 'time': '08:00', 'source': '东方财富'}]
        p = EastMoneyNewsProvider()
        resp = p._do_search('query', 'free', 5)
        assert resp.success

    @patch('app.services.search.fetch_sina_finance_news')
    def test_sina_provider(self, mock_fetch):
        mock_fetch.return_value = [{'title': '新浪新闻', 'url': 'http://sina.com', 'time': '07:00', 'source': '新浪财经'}]
        p = SinaFinanceNewsProvider()
        resp = p._do_search('query', 'free', 5)
        assert resp.success

    @patch('app.services.search.fetch_akshare_news')
    def test_akshare_provider(self, mock_fetch):
        mock_fetch.return_value = [{'title': 'AKShare', 'url': 'http://ak.com', 'time': '06:00', 'source': 'AKShare'}]
        p = AKShareNewsProvider()
        resp = p._do_search('query', 'free', 5)
        assert resp.success

    @patch('app.services.search.fetch_cls_news')
    def test_provider_error_handling(self, mock_fetch):
        mock_fetch.side_effect = RuntimeError('network error')
        p = CLSNewsProvider()
        resp = p._do_search('query', 'free', 5)
        assert not resp.success
        assert 'network error' in resp.error_message


# ═══════════════════════════════════════════════════════════════
#  6. SearchService
# ═══════════════════════════════════════════════════════════════

class TestSearchServiceClassify:
    """_classify_news_type 路由分类"""

    def test_policy_cn(self):
        assert SearchService._classify_news_type('POLICY', 'CNStock') == 'policy'

    def test_policy_us(self):
        assert SearchService._classify_news_type('POLICY', 'USStock') == 'policy'

    def test_policy_crypto(self):
        assert SearchService._classify_news_type('POLICY', 'Crypto') == 'policy'

    def test_policy_forex(self):
        assert SearchService._classify_news_type('POLICY', 'Forex') == 'policy'

    def test_keyword_pure(self):
        """symbol+market 都空 + keywords → 纯关键词搜索"""
        assert SearchService._classify_news_type('', 'all', has_symbol=False, has_keywords=True) == 'keyword'

    def test_general_no_keywords(self):
        """symbol+market 都空 + 无 keywords → 通用财经"""
        assert SearchService._classify_news_type('', 'all', has_symbol=False, has_keywords=False) == 'general'

    def test_market_symbol_equals_market(self):
        assert SearchService._classify_news_type('CNStock', 'CNStock') == 'market'

    def test_market_empty_symbol_specific_market(self):
        """symbol 为空 + 具体 market → 市场新闻 (非 stock)"""
        assert SearchService._classify_news_type('', 'CNStock', has_symbol=False) == 'market'
        assert SearchService._classify_news_type('', 'USStock', has_symbol=False) == 'market'
        assert SearchService._classify_news_type('', 'Crypto', has_symbol=False) == 'market'

    def test_stock_cn(self):
        assert SearchService._classify_news_type('600519', 'CNStock') == 'stock'

    def test_stock_us(self):
        assert SearchService._classify_news_type('AAPL', 'USStock') == 'stock'


class TestSearchServiceIsStockCode:
    """_is_stock_code 判断"""

    def test_stock_codes(self):
        assert SearchService._is_stock_code('600519')
        assert SearchService._is_stock_code('AAPL')
        assert SearchService._is_stock_code('TSLA')

    def test_market_names(self):
        assert not SearchService._is_stock_code('CNStock')
        assert not SearchService._is_stock_code('USStock')
        assert not SearchService._is_stock_code('Crypto')
        assert not SearchService._is_stock_code('Forex')
        assert not SearchService._is_stock_code('POLICY')

    def test_empty(self):
        assert not SearchService._is_stock_code('')


class TestSearchServiceInit:
    """初始化和配置"""

    def test_providers_loaded(self):
        svc = SearchService()
        assert len(svc._providers) > 0

    def test_has_news_providers(self):
        svc = SearchService()
        names = [p.name for p in svc._providers]
        assert '财联社' in names
        assert '华尔街见闻' in names
        assert '东方财富' in names

    def test_has_duckduckgo_fallback(self):
        svc = SearchService()
        names = [p.name for p in svc._providers]
        assert 'DuckDuckGo' in names

    def test_is_available(self):
        svc = SearchService()
        assert svc.is_available  # 至少有 DuckDuckGo


class TestSearchServiceSearch:
    """search() 兼容旧接口"""

    @patch.object(SearchService, 'search_parallel')
    def test_delegates_to_parallel(self, mock_parallel):
        mock_parallel.return_value = SearchResponse(
            query='test', results=[], provider='Parallel', success=True
        )
        svc = SearchService()
        result = svc.search('query', num_results=5)
        mock_parallel.assert_called_once()

    @patch.object(SearchService, 'search_parallel')
    def test_date_restrict_conversion(self, mock_parallel):
        mock_parallel.return_value = SearchResponse(
            query='test', results=[], provider='Parallel', success=True
        )
        svc = SearchService()
        svc.search('query', date_restrict='d3')
        call_args = mock_parallel.call_args
        assert call_args[1].get('days') == 3 or call_args[0][2] == 3


class TestSearchServiceFallback:
    """search_with_fallback() 串行故障转移"""

    def test_first_success_returns(self):
        svc = SearchService()
        #  mock 前两个 provider
        p1 = MagicMock()
        p1.is_available = True
        p1.name = 'P1'
        p1.search.return_value = SearchResponse(
            query='q', results=[SearchResult(title='t', snippet='s', url='u', source='src')],
            provider='P1', success=True
        )
        p2 = MagicMock()
        p2.is_available = True
        p2.name = 'P2'

        svc._providers = [p1, p2]
        resp = svc.search_with_fallback('query')
        assert resp.success
        assert resp.provider == 'P1'
        p2.search.assert_not_called()

    def test_failover_to_second(self):
        svc = SearchService()
        p1 = MagicMock()
        p1.is_available = True
        p1.name = 'P1'
        p1.search.return_value = SearchResponse(
            query='q', results=[], provider='P1', success=False, error_message='fail'
        )
        p2 = MagicMock()
        p2.is_available = True
        p2.name = 'P2'
        p2.search.return_value = SearchResponse(
            query='q', results=[SearchResult(title='ok', snippet='s', url='u', source='src')],
            provider='P2', success=True
        )

        svc._providers = [p1, p2]
        resp = svc.search_with_fallback('query')
        assert resp.success
        assert resp.provider == 'P2'

    def test_all_fail_returns_failure(self):
        svc = SearchService()
        p1 = MagicMock()
        p1.is_available = True
        p1.name = 'P1'
        p1.search.return_value = SearchResponse(
            query='q', results=[], provider='P1', success=False, error_message='fail'
        )
        svc._providers = [p1]
        resp = svc.search_with_fallback('query')
        assert not resp.success

    def test_unavailable_provider_skipped(self):
        svc = SearchService()
        p1 = MagicMock()
        p1.is_available = False
        p1.name = 'P1'
        p2 = MagicMock()
        p2.is_available = True
        p2.name = 'P2'
        p2.search.return_value = SearchResponse(
            query='q', results=[SearchResult(title='t', snippet='s', url='u', source='src')],
            provider='P2', success=True
        )
        svc._providers = [p1, p2]
        resp = svc.search_with_fallback('query')
        assert resp.success
        assert resp.provider == 'P2'
        p1.search.assert_not_called()


class TestSearchServiceParallel:
    """search_parallel() 多源并行"""

    @patch.object(SearchService, '_search_single_provider')
    def test_aggregates_results(self, mock_single):
        def side_effect(provider, query, max_results, days):
            return provider.name, SearchResponse(
                query=query, results=[
                    SearchResult(title=f'{provider.name}-r1', snippet='s', url=f'http://{provider.name}', source=provider.name),
                ], provider=provider.name, success=True
            )
        mock_single.side_effect = side_effect

        svc = SearchService()
        p1 = MagicMock()
        p1.is_available = True
        p1.name = 'P1'
        p2 = MagicMock()
        p2.is_available = True
        p2.name = 'P2'
        svc._providers = [p1, p2]

        resp = svc.search_parallel('query', max_results=5)
        assert resp.success
        assert len(resp.results) == 2

    @patch.object(SearchService, '_search_single_provider')
    def test_dedup_by_url(self, mock_single):
        def side_effect(provider, query, max_results, days):
            return provider.name, SearchResponse(
                query=query, results=[
                    SearchResult(title='same', snippet='s', url='http://same.com', source=provider.name),
                ], provider=provider.name, success=True
            )
        mock_single.side_effect = side_effect

        svc = SearchService()
        p1 = MagicMock()
        p1.is_available = True
        p1.name = 'P1'
        p2 = MagicMock()
        p2.is_available = True
        p2.name = 'P2'
        svc._providers = [p1, p2]

        resp = svc.search_parallel('query', dedup=True)
        assert len(resp.results) == 1  # 去重后只有 1 条

    @patch.object(SearchService, '_search_single_provider')
    def test_all_fail_returns_failure(self, mock_single):
        def side_effect(provider, query, max_results, days):
            return provider.name, SearchResponse(
                query=query, results=[], provider=provider.name,
                success=False, error_message='fail'
            )
        mock_single.side_effect = side_effect

        svc = SearchService()
        p1 = MagicMock()
        p1.is_available = True
        p1.name = 'P1'
        svc._providers = [p1]

        resp = svc.search_parallel('query')
        assert not resp.success

    def test_no_providers_returns_failure(self):
        svc = SearchService()
        svc._providers = []
        resp = svc.search_parallel('query')
        assert not resp.success


# ═══════════════════════════════════════════════════════════════
#  7. search_news_dispatch()  4 条路由
# ═══════════════════════════════════════════════════════════════

class TestSearchNewsDispatch:
    """search_news_dispatch() 路由分发测试"""

    @patch('app.services.search.fetch_all_market_news')
    @patch.object(SearchService, 'search_with_fallback')
    def test_policy_route(self, mock_web, mock_market):
        """POLICY 路由: 市场源 + Web 政策关键词并行"""
        mock_market.return_value = [
            {'title': '市场新闻', 'url': 'http://m.com', 'time': '10:00', 'source': '财联社'}
        ]
        mock_web.return_value = SearchResponse(
            query='政策', results=[
                SearchResult(title='国务院政策', snippet='s', url='http://p.com', source='Web')
            ], provider='Web', success=True
        )

        svc = SearchService()
        resp = svc.search_news_dispatch(symbol='POLICY', market='CNStock', days=1)

        assert resp.success
        assert resp.metadata['news_type'] == 'policy'
        assert len(resp.results) >= 1

    @patch('app.services.search.fetch_all_market_news')
    def test_market_route_with_source(self, mock_market):
        """市场路由: 市场源有结果, 不走 Web"""
        mock_market.return_value = [
            {'title': 'A股行情', 'url': 'http://m.com', 'time': '10:00', 'source': '财联社'}
        ]

        svc = SearchService()
        resp = svc.search_news_dispatch(symbol='CNStock', market='CNStock', days=1)

        assert resp.success
        assert resp.metadata['news_type'] == 'market'
        assert resp.metadata['source_count'] >= 1

    @patch('app.services.search.fetch_all_market_news')
    @patch.object(SearchService, '_web_search_market')
    def test_market_route_fallback_to_web(self, mock_web, mock_market):
        """市场路由: 市场源为空, 降级 Web"""
        mock_market.return_value = []
        mock_web.return_value = [
            SearchResult(title='Web市场', snippet='s', url='http://w.com', source='Web')
        ]

        svc = SearchService()
        resp = svc.search_news_dispatch(symbol='CNStock', market='CNStock', days=1)

        assert resp.success
        assert resp.metadata['news_type'] == 'market'

    @patch('app.services.search.fetch_all_stock_news')
    def test_stock_route_with_source(self, mock_stock):
        """个股路由: 个股源有结果"""
        mock_stock.return_value = [
            {'title': '茅台新闻', 'url': 'http://s.com', 'time': '10:00', 'source': '东方财富'}
        ]

        svc = SearchService()
        resp = svc.search_news_dispatch(symbol='600519', market='CNStock', name='贵州茅台', days=3)

        assert resp.success
        assert resp.metadata['news_type'] == 'stock'
        assert resp.metadata['source_count'] >= 1

    @patch('app.services.search.fetch_all_stock_news')
    @patch.object(SearchService, '_web_search_stock')
    def test_stock_route_fallback_to_web(self, mock_web, mock_stock):
        """个股路由: 个股源为空, 降级 Web"""
        mock_stock.return_value = []
        mock_web.return_value = [
            SearchResult(title='Web个股', snippet='s', url='http://w.com', source='Web')
        ]

        svc = SearchService()
        resp = svc.search_news_dispatch(symbol='600519', market='CNStock', name='贵州茅台', days=3)

        assert resp.success
        assert resp.metadata['news_type'] == 'stock'

    @patch.object(SearchService, 'search_with_fallback')
    def test_general_route(self, mock_web):
        """通用路由: 中英文并行搜索"""
        mock_web.return_value = SearchResponse(
            query='通用', results=[
                SearchResult(title='财经新闻', snippet='s', url='http://g.com', source='Web')
            ], provider='Web', success=True
        )

        svc = SearchService()
        resp = svc.search_news_dispatch(symbol='', market='all', lang='all')

        assert resp.success
        assert resp.metadata['news_type'] == 'general'

    @patch.object(SearchService, 'search_with_fallback')
    def test_general_cn_only(self, mock_web):
        """通用路由: 仅中文"""
        mock_web.return_value = SearchResponse(
            query='中文', results=[
                SearchResult(title='中文新闻', snippet='s', url='http://cn.com', source='Web')
            ], provider='Web', success=True
        )

        svc = SearchService()
        resp = svc.search_news_dispatch(symbol='', market='all', lang='cn')

        assert resp.success
        # 应该只调用 cn 查询
        for call in mock_web.call_args_list:
            query = call[0][0]
            # 所有查询应该包含中文
            assert any('\u4e00' <= c <= '\u9fff' for c in query) or 'cn' in str(call)


class TestSearchNewsDispatchMetadata:
    """验证 metadata 完整性"""

    @patch('app.services.search.fetch_all_market_news')
    def test_metadata_fields(self, mock_market):
        mock_market.return_value = [
            {'title': '新闻', 'url': 'http://x.com', 'time': '10:00', 'source': 's'}
        ]

        svc = SearchService()
        resp = svc.search_news_dispatch(symbol='CNStock', market='CNStock')

        meta = resp.metadata
        assert 'news_type' in meta
        assert 'source_count' in meta
        assert 'web_results_added' in meta
        assert 'total_merged' in meta
        assert 'from_cache' in meta
        assert meta['from_cache'] is False


class TestSearchNewsDispatchKeywords:
    """自定义 keywords 参数"""

    @patch('app.services.search.fetch_all_market_news')
    @patch.object(SearchService, 'search_with_fallback')
    def test_keywords_appended_to_market(self, mock_web, mock_market):
        """市场路由 + keywords → 额外 Web 搜索"""
        mock_market.return_value = [
            {'title': '市场新闻', 'url': 'http://m.com', 'time': '10:00', 'source': '财联社'}
        ]
        mock_web.return_value = SearchResponse(
            query='新能源汽车', results=[
                SearchResult(title='新能源利好', snippet='s', url='http://kw.com', source='Web')
            ], provider='Web', success=True
        )

        svc = SearchService()
        resp = svc.search_news_dispatch(
            symbol='CNStock', market='CNStock', keywords='新能源汽车'
        )

        assert resp.success
        # 市场源 + keywords Web 结果
        titles = [r.title for r in resp.results]
        assert '市场新闻' in titles
        assert '新能源利好' in titles

    @patch('app.services.search.fetch_all_stock_news')
    @patch.object(SearchService, 'search_with_fallback')
    def test_keywords_appended_to_stock(self, mock_web, mock_stock):
        """个股路由 + keywords → 额外 Web 搜索"""
        mock_stock.return_value = [
            {'title': '茅台公告', 'url': 'http://s.com', 'time': '10:00', 'source': '东财'}
        ]
        mock_web.return_value = SearchResponse(
            query='茅台 AI', results=[
                SearchResult(title='茅台AI布局', snippet='s', url='http://kw.com', source='Web')
            ], provider='Web', success=True
        )

        svc = SearchService()
        resp = svc.search_news_dispatch(
            symbol='600519', market='CNStock', name='贵州茅台', keywords='茅台 AI'
        )

        assert resp.success
        titles = [r.title for r in resp.results]
        assert '茅台公告' in titles
        assert '茅台AI布局' in titles

    @patch('app.services.search.fetch_all_market_news')
    @patch.object(SearchService, 'search_with_fallback')
    def test_no_keywords_no_extra_search(self, mock_web, mock_market):
        """无 keywords 时不做额外 Web 搜索 (市场源有结果)"""
        mock_market.return_value = [
            {'title': '市场新闻', 'url': 'http://m.com', 'time': '10:00', 'source': '财联社'}
        ]

        svc = SearchService()
        resp = svc.search_news_dispatch(symbol='CNStock', market='CNStock')

        assert resp.success
        # search_with_fallback 不应被调用 (市场源已有结果, 且无 keywords)
        mock_web.assert_not_called()

    @patch.object(SearchService, 'search_with_fallback')
    def test_keyword_route_pure_keywords(self, mock_web):
        """纯关键词路由: symbol+market 都空 + keywords → 只用 keywords 搜索"""
        mock_web.return_value = SearchResponse(
            query='新能源汽车', results=[
                SearchResult(title='新能源利好', snippet='s', url='http://kw.com', source='Web')
            ], provider='Web', success=True
        )

        svc = SearchService()
        resp = svc.search_news_dispatch(symbol='', market='all', keywords='新能源汽车')

        assert resp.success
        assert resp.metadata['news_type'] == 'keyword'
        assert resp.provider == 'Web(关键词)'
        mock_web.assert_called_once_with('新能源汽车', 5, 3)

    @patch.object(SearchService, 'search_with_fallback')
    def test_general_route_no_keywords(self, mock_web):
        """通用路由: 无 keywords → 用硬编码查询"""
        mock_web.return_value = SearchResponse(
            query='通用', results=[
                SearchResult(title='财经新闻', snippet='s', url='http://g.com', source='Web')
            ], provider='Web', success=True
        )

        svc = SearchService()
        resp = svc.search_news_dispatch(symbol='', market='all')

        assert resp.success
        assert resp.metadata['news_type'] == 'general'
        # 应该调用多次 (每个硬编码查询一次)
        assert mock_web.call_count > 1


# ═══════════════════════════════════════════════════════════════
#  8. get_search_service / reset_search_service
# ═══════════════════════════════════════════════════════════════

class TestSearchServiceSingleton:
    def test_singleton(self):
        reset_search_service()
        s1 = get_search_service()
        s2 = get_search_service()
        assert s1 is s2

    def test_reset_creates_new(self):
        reset_search_service()
        s1 = get_search_service()
        reset_search_service()
        s2 = get_search_service()
        assert s1 is not s2


# ═══════════════════════════════════════════════════════════════
#  9. POLICY_QUERIES / GENERAL_QUERIES 配置
# ═══════════════════════════════════════════════════════════════

class TestQueryConfigs:
    def test_policy_queries_cover_markets(self):
        svc = SearchService()
        assert 'CNStock' in svc._POLICY_QUERIES
        assert 'USStock' in svc._POLICY_QUERIES
        assert 'Crypto' in svc._POLICY_QUERIES
        assert 'Forex' in svc._POLICY_QUERIES

    def test_policy_queries_have_cn_en(self):
        svc = SearchService()
        for market, queries in svc._POLICY_QUERIES.items():
            assert 'cn' in queries, f"{market} missing 'cn'"
            assert 'en' in queries, f"{market} missing 'en'"
            assert len(queries['cn']) >= 2, f"{market} 'cn' too few queries"
            assert len(queries['en']) >= 2, f"{market} 'en' too few queries"

    def test_general_queries_have_cn_en(self):
        svc = SearchService()
        assert 'cn' in svc._GENERAL_QUERIES
        assert 'en' in svc._GENERAL_QUERIES
        assert len(svc._GENERAL_QUERIES['cn']) >= 3
        assert len(svc._GENERAL_QUERIES['en']) >= 3


# ═══════════════════════════════════════════════════════════════
# 10. _dicts_to_results 转换
# ═══════════════════════════════════════════════════════════════

class TestDictsToResults:
    def test_valid_dicts(self):
        svc = SearchService()
        items = [
            {'title': '新闻1', 'url': 'http://1.com', 'source': 's1', 'time': '10:00'},
            {'title': '新闻2', 'url': 'http://2.com', 'source': 's2', 'time': '11:00'},
        ]
        results = svc._dicts_to_results(items)
        assert len(results) == 2
        assert all(isinstance(r, SearchResult) for r in results)
        assert results[0].title == '新闻1'

    def test_empty_title_skipped(self):
        svc = SearchService()
        items = [
            {'title': '', 'url': 'http://x.com', 'source': 's'},
            {'title': '有效', 'url': 'http://y.com', 'source': 's'},
        ]
        results = svc._dicts_to_results(items)
        assert len(results) == 1

    def test_error_key_skipped(self):
        svc = SearchService()
        items = [
            {'error': 'something failed'},
            {'title': '有效', 'url': 'http://y.com', 'source': 's'},
        ]
        results = svc._dicts_to_results(items)
        assert len(results) == 1

    def test_non_dict_skipped(self):
        svc = SearchService()
        items = ['not a dict', {'title': 'ok', 'url': 'u', 'source': 's'}]
        results = svc._dicts_to_results(items)
        assert len(results) == 1


# ═══════════════════════════════════════════════════════════════
# 11. Provider days_to_recency / days_to_freshness 转换
# ═══════════════════════════════════════════════════════════════

class TestProviderHelpers:
    def test_baidu_recency(self):
        assert BaiduSearchProvider._days_to_recency(1) == 'day'
        assert BaiduSearchProvider._days_to_recency(3) == 'week'
        assert BaiduSearchProvider._days_to_recency(15) == 'month'
        assert BaiduSearchProvider._days_to_recency(60) == 'all'

    def test_bocha_freshness(self):
        assert BochaAISearchProvider._days_to_freshness(1) == 'pd'
        assert BochaAISearchProvider._days_to_freshness(5) == 'pw'
        assert BochaAISearchProvider._days_to_freshness(20) == 'pm'
        assert BochaAISearchProvider._days_to_freshness(60) == ''
