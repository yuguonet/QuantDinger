# -*- coding: utf-8 -*-
"""tape.py 源表/分发器测试（2026-10-26；2026-10-27 改锁 coord_tape 边界）。

验证的是"插槽"的行为，不是具体数据源：
  · 源抛异常 → 继续下一个，不中断；
  · 源返回错误 dict `{"code","error"}` → 不算成功（信封不合协议，必须换源）；
  · 全失败 → 保持原契约（盘口返回 error dict、分笔返回 []、资金流带 error）；
  · 公共函数签名冻结。

2026-10-27 起 orderbook/ticks 插槽 = coord_tape provider 表（Phase C 起
get_order_book/get_ticks_today 走 coord_tape 多源调度，旧 *_SOURCES 槽表
已无人消费），分发语义由 dispatch.py 统一保证；fund_flow_daily 仍走
_first_ok + _FUND_FLOW_DAILY_SOURCES（本文件后半段，原样锁定）。
"""
import inspect

import pytest

from app.market_cn import tape


# ── 分发器语义（tape 公共边界；插槽注入 coord_tape provider）───────

class Fake:
    """最小假 provider，形态对齐 test_coord_tape.Fake。"""

    def __init__(self, name, caps, results=None, raises=None, priority=10):
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


def _patch_coord(monkeypatch, providers):
    from app.data_sources import coord_tape as C
    monkeypatch.setattr(C, "get_providers", lambda kind, *a, **k: list(providers))


def test_orderbook_falls_through_on_exception(monkeypatch):
    dead = Fake("a", {"orderbook": True}, raises=RuntimeError("tcp down"))
    good = Fake("b", {"orderbook": True},
                results=[{"object": {"code": "600519", "price": 10.0, "source": "b"}}])
    _patch_coord(monkeypatch, [dead, good])
    out = tape.get_order_book("600519")
    assert out["price"] == 10.0 and out["source"] == "b"
    assert dead.calls == 1 and good.calls == 1


def test_error_dict_is_not_treated_as_success(monkeypatch):
    """{"code","error"} 是真值但信封不合协议 —— 必须继续试下一个源。"""
    bad = Fake("bad", {"orderbook": True},
               results=[{"code": "600519", "error": "boom"}])
    good = Fake("good", {"orderbook": True},
                results=[{"object": {"code": "600519", "price": 9.9}}])
    _patch_coord(monkeypatch, [bad, good])
    out = tape.get_order_book("600519")
    assert out["price"] == 9.9
    assert bad.calls == 1 and good.calls == 1


def test_orderbook_all_fail_keeps_error_contract(monkeypatch):
    dead = Fake("a", {"orderbook": True}, raises=RuntimeError("down"))
    dead2 = Fake("b", {"orderbook": True}, raises=RuntimeError("down too"))
    _patch_coord(monkeypatch, [dead, dead2])
    out = tape.get_order_book("600519")
    assert out["code"] == "600519" and "error" in out


def test_ticks_all_fail_returns_empty_list(monkeypatch):
    dead = Fake("a", {"ticks_today": True}, raises=RuntimeError("down"))
    _patch_coord(monkeypatch, [dead])
    assert tape.get_ticks_today("600519") == []


def test_fund_flow_daily_propagates_last_error(monkeypatch):
    monkeypatch.setattr(tape, "_FUND_FLOW_DAILY_SOURCES", [
        ("sina", lambda code, days: {"code": code, "error": "sina boom"}),
        ("em", lambda code, days: {"code": code, "error": "em boom"}),
    ])
    out = tape.get_fund_flow_daily("600519")
    assert out["error"] == "em boom"


def test_fund_flow_daily_stamps_source(monkeypatch):
    monkeypatch.setattr(tape, "_FUND_FLOW_DAILY_SOURCES", [
        ("sina", lambda code, days: {"code": code, "data": [1, 2, 3]}),
    ])
    out = tape.get_fund_flow_daily("600519")
    assert out["source"] == "sina" and out["data"] == [1, 2, 3]


# ── 签名冻结 ────────────────────────────────────────────────────

def test_public_api_signatures_frozen():
    expected = {
        "get_order_book": ["code"],
        "get_ticks_today": ["code", "limit"],
        "get_ticks_history": ["code", "date", "limit"],
        "get_fund_flow_daily": ["code", "days"],
        "get_realtime_snapshot": ["code"],
        "get_fund_flow_realtime": ["code"],
    }
    for name, params in expected.items():
        assert list(inspect.signature(getattr(tape, name)).parameters) == params, name
