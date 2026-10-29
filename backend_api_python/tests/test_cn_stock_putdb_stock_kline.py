# -*- coding: utf-8 -*-
"""test_cn_stock_putdb_stock_kline — 个股 1D/1m 回填落库（coordinate_market_kline → kline_*）。

零联网 / 零真库：mock coordinator 返回 bar、mock 连接池与 execute_values、mock 交易日历。
重点验证:
  - 抓取改走 coordinate_market_kline（market/timeframe/count/symbols 参数正确）
  - time 字符串 → datetime 归一（含 1D 归一到 15:00）
  - 仅保留目标交易日 bar（过滤掉前一日的）
  - 失败 = 无目标日数据的 code（前一日被过滤也算失败）
  - 同步率 < 90% → status=error（生产保护）；全成功 → ok
  - dry_run 不写库
"""

import sys

sys.path.insert(0, ".")

import app.data_sources.cn_stock_putdb as P


class _FakeCursor:
    def __init__(self):
        self.rowcount = 0

    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return (0,)  # _count_existing_kline: 已有 0 条 → 不跳过

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass


class _FakeConn:
    def __init__(self):
        self.cur = _FakeCursor()

    def cursor(self):
        return self.cur

    def commit(self):
        pass

    def rollback(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakePool:
    def connection(self):
        return _FakeConn()


class _FakeMgr:
    def ensure_year_table(self, *a, **k):
        pass


class _FakeCoord:
    def __init__(self, bars):
        self.bars = bars
        self.calls = []

    def coordinate_market_kline(self, **kw):
        self.calls.append(kw)
        return self.bars


def _patch(monkeypatch, coord, written_recorder):
    monkeypatch.setattr(P, "get_pool", lambda: _FakePool())
    monkeypatch.setattr("app.utils.db_market.get_market_db_manager", lambda: _FakeMgr())
    monkeypatch.setattr(P, "execute_values",
                        lambda cur, sql, vals, page_size=5000: written_recorder.append(list(vals)))
    monkeypatch.setattr("app.data_sources.coordinator.get_coordinator", lambda: coord)
    monkeypatch.setattr(P, "_target_trading_day", lambda: "2026-10-26")
    # 每例清防重状态：_last_stock_sync 是模块级（600s 内同 (tf,日) 只跑一次），
    # 用例间共享会让后续用例撞"防重跳过"（2026-10-27 修测试隔离，非生产行为变更）
    monkeypatch.setattr(P, "_last_stock_sync", {})


def test_sync_stock_kline_1d_filters_prev_day_and_normalizes(monkeypatch):
    # 目标交易日 2026-10-26；399001 的 bar 是 10-25（前一日）应被过滤；600519 无数据
    bars = [
        {"symbol": "000001", "time": "2026-10-26", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 100.0},
        {"symbol": "399001", "time": "2026-10-25 15:00:00", "open": 3, "high": 4, "low": 2, "close": 3.5, "volume": 50.0},
    ]
    coord = _FakeCoord(bars)
    rec = []
    _patch(monkeypatch, coord, rec)

    r = P.sync_stock_kline("1D", symbols=["000001", "399001", "600519"])
    assert r["written"] == 1                          # 仅 000001 的 1 根
    assert r["failed"] == 2                           # 前一日过滤 + 无数据 均算失败
    assert r["status"] == "error"                     # 仅 1/3 成功 → 同步率 < 90%
    # 1D 时间应归一到 15:00:00
    assert rec and len(rec) == 1
    written_vals = rec[0]
    assert len(written_vals) == 1
    dt = written_vals[0][1]                            # (sym, time, o, h, l, c, v)
    assert dt.hour == 15 and dt.minute == 0 and dt.second == 0
    # coordinate_market_kline 调用参数正确
    kw = coord.calls[0]
    assert kw["market"] == "CNStock" and kw["timeframe"] == "1D"
    assert kw["count"] == 1 and kw["symbols"] == ["000001", "399001", "600519"]


def test_sync_stock_kline_1m_filters_and_writes(monkeypatch):
    # 2026-10-29 夹具同型化：拉取条数与校验基准解耦（count 240→TF_FETCH_COUNT 300），
    # 且 1m 增加交易时段过滤（09:30 竞价点 / 13:00 / 盘后 15:01~15:30 不入库）。
    # 故补一根 09:30 断言被剔除，其余断言（条数/失败数/状态）语义不变。
    bars = [
        {"symbol": "000001", "time": "2026-10-26 09:30:00", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 9.0},
        {"symbol": "000001", "time": "2026-10-26 09:31:00", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10.0},
        {"symbol": "000001", "time": "2026-10-26 15:00:00", "open": 1, "high": 2, "low": 0.5, "close": 1.6, "volume": 12.0},
        {"symbol": "399001", "time": "2026-10-25 15:00:00", "open": 3, "high": 4, "low": 2, "close": 3.5, "volume": 5.0},
    ]
    coord = _FakeCoord(bars)
    rec = []
    _patch(monkeypatch, coord, rec)

    r = P.sync_stock_kline("1m", symbols=["000001", "399001"])
    assert r["written"] == 2                          # 000001 两根有效（09:30 被时段过滤）；399001 前一日过滤
    assert r["failed"] == 1
    assert r["status"] == "error"                     # 1/2 成功
    kw = coord.calls[0]
    assert kw["timeframe"] == "1m" and kw["count"] == P.TF_FETCH_COUNT["1m"]


def test_sync_stock_kline_all_ok_status_ok(monkeypatch):
    bars = [
        {"symbol": "000001", "time": "2026-10-26 15:00:00", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10.0},
        {"symbol": "600519", "time": "2026-10-26 15:00:00", "open": 9, "high": 9, "low": 9, "close": 9, "volume": 9.0},
    ]
    coord = _FakeCoord(bars)
    rec = []
    _patch(monkeypatch, coord, rec)
    r = P.sync_stock_kline("1m", symbols=["000001", "600519"])
    assert r["status"] == "ok"
    assert r["written"] == 2 and r["failed"] == 0


def test_sync_stock_kline_dry_run_no_write(monkeypatch):
    bars = [
        {"symbol": "000001", "time": "2026-10-26", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 100.0},
    ]
    coord = _FakeCoord(bars)
    rec = []
    _patch(monkeypatch, coord, rec)

    r = P.sync_stock_kline("1D", symbols=["000001"], dry_run=True)
    assert r["status"] == "ok"
    assert r["written"] == 1                          # dry_run 也计数（count of records）
    assert rec == []                                  # 未触发 execute_values（不写库）


def test_run_1d_run_1m_wrappers_shape(monkeypatch):
    bars = [
        {"symbol": "000001", "time": "2026-10-26", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 100.0},
    ]
    coord = _FakeCoord(bars)
    rec = []
    _patch(monkeypatch, coord, rec)

    r1 = P.run_1d(symbols=["000001"])
    assert set(r1.keys()) == {"status", "written", "skipped"}
    assert r1["status"] == "ok" and r1["written"] == 1 and r1["skipped"] is False

    r2 = P.run_1m(symbols=["000001"])
    assert set(r2.keys()) == {"status", "written", "skipped"}


def test_sync_stock_kline_empty_bars_reports_error(monkeypatch):
    coord = _FakeCoord([])
    rec = []
    _patch(monkeypatch, coord, rec)
    r = P.sync_stock_kline("1D", symbols=["000001"])
    assert r["status"] == "error"
    assert r["failed"] == 1                           # 全部失败
    assert rec == []                                  # 未写库


# ── 增量缺口拉取（2026-10-27 用户报障：重启后全量重跑已写 >95%）─────

class _GapConn(_FakeConn):
    """cursor.fetchall 返回'目标日已有 bar 的 symbol'（_missing_symbols 用）。"""

    def __init__(self, have):
        super().__init__()
        self._have = have

    def cursor(self):
        cur = _FakeCursor()
        cur.fetchall = lambda: [(s,) for s in self._have]
        return cur


class _GapPool(_FakePool):
    def __init__(self, have):
        self._have = have

    def connection(self):
        return _GapConn(self._have)


def _patch_gap(monkeypatch, coord, written_recorder, all_codes, have):
    _patch(monkeypatch, coord, written_recorder)
    monkeypatch.setattr(P, "get_pool", lambda: _GapPool(have))
    monkeypatch.setattr(P, "_get_all_stock_codes", lambda: list(all_codes))


def test_sync_stock_kline_incremental_only_fetches_missing(monkeypatch):
    """全市场模式 = 增量缺口：只拉目标日尚无 bar 的票，不重跑已完成部分。"""
    all_codes = ["000001", "600519", "399001", "000002"]
    have = ["000001", "600519"]                       # 已完成
    bars = [
        {"symbol": "399001", "time": "2026-10-26", "open": 1, "high": 2,
         "low": 0.5, "close": 1.5, "volume": 100.0},
        {"symbol": "000002", "time": "2026-10-26", "open": 1, "high": 2,
         "low": 0.5, "close": 1.5, "volume": 100.0},
    ]
    coord = _FakeCoord(bars)
    rec = []
    _patch_gap(monkeypatch, coord, rec, all_codes, have)

    r = P.sync_stock_kline("1D")
    assert coord.calls[0]["symbols"] == ["399001", "000002"]   # 只拉缺口
    assert r["failed"] == 0 and r["written"] == 2
    assert r["status"] == "ok"


def test_sync_stock_kline_complete_day_skips_fetch(monkeypatch):
    """目标日已齐 ⇒ 不发起任何远端拉取（重启重跑零成本）。"""
    all_codes = ["000001", "600519"]
    coord = _FakeCoord([])
    rec = []
    _patch_gap(monkeypatch, coord, rec, all_codes, have=all_codes)

    r = P.sync_stock_kline("1D")
    assert r["written"] == 0 and "跳过" in r["report"]
    assert coord.calls == []                          # 未触远端
