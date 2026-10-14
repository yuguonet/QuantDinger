# -*- coding: utf-8 -*-
"""market-screener 实时覆盖层（bar vs rt 对账）守卫。

【锁住的缺陷】2026-10-14「明天买什么股」：deep_analyze tech 只吃本地日线，
盘后 bar 滞后时 600408 报 close 3.69 / +4.53% / limit_up=false，实时却是
4.06 / +10.03% / 涨停封板。主 agent 闻到矛盾后在主对话逐只重拉 quote/flow/tech，
11 步烧穿 token 预算。修法：fetch_rt_map + apply_rt_overlay —— 历史指标留 bar，
可执行性字段（收盘/涨跌停）以 rt 为准，并显式标 stale_bar。

包目录名含连字符（market-screener），常规 import 不成立，走 modpath.resolve
（与 skill_run.py 同口径）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _helpers():
    from app.agent.modpath import import_module
    return import_module("app.agent.skills.market-screener._helpers")


def test_apply_rt_overlay_corrects_stale_bar_limit_up():
    h = _helpers()
    row = {
        "code": "600408",
        "name": "安泰集团",
        "tech": {
            "ok": True,
            "close": 3.69,
            "change_pct": 4.53,
            "limit_up": False,
            "rsi14": 67.4,
            "dist_ma20_pct": 14.95,
        },
    }
    rt = {
        "price": 4.06,
        "change_pct": 10.03,
        "previous_close": 3.69,
        "limit_up_price": 4.06,
        "limit_down_price": 3.33,
        "time": "2026-10-14 15:00:00",
    }
    h.apply_rt_overlay(row, rt, code="600408", name="安泰集团")
    assert row["stale_bar"] is True, row
    assert row["rt"]["price"] == 4.06
    assert row["rt"]["limit_up"] is True, row["rt"]
    # 可执行性字段以 rt 为准
    assert row["tech"]["close"] == 4.06
    assert row["tech"]["change_pct"] == 10.03
    assert row["tech"]["limit_up"] is True
    # 历史指标不得被 rt 改写
    assert row["tech"]["rsi14"] == 67.4
    assert row["tech"]["dist_ma20_pct"] == 14.95
    assert row["tech"]["bar_close"] == 3.69
    assert any("涨停封板" in x for x in row.get("rt_risks") or []), row


def test_apply_rt_overlay_no_stale_keeps_bar_metrics():
    h = _helpers()
    row = {"code": "002733", "name": "雄韬股份",
           "tech": {"ok": True, "close": 19.98, "change_pct": -5.84, "limit_up": False,
                    "rsi14": 58.7}}
    rt = {"price": 19.98, "change_pct": -5.84, "previous_close": 21.22,
          "limit_up_price": 23.34, "limit_down_price": 19.10, "time": "2026-10-14 15:00:00"}
    h.apply_rt_overlay(row, rt, code="002733", name="雄韬股份")
    assert row["stale_bar"] is False, row
    assert row["tech"]["close"] == 19.98
    assert row["tech"]["limit_up"] is False
    assert row["tech"].get("bar_close") is None
    assert not any("涨停封板" in x for x in row.get("rt_risks") or [])


def test_apply_rt_overlay_missing_rt_is_noop():
    h = _helpers()
    row = {"code": "x", "tech": {"close": 1.0, "limit_up": False, "rsi14": 50.0}}
    h.apply_rt_overlay(row, None)
    assert "rt" not in row
    assert "stale_bar" not in row
    assert row["tech"]["close"] == 1.0
    assert row["tech"]["limit_up"] is False


def test_fetch_rt_map_parses_quote_contract():
    h = _helpers()
    fake = {
        "count": 1,
        "error": None,
        "data": {
            "600408": {
                "stock_code": "600408", "last": 4.06, "changePercent": 10.03,
                "previousClose": 3.69, "limit_up": 4.06, "limit_down": 3.33,
                "name": "安泰集团", "time": "2026-10-14 15:00:00",
            },
            "000001": {"stock_code": "000001", "error": "未获取到行情"},
        },
    }
    import app.agent.tools.finance.data_tools as dt
    orig = dt.get_realtime_quote
    dt.get_realtime_quote = lambda codes: fake
    try:
        m = h.fetch_rt_map(["600408", "000001"])
    finally:
        dt.get_realtime_quote = orig
    assert "600408" in m and "000001" not in m, m
    assert m["600408"]["price"] == 4.06
    assert m["600408"]["limit_up_price"] == 4.06
