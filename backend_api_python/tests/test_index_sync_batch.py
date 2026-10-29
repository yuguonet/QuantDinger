# -*- coding: utf-8 -*-
"""指数同步写作业批量契约测试（零联网、零真库）。

被测对象 = cn_stock_putdb.sync() / .sync_index_fflow()：写侧作业直接调
coord_index 取数后落库；读侧（读库/聚合/闸门）在 cn_stock_ext，不在本文件范围。

核心断言是「一次打包 coordinate(kind, codes=[...])」——码间 pause / 重试池
收口在 dispatch.run_batch，不逐码循环 coordinate（2026-10-27 用户裁定）。
另一组断言是「已在目标日收官 ⇒ 零远端」的幂等跳过（2026-10-27 用户报障：
指数/资金流重启后重复写库）。

★ 2026-10-29 读写分离：曾有短暂版本把同步入口上移到 cn_stock_ext，同日按用户
裁定回滚 —— 写作业留在 putdb（直接调 coord），本文件只改指向、不收窄断言。
"""

import pytest

from app.data_sources import cn_stock_putdb as P

INDICES = list(P.INDICES.keys())  # 9 个指数全码


def _fake_out(kind: str, codes):
    """构造与 coord_index 批量返回同构的假 outcome。"""
    rows = []
    for c in (codes or []):
        rows.append({
            "code": c,
            "rows": [{"time": "2026-10-27 09:30:00", "close": 100.0, "open": 99.0,
                      "high": 101.0, "low": 98.0, "volume": 1000.0}],
            "source": "sina",
            "error": "",
        })
    return {"kind": kind, "ok": True, "source": "batch", "mode": "batch",
            "count": len(rows), "data": {"rows": rows}, "error": ""}


@pytest.fixture
def mock_coord(monkeypatch):
    calls = []

    def fake(kind, **params):
        calls.append((kind, params))
        return _fake_out(kind, params.get("codes") or [])

    # 内核内部 `from app.data_sources.coord_index import coordinate`，
    # 打模块属性即可覆盖；dry_run 不触库，get_pool 返回空桩。
    # 目标日钉死（5m 走 _target_trading_day、资金流走 _fflow_target_day，均不依赖
    # 交易日历/网络），_index_day_complete 默认 False（未收官 ⇒ 正常拉取）。
    monkeypatch.setattr("app.data_sources.coord_index.coordinate", fake)
    monkeypatch.setattr(P, "get_pool", lambda: None)
    monkeypatch.setattr(P, "_target_trading_day", lambda: "2026-10-27")
    monkeypatch.setattr(P, "_fflow_target_day", lambda: "2026-10-27")
    monkeypatch.setattr(P, "_index_day_complete", lambda *a, **k: False)
    return calls


def test_sync_packs_all_indices_into_one_coordinate_call(mock_coord):
    res = P.sync(dry_run=True)
    assert res["written"] == 9
    assert len(mock_coord) == 1                      # 一次打包，非逐码循环
    kind, params = mock_coord[0]
    assert kind == "index_kline"
    assert params.get("codes") == [s.split(".")[0] for s in INDICES]
    assert params.get("timeframe") == "5m"
    assert params.get("limit") == P.SINA_BARS


def test_sync_index_fflow_packs_all_indices(mock_coord):
    res = P.sync_index_fflow(dry_run=True)
    assert res["written"] == 9
    assert len(mock_coord) == 1
    kind, params = mock_coord[0]
    assert kind == "index_fflow"
    assert params.get("codes") == [s.split(".")[0] for s in INDICES]
    assert params.get("limit") == 240


def test_sync_respects_explicit_symbols(mock_coord):
    res = P.sync(symbols=["000001.SH", "399006.SZ"], dry_run=True)
    assert res["written"] == 2
    kind, params = mock_coord[0]
    assert params.get("codes") == ["000001", "399006"]


# ── 已收官跳过（2026-10-27 用户报障：指数/资金流重启后重复写库）─────

def test_sync_skips_completed_day_zero_remote(mock_coord, monkeypatch):
    """全部已收官 ⇒ 跳过，不发起任何远端拉取（重启零成本）。"""
    monkeypatch.setattr(P, "_index_day_complete", lambda *a, **k: True)
    res = P.sync(dry_run=True)
    assert res == {"written": 0, "failed": 0}
    assert mock_coord == []


def test_sync_fetches_only_incomplete_indices(mock_coord, monkeypatch):
    """部分收官 ⇒ 只补缺口票（不重写已收官的）。"""
    monkeypatch.setattr(P, "_index_day_complete",
                        lambda table, symbol, td: symbol.startswith("000001"))
    res = P.sync(dry_run=True)
    codes = mock_coord[0][1]["codes"]
    assert "000001" not in codes and len(codes) == 8
    assert res["written"] == 8


def test_sync_force_bypasses_completion_gate(mock_coord, monkeypatch):
    monkeypatch.setattr(P, "_index_day_complete", lambda *a, **k: True)
    res = P.sync(dry_run=True, force=True)
    assert res["written"] == 9                      # 强制覆写全部
    assert len(mock_coord) == 1


def test_sync_index_fflow_skips_completed_day(mock_coord, monkeypatch):
    monkeypatch.setattr(P, "_index_day_complete", lambda *a, **k: True)
    res = P.sync_index_fflow(dry_run=True)
    assert res == {"written": 0, "failed": 0}
    assert mock_coord == []


def test_index_day_complete_requires_1500_cutoff(monkeypatch):
    """半日数据（写到 14:xx）不算收官 —— 不可回补数据丢尾段即永久缺口。"""
    from datetime import datetime

    class _Cur:
        def __init__(self, last):
            self._last = last

        def execute(self, *a, **k):
            pass

        def fetchone(self):
            return (self._last,)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def __init__(self, last):
            self._c = _Cur(last)

        def cursor(self):
            return self._c

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Pool:
        def __init__(self, last):
            self._last = last

        def connection(self):
            return _Conn(self._last)

    monkeypatch.setattr(P, "get_pool", lambda: _Pool(datetime(2026, 10, 27, 14, 35)))
    assert P._index_day_complete("kline_index_5m", "000001.SH", "2026-10-27") is False
    monkeypatch.setattr(P, "get_pool", lambda: _Pool(datetime(2026, 10, 27, 15, 0)))
    assert P._index_day_complete("kline_index_5m", "000001.SH", "2026-10-27") is True
    monkeypatch.setattr(P, "get_pool", lambda: _Pool(None))
    assert P._index_day_complete("kline_index_5m", "000001.SH", "2026-10-27") is False
