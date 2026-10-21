# -*- coding: utf-8 -*-
"""market-screener prev_zt_streak 语义守卫（C3，2026-10-21）。

【锁住的缺陷】_helpers._prev_streak 的 idx=len-1-streak 恰落在主 streak 循环
「因涨幅不足而 break 的那根 bar」上，while 首查必败 → prev_zt_streak 恒 0：
· 前 4 板今收阴（首阴）报 0，真值 4（analysis-framework.md 维度 4：
  prev_zt_streak 高 + 当日收阴 = 首阴，是核心形态判据）；
· 3 连板报 0，真值 2（语义 =「今日之前」的连板数，common.py 渲染「连板X/前Y」）。

包目录名含连字符（market-screener），走 modpath.import_module（与
test_v6_market_screener_rt.py 同口径）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.modpath import import_module

_HELPERS = import_module("app.agent.skills.market-screener._helpers")

_CODE, _NAME = "600001", "测试股"  # 主板 → get_limit_pct=10.0


def _bars_from_closes(closes):
    """由收盘价序列构造最小日线：高开低走不影响 close 环比，量恒定。"""
    bars = []
    for c in closes:
        bars.append({"date": f"d{len(bars)}", "open": c, "close": c,
                     "high": c * 1.005, "low": c * 0.995, "volume": 1000})
    return bars


def test_prev_streak_first_red_after_4_boards():
    """首阴场景：前 4 连板今日收阴 → prev_zt_streak=4（修复前恒 0）。"""
    # 10.0 → 11.0 → 12.1 → 13.31 → 14.64 每步 +10%（4 板），今日 14.0 收阴
    bars = _bars_from_closes([10.0, 11.0, 12.1, 13.31, 14.64, 14.0])
    tech = _HELPERS.tech_summary(bars, _CODE, _NAME)
    assert tech["ok"] is True, tech
    assert tech["zt_streak"] == 0, tech  # 今日阴线，当日 streak=0
    assert tech["prev_zt_streak"] == 4, tech


def test_prev_streak_during_3_board_run():
    """连板场景：今日第 3 板 → prev_zt_streak=2（「今日之前」的连板数，修复前恒 0）。"""
    bars = _bars_from_closes([10.0, 11.0, 12.1, 13.31])
    tech = _HELPERS.tech_summary(bars, _CODE, _NAME)
    assert tech["ok"] is True, tech
    assert tech["zt_streak"] == 3, tech
    assert tech["prev_zt_streak"] == 2, tech


def test_prev_streak_regression_non_board_and_first_board():
    """回归防矫枉过正：无连板 → 0；昨日首板今日阴 → 1。"""
    # 无涨停史：prev=0
    tech0 = _HELPERS.tech_summary(
        _bars_from_closes([10.0, 10.5, 10.2]), _CODE, _NAME)
    assert tech0["zt_streak"] == 0 and tech0["prev_zt_streak"] == 0, tech0
    # 昨日首板、今日收阴：prev=1
    tech1 = _HELPERS.tech_summary(
        _bars_from_closes([10.0, 11.0, 10.5]), _CODE, _NAME)
    assert tech1["zt_streak"] == 0, tech1
    assert tech1["prev_zt_streak"] == 1, tech1
