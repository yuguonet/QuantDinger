# -*- coding: utf-8 -*-
"""工具层 > 能力层：注册期同名/近重名让位 + 大结果信封（2026-09-25）。

plan 提示不再二次过滤——让位发生在注册期，provider 里本就没有被遮蔽的能力。
"""


def test_near_dup_tool_names():
    from capabilities.loader import near_dup_tool_names as d
    assert d("daily", "daily_live")
    assert d("get_fund_flow_daily", "get_fund_flow")
    assert d("get_hot_sectors", "get_all_hot_sectors")
    assert d("get_realtime_snapshot", "get_realtime_quote")
    assert not d("get_northbound_daily", "get_f10_all")
    assert not d("", "x")


def test_register_skips_near_dup(monkeypatch):
    """注册期：近重名能力让位，不进 provider（工具层优先）。"""
    from capabilities import loader

    class P:
        def __init__(self):
            self.names = set()

        def __contains__(self, n):
            return n in self.names

        def list_by_domain(self, dom):
            return ["agent_get_kline", "get_realtime_quote"] if dom == "finance" else []

        def get_domains(self):
            return ["finance"]

        def register(self, name, fn, domain="common"):
            self.names.add(name)

    # 伪造 admission：一个近重名 + 一个独立
    monkeypatch.setattr(
        loader, "load_admitted",
        lambda *a, **k: [("mod", "daily", 5, 100), ("mod", "get_northbound_daily", 5, 100)],
    )
    monkeypatch.setattr(
        loader, "_wrap_guards", lambda fn, *a, **k: fn,
    )
    # importlib.import_module  stub
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


def test_capability_spill_has_indexable_data(monkeypatch, tmp_path):
    from capabilities import loader

    def fake_fn():
        return [{"c": 1, "t": "a"} for _ in range(200)]

    wrapped = loader._wrap_guards(fake_fn, timeout_s=5, max_chars=200, tool_name="demo")
    monkeypatch.setattr(loader, "_spill", lambda name, text: tmp_path / "x.json")
    out = wrapped()
    assert out.get("truncated") is True
    assert "data" in out and isinstance(out["data"], list)
    assert out["data"][0]["c"] == 1
