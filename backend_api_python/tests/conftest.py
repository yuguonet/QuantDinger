"""Shared pytest fixtures."""
import os
import sys
import types
from unittest.mock import MagicMock

# Ensure the backend package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_mock_module(name, version='0.0.0'):
    """Create a mock module that passes importlib checks."""
    mod = types.ModuleType(name)
    mod.__version__ = version
    mod.__file__ = f'/mock/{name}/__init__.py'
    mod.__path__ = []
    mod.__spec__ = None
    mod.__loader__ = None
    mod.__package__ = name
    return mod


# ── Mock heavy optional dependencies BEFORE any app imports ──
_MOCKED_MODULES = [
    'yfinance', 'yfinance.shared', 'akshare', 'ccxt', 'finnhub',
    'ib_insync', 'redis', 'bcrypt', 'bip_utils', 'gunicorn',
]
for _mod in _MOCKED_MODULES:
    if _mod not in sys.modules:
        sys.modules[_mod] = _make_mock_module(_mod)

# psycopg2: needs sub-modules with real-looking class attributes
if 'psycopg2' not in sys.modules:
    _pg = _make_mock_module('psycopg2')
    _pg.extras = _make_mock_module('psycopg2.extras')
    _pg.extras.RealDictCursor = type('RealDictCursor', (), {})
    # db_market.py L41 顶层 `from psycopg2.extras import execute_values` ——
    # 缺此属性则任何 import db_market 的测试收集即炸（2026-10-24 提速任务
    # 引入 db_market 测试时发现）。mock 语义不变：本环境零真实连接，
    # 需要真 DB 的用例自行 skip（判据见 test_window_cache.test_real_db_golden：
    # 检测 psycopg2.__file__ 是否 /mock/ 前缀，勿再用「有无 execute_values」）。
    _pg.extras.execute_values = lambda *a, **k: None
    _pg.pool = _make_mock_module('psycopg2.pool')
    _pg.pool.ThreadedConnectionPool = type('ThreadedConnectionPool', (), {})
    sys.modules['psycopg2'] = _pg
    sys.modules['psycopg2.extras'] = _pg.extras
    sys.modules['psycopg2.pool'] = _pg.pool

# Minimal env so config classes don't blow up
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests")
os.environ.setdefault("ADMIN_USER", "testadmin")
os.environ.setdefault("ADMIN_PASSWORD", "testpass123")
os.environ.setdefault("TQDM_DISABLE", "1")
os.environ.setdefault("CACHE_ENABLED", "false")
# 测试环境不起 mootdx 保活 daemon（2026-10-25 L1）：create_app → scheduler.start()
# 会调 start_mootdx_keepalive，测试会话不该留后台 TDX 连接线程。需测保活的
# 用例在自己的进程内显式起（tests/test_mootdx_direct_connect_20261025.py）。
os.environ.setdefault("QD_MOOTDX_KEEPALIVE_SEC", "0")

import pytest
from app import create_app


@pytest.fixture(scope="session")
def app():
    """Create application for testing."""
    application = create_app("testing")
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app):
    """A test client for the app."""
    return app.test_client()
