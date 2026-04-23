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
