# -*- coding: utf-8 -*-
"""coord_misc 契约测试（交易日历 / 个股资金流族）。

本族 2 个 kind 均已迁移 provider（sina）：trading_calendar（object 型）/ stock_flow_daily（rows 型）。
PENDING_KINDS 为空。
"""
import pytest

from app.data_sources import coord_misc as C
from app.data_sources import coord_pool
from app.data_sources.provider_misc import get_providers
from app.data_sources.dispatch import is_object, is_rows


class Fake:
    def __init__(self, name, priority=10, results=None, raises=None):
        self.name = name
        self.priority = priority
        self._results = list(results or [])
        self._raises = raises

    def fetch(self, kind, **params):
        if self._raises is not None:
            raise self._raises
        if not self._results:
            from app.data_sources.dispatch import NotSupported
            return NotSupported(kind)
        r = self._results.pop(0)
        return r(**params) if callable(r) else r


def _patch(monkeypatch, providers):
    monkeypatch.setattr(C, "get_providers", lambda kind: list(providers))


def test_each_kind_has_provider():
    for kind in C.CAPS:
        assert get_providers(kind), "%s 无可用源" % kind


def test_capability_values_callable():
    for p in get_providers("stock_flow_daily"):
        caps = getattr(p, "capabilities", {}) or {}
        bad = [k for k, v in caps.items() if not callable(v)]
        assert not bad, "provider %s 的 capabilities 存了非可调用值: %s" % (p.name, bad)


def test_validator_matches_envelope():
    assert C.CAPS["trading_calendar"].validator is is_object
    assert C.CAPS["stock_flow_daily"].validator is is_rows


def test_unknown_kind_is_explicit():
    out = C.coordinate("not_a_kind")
    assert out["ok"] is False and "unknown kind" in out["error"]


def test_unexpected_param_rejected():
    out = C.coordinate("stock_flow_daily", code="600519", oops=1)
    assert out["ok"] is False and "unexpected params" in out["error"]


def test_trading_calendar_success_shape(monkeypatch):
    _patch(monkeypatch, [Fake("sina", 50, results=[{"object": {"is_open": True}}])])
    out = C.coordinate("trading_calendar", date="2026-10-26")
    assert out["ok"] and out["count"] == 1
    assert out["data"]["object"]["is_open"] is True


def test_stock_flow_daily_rows_shape(monkeypatch):
    _patch(monkeypatch, [Fake("sina", 50, results=[{"rows": [{"d": "1"}, {"d": "2"}]}])])
    out = C.coordinate("stock_flow_daily", code="600519", days=30)
    assert out["ok"] and out["count"] == 2


def test_coordinate_failure_keeps_reason(monkeypatch):
    _patch(monkeypatch, [Fake("sina", 50, raises=RuntimeError("boom"))])
    out = C.coordinate("stock_flow_daily", code="600519", days=30)
    assert out["ok"] is False and "boom" in out["error"]
    assert out["count"] == 0 and out["data"] is None


def test_public_shape_matches_coord_pool(monkeypatch):
    _patch(monkeypatch, [Fake("sina", 50, results=[{"rows": [{"d": "1"}]}])])
    keys = {"kind", "ok", "source", "mode", "count", "data", "error"}
    a = C.coordinate("stock_flow_daily", code="600519", days=30)
    b = coord_pool.coordinate("not_a_kind")
    assert keys <= set(a) and keys <= set(b)
