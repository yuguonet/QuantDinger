# -*- coding: utf-8 -*-
"""tape.py 源表/分发器测试（2026-10-26）。

验证的是"插槽"的行为，不是具体数据源：
  · 源抛异常 → 继续下一个，不中断；
  · 源返回错误 dict `{"code","error"}` → 不算成功（原实现会误判为成功）；
  · 全失败 → 保持原契约（盘口返回 error dict、分笔返回 []、资金流带 error）；
  · 公共函数签名冻结。
"""
import inspect

import pytest

from app.market_cn import tape


# ── 分发器语义 ───────────────────────────────────────────────────

def test_orderbook_falls_through_on_exception(monkeypatch):
    def boom(code):
        raise RuntimeError("tcp down")

    def good(code):
        return {"code": code, "price": 10.0, "source": "b"}

    monkeypatch.setattr(tape, "_ORDER_BOOK_SOURCES", [("a", boom), ("b", good)])
    out = tape.get_order_book("600519")
    assert out["price"] == 10.0 and out["source"] == "b"


def test_error_dict_is_not_treated_as_success(monkeypatch):
    """{"code","error"} 是真值但失败 —— 必须继续试下一个源。"""
    monkeypatch.setattr(tape, "_ORDER_BOOK_SOURCES", [
        ("bad", lambda code: {"code": code, "error": "boom"}),
        ("good", lambda code: {"code": code, "price": 9.9}),
    ])
    out = tape.get_order_book("600519")
    assert out["price"] == 9.9


def test_orderbook_all_fail_keeps_error_contract(monkeypatch):
    def boom(code):
        raise RuntimeError("down")

    monkeypatch.setattr(tape, "_ORDER_BOOK_SOURCES", [("a", boom), ("b", boom)])
    out = tape.get_order_book("600519")
    assert out["code"] == "600519" and "error" in out


def test_ticks_all_fail_returns_empty_list(monkeypatch):
    def empty(code, limit):
        return []

    monkeypatch.setattr(tape, "_TICKS_TODAY_SOURCES", [("a", empty)])
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
