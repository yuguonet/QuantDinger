# -*- coding: utf-8 -*-
"""coord_index 契约测试（指数/北向/大盘/板块族）。

**零联网约定**：本文件所有 `coordinate()` 调用都 monkeypatch `get_providers`
注入假 provider；provider 只有 `fetch` 才会建连（__init__ 不会），因此
`get_providers()` 仅实例化 provider 的接线断言也是安全的。

验收：
  · PENDING kind 显式报"未迁移"，不静默、不联网；
  · 已注册 kind 的 CAPS 与 PENDING_KINDS 互斥；
  · 参数校验（多参）发生在取数之前；
  · 成功 / 失败信封形状正确，失败原因可追溯（不伪装成空数据）；
  · index_kline 把 timeframe 透传给 get_providers（5m 时 mootdx 被降级）。
"""

import pytest

from app.data_sources import coord_index as C
from app.data_sources import coord_pool
from app.data_sources.provider_index import get_providers
from app.data_sources.dispatch import is_rows


class Fake:
    """可编排行为的假 provider（不联网）。"""

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
    monkeypatch.setattr(C, "get_providers", lambda kind, *a, **k: list(providers))


# ── 注册表一致性（仅实例化 provider，不取数，安全）───────────────

def test_registered_kinds_have_providers():
    """已注册 kind（index_kline / index_fflow / index_quote / northbound_summary）
    必须至少有 1 个真实 provider 声明该 kind。"""
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


# ── 契约（coordinate，全部 mock，不联网）────────────────────────

def test_pending_kind_is_explicit():
    """PENDING kind 显式报"未迁移"，不静默、不联网。"""
    for kind in C.PENDING_KINDS:
        out = C.coordinate(kind)
        assert out["ok"] is False and "未迁移" in out["error"], kind


def test_unknown_kind_is_explicit():
    out = C.coordinate("not_a_kind")
    assert out["ok"] is False and "unknown kind" in out["error"]


def test_index_quote_rejects_unexpected_param():
    """index_quote 无必需参数；传多余参数应被拒（校验先于取数）。"""
    out = C.coordinate("index_quote", oops=1)
    assert out["ok"] is False and "unexpected params" in out["error"]


def test_coordinate_success_shape(monkeypatch):
    _patch(monkeypatch, [Fake("tencent", 10, results=[{"rows": [{"a": 1}, {"a": 2}]}])])
    out = C.coordinate("index_quote")
    assert out["ok"] and out["count"] == 2 and out["source"] == "tencent"
    assert out["data"]["rows"][1]["a"] == 2


def test_coordinate_failure_keeps_reason(monkeypatch):
    _patch(monkeypatch, [Fake("tencent", 10, raises=RuntimeError("boom"))])
    out = C.coordinate("northbound_summary")
    assert out["ok"] is False and "boom" in out["error"]
    assert out["count"] == 0 and out["data"] is None


def test_first_source_down_second_takes_over(monkeypatch):
    """★ 多源回退：首源抛异常 ⇒ 次源顶上（"源坏了随时换"的最直接证明）。"""
    dead = Fake("mootdx", 10, raises=RuntimeError("tdx down"))
    live = Fake("tencent", 20, results=[{"rows": [{"i": 1}]}])
    _patch(monkeypatch, [dead, live])
    out = C.coordinate("index_quote")
    assert out["ok"] and out["source"] == "tencent"
    assert dead.calls == 1 and live.calls == 1


def test_index_kline_passes_timeframe_to_provider_selection(monkeypatch):
    """index_kline 把 timeframe 透传给 get_providers（5m 时 mootdx 被降到 sina 之后）。"""
    seen = {}

    def fake_get(kind, timeframe=None):
        seen["timeframe"] = timeframe
        return [Fake("sina", 15, results=[{"rows": [{"t": 1}]}])]

    monkeypatch.setattr(C, "get_providers", fake_get)
    out = C.coordinate("index_kline", code="sh000001", timeframe="5m", limit=10)
    assert out["ok"] and seen.get("timeframe") == "5m"


# ── 批量分发（codes=[...] → dispatch.run_batch，2026-10-27）────────

class _FnFake:
    """每次调用按 fn(**params) 出信封的假 provider（可跨码多调）。"""

    def __init__(self, name, fn, priority=10):
        self.name, self.priority, self.fn, self.calls = name, priority, fn, 0

    def fetch(self, kind, **params):
        self.calls += 1
        return self.fn(**params)


def test_batch_per_item_source_fallback(monkeypatch):
    """批量逐码独立多源链：码 X 首源死由次源补，码 Y 首源直供（互不牵连）。"""
    monkeypatch.setattr(C, "BATCH_PAUSE", 0)
    monkeypatch.setattr(C, "BATCH_RETRY_PAUSE", 0)

    def flaky(**params):
        if params.get("code") == "000001":
            raise RuntimeError("tcp down")
        return {"rows": [{"code": params.get("code"), "src": "a"}]}

    def good(**params):
        return {"rows": [{"code": params.get("code"), "src": "b"}]}

    a, b = _FnFake("a", flaky), _FnFake("b", good)
    _patch(monkeypatch, [a, b])
    out = C.coordinate("index_kline", codes=["000001", "399006"], timeframe="1D", limit=1)
    assert out["ok"] and out["mode"] == "batch" and out["count"] == 2
    rows = out["data"]["rows"]
    assert [r["code"] for r in rows] == ["000001", "399006"]      # items 原顺序
    assert rows[0]["source"] == "b" and rows[0]["rows"][0]["src"] == "b"
    assert rows[1]["source"] == "a" and rows[1]["rows"][0]["src"] == "a"
    assert a.calls == 2 and b.calls == 1                          # Y 未浪费次源


def test_batch_retry_pool_retries_failed_items(monkeypatch):
    """重试池：首轮失败的 item 隔 retry_pause 整链重试一轮后命中。"""
    monkeypatch.setattr(C, "BATCH_PAUSE", 0)
    monkeypatch.setattr(C, "BATCH_RETRY_PAUSE", 0)
    state = {"n": 0}

    def flaky(**params):
        state["n"] += 1
        if state["n"] == 1:
            return {"rows": [], "error": "RST 突发窗"}
        return {"rows": [{"code": params.get("code")}]}

    p = _FnFake("em", flaky)
    _patch(monkeypatch, [p])
    out = C.coordinate("index_fflow", codes=["000001"], limit=5)
    assert out["ok"] and out["count"] == 1
    assert out["data"]["rows"][0]["rows"]
    assert p.calls == 2                                          # 首轮 + 重试池一轮


def test_batch_all_fail_reports_summary(monkeypatch):
    """全军覆没：ok=False 且 error 列出每个彻底失败 item 的原因。"""
    monkeypatch.setattr(C, "BATCH_PAUSE", 0)
    monkeypatch.setattr(C, "BATCH_RETRY_PAUSE", 0)
    def dead_fn(**k):
        raise RuntimeError("boom")

    dead = _FnFake("a", dead_fn)
    _patch(monkeypatch, [dead])
    out = C.coordinate("index_kline", codes=["000001", "399006"], timeframe="5m", limit=1)
    assert out["ok"] is False and out["count"] == 0
    assert "彻底失败 2/2" in out["error"] and "000001" in out["error"] and "399006" in out["error"]
    assert [r["code"] for r in out["data"]["rows"]] == ["000001", "399006"]


def test_index_quote_codes_uses_batch_path(monkeypatch):
    """index_quote 批量：腾讯声明 batch_kinds ⇒ run_batch 并发分支一次性 codes=[整批]
    （腾讯真·多股单请求），不是逐码 code=（效率取向，对齐"上游尽量多股并发"）。"""
    monkeypatch.setattr(C, "BATCH_PAUSE", 0)
    monkeypatch.setattr(C, "BATCH_RETRY_PAUSE", 0)

    class _BatchTencent:
        name = "tencent"
        priority = 10
        batch_kinds = {"index_quote"}
        capabilities = {"index_quote": lambda **k: {}}   # _do_batch 用此判服务 kind
        def __init__(self):
            self.calls = []
        def fetch(self, kind, **params):
            self.calls.append(dict(params))
            codes = params.get("codes") or []
            # 腾讯返扁平记录 [{code, price}]，无内层信封（真实形态）
            return {"rows": [{"code": c, "price": 1.0} for c in codes]}

    bt = _BatchTencent()
    _patch(monkeypatch, [bt])
    out = C.coordinate("index_quote", codes=["000001", "399006"])
    assert out["ok"] and out["mode"] == "batch" and out["count"] == 2
    rows = out["data"]["rows"]
    assert [r["code"] for r in rows] == ["000001", "399006"]
    # 扁平记录透出：每码 rows 为单条记录列表
    assert rows[0]["rows"] == [{"code": "000001", "price": 1.0}]
    # 并发批量分支：单个 fetch(kind, codes=[整批])，非逐码 code=
    assert len(bt.calls) == 1
    assert bt.calls[0].get("codes") == ["000001", "399006"]


def test_index_quote_codes_batch_fail_falls_back_serial(monkeypatch):
    """腾讯批量对某码返回空（RST 窗）⇒ 重试池由 mootdx 串行（code=）兜底补回，
    且兜底源标记正确（验证 mootdx fetch_index_quote 已兼容 code= 注入）。"""
    monkeypatch.setattr(C, "BATCH_PAUSE", 0)
    monkeypatch.setattr(C, "BATCH_RETRY_PAUSE", 0)

    class _BatchTencent:
        name = "tencent"
        priority = 10
        batch_kinds = {"index_quote"}
        capabilities = {"index_quote": lambda **k: {}}
        def fetch(self, kind, **params):
            return {"rows": [], "error": "RST 突发窗"}   # 腾讯整批失败（真实形态）

    class _SerialMootdx:
        name = "mootdx"
        priority = 20
        def fetch(self, kind, **params):
            assert "code" in params, "串行兜底必须按 code= 注入"
            return {"rows": [{"code": params["code"], "price": 2.0}]}

    _patch(monkeypatch, [_BatchTencent(), _SerialMootdx()])
    out = C.coordinate("index_quote", codes=["000001", "399006"])
    rows = {r["code"]: r for r in out["data"]["rows"]}
    assert rows["399006"]["rows"] and rows["000001"]["rows"]   # 两码均由 mootdx 补回
    assert rows["000001"]["source"] == "mootdx"               # 兜底源标记正确
    assert rows["399006"]["source"] == "mootdx"


def test_index_quote_code_single_routes_to_single_run(monkeypatch):
    """index_quote 单码（code=）走单条 run()，不进批量分支。"""
    _patch(monkeypatch, [Fake("tencent", 10, results=[{"rows": [{"code": "000001"}]}])])
    out = C.coordinate("index_quote", code="000001")
    assert out["ok"] and out["mode"] == "sequential"
    assert out["data"]["rows"][0]["code"] == "000001"


def test_public_shape_matches_coord_pool(monkeypatch):
    """返回结构与 coord_pool 同构，调用方换接口零学习成本（全程 mock，不联网）。"""
    _patch(monkeypatch, [Fake("tencent", 10, results=[{"rows": [{"a": 1}]}])])
    keys = {"kind", "ok", "source", "mode", "count", "data", "error"}
    a = C.coordinate("index_quote")
    b = coord_pool.coordinate("not_a_kind")  # 未知 kind 早返回，无取数
    assert keys <= set(a) and keys <= set(b)
