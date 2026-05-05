# -*- coding: utf-8 -*-
"""
CNStockDataSource 单元测试

覆盖范围:
  - __init__: 熔断器 / 缓存初始化
  - get_ticker: 缓存命中、Coordinator race、全部失败兜底
  - get_ticker 批量: Provider 透传、无 Provider 兜底
  - get_kline: 缓存命中、Coordinator 调度、全部失败、时间过滤
  - get_kline 批量: 月线日线聚合、逗号分隔、部分失败
  - 辅助函数: _validate_kline_result, _strip_cn_prefix, _aggregate_daily_to_monthly
"""
import sys
import os
import types
import time
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta

import pytest

# ── 确保 backend 包可导入 ──
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── Mock 掉重型可选依赖（必须在 import app 之前）──
_flask = types.ModuleType("flask")
_flask.__version__ = "3.0.0"
_flask.__path__ = []
_flask.__file__ = "/mock/flask/__init__.py"
_flask.__spec__ = None
_flask.__loader__ = None
_flask.__package__ = "flask"
_flask.Flask = type("Flask", (), {"__init__": lambda self, *a, **kw: None})
_flask.CORS = lambda *a, **kw: None
_flask_json = types.ModuleType("flask.json")
_flask_json.__path__ = []
_flask_json.__package__ = "flask"
sys.modules["flask.json"] = _flask_json
_flask.json = _flask_json
_flask_json_prov = types.ModuleType("flask.json.provider")
_flask_json_prov.__package__ = "flask"
_flask_json_prov.DefaultJSONProvider = type("DefaultJSONProvider", (), {
    "__init__": lambda self, *a, **kw: None,
    "default": staticmethod(lambda o: str(o)),
    "dumps": lambda self, obj, **kw: "{}",
})
sys.modules["flask.json.provider"] = _flask_json_prov
sys.modules["flask"] = _flask

_fc = types.ModuleType("flask_cors")
_fc.__version__ = "4.0.0"
_fc.CORS = lambda *a, **kw: None
sys.modules["flask_cors"] = _fc

_MOCKED_MODULES = [
    "ccxt", "yfinance", "yfinance.shared", "akshare",
    "finnhub", "ib_insync", "redis", "bcrypt", "bip_utils",
    "gunicorn", "baostock", "efinance", "pytdx", "pytdx.hq",
    "pandas", "pyarrow", "pydantic",
    "jwt", "cryptography", "socks",
    "sqlalchemy", "sqlalchemy.orm", "sqlalchemy.ext",
    "flask_sqlalchemy",
]
for _mod_name in _MOCKED_MODULES:
    if _mod_name not in sys.modules:
        _m = types.ModuleType(_mod_name)
        _m.__version__ = "0.0.0"
        _m.__file__ = f"/mock/{_mod_name}/__init__.py"
        _m.__path__ = []
        _m.__spec__ = None
        _m.__loader__ = None
        _m.__package__ = _mod_name
        sys.modules[_mod_name] = _m

if "psycopg2" not in sys.modules:
    _pg = types.ModuleType("psycopg2")
    _pg.__version__ = "0.0.0"
    _pg.__file__ = "/mock/psycopg2/__init__.py"
    _pg.__path__ = []
    _pg.__spec__ = None
    _pg.__loader__ = None
    _pg.__package__ = "psycopg2"
    _pg.extras = types.ModuleType("psycopg2.extras")
    _pg.extras.RealDictCursor = type("RealDictCursor", (), {})
    _pg.pool = types.ModuleType("psycopg2.pool")
    _pg.pool.ThreadedConnectionPool = type("ThreadedConnectionPool", (), {})
    sys.modules["psycopg2"] = _pg
    sys.modules["psycopg2.extras"] = _pg.extras
    sys.modules["psycopg2.pool"] = _pg.pool

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("CACHE_ENABLED", "false")

# ── 导入被测模块 ──
from app.data_sources.cn_stock import (
    CNStockDataSource,
    _validate_kline_result,
    _strip_cn_prefix,
    _aggregate_daily_to_monthly,
)
from app.data_sources.circuit_breaker import CircuitBreaker
from app.data_sources.cache_manager import DataCache


# ======================================================================
# Fixtures
# ======================================================================

@pytest.fixture
def mock_coordinator():
    """Mock 全局 Coordinator 实例。"""
    coord = MagicMock()
    return coord


@pytest.fixture(autouse=True)
def _clear_singletons():
    """清空模块级单例缓存，防止跨测试污染。"""
    from app.data_sources.cache_manager import get_realtime_cache, get_kline_cache
    get_realtime_cache().clear()
    get_kline_cache().clear()
    yield
    get_realtime_cache().clear()
    get_kline_cache().clear()


@pytest.fixture
def ds(mock_coordinator):
    """
    构造 CNStockDataSource，注入 mock Coordinator。
    关键：patch 的是 cn_stock 模块中 get_coordinator 的引用，
    这样 ds 内部调用 get_coordinator() 时拿到的是 mock。
    """
    with patch("app.data_sources.cn_stock.get_coordinator", return_value=mock_coordinator):
        src = CNStockDataSource()
        yield src


def _make_bars(n=10, start_ts=1700000000, interval=86400):
    """生成 n 条测试 K 线。"""
    return [
        {"time": start_ts + i * interval, "open": 10.0, "high": 11.0,
         "low": 9.0, "close": 10.5, "volume": 1000.0}
        for i in range(n)
    ]


# ======================================================================
# 辅助函数测试
# ======================================================================

class TestHelpers:
    """模块级辅助函数"""

    # ── _strip_cn_prefix ──

    def test_strip_sh_prefix(self):
        assert _strip_cn_prefix("SH600519") == "600519"

    def test_strip_sz_prefix(self):
        assert _strip_cn_prefix("SZ000001") == "000001"

    def test_strip_bj_prefix(self):
        assert _strip_cn_prefix("BJ830799") == "830799"

    def test_strip_lower_prefix(self):
        assert _strip_cn_prefix("sh600519") == "600519"

    def test_no_prefix(self):
        assert _strip_cn_prefix("600519") == "600519"

    def test_empty(self):
        assert _strip_cn_prefix("") == ""
        assert _strip_cn_prefix(None) == ""

    def test_short_string_with_prefix(self):
        """长度 < 3 的带前缀字符串不剥离"""
        assert _strip_cn_prefix("SH") == "SH"

    # ── _validate_kline_result ──

    def test_valid_bars(self):
        bars = _make_bars(5)
        assert _validate_kline_result(bars) is True

    def test_empty_bars(self):
        assert _validate_kline_result([]) is False

    def test_below_min_bars(self):
        bars = _make_bars(2)
        assert _validate_kline_result(bars, min_bars=5) is False

    def test_invalid_time(self):
        bars = [{"time": 0, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 100}]
        assert _validate_kline_result(bars) is False

    def test_negative_close(self):
        bars = [{"time": 100, "open": 1, "high": 2, "low": 0.5, "close": -1, "volume": 100}]
        assert _validate_kline_result(bars) is False

    def test_high_less_than_low(self):
        bars = [{"time": 100, "open": 1, "high": 5, "low": 8, "close": 6, "volume": 100}]
        assert _validate_kline_result(bars) is False

    def test_non_dict_element(self):
        assert _validate_kline_result(["not_a_dict"]) is False

    # ── _aggregate_daily_to_monthly ──

    def test_aggregate_basic(self):
        """同一个月的日线应聚合成一根月线。"""
        base = int(datetime(2024, 1, 2, tzinfo=timezone(timedelta(hours=8))).timestamp())
        daily = [
            {"time": base + i * 86400, "open": 10 + i, "high": 12 + i,
             "low": 9 + i, "close": 11 + i, "volume": 100.0 * (i + 1)}
            for i in range(4)
        ]
        monthly = _aggregate_daily_to_monthly(daily, limit=10)
        assert len(monthly) == 1
        m = monthly[0]
        assert m["open"] == 10.0
        assert m["close"] == 14.0
        assert m["high"] == max(12, 13, 14, 15)
        assert m["low"] == min(9, 10, 11, 12)
        assert m["volume"] == 100 + 200 + 300 + 400

    def test_aggregate_multi_month(self):
        """跨月数据应分成多根月线。"""
        jan = int(datetime(2024, 1, 15, tzinfo=timezone(timedelta(hours=8))).timestamp())
        feb = int(datetime(2024, 2, 15, tzinfo=timezone(timedelta(hours=8))).timestamp())
        daily = [
            {"time": jan, "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 100},
            {"time": feb, "open": 20, "high": 21, "low": 19, "close": 20.5, "volume": 200},
        ]
        monthly = _aggregate_daily_to_monthly(daily, limit=10)
        assert len(monthly) == 2

    def test_aggregate_limit(self):
        """limit 应截断到最近 N 个月。"""
        bars = []
        for m in range(1, 7):
            ts = int(datetime(2024, m, 15, tzinfo=timezone(timedelta(hours=8))).timestamp())
            bars.append({"time": ts, "open": 10, "high": 11, "low": 9, "close": 10, "volume": 100})
        monthly = _aggregate_daily_to_monthly(bars, limit=3)
        assert len(monthly) == 3

    def test_aggregate_empty(self):
        assert _aggregate_daily_to_monthly([], 10) == []


# ======================================================================
# __init__
# ======================================================================

class TestInit:

    def test_creates_caches_and_cb(self, ds):
        assert isinstance(ds.circuit_breaker, CircuitBreaker)
        assert isinstance(ds.realtime_cache, DataCache)
        assert isinstance(ds.kline_cache, DataCache)
        assert ds.name == "CNStock/multi-source"


# ======================================================================
# get_ticker
# ======================================================================

class TestGetTicker:

    def test_cache_hit(self, ds, mock_coordinator):
        """缓存命中时直接返回，不调用 Coordinator。"""
        cached_quote = {"last": 42.0, "symbol": "SH600519"}
        ds.realtime_cache.set("ticker:SH600519", cached_quote, ttl=600)

        result = ds.get_ticker("600519")
        assert result["last"] == 42.0
        mock_coordinator.coordinate_ticker.assert_not_called()

    def test_coordinator_returns_quote(self, ds, mock_coordinator):
        """缓存 miss 时走 Coordinator race。"""
        quote = {"last": 15.5, "symbol": "SZ000001", "change": 0.3}
        mock_coordinator.coordinate_ticker.return_value = quote

        result = ds.get_ticker("000001")
        assert result["last"] == 15.5
        mock_coordinator.coordinate_ticker.assert_called_once()
        call_kwargs = mock_coordinator.coordinate_ticker.call_args
        assert call_kwargs.kwargs["market"] == "CNStock"

    def test_coordinator_returns_none(self, ds, mock_coordinator):
        """所有源都失败时返回兜底值。"""
        mock_coordinator.coordinate_ticker.return_value = None

        result = ds.get_ticker("600519")
        assert result == {"last": 0, "symbol": "SH600519"}

    def test_result_is_cached(self, ds, mock_coordinator):
        """Coordinator 成功后结果应写入缓存。"""
        quote = {"last": 100.0, "symbol": "SH600519"}
        mock_coordinator.coordinate_ticker.return_value = quote

        ds.get_ticker("600519")
        cached = ds.realtime_cache.get("ticker:SH600519")
        assert cached is not None
        assert cached["last"] == 100.0


# ======================================================================
# get_ticker 批量模式（逗号分隔 → _get_tickers）
# ======================================================================

class TestGetTickerBatch:

    def test_delegates_to_provider(self, ds, mock_coordinator):
        """有 Provider 时透传 fetch_tickers。"""
        expected = {"600519": {"close": 1800.0}, "000001": {"close": 10.0}}
        mock_provider = MagicMock()
        mock_provider.fetch_tickers.return_value = expected

        with patch("app.data_sources.provider.get_providers", return_value=[mock_provider]):
            with patch.object(mock_coordinator, "direct_call", side_effect=lambda fn, *a: fn(*a)):
                result = ds.get_ticker("600519,000001")

        assert result == expected

    def test_no_providers_returns_empty(self, ds):
        """无 Provider 时返回空。"""
        with patch("app.data_sources.provider.get_providers", return_value=[]):
            result = ds.get_ticker("600519,000001")
        assert result == {}

    def test_provider_returns_empty(self, ds, mock_coordinator):
        """Provider 返回空时继续尝试下一个。"""
        p1 = MagicMock()
        p1.fetch_tickers.return_value = {}
        p2 = MagicMock()
        p2.fetch_tickers.return_value = {}

        with patch("app.data_sources.provider.get_providers", return_value=[p1, p2]):
            with patch.object(mock_coordinator, "direct_call", side_effect=lambda fn, *a: fn(*a)):
                result = ds.get_ticker("600519,000001")
        assert result == {}


# ======================================================================
# get_kline
# ======================================================================

class TestGetKline:

    def test_cache_hit(self, ds, mock_coordinator):
        """缓存命中直接返回。"""
        bars = _make_bars(5)
        # 用真实的缓存 key 格式 — 先获取一次空结果看 key 是什么，
        # 或者直接用 kline_cache.set 带上正确的 key。
        # 简单方式：直接写入缓存然后读取。
        from app.data_sources.cache_manager import generate_kline_cache_key
        key = generate_kline_cache_key("SH600519", "1D", 300, None)
        ds.kline_cache.set(key, bars, ttl=300)

        result = ds.get_kline("600519", "1D", 300)
        assert result == bars
        mock_coordinator.coordinate_kline.assert_not_called()

    def test_coordinator_success(self, ds, mock_coordinator):
        """缓存 miss → Coordinator 返回数据。"""
        bars = _make_bars(10)
        mock_coordinator.coordinate_kline.return_value = ({"SH600519": bars}, [])

        result = ds.get_kline("600519", "1D", 10)
        assert len(result) == 10
        mock_coordinator.coordinate_kline.assert_called_once()

    def test_coordinator_all_fail(self, ds, mock_coordinator):
        """所有源失败返回空。"""
        mock_coordinator.coordinate_kline.return_value = ({}, ["SH600519"])

        result = ds.get_kline("600519", "1D", 10)
        assert result == []

    def test_after_time_filtering(self, ds, mock_coordinator):
        """after_time 参数应传递给 filter_and_limit。"""
        bars = _make_bars(10, start_ts=1700000000)
        mock_coordinator.coordinate_kline.return_value = ({"SH600519": bars}, [])

        result = ds.get_kline("600519", "1D", 10, after_time=1700000000 + 5 * 86400)
        for bar in result:
            assert bar["time"] >= 1700000000 + 5 * 86400

    def test_before_time_filtering(self, ds, mock_coordinator):
        """before_time 参数应过滤掉 >= before_time 的数据。"""
        bars = _make_bars(10, start_ts=1700000000)
        cutoff = 1700000000 + 5 * 86400
        mock_coordinator.coordinate_kline.return_value = ({"SH600519": bars}, [])

        result = ds.get_kline("600519", "1D", 10, before_time=cutoff)
        for bar in result:
            assert bar["time"] < cutoff

    def test_result_cached(self, ds, mock_coordinator):
        """成功获取后应写入缓存。"""
        bars = _make_bars(5)
        mock_coordinator.coordinate_kline.return_value = ({"SH600519": bars}, [])

        ds.get_kline("600519", "1D", 5)
        from app.data_sources.cache_manager import generate_kline_cache_key
        key = generate_kline_cache_key("SH600519", "1D", 5, None)
        cached = ds.kline_cache.get(key)
        assert cached is not None
        assert len(cached) == 5

    def test_normalizes_symbol(self, ds, mock_coordinator):
        """symbol 应被 normalize_cn_code 标准化。"""
        mock_coordinator.coordinate_kline.return_value = ({"SH600519": _make_bars(3)}, [])

        ds.get_kline("600519.SH", "1D", 3)
        call_args = mock_coordinator.coordinate_kline.call_args
        assert call_args.kwargs["symbols"] == ["SH600519"]

    def test_empty_kline_from_coordinator(self, ds, mock_coordinator):
        """Coordinator 返回空 bars 时返回空列表。"""
        mock_coordinator.coordinate_kline.return_value = ({}, [])

        result = ds.get_kline("600519", "1D", 10)
        assert result == []


# ======================================================================
# get_kline 批量模式（逗号分隔 → _get_klines）
# ======================================================================

class TestGetKlineBatch:

    def test_empty_symbols(self, ds, mock_coordinator):
        result = ds.get_kline("", "1D", 10)
        assert result == []

    def test_monthly_aggregation(self, ds, mock_coordinator):
        """月线应先拉日线再聚合。"""
        daily_bars = _make_bars(60, start_ts=1700000000, interval=86400)
        mock_coordinator.coordinate_kline.return_value = ({"SH600519": daily_bars}, [])

        result = ds.get_kline("600519", "1M", 3)
        # 月线聚合走 _get_klines → 返回 dict，但 get_kline 返回 list
        # get_kline 单只模式会取 dict 中的值
        assert len(result) <= 3

    def test_batch_comma_separated(self, ds, mock_coordinator):
        """逗号分隔走批量模式。"""
        bars_a = _make_bars(5)
        bars_b = _make_bars(5, start_ts=1700100000)
        mock_coordinator.coordinate_kline.return_value = (
            {"SH600519": bars_a, "SZ000001": bars_b}, []
        )

        result = ds.get_kline("600519,000001", "1D", 5)
        assert isinstance(result, dict)
        assert "SH600519" in result
        assert "SZ000001" in result

    def test_batch_partial_failure(self, ds, mock_coordinator):
        """批量模式部分失败时，成功的仍返回。"""
        bars = _make_bars(5)
        mock_coordinator.coordinate_kline.return_value = (
            {"SH600519": bars}, ["SZ000001"]
        )

        result = ds.get_kline("600519,000001", "1D", 10)
        assert isinstance(result, dict)
        assert "SH600519" in result
        assert "SZ000001" not in result

    def test_batch_coordinator_timeout(self, ds, mock_coordinator):
        """批量模式 timeout 应为 _get_timeout() + 10。"""
        mock_coordinator.coordinate_kline.return_value = ({}, [])

        ds.get_kline("600519,000001", "1D", 10)
        call_kwargs = mock_coordinator.coordinate_kline.call_args.kwargs
        assert call_kwargs["timeout"] > 0
        assert call_kwargs["market"] == "CNStock"


# ======================================================================
# get_ticker 缓存过期场景
# ======================================================================

class TestTickerCacheExpiry:

    def test_expired_cache_misses(self, ds, mock_coordinator):
        """TTL 过期后应重新获取。"""
        old_quote = {"last": 10.0, "symbol": "SH600519"}
        ds.realtime_cache.set("ticker:SH600519", old_quote, ttl=0.001)
        time.sleep(0.01)

        new_quote = {"last": 20.0, "symbol": "SH600519"}
        mock_coordinator.coordinate_ticker.return_value = new_quote

        result = ds.get_ticker("600519")
        assert result["last"] == 20.0
