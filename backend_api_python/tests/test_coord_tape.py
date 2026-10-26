# -*- coding: utf-8 -*-
"""coord_tape 契约测试（分笔/盘口族）。

核心是 **杀首选源仍可用**（v3 §7 验收第 1 条）：mootdx（priority=10）挂掉后，
tencent（priority=20）必须顶上 —— 这是全方案"源坏了随时换"的最直接证明。
"""
import inspect

import pytest

from app.data_sources import coord_pool
from app.data_sources import coord_tape as C
from app.data_sources.dispatch import is_object, is_rows
from app.data_sources.provider_tape import get_providers


class Fake:
    def __init__(self, name, priority, caps, results=None, raises=None):
        self.name = name
        self.priority = priority
        self.capabilities = caps
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


def test_orderbook_has_two_sources():
    """orderbook 是真多源（mootdx + sina + tencent + eastmoney_dc）；ticks_today 也是多源——如实。"""
    assert [p.name for p in get_providers("orderbook")] == ["mootdx", "sina", "tencent", "eastmoney_dc"]
    assert [p.name for p in get_providers("ticks_today")] == ["mootdx", "sina", "tencent", "eastmoney_dc"]


def test_kill_top_source_tencent_takes_over(monkeypatch):
    """★ 杀首选源：mootdx 抛异常 ⇒ tencent 顶上。"""
    dead = Fake("mootdx", 10, {"orderbook": True}, raises=RuntimeError("tdx down"))
    live = Fake("tencent", 20, {"orderbook": True},
                results=[{"object": {"price": 9.9}}])
    _patch(monkeypatch, [dead, live])
    out = C.coordinate("orderbook", code="600519")
    assert out["ok"] is True
    assert out["source"] == "tencent"
    assert dead.calls == 1 and live.calls == 1


def test_kill_both_sources_is_explicit(monkeypatch):
    dead1 = Fake("mootdx", 10, {"orderbook": True}, raises=RuntimeError("x"))
    dead2 = Fake("tencent", 20, {"orderbook": True}, raises=RuntimeError("y"))
    _patch(monkeypatch, [dead1, dead2])
    out = C.coordinate("orderbook", code="600519")
    assert out["ok"] is False
    assert "mootdx" in out["error"] and "tencent" in out["error"]


def test_ticks_today_single_source_short_circuit(monkeypatch):
    p = Fake("mootdx", 10, {"ticks_today": True}, results=[{"rows": [{"p": 1}]}])
    _patch(monkeypatch, [p])
    out = C.coordinate("ticks_today", code="600519")
    assert out["ok"] and out["count"] == 1 and out["mode"] == "sequential"


def test_orderbook_empty_object_is_success(monkeypatch):
    """{"object": None} = 确实无盘口，是成功不是失败。"""
    p = Fake("mootdx", 10, {"orderbook": True}, results=[{"object": None}])
    _patch(monkeypatch, [p])
    out = C.coordinate("orderbook", code="600519")
    assert out["ok"] is True and out["count"] == 0


def test_unknown_kind_and_param_guard():
    assert C.coordinate("not_a_kind")["ok"] is False
    assert "unknown kind" in C.coordinate("not_a_kind")["error"]
    assert "unexpected params" in C.coordinate("orderbook", date="x")["error"]


def test_caps_match_providers():
    """CAPS 的每个 kind 至少有一个 provider；validator 与信封形状匹配。"""
    for kind, spec in C.TAPE_CAPS.items():
        assert get_providers(kind), "%s 无可用源" % kind
        assert spec.validator in (is_rows, is_object)
    # object 型 kind 的 validator 必须是 is_object
    assert C.TAPE_CAPS["orderbook"].validator is is_object
    assert C.TAPE_CAPS["ticks_today"].validator is is_rows


def test_public_shape_matches_coord_pool(monkeypatch):
    """两族返回结构同构：调用方换接口零学习成本。"""
    keys = {"kind", "ok", "source", "mode", "count", "data", "error"}
    _patch(monkeypatch, [Fake("mootdx", 10, {"orderbook": True},
                             results=[{"object": {"price": 1.0}}])])
    a = C.coordinate("orderbook", code="600519")
    b = coord_pool.coordinate("not_a_kind")
    assert keys <= set(a) and keys <= set(b)


def test_public_api_signatures_frozen(monkeypatch):
    """只验签名；不打 patch 会真实连 TDX（mootdx_client 是全局单例）。"""
    monkeypatch.setattr(C, "get_providers", lambda kind: [])
    sig = inspect.signature(C.coordinate)
    params = list(sig.parameters)
    assert params[0] == "kind" and params[1] == "params"
    assert sig.parameters["params"].kind is inspect.Parameter.VAR_KEYWORD


def test_extra_param_lists_allowed_names(monkeypatch):
    """参数校验发生在取数之前；用空源列表避免真实联网（前一轮教训）。"""
    monkeypatch.setattr(C, "get_providers", lambda kind: [])
    out = C.coordinate("ticks_history", code="600519", date="2026-10-23", limit=100)
    # 参数合法 ⇒ 错误是“无可用源”，而不是“参数不对”
    assert "unexpected params" not in (out["error"] or "")
    assert out["error"] == "本族无可用源"

    bad = C.coordinate("ticks_history", code="600519", oops=1)
    assert "unexpected params" in bad["error"]
