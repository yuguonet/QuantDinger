# -*- coding: utf-8 -*-
"""P1 弱转强回捞（合并收紧版）守卫 2026-10-23。

条件：回踩 MA5≤2.5% + 滞跌 + 主力≥2000万 + 今日>昨日 + 日线结构好。
只回捞、不改 p_up 排名；不进 VWAP/m15。
"""
from __future__ import annotations


def test_w2s_reject_far_from_ma5() -> None:
    import importlib
    h = importlib.import_module("app.agent.skills.market-screener._helpers")
    ok, why = h._w2s_tech_ok({"tech": {
        "near_support": False, "dist_ma5_pct": 12.0, "dist_ma20_pct": 15.0,
        "change_pct": 1.0, "vol_ratio": 1.0, "ma_bull_stack": True}})
    assert not ok and "MA5" in why


def test_w2s_reject_dumping() -> None:
    import importlib
    h = importlib.import_module("app.agent.skills.market-screener._helpers")
    ok, why = h._w2s_tech_ok({"tech": {
        "near_support": True, "dist_ma5_pct": 1.0, "dist_ma20_pct": 5.0,
        "change_pct": -3.5, "vol_ratio": 3.0, "ma_bull_stack": True}})
    assert not ok and "放量" in why


def test_w2s_pass_pullback_stall() -> None:
    """002164 形态：贴 MA5、缩量回踩、日线多头。"""
    import importlib
    h = importlib.import_module("app.agent.skills.market-screener._helpers")
    ok, why = h._w2s_tech_ok({"tech": {
        "near_support": True, "dist_ma5_pct": 0.37, "dist_ma20_pct": 12.0,
        "change_pct": -4.6, "vol_ratio": 0.65, "ma_bull_stack": True}})
    assert ok, why


def test_w2s_flow_floor(monkeypatch) -> None:
    import importlib
    h = importlib.import_module("app.agent.skills.market-screener._helpers")
    monkeypatch.setattr(h, "flow_summary",
                        lambda code: {"ok": True, "main_net_wan": 800.0})
    ok, why, _ = h._w2s_flow_ok("002164")
    assert not ok and "2000" in why


def test_w2s_flow_accel(monkeypatch) -> None:
    import importlib
    from datetime import date, timedelta
    h = importlib.import_module("app.agent.skills.market-screener._helpers")
    monkeypatch.setattr(h, "flow_summary",
                        lambda code: {"ok": True, "main_net_wan": 5787.0})

    today = date.today()
    yday = (today - timedelta(days=1)).isoformat()
    day_before = (today - timedelta(days=2)).isoformat()

    def _ff(scope="", codes="", days=0):
        return {"data": {codes: {"data": [
            {"date": day_before, "main_net": 1e6},   # 前日 100 万
            {"date": yday, "main_net": 2e6},         # 昨日 200 万
        ]}}}

    import app.agent.tools.finance.fund_flow_tools as fft
    monkeypatch.setattr(fft, "get_fund_flow", _ff)
    ok, why, extra = h._w2s_flow_ok("002164")
    assert ok, (why, extra)
    assert extra.get("net_accel") is True
    assert extra.get("y_main_net_wan") == 200.0

    # 未加速：今日 150 万 < 昨日 200 万 → 拒（再抬门槛到 2000 后此处走 floor）
    monkeypatch.setattr(h, "flow_summary",
                        lambda code: {"ok": True, "main_net_wan": 2100.0})
    def _ff_big(scope="", codes="", days=0):
        return {"data": {codes: {"data": [
            {"date": yday, "main_net": 5e7},  # 昨日 5000 万
        ]}}}
    monkeypatch.setattr(fft, "get_fund_flow", _ff_big)
    ok, why, extra = h._w2s_flow_ok("002164")
    assert not ok and "加速" in why
    assert extra.get("net_accel") is False
