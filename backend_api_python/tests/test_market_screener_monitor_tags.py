# -*- coding: utf-8 -*-
"""Regression tests for market-screener research hints passed to deep analysis."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.modpath import import_module

_MONITOR_TAGS = import_module("app.agent.skills.market-screener.monitor_tags")
attach_monitor = _MONITOR_TAGS.attach_monitor
monitor_chips = _MONITOR_TAGS.monitor_chips


def test_weak_market_small_cap_and_early_signal_are_soft_hints():
    out = {
        "funnel": {"verdict": "neutral", "idx_ret": -1.2},
        "picked_detail": [{
            "code": "000001",
            "kind": "jump",
            "t_first": "09:35",
            "circ_mv": 7_999_000_000,
        }],
        "analyzed_rank": [{"code": "000001"}],
    }

    attach_monitor(out)

    chips = out["picked_detail"][0]["monitor"]
    assert any("早盘信号" in chip for chip in chips)
    assert any("流通市值<80亿" in chip for chip in chips)
    assert out["analyzed_rank"][0]["monitor"] == chips


def test_weak_market_large_cap_is_not_marked_as_automatic_priority():
    chips = monitor_chips(
        {"circ_mv": 8_000_000_000},
        t_first="10:30",
        kind="jump",
        market_verdict="shrink",
    )

    assert any("流通市值≥80亿" in chip and "不据此单独优先" in chip for chip in chips)


def test_cap_hint_requires_weak_market_and_known_cap():
    strong = monitor_chips(
        {"circ_mv": 5_000_000_000},
        kind="jump",
        market_verdict="strong",
    )
    unknown = monitor_chips(
        {},
        kind="jump",
        market_verdict="weak",
    )

    assert not any("80亿" in chip for chip in strong)
    assert not any("80亿" in chip for chip in unknown)


def test_positive_index_return_overrides_weak_verdict_for_cap_hint():
    chips = monitor_chips(
        {"circ_mv": 5_000_000_000},
        kind="jump",
        market_verdict="weak",
        market_idx_ret=0.4,
    )

    assert not any("80亿" in chip for chip in chips)


def test_limit_up_reference_does_not_get_market_cap_hint():
    chips = monitor_chips(
        {"circ_mv": 5_000_000_000},
        t_first="09:35",
        kind="limit_up",
        market_verdict="weak",
    )

    assert any("早盘涨停" in chip for chip in chips)
    assert not any("80亿" in chip for chip in chips)


def test_full_sample_hints_replace_short_window_price_labels():
    moderate_gain = monitor_chips(
        {"chg_pct": 3.0},
        kind="jump",
    )
    red_close = monitor_chips(
        {"chg_pct": -1.0, "ret5_pct": -2.0},
        kind="jump",
    )
    hot_position = monitor_chips(
        {"loc": "hot"},
        kind="jump",
    )

    assert not any("温和追高格" in chip for chip in moderate_gain)
    assert any("不能单独认定为洗盘" in chip for chip in red_close)
    assert any("不把超跌当买点" in chip for chip in red_close)
    assert any("位置透支档长样本" in chip for chip in hot_position)
