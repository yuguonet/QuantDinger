# -*- coding: utf-8 -*-
"""跨族一致性（coord_basic / coord_misc / coord_index）。

**零联网约定**：所有 `coordinate()` 调用均 monkeypatch 对应族的 `get_providers`
注入假 provider；provider 只有 `fetch` 才建连，本文件从不触发真实取数。

验证三族对外契约同源：
  · PENDING kind 显式报"未迁移"；
  · 未知 kind 显式报 unknown kind；
  · 成功信封结构同构（kind/ok/source/mode/count/data/error）；
  · validator 与信封形状匹配（object / rows）；
  · 单族失败原因可追溯。
"""

import pytest

from app.data_sources import coord_basic, coord_index, coord_misc
from app.data_sources.dispatch import is_object, is_rows


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


def _patch(mod, monkeypatch, providers):
    monkeypatch.setattr(mod, "get_providers", lambda kind, *a, **k: list(providers))


def test_pending_kinds_reported_explicitly(monkeypatch):
    for mod in (coord_index, coord_basic, coord_misc):
        for k in mod.PENDING_KINDS:
            _patch(mod, monkeypatch, [])
            out = mod.coordinate(k)
            assert out["ok"] is False and "未迁移" in out["error"], (mod.__name__, k)


def test_pending_kinds_listed():
    # index 族有 7 个 PENDING；basic / misc 已全部迁移
    assert coord_index.PENDING_KINDS and coord_basic.PENDING_KINDS == ()
    assert coord_misc.PENDING_KINDS == ()


def test_unknown_kind_in_each_family(monkeypatch):
    for mod in (coord_basic, coord_misc, coord_index):
        _patch(mod, monkeypatch, [])
        out = mod.coordinate("__nope__")
        assert out["ok"] is False and "unknown kind" in out["error"]


def test_return_shape_isomorphic_across_families(monkeypatch):
    keys = {"kind", "ok", "source", "mode", "count", "data", "error"}
    _patch(coord_basic, monkeypatch, [Fake("akshare", 50, results=[{"rows": [{"x": 1}]}])])
    _patch(coord_misc, monkeypatch, [Fake("sina", 50, results=[{"object": {"d": 1}}])])
    _patch(coord_index, monkeypatch, [Fake("tencent", 50, results=[{"rows": [{"i": 1}]}])])
    outs = [coord_basic.coordinate("stock_basic"),
            coord_misc.coordinate("trading_calendar", date="2026-10-26"),
            coord_index.coordinate("index_quote")]
    for out in outs:
        assert keys <= set(out), out
        assert out["ok"] is True  # 假源成功：验证信封而非真实取数


def test_caps_validator_shape_matches_envelope():
    for mod in (coord_basic, coord_misc, coord_index):
        for kind, spec in mod.CAPS.items():
            if kind in mod.PENDING_KINDS:
                continue
            assert spec.validator in (is_rows, is_object), (mod.__name__, kind)


def test_coord_misc_calendar_object_envelope(monkeypatch):
    _patch(coord_misc, monkeypatch,
           [Fake("sina", 50, results=[{"object": {"date": "2026-10-26", "is_trading_day": True}}])])
    out = coord_misc.coordinate("trading_calendar", date="2026-10-26")
    assert out["ok"] and out["data"]["object"]["is_trading_day"] is True


def test_coord_basic_failure_is_explicit(monkeypatch):
    _patch(coord_basic, monkeypatch,
           [Fake("akshare", 50, raises=RuntimeError("upstream down"))])
    out = coord_basic.coordinate("cn_fundamental", code="600519")
    assert out["ok"] is False and "upstream down" in out["error"]


def test_coord_index_rows_envelope(monkeypatch):
    _patch(coord_index, monkeypatch,
           [Fake("akshare", 50, results=[{"rows": [{"code": "sh000001", "price": 1.0}]}])])
    out = coord_index.coordinate("index_quote")
    assert out["ok"] and out["count"] == 1
