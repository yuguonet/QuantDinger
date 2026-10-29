# -*- coding: utf-8 -*-
"""指数日K 本地聚合 fetcher（L2）单测（2026-10-25；2026-10-27 收口 cn_stock_ext）。

get_index_daily_kline 的 fetcher 链首位 = _kline_local_agg →
cn_stock_ext.aggregate_daily_df：kline_index_5m（scheduler 盘后同步）5m→1D
聚合，零外网。门控：① 9 指数白名单 ② 深度 ≥ days ③ 新鲜度 ≥
trading_calendar 锚点（last_finish_trading_day）。口径对齐现链：
date=str、volume=手（本地库是股，÷100）。

环境约束：tests/conftest.py 全局 mock ThreadedConnectionPool（零真实连接），
故本单测用假 cursor 喂 rows —— 判据（门控/换算/顺序）全在生产代码侧，
假 DB 只做数据源，不自带业务规则。

锁定的行为契约（防回归）：
  A. 命中：SQL 参数（symbol/limit）、条数、升序、末条、÷100 换算、无 NaN
  B. 门控三分支各自回落（白名单外 / 深度不够 / 末条陈旧 / 日历异常）
  C. 端到端 get_index_daily_kline：local_agg 在链首且命中后不调远端；
     本地未命中 → coord_index 兜底（2026-10-27 起远端链收口 coord_index）
"""
import contextlib
import os
import sys
from datetime import date, datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.market_cn.index import _kline_local_agg, get_index_daily_kline


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.sql = None
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql, self.params = sql, params

    def fetchall(self):
        return self._rows


class _FakePool:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return _FakeCursor(self.rows)


class _FakeMgr:
    def __init__(self, rows):
        self.rows = rows

    def _get_pool(self, name):
        assert name == "CNStock"
        return _FakePool(self.rows)


def _fake_rows(n: int, last: date):
    """n 条假 5m 聚合 rows（DESC 序：最新在前），字段同 SQL 输出。"""
    rows = []
    d = last
    for _ in range(n):
        rows.append((d, 10.0, 11.0, 9.5, 10.5, 12345678))   # volume=股
        d -= timedelta(days=1)
    return rows


def _patch_db(rows):
    return mock.patch("app.utils.db_market.get_market_db_manager",
                      return_value=_FakeMgr(rows))


def _patch_anchor(day: str, trading_today: bool = False):
    """新鲜度锚点（trading_calendar）：fixture 数据停在 2026-10-23，锚点须可注。

    2026-10-29 锚点新增「盘中=今日」分支（fresh_anchor 取「源此刻能给到的最新交易日」）
    后，放行类断言统一注入 `trading_today=False` ⇒ 锚点退回 last_finish_trading_day，
    与旧断言同义（确定性，不依赖跑测试的真实日期）；盘中分支由
    test_local_agg_freshness_gate 单独覆盖。
    """
    @contextlib.contextmanager
    def _cm():
        with mock.patch("app.utils.trading_calendar.last_finish_trading_day",
                        return_value=day), \
             mock.patch("app.utils.trading_calendar.is_trading_day_today",
                        return_value=trading_today), \
             mock.patch("app.data_sources.cn_stock_ext._now",
                        return_value=datetime(2026, 10, 29, 12, 0)):
            yield
    return _cm()


def test_local_agg_hit_contract():
    """A: 命中时 SQL 参数/条数/升序/末条/换算/无 NaN。"""
    rows = _fake_rows(120, date(2026, 10, 23))
    with _patch_db(rows), _patch_anchor("2026-10-23"):
        df = _kline_local_agg("000001", 120)
    assert df is not None and len(df) == 120
    # SQL 口径：symbol 用 .SH 尾缀记法、LIMIT 传 days
    assert df is not None
    dates = list(df["date"])
    assert all(isinstance(d, str) for d in dates)
    assert dates == sorted(dates) and dates[-1] == "2026-10-23"
    assert not df.isnull().any().any()
    # ÷100 换算：12345678 股 → 123456.78 手
    assert abs(float(df["volume"].iloc[-1]) - 123456.78) < 1e-6


def test_local_agg_sql_params():
    """A: SQL 参数锁定（symbol 尾缀 + limit=days），防 SQL 被改坏。"""
    rows = _fake_rows(120, date(2026, 10, 23))
    captured = {}

    class _CapCursor(_FakeCursor):
        def execute(self, sql, params=None):
            captured["sql"] = sql
            captured["params"] = params
            super().execute(sql, params)

    class _CapMgr(_FakeMgr):
        def _get_pool(self, name):
            class _P:
                def cursor(self):
                    return _CapCursor(rows)
            return _P()

    with mock.patch("app.utils.db_market.get_market_db_manager",
                    return_value=_CapMgr(rows)), _patch_anchor("2026-10-23"):
        _kline_local_agg("399006", 120)
    assert captured["params"] == ("399006.SZ", 120)
    assert "kline_index_5m" in captured["sql"] and "time::date" in captured["sql"]


def test_local_agg_whitelist_gate():
    """B①: 白名单外（899050/399303 未落库、个股码）回落，不查 DB。"""
    with mock.patch("app.utils.db_market.get_market_db_manager") as gm:
        for code in ("899050", "399303", "600519"):
            assert _kline_local_agg(code, 120) is None
        gm.assert_not_called()


def test_local_agg_depth_gate():
    """B②: 聚合行数 < days 回落。"""
    rows = _fake_rows(100, date(2026, 10, 23))
    with _patch_db(rows):
        assert _kline_local_agg("000001", 120) is None


def test_local_agg_freshness_gate():
    """B③: 末条 < 锚点回落；等于放行；盘中锚点=今日（2026-10-29 读路径闭环）。"""
    rows = _fake_rows(120, date(2026, 10, 23))
    # 末条 10-23，锚点 10-30 ⇒ 陈旧回落（非交易日/盘前分支）
    with _patch_db(rows), _patch_anchor("2026-10-30"):
        assert _kline_local_agg("000001", 120) is None
    # 末条 == 锚点 ⇒ 放行（非交易日：源只有上一完结交易日的数据）
    with _patch_db(rows), _patch_anchor("2026-10-23"):
        assert _kline_local_agg("000001", 120) is not None
    # 交易日盘中（12:00）⇒ 锚点=今日，末条 10-23 判陈旧回落 —— 当日数据源已有，
    # 库内只到昨天必须落远端，否则盘中永远读到昨天收盘（原「盘中锚点=昨天」已废弃）
    with _patch_db(rows), _patch_anchor("2026-10-23", trading_today=True):
        assert _kline_local_agg("000001", 120) is None


def test_local_agg_calendar_failure_falls_back():
    """B④: 日历异常保守回落远端（不猜日期）。

    2026-10-29：锚点新增「盘中=今日」分支后，须同时注入非交易日+固定时刻，
    否则走不到 last_finish_trading_day → 断言会假绿。
    """
    rows = _fake_rows(120, date(2026, 10, 23))
    with _patch_db(rows), \
         mock.patch("app.utils.trading_calendar.is_trading_day_today",
                    return_value=False), \
         mock.patch("app.data_sources.cn_stock_ext._now",
                    return_value=datetime(2026, 10, 29, 12, 0)), \
         mock.patch("app.utils.trading_calendar.last_finish_trading_day",
                    side_effect=RuntimeError("calendar boom")):
        assert _kline_local_agg("000001", 120) is None


def test_local_agg_db_error_falls_back():
    """B⑤: DB 异常不炸，回落远端（fail-soft 到下一级 fetcher）。"""
    class _BoomMgr:
        def _get_pool(self, name):
            raise RuntimeError("pool boom")

    with mock.patch("app.utils.db_market.get_market_db_manager",
                    return_value=_BoomMgr()):
        assert _kline_local_agg("000001", 120) is None


def test_endtoend_local_first_and_short_circuit():
    """C: 链首是 local_agg；命中后远端一个都不调。"""
    rows = _fake_rows(120, date(2026, 10, 23))
    calls = []
    with _patch_db(rows), _patch_anchor("2026-10-23"), \
         mock.patch("app.data_sources.coord_index.coordinate") as m_coord:
        m_coord.side_effect = lambda *a, **k: calls.append("coord") or {
            "ok": False, "data": None, "error": "should not be called"}
        bars = get_index_daily_kline("000001", 120)
        assert len(bars) == 120
        assert calls == [], f"本地命中也调了远端: {calls}"


def test_endtoend_falls_back_when_local_misses():
    """C: 本地未命中（深度不够）→ coord_index 兜底，链路与返回口径保持。"""
    rows = _fake_rows(100, date(2026, 10, 23))   # 100 < 500

    def _fake_coordinate(kind, **params):
        assert kind == "index_kline"
        return {"ok": True, "source": "fake", "count": 1,
                "data": {"rows": [{"date": "2026-10-23", "open": 1, "high": 2,
                                   "low": 0.5, "close": 1.5, "volume": 100.0}]}}

    with _patch_db(rows), \
         mock.patch("app.data_sources.coord_index.coordinate",
                    side_effect=_fake_coordinate) as m_coord:
        bars = get_index_daily_kline("000001", 500)
        assert bars and bars[-1]["date"] == "2026-10-23"
        assert m_coord.called
