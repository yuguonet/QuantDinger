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


def test_apply_rt_overlay_limit_down_not_buyable():
    """跌停封死 → rt.buyable=False，且 rt_risks 标明（不得进可买表）。"""
    h = _helpers()
    row = {"code": "001231", "name": "农心科技",
           "tech": {"ok": True, "close": 22.82, "limit_up": False}}
    rt = {"price": 22.82, "change_pct": -9.98, "previous_close": 25.35,
          "limit_up_price": 27.89, "limit_down_price": 22.82,
          "time": "2026-10-15 13:20:00"}
    h.apply_rt_overlay(row, rt, code="001231", name="农心科技")
    assert row["rt"]["limit_down"] is True, row["rt"]
    assert row["rt"]["buyable"] is False, row["rt"]
    assert any("跌停" in x for x in row.get("rt_risks") or []), row


def test_apply_rt_overlay_limit_up_not_buyable():
    h = _helpers()
    row = {"code": "600812", "name": "华北制药",
           "tech": {"ok": True, "close": 6.57, "limit_up": True}}
    rt = {"price": 6.57, "change_pct": 10.05, "previous_close": 5.97,
          "limit_up_price": 6.57, "limit_down_price": 5.37,
          "time": "2026-10-15 10:00:00"}
    h.apply_rt_overlay(row, rt, code="600812", name="华北制药")
    assert row["rt"]["limit_up"] is True
    assert row["rt"]["buyable"] is False, row["rt"]


def test_get_limit_pct_board_and_st_and_new():
    """2026-10-15 重写：ST 跟随板；N/C 新股无涨跌幅。"""
    from app.agent.modpath import import_module
    c = import_module("app.agent.skills.market-screener.common")
    # 普通
    assert c.get_limit_pct("600000", "浦发银行") == 10.0
    assert c.get_limit_pct("300750", "宁德时代") == 20.0
    assert c.get_limit_pct("688981", "中芯国际") == 20.0
    assert c.get_limit_pct("920090", "同辉北创") == 30.0
    # ST 跟随所在板（实测主板 ST ±10%，创业 ST ±20%）
    assert c.get_limit_pct("002168", "ST惠程") == 10.0
    assert c.get_limit_pct("600381", "*ST春天") == 10.0
    assert c.get_limit_pct("300147", "*ST香雪") == 20.0
    # 新股 N/C → 0
    assert c.get_limit_pct("301569", "N联亚药业") == 0.0
    assert c.get_limit_pct("301660", "C粤芯半导体-U") == 0.0
    # 无涨跌幅时 is_limit_locked 不得误判涨停
    assert c.is_limit_locked("301569", "N联亚药业", 30.77, 7.0) is False
    assert c.is_limit_locked("600000", "浦发银行", 10.62, 9.65) is True


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


def test_apply_rt_overlay_sentinel_no_limit_new_stock():
    """新股上市首日 quote 给 -1/-1 = 无涨跌幅；不得回退公式误判涨停（2026-10-15 N联亚）。"""
    h = _helpers()
    row = {"code": "301569", "name": "N联亚药业",
           "tech": {"ok": True, "close": 30.77, "limit_up": False, "rsi14": 80.0}}
    rt = {"price": 30.77, "change_pct": 339.57, "previous_close": 7.0,
          "limit_up_price": -1.0, "limit_down_price": -1.0, "time": "2026-10-15 10:45:00"}
    h.apply_rt_overlay(row, rt, code="301569", name="N联亚药业")
    assert row["rt"]["limit_up_price"] is None, row["rt"]
    assert row["rt"]["limit_down_price"] is None, row["rt"]
    assert row["rt"]["limit_band"] == "no_limit"
    assert row["rt"]["limit_up"] is False, row["rt"]
    assert row["tech"]["limit_up"] is False
    assert not any("涨停封板" in x for x in row.get("rt_risks") or []), row


def test_apply_rt_overlay_sentinel_st_not_limit_up():
    """*ST 哨兵 -1 不得判涨停（2026-10-15 *ST天成）。"""
    h = _helpers()
    row = {"code": "600112", "name": "*ST天成",
           "tech": {"ok": True, "close": 0.73, "limit_up": False}}
    rt = {"price": 0.73, "change_pct": 0.0, "previous_close": 0.73,
          "limit_up_price": -1.0, "limit_down_price": -1.0, "time": "2026-10-15 10:00:00"}
    h.apply_rt_overlay(row, rt, code="600112", name="*ST天成")
    assert row["rt"]["limit_up"] is False, row["rt"]
    assert row["rt"]["limit_band"] == "no_limit"


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
