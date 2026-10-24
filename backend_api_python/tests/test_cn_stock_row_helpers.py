"""test_cn_stock_row_helpers.py — DB 行转换/取数 helper 行为契约（R1/R2 重构护栏）。

来源：cn_stock 冗余审计处置（R1 行→bar 双实现 / R2 snapshot 取数 preamble 双实现）
产出：2026-10-24 | 状态：实现前（红）

锁定的现状契约（重构必须逐位保持，勿随"顺手优化"改变）：
  _rows_to_bars:
    · naive datetime → 补 Asia/Shanghai 取 epoch（DB time 列是 naive 北京时间，
      按 UTC 解会偏 8 小时——这是 R1 重复里最易错的点，故收成单实现）；
    · tz-aware datetime → 直接 timestamp()；
    · 非 datetime（int/float/str）→ int() 直转；
    · 五字段缺失给 0（DB NOT NULL 下不可达，防御兜底非契约承诺，但现状如此）；
    · 顺序保持、空输入空输出。
  _query_snapshot_rows:
    · psycopg2 默认游标 tuple 按 description 转 dict（该坑两处各修过一次，
      是 R2 提取的直接动因）；
    · 读失败记 warning 返回 []（不抛、不静默）；
    · symbol 剥市场前缀在 helper 内收口。
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.data_sources.cn_stock import _query_snapshot_rows, _rows_to_bars

_BJ = ZoneInfo("Asia/Shanghai")


# ── _rows_to_bars ──────────────────────────────────────────────

class TestRowsToBars:

    def test_naive_datetime_uses_beijing_epoch(self):
        """naive datetime 按北京口径转 epoch（不是 UTC！）。"""
        # 2025-01-02 15:00 北京 = 2025-01-02 07:00 UTC
        rows = [{"time": datetime(2025, 1, 2, 15, 0), "open": 1, "high": 2,
                 "low": 0.5, "close": 1.5, "volume": 100}]
        bars = _rows_to_bars(rows)
        assert bars[0]["time"] == int(datetime(2025, 1, 2, 15, 0, tzinfo=_BJ).timestamp())

    def test_aware_datetime_direct_timestamp(self):
        """tz-aware 直接 timestamp()（不再补 tz）。"""
        aware = datetime(2025, 1, 2, 7, 0, tzinfo=timezone.utc)
        rows = [{"time": aware, "open": 1, "high": 2, "low": 0.5,
                 "close": 1.5, "volume": 100}]
        bars = _rows_to_bars(rows)
        assert bars[0]["time"] == int(aware.timestamp())

    def test_epoch_passthrough(self):
        """int/float epoch 直转（兼容直接给 epoch 的调用方）。"""
        rows = [{"time": 1735801200, "open": 1, "high": 2, "low": 0.5,
                 "close": 1.5, "volume": 100}]
        assert _rows_to_bars(rows)[0]["time"] == 1735801200

    def test_missing_fields_default_zero(self):
        """五字段缺失给 0（现状 row.get(..., 0) 口径）。"""
        rows = [{"time": 1735801200}]
        bar = _rows_to_bars(rows)[0]
        assert bar == {"time": 1735801200, "open": 0.0, "high": 0.0,
                       "low": 0.0, "close": 0.0, "volume": 0.0}

    def test_float_coercion(self):
        """五字段 float 化（DB 可能是 Decimal/str）。"""
        rows = [{"time": 1735801200, "open": "1.5", "high": 2, "low": 0.5,
                 "close": 1.25, "volume": "100"}]
        bar = _rows_to_bars(rows)[0]
        assert (bar["open"], bar["high"], bar["close"], bar["volume"]) == (
            1.5, 2.0, 1.25, 100.0)

    def test_order_and_empty(self):
        """顺序保持；空输入空输出。"""
        assert _rows_to_bars([]) == []
        rows = [{"time": 100 + i, "open": 1, "high": 1, "low": 1,
                 "close": 1, "volume": 1} for i in range(3)]
        assert [b["time"] for b in _rows_to_bars(rows)] == [100, 101, 102]


# ── _query_snapshot_rows ───────────────────────────────────────

class _FakeCursor:
    def __init__(self, rows, cols):
        self._rows, self.cols = rows, cols
        self.description = [(c,) for c in cols]
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        self.executed.append((sql, params))

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return self._cursor


class _FakePool:
    def __init__(self, cursor):
        self._cursor = cursor

    def connection(self):
        return _FakeConn(self._cursor)


class _FakeMgr:
    def __init__(self, pool):
        self._pool = pool
        self.pools_asked = []

    def _get_pool(self, market):
        self.pools_asked.append(market)
        return self._pool


class TestQuerySnapshotRows:

    def _patch(self, monkeypatch, mgr):
        import app.utils.db_market as dbm
        monkeypatch.setattr(dbm, "get_market_db_manager", lambda: mgr)

    def test_tuple_rows_converted_by_description(self, monkeypatch):
        """psycopg2 tuple 行必须按 description 转 dict（R2 提取的核心动因）。"""
        cur = _FakeCursor(
            rows=[(datetime(2025, 1, 2, 9, 30), 10.0, 9.0, 11.0, 8.5, 1000)],
            cols=["time", "last", "open", "high", "low", "volume"],
        )
        self._patch(monkeypatch, _FakeMgr(_FakePool(cur)))
        out = _query_snapshot_rows(
            "600519", "SELECT time, \"last\" FROM realtime_snapshot "
            "WHERE symbol = %s", (), "ctx")
        assert out == [{"time": datetime(2025, 1, 2, 9, 30), "last": 10.0,
                        "open": 9.0, "high": 11.0, "low": 8.5, "volume": 1000}]

    def test_market_prefix_stripped_in_helper(self, monkeypatch):
        """symbol 剥前缀在 helper 内收口（调用点不再各自 strip）。"""
        cur = _FakeCursor(rows=[(datetime(2025, 1, 2, 9, 30), 10.0, 9.0, 11.0,
                                 8.5, 10.0, 1000)],
                          cols=["time", "last", "open", "high", "low", "volume"])
        self._patch(monkeypatch, _FakeMgr(_FakePool(cur)))
        _query_snapshot_rows("SH600519", "SELECT 1 WHERE symbol = %s", (), "ctx")
        sql, params = cur.executed[0]
        assert params == ("600519",), "helper 内剥前缀，params 必须是纯代码"

    def test_cnstock_pool(self, monkeypatch):
        """固定走 CNStock 库连接池。"""
        cur = _FakeCursor(rows=[], cols=["time", "last"])
        mgr = _FakeMgr(_FakePool(cur))
        self._patch(monkeypatch, mgr)
        _query_snapshot_rows("600519", "SELECT 1 WHERE symbol = %s", (), "ctx")
        assert mgr.pools_asked == ["CNStock"]

    def test_empty_result(self, monkeypatch):
        """空结果返 []（调用方按空走降级）。"""
        cur = _FakeCursor(rows=[], cols=["time", "last"])
        self._patch(monkeypatch, _FakeMgr(_FakePool(cur)))
        assert _query_snapshot_rows("600519", "SELECT 1", (), "ctx") == []

    def test_exception_returns_empty_and_warns(self, monkeypatch, caplog):
        """读失败记 warning 返回 []——不抛（调用方降级）也不静默（排查有迹）。"""
        class _BoomMgr(_FakeMgr):
            def _get_pool(self, market):
                raise RuntimeError("connection refused")
        self._patch(monkeypatch, _BoomMgr(_FakePool(_FakeCursor([], ["time"]))))
        with caplog.at_level("WARNING"):
            out = _query_snapshot_rows("600519", "SELECT 1", (), "最近一拍")
        assert out == []
        assert "最近一拍" in caplog.text and "connection refused" in caplog.text

    def test_params_appended_after_symbol(self, monkeypatch):
        """params 追加在 symbol 之后（SQL 占位符顺序 symbol + 其余）。"""
        cur = _FakeCursor(rows=[], cols=["time"])
        self._patch(monkeypatch, _FakeMgr(_FakePool(cur)))
        _query_snapshot_rows(
            "600519",
            "SELECT 1 WHERE symbol = %s AND time >= %s", ("2026-01-01",), "ctx")
        _, params = cur.executed[0]
        assert params == ("600519", "2026-01-01")
