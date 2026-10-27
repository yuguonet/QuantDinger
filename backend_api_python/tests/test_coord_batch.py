# -*- coding: utf-8 -*-
"""批量分发（run_batch 串行/并发混合 + fanout_codes）契约测试（零联网）。

验证：
  · fanout_codes 一次多股并发取数、合并信封、单码失败不牵连其它码；
  · run_batch 并发候选（声明 batch_kinds）整批一次、串行候选（RST 风险）逐码回退；
  · 并发失败 ⇒ 串行候选逐码补；
  · object 类空信封（object=None）算合法"暂无"，非取数失败；
  · coord_basic / coord_tape 的 codes=[...] 入口路由到 run_batch。
"""

import pytest

from app.data_sources import coord_tape as T
from app.data_sources import coord_basic as B
from app.data_sources.dispatch import NotSupported, fanout_codes


# ── fanout_codes ──────────────────────────────────────────────

def test_fanout_codes_collects_all_and_calls_once_each():
    calls = []

    def fn(code, **kw):
        calls.append(code)
        return {"rows": [{"c": code}]}

    out = fanout_codes(fn, ["000001", "000002", "000003"], {}, "fake", timeout=2.0)
    entries = out["rows"]
    assert [e["code"] for e in entries] == ["000001", "000002", "000003"]
    assert all(e["rows"] == {"rows": [{"c": e["code"]}]} for e in entries)
    assert sorted(calls) == ["000001", "000002", "000003"]


def test_fanout_codes_isolates_single_code_failure():
    def fn(code, **kw):
        if code == "000002":
            raise RuntimeError("boom")
        return {"rows": [{"c": code}]}

    out = fanout_codes(fn, ["000001", "000002", "000003"], {}, "fake", timeout=2.0)
    entries = {e["code"]: e for e in out["rows"]}
    assert entries["000001"]["rows"] == {"rows": [{"c": "000001"}]}
    assert entries["000002"]["rows"] is None and "boom" in entries["000002"]["error"]
    assert entries["000003"]["rows"] == {"rows": [{"c": "000003"}]}


def test_fanout_codes_not_supported_is_not_error():
    """NotSupported ⇒ 跳过：不记 error（不进失败表，与单请求 skipped 语义一致）。"""

    def fn(code, **kw):
        return NotSupported("k")

    out = fanout_codes(fn, ["000001"], {}, "fake", timeout=2.0)
    e = out["rows"][0]
    assert e["rows"] is None and e["error"] == ""


# ── 测试用可侦察 provider（不联网）────────────────────────────

class SpyProvider:
    def __init__(self, name, priority, caps, batch_kinds=None, recorder=None):
        self.name = name
        self.priority = priority
        self.capabilities = caps
        self.batch_kinds = batch_kinds or set()
        self.recorder = recorder

    def fetch(self, kind, **params):
        if self.recorder is not None:
            self.recorder.append((self.name, kind, dict(params)))
        fn = self.capabilities.get(kind)
        if fn is None:
            return NotSupported(kind)
        codes = params.get("codes")
        if codes is not None and kind in self.batch_kinds:
            rest = {k: v for k, v in params.items() if k not in ("code", "codes")}
            return fanout_codes(fn, list(codes), rest, name=self.name, timeout=2.0)
        return fn(**params)


def test_tape_batch_concurrent_then_no_serial_when_batch_ok(monkeypatch):
    rec = []
    batch = SpyProvider("tencent", 10,
                        {"ticks_today": lambda code, **kw: {"rows": [{"c": code}]}},
                        batch_kinds={"ticks_today"}, recorder=rec)
    serial = SpyProvider("sina", 20,
                         {"ticks_today": lambda code, **kw: {"rows": [{"c": code}]}},
                         recorder=rec)
    monkeypatch.setattr(T, "get_providers", lambda kind, *a, **k: [serial, batch])
    out = T.coordinate("ticks_today", codes=["000001", "000002"])
    assert out["ok"]
    batch_calls = [c for c in rec if c[0] == "tencent"]
    assert len(batch_calls) == 1 and batch_calls[0][2].get("codes") == ["000001", "000002"]
    assert not [c for c in rec if c[0] == "sina"]          # 并发成功 ⇒ 串行未触发
    assert [r["code"] for r in out["data"]["rows"]] == ["000001", "000002"]


def test_tape_batch_failure_falls_back_to_serial(monkeypatch):
    rec = []
    batch = SpyProvider("tencent", 10,
                        {"ticks_today": lambda code, **kw: {"rows": [], "error": "RST"}},
                        batch_kinds={"ticks_today"}, recorder=rec)
    serial = SpyProvider("sina", 20,
                         {"ticks_today": lambda code, **kw: {"rows": [{"c": code}]}},
                         recorder=rec)
    monkeypatch.setattr(T, "get_providers", lambda kind, *a, **k: [serial, batch])
    out = T.coordinate("ticks_today", codes=["000001", "000002"])
    assert out["ok"]
    assert len([c for c in rec if c[0] == "tencent"]) == 1        # 并发整批一次
    serial_calls = [c for c in rec if c[0] == "sina"]
    assert len(serial_calls) == 2 and all("code" in c[2] for c in serial_calls)


def test_basic_codes_is_serial_only(monkeypatch):
    rec = []
    serial = SpyProvider("sina", 20,
                         {"cn_fundamental": lambda code, **kw: {"object": {"c": code}}},
                         recorder=rec)
    monkeypatch.setattr(B, "get_providers", lambda kind, *a, **k: [serial])
    out = B.coordinate("cn_fundamental", codes=["000001", "000002"])
    assert out["ok"]
    assert len(rec) == 2 and all("code" in c[2] for c in rec)     # 无并发源 ⇒ 逐码串行


def test_tape_orderbook_object_empty_is_legit_success(monkeypatch):
    serial = SpyProvider("mootdx", 10,
                         {"orderbook": lambda code, **kw: {"object": None}}, recorder=[])
    monkeypatch.setattr(T, "get_providers", lambda kind, *a, **k: [serial])
    out = T.coordinate("orderbook", codes=["000001"])
    assert out["ok"] and out["data"]["rows"][0]["rows"] == {"object": None}


def test_batch_not_supported_not_in_failure_table(monkeypatch):
    """NotSupported ⇒ 非失败：不进彻底失败表、error 摘要为空（Outcome.skipped 计数）。

    失败表只收真失败（带 error）；「没人支持」是跳过，不是失败（2026-10-27 用户裁定）。
    """
    ns = SpyProvider("ns", 10, {}, recorder=[])          # 无该 kind ⇒ fetch 返 NotSupported
    monkeypatch.setattr(T, "get_providers", lambda kind, *a, **k: [ns])
    out = T.coordinate("ticks_today", codes=["000001", "000002"])
    assert out["ok"] is False                            # 无任何数据
    assert out["error"] == ""                            # 不进失败表摘要
    for e in out["data"]["rows"]:
        assert e["error"] == "" and e["rows"] is None
        assert "未提供" not in str(e) and "失败" not in str(e)

    # 内层 Outcome.skipped 计数（公共信封不带该键，直调 run_batch 验）
    from app.data_sources import dispatch as D
    batch_out = D.run_batch("sequential", [ns], "ticks_today",
                            ["000001", "000002"], {}, "code", D.is_rows)
    assert batch_out.skipped == 2 and batch_out.error == ""
