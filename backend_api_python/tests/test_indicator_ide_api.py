"""
Indicator-IDE API 功能测试

覆盖 /api/indicator/* 下的所有端点（来自 indicator.py, kline.py, backtest.py）:
  1. GET  /api/indicator/getIndicators        — 获取指标列表
  2. POST /api/indicator/saveIndicator         — 创建/更新指标
  3. POST /api/indicator/deleteIndicator       — 删除指标
  4. GET  /api/indicator/getIndicatorParams    — 获取指标参数声明
  5. POST /api/indicator/verifyCode            — 验证指标代码
  6. POST /api/indicator/aiGenerate            — AI 生成指标代码 (SSE)
  7. POST /api/indicator/codeQualityHints      — 代码质量检查
  8. POST /api/indicator/parseStrategyConfig   — 解析 @strategy 注解
  9. POST /api/indicator/callIndicator         — 调用另一个指标
 10. GET  /api/indicator/kline                 — 获取 K 线数据
 11. GET  /api/indicator/price                 — 获取最新价格
 12. POST /api/indicator/backtest              — 运行回测
 13. GET  /api/indicator/backtest/precision-info — 回测精度信息

使用 pytest + Flask test client，mock 数据库和外部依赖，无需真实后端运行。
"""

import json
import os
import sys
import time
from unittest.mock import patch, MagicMock

import pytest
import numpy as np
import pandas as pd

# ── 确保可以导入 backend 模块 ──
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ═══════════════════════════════════════════════════════════════════════
# Constants & Helpers
# ═══════════════════════════════════════════════════════════════════════

SAMPLE_INDICATOR_CODE = '''
my_indicator_name = "Test RSI"
my_indicator_description = "Simple RSI indicator for testing"

# @param rsi_len int 14 RSI period
# @strategy stopLossPct 0.03
# @strategy takeProfitPct 0.06
# @strategy tradeDirection long

rsi_len = params.get('rsi_len', 14)
df = df.copy()

delta = df['close'].diff()
gain = delta.clip(lower=0)
loss = (-delta).clip(lower=0)
avg_gain = gain.ewm(alpha=1/rsi_len, adjust=False).mean()
avg_loss = loss.ewm(alpha=1/rsi_len, adjust=False).mean()
rs = avg_gain / avg_loss.replace(0, np.nan)
rsi = 100 - (100 / (1 + rs))
rsi = rsi.fillna(50)

raw_buy = (rsi < 30)
raw_sell = (rsi > 70)
buy = (raw_buy.fillna(False) & (~raw_buy.shift(1).fillna(False))).astype(bool)
sell = (raw_sell.fillna(False) & (~raw_sell.shift(1).fillna(False))).astype(bool)
df['buy'] = buy
df['sell'] = sell

buy_marks = [df['low'].iloc[i] * 0.995 if bool(df['buy'].iloc[i]) else None for i in range(len(df))]
sell_marks = [df['high'].iloc[i] * 1.005 if bool(df['sell'].iloc[i]) else None for i in range(len(df))]

output = {
  'name': my_indicator_name,
  'plots': [
    {'name': 'RSI(14)', 'data': rsi.tolist(), 'color': '#faad14', 'overlay': False}
  ],
  'signals': [
    {'type': 'buy', 'text': 'B', 'data': buy_marks, 'color': '#00E676'},
    {'type': 'sell', 'text': 'S', 'data': sell_marks, 'color': '#FF5252'}
  ]
}
'''

SIMPLE_INVALID_CODE = 'x = 1 / 0  # division by zero'

MOCK_USER_ID = 1
MOCK_TOKEN = "mock_jwt_token_for_test"


def _generate_mock_kline(n=200):
    """生成模拟 K 线数据"""
    from datetime import datetime, timedelta
    dates = [datetime.now() - timedelta(minutes=i) for i in range(n)]
    dates.reverse()
    np.random.seed(42)
    returns = np.random.normal(0, 0.002, n)
    price_path = 10000 * np.exp(np.cumsum(returns))
    close = price_path
    high = close * (1 + np.abs(np.random.normal(0, 0.001, n)))
    low = close * (1 - np.abs(np.random.normal(0, 0.001, n)))
    open_p = close * (1 + np.random.normal(0, 0.001, n))
    high = np.maximum(high, np.maximum(open_p, close))
    low = np.minimum(low, np.minimum(open_p, close))
    volume = np.abs(np.random.normal(100, 50, n)) * 1000
    return pd.DataFrame({
        'time': [int(d.timestamp() * 1000) for d in dates],
        'open': open_p, 'high': high, 'low': low, 'close': close, 'volume': volume,
    })


def _fake_verify_token(token):
    if token == MOCK_TOKEN:
        return {
            'sub': 'testuser',
            'user_id': MOCK_USER_ID,
            'role': 'user',
            'token_version': 1,
        }
    return None


def _fake_get_db_connection():
    """Mock 数据库连接，返回 in-memory dict-based cursor"""
    class FakeCursor:
        def __init__(self):
            self._rows = []
            self._lastrowid = None

        def execute(self, sql, params=()):
            sql_lower = sql.strip().lower()
            if "from qd_indicator_codes" in sql_lower and "select" in sql_lower:
                self._rows = [{
                    'id': 101, 'user_id': MOCK_USER_ID, 'is_buy': 0, 'end_time': 1,
                    'name': 'Test RSI', 'code': SAMPLE_INDICATOR_CODE,
                    'description': 'Simple RSI indicator',
                    'publish_to_community': 0, 'pricing_type': 'free', 'price': 0,
                    'is_encrypted': 0, 'preview_image': '', 'vip_free': 0,
                    'createtime': int(time.time()), 'updatetime': int(time.time()),
                    'created_at': None, 'updated_at': None,
                }]
            elif "insert into qd_indicator_codes" in sql_lower:
                self._lastrowid = 202
                self._rows = []
            elif "update qd_indicator_codes" in sql_lower:
                self._rows = []
            elif "delete from qd_indicator_codes" in sql_lower:
                self._rows = []
            elif "select code from qd_indicator_codes" in sql_lower:
                self._rows = [{'code': SAMPLE_INDICATOR_CODE}]
            elif "select is_buy" in sql_lower and "from qd_indicator_codes" in sql_lower:
                self._rows = [{'is_buy': 0}]
            elif "select publish_to_community" in sql_lower:
                self._rows = [{'publish_to_community': 0, 'review_status': None}]
            elif "alter table" in sql_lower:
                pass
            elif "token_version" in sql_lower and "qd_users" in sql_lower:
                self._rows = [{'token_version': 1}]
            else:
                self._rows = []

        def fetchall(self):
            return list(self._rows)

        def fetchone(self):
            return self._rows[0] if self._rows else None

        @property
        def lastrowid(self):
            return self._lastrowid

        def close(self):
            pass

    class FakeDB:
        def cursor(self):
            return FakeCursor()
        def commit(self):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass

    return FakeDB()


# ═══════════════════════════════════════════════════════════════════════
# Flask App Fixture (独立于 conftest.py 的全局 fixture)
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture
def ide_app():
    """创建独立的 Flask 测试应用，注册 indicator 路由"""
    from flask import Flask
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.config['SECRET_KEY'] = 'test-secret-key'

    from app.routes.indicator import indicator_bp
    from app.routes.kline import kline_bp
    from app.routes.backtest import backtest_bp

    app.register_blueprint(indicator_bp, url_prefix='/api/indicator')
    app.register_blueprint(kline_bp, url_prefix='/api/indicator')
    app.register_blueprint(backtest_bp, url_prefix='/api/indicator')

    return app


@pytest.fixture
def ide_client(ide_app):
    return ide_app.test_client()


def _auth_headers():
    return {
        'Authorization': f'Bearer {MOCK_TOKEN}',
        'Content-Type': 'application/json',
    }


# ═══════════════════════════════════════════════════════════════════════
# 1. GET /api/indicator/getIndicators
# ═══════════════════════════════════════════════════════════════════════

class TestGetIndicators:
    @patch('app.routes.indicator.get_db_connection', side_effect=_fake_get_db_connection)
    @patch('app.utils.auth.verify_token', side_effect=_fake_verify_token)
    def test_get_indicators_success(self, mock_token, mock_db, ide_client):
        resp = ide_client.get('/api/indicator/getIndicators', headers=_auth_headers())
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['code'] == 1
        assert isinstance(data['data'], list)
        assert len(data['data']) >= 1
        ind = data['data'][0]
        assert ind['id'] == 101
        assert ind['name'] == 'Test RSI'
        assert 'code' in ind

    def test_get_indicators_no_auth(self, ide_client):
        resp = ide_client.get('/api/indicator/getIndicators')
        assert resp.status_code == 401


# ═══════════════════════════════════════════════════════════════════════
# 2. POST /api/indicator/saveIndicator
# ═══════════════════════════════════════════════════════════════════════

class TestSaveIndicator:
    @patch('app.routes.indicator.get_db_connection', side_effect=_fake_get_db_connection)
    @patch('app.utils.auth.verify_token', side_effect=_fake_verify_token)
    def test_save_indicator_create(self, mock_token, mock_db, ide_client):
        resp = ide_client.post(
            '/api/indicator/saveIndicator',
            headers=_auth_headers(),
            data=json.dumps({'id': 0, 'code': SAMPLE_INDICATOR_CODE, 'name': 'New Indicator'}),
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['code'] == 1
        assert data['data']['id'] == 202

    @patch('app.routes.indicator.get_db_connection', side_effect=_fake_get_db_connection)
    @patch('app.utils.auth.verify_token', side_effect=_fake_verify_token)
    def test_save_indicator_update(self, mock_token, mock_db, ide_client):
        resp = ide_client.post(
            '/api/indicator/saveIndicator',
            headers=_auth_headers(),
            data=json.dumps({'id': 101, 'code': SAMPLE_INDICATOR_CODE, 'name': 'Updated'}),
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['code'] == 1

    @patch('app.utils.auth.verify_token', side_effect=_fake_verify_token)
    def test_save_indicator_empty_code(self, mock_token, ide_client):
        resp = ide_client.post(
            '/api/indicator/saveIndicator',
            headers=_auth_headers(),
            data=json.dumps({'id': 0, 'code': ''}),
        )
        assert resp.status_code == 400


# ═══════════════════════════════════════════════════════════════════════
# 3. POST /api/indicator/deleteIndicator
# ═══════════════════════════════════════════════════════════════════════

class TestDeleteIndicator:
    @patch('app.routes.indicator.get_db_connection', side_effect=_fake_get_db_connection)
    @patch('app.utils.auth.verify_token', side_effect=_fake_verify_token)
    def test_delete_indicator_success(self, mock_token, mock_db, ide_client):
        resp = ide_client.post(
            '/api/indicator/deleteIndicator',
            headers=_auth_headers(),
            data=json.dumps({'id': 101}),
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['code'] == 1

    @patch('app.utils.auth.verify_token', side_effect=_fake_verify_token)
    def test_delete_indicator_missing_id(self, mock_token, ide_client):
        resp = ide_client.post(
            '/api/indicator/deleteIndicator',
            headers=_auth_headers(),
            data=json.dumps({}),
        )
        assert resp.status_code == 400


# ═══════════════════════════════════════════════════════════════════════
# 4. GET /api/indicator/getIndicatorParams
# ═══════════════════════════════════════════════════════════════════════

class TestGetIndicatorParams:
    @patch('app.services.indicator_params.get_db_connection', side_effect=_fake_get_db_connection)
    @patch('app.utils.auth.verify_token', side_effect=_fake_verify_token)
    def test_get_indicator_params_success(self, mock_token, mock_db, ide_client):
        resp = ide_client.get(
            '/api/indicator/getIndicatorParams?indicator_id=101',
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['code'] == 1
        assert isinstance(data['data'], list)
        param_names = [p['name'] for p in data['data']]
        assert 'rsi_len' in param_names

    @patch('app.utils.auth.verify_token', side_effect=_fake_verify_token)
    def test_get_indicator_params_missing_id(self, mock_token, ide_client):
        resp = ide_client.get('/api/indicator/getIndicatorParams', headers=_auth_headers())
        assert resp.status_code == 400


# ═══════════════════════════════════════════════════════════════════════
# 5. POST /api/indicator/verifyCode
# ═══════════════════════════════════════════════════════════════════════

class TestVerifyCode:
    @patch('app.utils.auth.verify_token', side_effect=_fake_verify_token)
    def test_verify_code_success(self, mock_token, ide_client):
        resp = ide_client.post(
            '/api/indicator/verifyCode',
            headers=_auth_headers(),
            data=json.dumps({'code': SAMPLE_INDICATOR_CODE}),
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['code'] == 1
        assert data['data']['plots_count'] >= 1

    @patch('app.utils.auth.verify_token', side_effect=_fake_verify_token)
    def test_verify_code_empty(self, mock_token, ide_client):
        resp = ide_client.post(
            '/api/indicator/verifyCode',
            headers=_auth_headers(),
            data=json.dumps({'code': ''}),
        )
        assert resp.status_code == 400

    @patch('app.utils.auth.verify_token', side_effect=_fake_verify_token)
    def test_verify_code_runtime_error(self, mock_token, ide_client):
        resp = ide_client.post(
            '/api/indicator/verifyCode',
            headers=_auth_headers(),
            data=json.dumps({'code': SIMPLE_INVALID_CODE}),
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['code'] == 0

    @patch('app.utils.auth.verify_token', side_effect=_fake_verify_token)
    def test_verify_code_missing_output(self, mock_token, ide_client):
        resp = ide_client.post(
            '/api/indicator/verifyCode',
            headers=_auth_headers(),
            data=json.dumps({'code': 'df = df.copy()\nx = 1'}),
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['code'] == 0
        assert data['data']['type'] == 'MissingOutput'

    @patch('app.utils.auth.verify_token', side_effect=_fake_verify_token)
    def test_verify_code_with_params(self, mock_token, ide_client):
        resp = ide_client.post(
            '/api/indicator/verifyCode',
            headers=_auth_headers(),
            data=json.dumps({'code': SAMPLE_INDICATOR_CODE, 'params': {'rsi_len': 20}}),
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['code'] == 1


# ═══════════════════════════════════════════════════════════════════════
# 6. POST /api/indicator/aiGenerate (SSE)
# ═══════════════════════════════════════════════════════════════════════

class TestAIGenerate:
    @patch('app.routes.indicator._request_lang', return_value='zh-CN')
    @patch('app.utils.auth.verify_token', side_effect=_fake_verify_token)
    def test_ai_generate_empty_prompt_returns_sse_error(self, mock_token, mock_lang, ide_client):
        resp = ide_client.post(
            '/api/indicator/aiGenerate',
            headers=_auth_headers(),
            data=json.dumps({'prompt': ''}),
        )
        assert resp.status_code == 200
        assert 'text/event-stream' in resp.content_type
        body = resp.data.decode()
        assert '不能为空' in body or 'prompt' in body.lower()

    @patch('app.routes.indicator._request_lang', return_value='en-US')
    @patch('app.utils.auth.verify_token', side_effect=_fake_verify_token)
    def test_ai_generate_empty_prompt_english(self, mock_token, mock_lang, ide_client):
        resp = ide_client.post(
            '/api/indicator/aiGenerate',
            headers=_auth_headers(),
            data=json.dumps({'prompt': ''}),
        )
        assert resp.status_code == 200
        body = resp.data.decode()
        assert '[DONE]' in body

    @patch('app.routes.indicator._request_lang', return_value='en-US')
    @patch('app.utils.auth.verify_token', side_effect=_fake_verify_token)
    def test_ai_generate_with_template_fallback(self, mock_token, mock_lang, ide_client):
        """无 LLM API key 时应 fallback 到模板代码"""
        with patch('app.services.billing_service.get_billing_service') as mock_billing:
            mock_billing_instance = MagicMock()
            mock_billing_instance.check_and_consume.return_value = (True, "ok")
            mock_billing.return_value = mock_billing_instance
            # Patch LLMService so get_api_key returns None → template fallback
            with patch('app.services.llm.LLMService') as mock_llm_cls:
                mock_llm = MagicMock()
                mock_llm.provider = MagicMock()
                mock_llm.provider.value = 'none'
                mock_llm.get_code_generation_model.return_value = 'test'
                mock_llm.get_api_key.return_value = None
                mock_llm.get_base_url.return_value = ''
                mock_llm_cls.return_value = mock_llm
                resp = ide_client.post(
                    '/api/indicator/aiGenerate',
                    headers=_auth_headers(),
                    data=json.dumps({'prompt': 'Create a simple MA indicator'}),
                )
        assert resp.status_code == 200
        assert 'text/event-stream' in resp.content_type
        body = resp.data.decode()
        assert '[DONE]' in body
        assert 'my_indicator_name' in body


# ═══════════════════════════════════════════════════════════════════════
# 7. POST /api/indicator/codeQualityHints
# ═══════════════════════════════════════════════════════════════════════

class TestCodeQualityHints:
    @patch('app.utils.auth.verify_token', side_effect=_fake_verify_token)
    def test_code_quality_hints_valid_code(self, mock_token, ide_client):
        resp = ide_client.post(
            '/api/indicator/codeQualityHints',
            headers=_auth_headers(),
            data=json.dumps({'code': SAMPLE_INDICATOR_CODE}),
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['code'] == 1
        hint_codes = [h['code'] for h in data['data']['hints']]
        assert 'EMPTY_CODE' not in hint_codes

    @patch('app.utils.auth.verify_token', side_effect=_fake_verify_token)
    def test_code_quality_hints_empty_code(self, mock_token, ide_client):
        resp = ide_client.post(
            '/api/indicator/codeQualityHints',
            headers=_auth_headers(),
            data=json.dumps({'code': ''}),
        )
        assert resp.status_code == 200
        data = resp.get_json()
        hint_codes = [h['code'] for h in data['data']['hints']]
        assert 'EMPTY_CODE' in hint_codes

    @patch('app.utils.auth.verify_token', side_effect=_fake_verify_token)
    def test_code_quality_hints_missing_strategy(self, mock_token, ide_client):
        code = '''
my_indicator_name = "X"
my_indicator_description = "Y"
df = df.copy()
df['buy'] = True
df['sell'] = True
output = {'name': 'X', 'plots': [], 'signals': []}
'''
        resp = ide_client.post(
            '/api/indicator/codeQualityHints',
            headers=_auth_headers(),
            data=json.dumps({'code': code}),
        )
        data = resp.get_json()
        hint_codes = [h['code'] for h in data['data']['hints']]
        assert 'NO_STRATEGY_ANNOTATIONS' in hint_codes


# ═══════════════════════════════════════════════════════════════════════
# 8. POST /api/indicator/parseStrategyConfig
# ═══════════════════════════════════════════════════════════════════════

class TestParseStrategyConfig:
    @patch('app.utils.auth.verify_token', side_effect=_fake_verify_token)
    def test_parse_strategy_config_success(self, mock_token, ide_client):
        resp = ide_client.post(
            '/api/indicator/parseStrategyConfig',
            headers=_auth_headers(),
            data=json.dumps({'code': SAMPLE_INDICATOR_CODE}),
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['code'] == 1
        cfg = data['data']['strategyConfig']
        assert cfg.get('stopLossPct') == 0.03
        assert cfg.get('takeProfitPct') == 0.06
        assert cfg.get('tradeDirection') == 'long'
        params = data['data']['indicatorParams']
        param_names = [p['name'] for p in params]
        assert 'rsi_len' in param_names

    @patch('app.utils.auth.verify_token', side_effect=_fake_verify_token)
    def test_parse_strategy_config_empty_code(self, mock_token, ide_client):
        resp = ide_client.post(
            '/api/indicator/parseStrategyConfig',
            headers=_auth_headers(),
            data=json.dumps({'code': ''}),
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['code'] == 1
        assert data['data']['strategyConfig'] == {}
        assert data['data']['indicatorParams'] == []


# ═══════════════════════════════════════════════════════════════════════
# 9. POST /api/indicator/callIndicator
# ═══════════════════════════════════════════════════════════════════════

class TestCallIndicator:
    @patch('app.routes.indicator.get_db_connection', side_effect=_fake_get_db_connection)
    @patch('app.utils.auth.verify_token', side_effect=_fake_verify_token)
    def test_call_indicator_success(self, mock_token, mock_db, ide_client):
        kline_data = _generate_mock_kline(50).to_dict(orient='records')
        resp = ide_client.post(
            '/api/indicator/callIndicator',
            headers=_auth_headers(),
            data=json.dumps({
                'indicatorRef': 101,
                'klineData': kline_data,
                'params': {},
            }),
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['code'] == 1
        assert 'df' in data['data']
        assert 'columns' in data['data']
        assert len(data['data']['df']) == 50

    @patch('app.utils.auth.verify_token', side_effect=_fake_verify_token)
    def test_call_indicator_missing_ref(self, mock_token, ide_client):
        resp = ide_client.post(
            '/api/indicator/callIndicator',
            headers=_auth_headers(),
            data=json.dumps({'klineData': [{'open': 1, 'high': 2, 'low': 0, 'close': 1, 'volume': 100}]}),
        )
        assert resp.status_code == 400

    @patch('app.utils.auth.verify_token', side_effect=_fake_verify_token)
    def test_call_indicator_empty_kline(self, mock_token, ide_client):
        resp = ide_client.post(
            '/api/indicator/callIndicator',
            headers=_auth_headers(),
            data=json.dumps({'indicatorRef': 101, 'klineData': []}),
        )
        assert resp.status_code == 400


# ═══════════════════════════════════════════════════════════════════════
# 10. GET /api/indicator/kline
# ═══════════════════════════════════════════════════════════════════════

class TestKline:
    @patch('app.routes.kline.kline_service')
    def test_get_kline_success(self, mock_service, ide_client):
        mock_service.get_kline.return_value = [
            {'time': 1700000000, 'open': 100, 'high': 110, 'low': 90, 'close': 105, 'volume': 1000},
            {'time': 1700003600, 'open': 105, 'high': 115, 'low': 95, 'close': 110, 'volume': 1200},
        ]
        resp = ide_client.get(
            '/api/indicator/kline?market=Crypto&symbol=BTC/USDT&timeframe=1D&limit=100',
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['code'] == 1
        assert len(data['data']) == 2

    def test_get_kline_missing_symbol(self, ide_client):
        resp = ide_client.get('/api/indicator/kline?market=Crypto&timeframe=1D')
        assert resp.status_code == 400

    @patch('app.routes.kline.kline_service')
    def test_get_kline_no_data(self, mock_service, ide_client):
        mock_service.get_kline.return_value = []
        resp = ide_client.get('/api/indicator/kline?market=Crypto&symbol=FAKE/USDT&timeframe=1D')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['code'] == 0


# ═══════════════════════════════════════════════════════════════════════
# 11. GET /api/indicator/price
# ═══════════════════════════════════════════════════════════════════════

class TestPrice:
    @patch('app.routes.kline.kline_service')
    def test_get_price_success(self, mock_service, ide_client):
        mock_service.get_latest_price.return_value = {
            'symbol': 'BTC/USDT', 'price': 65000.0, 'time': 1700000000,
        }
        resp = ide_client.get('/api/indicator/price?market=Crypto&symbol=BTC/USDT')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['code'] == 1
        assert data['data']['price'] == 65000.0

    def test_get_price_missing_symbol(self, ide_client):
        resp = ide_client.get('/api/indicator/price?market=Crypto')
        assert resp.status_code == 400

    @patch('app.routes.kline.kline_service')
    def test_get_price_no_data(self, mock_service, ide_client):
        mock_service.get_latest_price.return_value = None
        resp = ide_client.get('/api/indicator/price?market=Crypto&symbol=FAKE/USDT')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['code'] == 0


# ═══════════════════════════════════════════════════════════════════════
# 12. POST /api/indicator/backtest
# ═══════════════════════════════════════════════════════════════════════

class TestBacktest:
    @patch('app.routes.backtest.backtest_service')
    @patch('app.routes.backtest.get_db_connection', side_effect=_fake_get_db_connection)
    @patch('app.utils.auth.verify_token', side_effect=_fake_verify_token)
    def test_backtest_success(self, mock_token, mock_db, mock_bt, ide_client):
        mock_bt.run.return_value = {
            'totalReturn': 15.5, 'maxDrawdown': -8.2, 'sharpeRatio': 1.35,
            'winRate': 62.5, 'profitFactor': 1.8, 'totalTrades': 24,
            'equityCurve': [{'time': '2024-01-01', 'value': 10000}],
            'trades': [],
        }
        mock_bt.run_multi_timeframe.return_value = mock_bt.run.return_value
        mock_bt.persist_run.return_value = 42
        resp = ide_client.post(
            '/api/indicator/backtest',
            headers=_auth_headers(),
            data=json.dumps({
                'indicatorCode': SAMPLE_INDICATOR_CODE,
                'symbol': 'BTC/USDT', 'market': 'Crypto', 'timeframe': '1D',
                'startDate': '2024-01-01', 'endDate': '2024-06-30',
                'initialCapital': 10000, 'commission': 0.001,
                'leverage': 1, 'tradeDirection': 'long',
                'persist': False,
            }),
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['code'] == 1

    @patch('app.utils.auth.verify_token', side_effect=_fake_verify_token)
    def test_backtest_missing_params(self, mock_token, ide_client):
        resp = ide_client.post(
            '/api/indicator/backtest',
            headers=_auth_headers(),
            data=json.dumps({'symbol': 'BTC/USDT'}),
        )
        assert resp.status_code == 400


# ═══════════════════════════════════════════════════════════════════════
# 13. GET /api/indicator/backtest/precision-info
# ═══════════════════════════════════════════════════════════════════════

class TestBacktestPrecisionInfo:
    @patch('app.routes.backtest.backtest_service')
    def test_precision_info_success(self, mock_service, ide_client):
        mock_service.get_execution_timeframe.return_value = ('1D', {
            'execTimeframe': '1D', 'estimatedBars': 180, 'rangeDays': 180,
        })
        resp = ide_client.get(
            '/api/indicator/backtest/precision-info?market=Crypto&startDate=2024-01-01&endDate=2024-06-30',
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['code'] == 1

    def test_precision_info_missing_dates(self, ide_client):
        resp = ide_client.get('/api/indicator/backtest/precision-info?market=Crypto')
        assert resp.status_code == 400


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
