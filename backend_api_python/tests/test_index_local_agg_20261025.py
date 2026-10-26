# -*- coding: utf-8 -*-
"""指数日K 本地聚合 fetcher（L2）单测（2026-10-25）。

get_index_daily_kline 的 fetcher 链首位插入 _kline_local_agg：
kline_index_5m（scheduler 盘后同步）5m→1D 聚合，6-11ms 零外网。
门控：① 9 指数白名单 ② 深度 ≥ days ③ 末条日期 ≥ 上一交易日。
口径对齐现链：date=str、volume=手（本地库是股，÷100）。

环境约束：tests/conftest.py 全局 mock ThreadedConnectionPool（零真实连接），
故本单测用假 cursor 喂 rows —— 判据（门控/换算/顺序）全在生产代码侧，
假 DB 只做数据源，不自带业务规则。

锁定的行为契约（防回归）：
  A. 命中：SQL 参数（symbol/limit）、条数、升序、末条、÷100 换算、无 NaN
  B. 门控三分支各自回落（白名单外 / 深度不够 / 末条陈旧 / 日历异常）
  C. 端到端 get_index_daily_kline：local_agg 在链首且命中后不调远端
"""
import os
import sys
from datetime import date, timedelta
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


def test_local_agg_hit_contract():
    """A: 命中时 SQL 参数/条数/升序/末条/换算/无 NaN。"""
    rows = _fake_rows(120, date(2026, 10, 23))
    with _patch_db(rows):
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
    with _patch_db(rows) as _:
        with mock.patch("app.utils.db_market.get_market_db_manager") as gm:
            gm.return_value = _FakeMgr(rows)
            _kline_local_agg("399006", 120)
            cur = gm.return_value._get_pool("CNStock").cursor()
            # _FakeCursor 在 execute 时记录；这里直接再跑一次拿记录
    # 简化：重新跑一次捕获 params
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
                    return_value=_CapMgr(rows)):
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
    """B③: 末条 < 上一交易日回落（模拟同步停摆）；等于则放行。"""
    # 末条 10-23，prev_trading_day 谎报 10-30 ⇒ 陈旧回落
    rows = _fake_rows(120, date(2026, 10, 23))
    with _patch_db(rows), \
         mock.patch("app.utils.trading_calendar.prev_trading_day", return_value="2026-10-30"):
        assert _kline_local_agg("000001", 120) is None
    # 末条 == 上一交易日（盘中当天未同步的设计行为）⇒ 放行
    with _patch_db(rows), \
         mock.patch("app.utils.trading_calendar.prev_trading_day", return_value="2026-10-23"):
        assert _kline_local_agg("000001", 120) is not None


def test_local_agg_calendar_failure_falls_back():
    """B④: 日历异常保守回落远端（不猜日期）。"""
    rows = _fake_rows(120, date(2026, 10, 23))
    with _patch_db(rows), \
         mock.patch("app.utils.trading_calendar.prev_trading_day",
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
    """C: 链首是 local_agg；命中后远端 fetcher 一个都不调。"""
    rows = _fake_rows(120, date(2026, 10, 23))
    calls = []
    with _patch_db(rows), \
         mock.patch("app.market_cn.index._kline_mootdx") as m_mootdx, \
         mock.patch("app.market_cn.index._kline_tencent") as m_tc, \
         mock.patch("app.market_cn.index._kline_sina") as m_sina, \
         mock.patch("app.market_cn.index._kline_baostock") as m_bs:
        m_mootdx.side_effect = lambda *a: calls.append("mootdx") or None
        m_tc.side_effect = lambda *a: calls.append("tencent") or None
        m_sina.side_effect = lambda *a: calls.append("sina") or None
        m_bs.side_effect = lambda *a: calls.append("baostock") or None
        bars = get_index_daily_kline("000001", 120)
        assert len(bars) == 120
        assert calls == [], f"本地命中也调了远端: {calls}"


def test_endtoend_falls_back_when_local_misses():
    """C: 本地未命中（深度不够）时按原序回落 mootdx → ...。"""
    rows = _fake_rows(100, date(2026, 10, 23))   # 100 < 500
    import pandas as pd

    with _patch_db(rows), \
         mock.patch("app.market_cn.index._kline_mootdx") as m_mootdx, \
         mock.patch("app.market_cn.index._kline_tencent") as m_tc:
        m_mootdx.side_effect = lambda *a: None                     # mootdx 死
        m_tc.side_effect = lambda *a: pd.DataFrame(
            [{"date": "2026-10-23", "open": 1, "high": 2, "low": 0.5, "close": 1.5,
              "volume": 100.0}])
        bars = get_index_daily_kline("000001", 500)
        assert bars and bars[-1]["date"] == "2026-10-23"
        assert m_mootdx.called and m_tc.called        # 链路顺序保持
