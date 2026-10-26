# -*- coding: utf-8 -*-
"""coord_basic / coord_misc / coord_index 契约测试（假 provider，不联网）。"""
import pytest

from app.data_sources import coord_basic, coord_index, coord_misc
from app.data_sources.dispatch import is_object, is_rows


def test_pending_kinds_reported_explicitly():
    for mod, k in ((coord_index, "index_kline"), (coord_index, "sector_flow")):
        out = mod.coordinate(k)
        assert out["ok"] is False and "未迁移" in out["error"]


def test_pending_kinds_listed_in_all_three():
    assert coord_index.PENDING_KINDS and coord_basic.PENDING_KINDS == ()
    assert coord_misc.PENDING_KINDS == ()


def test_unknown_kind_in_each_family():
    for mod in (coord_basic, coord_misc, coord_index):
        out = mod.coordinate("__nope__")
        assert out["ok"] is False and "unknown kind" in out["error"]


def test_return_shape_isomorphic_across_families():
    keys = {"kind", "ok", "source", "mode", "count", "data", "error"}
    outs = [coord_basic.coordinate("stock_basic"),
            coord_misc.coordinate("trading_calendar", date="2026-10-26"),
            coord_index.coordinate("index_quote")]
    for out in outs:
        assert keys <= set(out), out
    # 隔离测试保证这些 kind 的 provider 是真的；此处不联网断言 ok
    # （provider 可能在 CI 无网络时失败，所以只查结构不查 ok）


def test_caps_validator_shape_matches_envelope():
    for mod in (coord_basic, coord_misc, coord_index):
        for kind, spec in mod.CAPS.items():
            if kind in mod.PENDING_KINDS:
                continue
            assert spec.validator in (is_rows, is_object), (mod.__name__, kind)


def test_coord_misc_calendar_object_envelope(monkeypatch):
    """object 信封：trading_calendar 委托 app.utils（允许的基础接口）。"""

    class P:
        name = "sina"
        priority = 50
        capabilities = {"trading_calendar": True}

        def fetch(self, kind, **params):
            return {"object": {"date": "2026-10-26", "is_trading_day": True}}

    monkeypatch.setattr(coord_misc, "get_providers", lambda kind: [P()])
    out = coord_misc.coordinate("trading_calendar", date="2026-10-26")
    assert out["ok"] and out["data"]["object"]["is_trading_day"] is True


def test_coord_basic_failure_is_explicit(monkeypatch):
    class P:
        name = "akshare"
        priority = 50
        capabilities = {"cn_fundamental": True}

        def fetch(self, kind, **params):
            raise RuntimeError("upstream down")

    monkeypatch.setattr(coord_basic, "get_providers", lambda kind: [P()])
    out = coord_basic.coordinate("cn_fundamental", code="600519")
    assert out["ok"] is False and "boom" in out["error"] or "upstream down" in out["error"]


def test_coord_index_rows_envelope(monkeypatch):
    class P:
        name = "akshare"
        priority = 50
        capabilities = {"index_quote": True}

        def fetch(self, kind, **params):
            return {"rows": [{"code": "sh000001", "price": 1.0}]}

    monkeypatch.setattr(coord_index, "get_providers", lambda kind: [P()])
    out = coord_index.coordinate("index_quote")
    assert out["ok"] and out["count"] == 1
