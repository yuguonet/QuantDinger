# -*- coding: utf-8 -*-
"""coord_basic 契约测试（基本面/基础信息族）。

本族 3 个 kind 均已迁移 provider（akshare）：cn_fundamental / cn_financial（object 型）
/ stock_basic（rows 型）。PENDING_KINDS 为空。
"""
import pytest

from app.data_sources import coord_basic as C
from app.data_sources import coord_pool
from app.data_sources.provider_basic import get_providers
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
    for p in get_providers("stock_basic"):
        caps = getattr(p, "capabilities", {}) or {}
        bad = [k for k, v in caps.items() if not callable(v)]
        assert not bad, "provider %s 的 capabilities 存了非可调用值: %s" % (p.name, bad)


def test_validator_matches_envelope():
    """object 型 kind 必须用 is_object；rows 型必须用 is_rows。"""
    assert C.CAPS["cn_fundamental"].validator is is_object
    assert C.CAPS["cn_financial"].validator is is_object
    assert C.CAPS["stock_basic"].validator is is_rows


def test_unknown_kind_is_explicit():
    out = C.coordinate("not_a_kind")
    assert out["ok"] is False and "unknown kind" in out["error"]


def test_unexpected_param_rejected():
    out = C.coordinate("cn_fundamental", oops=1)
    assert out["ok"] is False and "unexpected params" in out["error"]


def test_cn_fundamental_success_shape(monkeypatch):
    _patch(monkeypatch, [Fake("akshare", 50,
                              results=[{"object": {"code": "600519", "pe": 12.3}}])])
    out = C.coordinate("cn_fundamental", code="600519")
    assert out["ok"] and out["count"] == 1
    assert out["data"]["object"]["code"] == "600519"


def test_stock_basic_rows_shape(monkeypatch):
    _patch(monkeypatch, [Fake("akshare", 50, results=[{"rows": [{"code": "1"}, {"code": "2"}]}])])
    out = C.coordinate("stock_basic")
    assert out["ok"] and out["count"] == 2


def test_coordinate_failure_keeps_reason(monkeypatch):
    _patch(monkeypatch, [Fake("akshare", 50, raises=RuntimeError("boom"))])
    out = C.coordinate("cn_financial", code="600519")
    assert out["ok"] is False and "boom" in out["error"]
    assert out["count"] == 0 and out["data"] is None


def test_public_shape_matches_coord_pool(monkeypatch):
    keys = {"kind", "ok", "source", "mode", "count", "data", "error"}
    a = C.coordinate("cn_fundamental", code="600519")
    b = coord_pool.coordinate("not_a_kind")
    assert keys <= set(a) and keys <= set(b)
