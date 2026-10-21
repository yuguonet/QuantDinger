# -*- coding: utf-8 -*-
"""market-screener 单根 K 线候选不崩守卫（2026-10-21）。

【锁住的缺陷】profile_candidates 的 change_pct 回填（_helpers.py:937）脱出了
`len(bars) >= 2` 保护，候选只有 1 根日线时 `bars[-2]` 抛 IndexError。
run_skill(market-screener) 连续 4 轮派发失败（job 20261021_093059 等），
候选池、market_state 全部拿不到。

包目录名含连字符（market-screener），走 modpath.import_module（与
test_v6_market_screener_rt.py 同口径）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.modpath import import_module

_COMMON = import_module("app.agent.skills.market-screener.common")
_HELPERS = import_module("app.agent.skills.market-screener._helpers")

_ONE_BAR = [
    {"date": "2026-10-21", "open": 10.0, "close": 10.5,
     "high": 10.6, "low": 9.9, "volume": 1000},
]


def test_profile_candidates_single_bar_no_crash():
    """只有 1 根日线的候选：不抛 IndexError，change_pct 留空并计入 missing。"""
    orig = _COMMON.fetch_kline
    _COMMON.fetch_kline = lambda code, days=120: list(_ONE_BAR)
    try:
        result = _HELPERS.profile_candidates(
            {
                "strategy": "post_market",
                "market": {"mood": "neutral"},
                "candidates": [{"code": "000001", "name": "平安银行", "source": "test"}],
                "main_themes": [],
            },
            with_flow=False,
            with_pred=False,
        )
    finally:
        _COMMON.fetch_kline = orig

    profiles = result.get("profiles") or []
    assert len(profiles) == 1, result
    p = profiles[0]
    assert p["code"] == "000001"
    # 单根日线算不出环比涨幅：应留空而非崩溃
    assert p["change_pct"] is None, p
    assert p["price"] == 10.5, p


def test_profile_candidates_two_bars_change_pct_kept():
    """正常 2 根日线：change_pct 照常回填（防修复矫枉过正）。"""
    two = [
        {"date": "2026-10-20", "open": 9.0, "close": 10.0,
         "high": 10.1, "low": 8.9, "volume": 1000},
        {"date": "2026-10-21", "open": 10.0, "close": 10.5,
         "high": 10.6, "low": 9.9, "volume": 1200},
    ]
    orig = _COMMON.fetch_kline
    _COMMON.fetch_kline = lambda code, days=120: list(two)
    try:
        result = _HELPERS.profile_candidates(
            {
                "strategy": "post_market",
                "market": {"mood": "neutral"},
                "candidates": [{"code": "000001", "name": "平安银行", "source": "test"}],
                "main_themes": [],
            },
            with_flow=False,
            with_pred=False,
        )
    finally:
        _COMMON.fetch_kline = orig

    profiles = result.get("profiles") or []
    assert len(profiles) == 1, result
    assert profiles[0]["change_pct"] == 5.0, profiles[0]
    assert profiles[0]["price"] == 10.5
