# -*- coding: utf-8 -*-
"""启动期同功能筛选 + 缓存契约（2026-09-25）。"""


def _meta(name, doc=""):
    return {"name": name, "doc": doc, "sig": "()"}


def test_code_judge_same_and_diff():
    from capabilities.func_overlap import code_judge_pair
    cap = _meta("daily", "历史日线 K 线")
    tool = _meta("agent_get_kline", "K线数据 OHLCV")
    v, r = code_judge_pair(cap, tool)
    assert v == "same"

    cap2 = _meta("get_northbound_daily", "北向资金每日净买")
    tool2 = _meta("get_f10_all", "F10 全文")
    v2, _ = code_judge_pair(cap2, tool2)
    assert v2 == "different"


def test_build_report_superseded_by_authority():
    from capabilities.func_overlap import build_report
    tools = [_meta("agent_get_kline", "K线")]
    caps = [
        {"module": "m", "name": "x", "doc": "", "superseded_by": "agent_get_kline"},
        {"module": "m", "name": "get_northbound_daily", "doc": "北向", "superseded_by": ""},
    ]
    rep = build_report(tools, caps, use_llm=False)
    assert rep["x"]["decision"] == "shadowed"
    assert rep["x"]["superseded_by"] == "agent_get_kline"
    assert rep["get_northbound_daily"]["decision"] == "keep"


def test_cache_roundtrip(monkeypatch, tmp_path):
    from capabilities import func_overlap as fo
    cache = tmp_path / "overlap_cache.json"
    monkeypatch.setattr(fo, "_CACHE_PATH", cache)
    tools = [_meta("agent_get_kline")]
    caps = [{"module": "m", "name": "daily", "doc": "", "superseded_by": ""}]
    fp = fo.fingerprint_inventory(tools, caps)
    rep = fo.build_report(tools, caps, use_llm=False)
    fo.save_cache(fp, rep, used_llm=False)
    got = fo.load_cache()
    assert got and got["fingerprint"] == fp
    assert got["report"]["daily"]["decision"] == "shadowed"


def test_register_uses_overlap(monkeypatch):
    from capabilities import loader, func_overlap

    class P:
        def __init__(self):
            self.names = set()

        def __contains__(self, n):
            return n in self.names

        def list_by_domain(self, dom):
            return ["agent_get_kline"] if dom == "finance" else []

        def get_domains(self):
            return ["finance"]

        def get(self, n):
            return lambda: None

        def get_tool_names(self):
            return list(self.names) + ["agent_get_kline"]

        def register(self, name, fn, domain="common"):
            self.names.add(name)

    monkeypatch.setattr(
        loader, "load_admitted",
        lambda *a, **k: [("mod", "daily", 5, 100), ("mod", "get_northbound_daily", 5, 100)],
    )
    monkeypatch.setattr(
        loader, "load_admitted_meta",
        lambda *a, **k: [
            {"module": "mod", "name": "daily", "timeout_s": 5, "max_chars": 100,
             "doc": "", "superseded_by": ""},
            {"module": "mod", "name": "get_northbound_daily", "timeout_s": 5,
             "max_chars": 100, "doc": "北向资金", "superseded_by": ""},
        ],
    )
    monkeypatch.setattr(loader, "_wrap_guards", lambda fn, *a, **k: fn)
    monkeypatch.setattr(func_overlap, "load_or_build",
                        lambda *a, **k: {
                            "daily": {"decision": "shadowed", "superseded_by": "agent_get_kline",
                                      "reason": "test"},
                            "get_northbound_daily": {"decision": "keep", "superseded_by": "",
                                                     "reason": "no_overlap"},
                        })
    import types, sys
    fake = types.ModuleType("mod")
    fake.daily = lambda: 1
    fake.get_northbound_daily = lambda: 2
    monkeypatch.setitem(sys.modules, "mod", fake)

    p = P()
    n = loader.register_capabilities(p)
    assert n == 1
    assert "get_northbound_daily" in p.names
    assert "daily" not in p.names
