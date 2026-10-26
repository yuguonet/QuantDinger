# -*- coding: utf-8 -*-
"""dispatch + coord_pool 契约测试（新层，不接入系统）。

验收三件事（方案 §10-B）：
  1. **三模式 × 四态**（合法 / 空信封 / 畸形 / NotSupported）行为正确；
  2. **加 kind 只改两个文件**：POOL_CAPS 与 provider.capabilities 双向一致；
  3. 失败不会被伪装成空数据（这是与原 `dragon_limit._ak_*` 的关键差别）。

全部用假 provider，不联网、不依赖 akshare。
"""
import pytest

from app.data_sources import dispatch as D
from app.data_sources import coord_pool as C
from app.data_sources.provider_pool import get_providers


class FakeProvider:
    """可编排行为的假 provider。"""

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
            return D.NotSupported(kind)
        r = self._results.pop(0)
        return r(kind, **params) if callable(r) else r


def _patch(monkeypatch, providers):
    monkeypatch.setattr(C, "get_providers", lambda kind: list(providers))


# ── dispatch：sequential ────────────────────────────────────────

def test_sequential_falls_through_on_exception():
    bad = FakeProvider("bad", raises=RuntimeError("tcp down"))
    good = FakeProvider("good", results=[{"rows": [{"a": 1}]}])
    out = D.run("sequential", [bad, good], "k", {}, D.is_rows, timeout=2.0)
    assert out.ok and out.source == "good" and out.data["rows"][0]["a"] == 1
    assert "bad" in out.error or out.error == ""


def test_sequential_all_fail_is_explicit():
    bad = FakeProvider("a", raises=RuntimeError("x"))
    bad2 = FakeProvider("b", raises=RuntimeError("y"))
    out = D.run("sequential", [bad, bad2], "k", {}, D.is_rows, timeout=2.0)
    assert out.ok is False
    assert "a" in out.error and "b" in out.error      # 失败原因可追


def test_not_supported_is_skipped_not_counted():
    ns = FakeProvider("ns")                            # 空 results ⇒ NotSupported
    good = FakeProvider("good", results=[{"rows": []}])
    out = D.run("sequential", [ns, good], "k", {}, D.is_rows, timeout=2.0)
    assert out.ok and out.source == "good"
    assert out.skipped == 1


def test_empty_envelope_is_success():
    """{"rows": []} = 当天确实没有榜单，不是失败。"""
    p = FakeProvider("p", results=[{"rows": []}])
    out = D.run("sequential", [p], "k", {}, D.is_rows, timeout=2.0)
    assert out.ok and out.data == {"rows": []}


def test_malformed_envelope_is_failure():
    p = FakeProvider("p", results=[{"oops": 1}])
    out = D.run("sequential", [p], "k", {}, D.is_rows, timeout=2.0)
    assert out.ok is False and "信封" in out.error


def test_single_source_short_circuit():
    """单源时只调一次，不进入候选循环。"""
    p = FakeProvider("only", results=[{"rows": [1]}])
    out = D.run("sequential", [p], "k", {}, D.is_rows, timeout=2.0)
    assert out.ok and p.calls == 1 and out.attempts == 1


# ── dispatch：race ─────────────────────────────────────────────

def test_race_returns_first_valid():
    slow = FakeProvider("slow", results=[lambda kind, **kw: (__import__("time").sleep(0.3), {"rows": ["slow"]})[1]])
    fast = FakeProvider("fast", results=[{"rows": ["fast"]}])
    out = D.run("race", [slow, fast], "k", {}, D.is_rows, timeout=2.0)
    assert out.ok and out.source == "fast"


def test_race_single_source_equals_sequential():
    p = FakeProvider("only", results=[{"rows": [1]}])
    out = D.run("race", [p], "k", {}, D.is_rows, timeout=2.0)
    assert out.ok and out.mode == "sequential"        # 单源短路


# ── dispatch：paged ────────────────────────────────────────────

def test_paged_merges_same_source():
    pages = {
        0: {"rows": [{"i": 0}, {"i": 1}, {"i": 2}]},
        1: {"rows": [{"i": 3}, {"i": 4}]},
    }
    p = FakeProvider("p", results=[
        lambda kind, page=0, **kw: pages.get(page, {"rows": []}),
        lambda kind, page=0, **kw: pages.get(page, {"rows": []}),
        lambda kind, page=0, **kw: pages.get(page, {"rows": []}),
    ])
    out = D.run("paged", [p], "k", {}, D.is_rows, timeout=2.0,
                page_size=3, max_pages=3)
    assert out.ok and len(out.data["rows"]) == 5


# ── coord_pool 契约 ────────────────────────────────────────────

def test_caps_match_provider_capabilities():
    """加 kind 只改两个文件：POOL_CAPS 与 provider.capabilities 必须双向一致。"""
    caps = set(C.POOL_CAPS)
    for p in get_providers("limit_up_pool"):
        declared = set(getattr(p, "capabilities", {}) or {})
        assert declared == caps, "provider %s 声明与 POOL_CAPS 不一致: %s" % (
            p.name, declared ^ caps)


def test_capability_values_are_callable():
    """capabilities 的值必须是**可调用对象**。

    2026-10-26 真实冒烟抓到的 bug：存方法名字符串 + `getattr(self, method)`
    会得到 AttributeError（取数函数在模块级、不在类上）。当时 14 个单测全绿，
    因为只比对了 key。此用例锁住“值可调用”。
    """
    for p in get_providers("hot_rank"):
        caps = getattr(p, "capabilities", {}) or {}
        bad = [k for k, v in caps.items() if not callable(v)]
        assert not bad, "provider %s 的 capabilities 存了非可调用值: %s" % (p.name, bad)


def test_unknown_kind_is_explicit():
    out = C.coordinate("not_a_kind")
    assert out["ok"] is False and "unknown kind" in out["error"]


def test_unexpected_param_rejected():
    out = C.coordinate("hot_rank", trade_date="2026-10-26")
    assert out["ok"] is False and "unexpected params" in out["error"]


def test_coordinate_success_shape(monkeypatch):
    _patch(monkeypatch, [FakeProvider("p", results=[{"rows": [{"x": 1}, {"x": 2}]}])])
    out = C.coordinate("limit_up_pool", trade_date="2026-10-26")
    assert out["ok"] and out["count"] == 2 and out["source"] == "p"
    assert out["data"]["rows"][1]["x"] == 2


def test_coordinate_failure_keeps_reason(monkeypatch):
    _patch(monkeypatch, [FakeProvider("p", raises=RuntimeError("boom"))])
    out = C.coordinate("hot_rank")
    assert out["ok"] is False and "boom" in out["error"]
    assert out["count"] == 0 and out["data"] is None   # 不伪造空数据
