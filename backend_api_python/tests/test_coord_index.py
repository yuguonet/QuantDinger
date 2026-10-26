# -*- coding: utf-8 -*-
"""coord_index 契约测试（指数/北向/大盘/板块族）。

与 test_coord_pool/tape 同构。本族有 8 个 kind 处于 PENDING（尚未迁移 provider），
coordinate 必须**显式**报"未迁移"，不能静默当成空数据。
"""
import pytest

from app.data_sources import coord_index as C
from app.data_sources import coord_pool
from app.data_sources.provider_index import get_providers
from app.data_sources.dispatch import is_rows


class Fake:
    def __init__(self, name, priority=10, results=None, raises=None):
        self.name = name
        self.priority = priority
        self._results = list(results or [])
        self._raises = raises
        self.calls = 0

    def fetch(self, kind, **params):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        if not self._results:
            from app.data_sources.dispatch import NotSupported
            return NotSupported(kind)
        r = self._results.pop(0)
        return r(**params) if callable(r) else r


def _patch(monkeypatch, providers):
    monkeypatch.setattr(C, "get_providers", lambda kind: list(providers))


def test_registered_kinds_have_providers():
    """已迁移的 kind（index_quote / northbound_summary）必须有可用源。"""
    for kind in C.CAPS:
        assert get_providers(kind), "%s 已注册却无可用源" % kind


def test_caps_and_pending_disjoint():
    """CAPS 与 PENDING_KINDS 必须互斥：一个 kind 不能既已注册又未迁移。"""
    overlap = set(C.CAPS) & set(C.PENDING_KINDS)
    assert not overlap, "CAPS 与 PENDING_KINDS 重叠: %s" % overlap


def test_capability_values_callable():
    """capabilities 的值必须是可调用对象（锁 2026-10-26 抓到的 bug）。"""
    for p in get_providers("index_quote"):
        caps = getattr(p, "capabilities", {}) or {}
        bad = [k for k, v in caps.items() if not callable(v)]
        assert not bad, "provider %s 的 capabilities 存了非可调用值: %s" % (p.name, bad)


def test_pending_kind_is_explicit():
    """PENDING kind 显式报"未迁移"，不静默、不联网。"""
    for kind in C.PENDING_KINDS:
        out = C.coordinate(kind)
        assert out["ok"] is False and "未迁移" in out["error"], kind


def test_unknown_kind_is_explicit():
    out = C.coordinate("not_a_kind")
    assert out["ok"] is False and "unknown kind" in out["error"]


def test_index_quote_no_param_ok(monkeypatch):
    """index_quote 无必需参数；传多余参数应被拒。"""
    out = C.coordinate("index_quote", oops=1)
    assert out["ok"] is False and "unexpected params" in out["error"]


def test_coordinate_success_shape(monkeypatch):
    _patch(monkeypatch, [Fake("akshare", 50, results=[{"rows": [{"a": 1}, {"a": 2}]}])])
    out = C.coordinate("index_quote")
    assert out["ok"] and out["count"] == 2 and out["source"] == "akshare"
    assert out["data"]["rows"][1]["a"] == 2


def test_coordinate_failure_keeps_reason(monkeypatch):
    _patch(monkeypatch, [Fake("akshare", 50, raises=RuntimeError("boom"))])
    out = C.coordinate("northbound_summary")
    assert out["ok"] is False and "boom" in out["error"]
    assert out["count"] == 0 and out["data"] is None


def test_public_shape_matches_coord_pool(monkeypatch):
    """返回结构与 coord_pool 同构，调用方换接口零学习成本。"""
    _patch(monkeypatch, [Fake("akshare", 50, results=[{"rows": [{"a": 1}]}])])
    keys = {"kind", "ok", "source", "mode", "count", "data", "error"}
    a = C.coordinate("index_quote")
    b = coord_pool.coordinate("not_a_kind")
    assert keys <= set(a) and keys <= set(b)
