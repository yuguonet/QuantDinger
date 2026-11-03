# -*- coding: utf-8 -*-
"""选人层加法候选的风险软排序回归测试。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.modpath import import_module

_helpers = import_module("app.agent.skills.market-screener._helpers")
_run = import_module("app.agent.skills.market-screener.run")


def test_score_pool_softly_deprioritizes_stretched_streak_candidates(monkeypatch):
    """高连板且远离 MA5 只在规则层级内降序，不剔除，也不盖过 rule.pass。"""
    profiles = [
        {
            "code": "600001",
            "p_up": 0.80,
            "rule": {"pass": False},
            "tech": {"zt_streak": 3, "dist_ma5_pct": 10.1},
        },
        {
            "code": "600002",
            "p_up": 0.70,
            "rule": {"pass": False},
            "tech": {"zt_streak": 1, "dist_ma5_pct": 12.0},
        },
        {
            "code": "600003",
            "p_up": 0.60,
            "rule": {"pass": True},
            "tech": {"zt_streak": 4, "dist_ma5_pct": 15.0},
        },
    ]
    monkeypatch.setattr(
        _helpers,
        "profile_candidates",
        lambda *_args, **_kwargs: {"profiles": profiles},
    )

    selected, stats, _meta = _helpers.score_pool(
        [{"code": p["code"]} for p in profiles],
        p_up_floor=0.0,
        rank_cut=0.0,
        floor_keep=0,
        no_pred_keep=0,
    )

    assert [p["code"] for p in selected] == ["600003", "600002", "600001"]
    assert stats["stretched_streak_risk"] == 2
    assert len(selected) == 3


def test_stretched_streak_risk_requires_both_observed_fields():
    """任一指标缺失时不猜风险档；边界条件严格为 streak>=3 且乖离>10%。"""
    risk = _helpers._has_stretched_streak_risk
    assert risk({"tech": {"zt_streak": 3, "dist_ma5_pct": 10.01}})
    assert not risk({"tech": {"zt_streak": 3, "dist_ma5_pct": 10.0}})
    assert not risk({"tech": {"zt_streak": 2, "dist_ma5_pct": 15.0}})
    assert not risk({"tech": {"zt_streak": 3}})
    assert not risk({"tech": None})


def test_baseline_selection_deprioritizes_risk_without_hard_excluding(monkeypatch):
    """保底候选先选同窗口内低风险票；风险票仍在名额内时保留。"""
    # 2026-11-03：_baseline_selection 改为「全池 + 连板高度→收盘位置低」排序
    # （去掉窗口 60 与 p_up 闸）。同型化：令三只票 zt_streak / close_pos_in_day 相等，
    # 排序差异只剩末位「风险票垫底」（连板≥3 且距 MA5>10%），断言语义保持不变。
    profiles = [
        {"code": "600001", "hard": [], "p_up": 0.7,
         "tech": {"zt_streak": 3, "dist_ma5_pct": 12, "close_pos_in_day": 80}},
        {"code": "600002", "hard": [], "p_up": 0.7,
         "tech": {"zt_streak": 3, "dist_ma5_pct": 4, "close_pos_in_day": 80}},
        {"code": "600003", "hard": [], "p_up": 0.7,
         "tech": {"zt_streak": 3, "dist_ma5_pct": 8, "close_pos_in_day": 80}},
    ]
    monkeypatch.setattr(
        _run,
        "_profile_candidates",
        lambda *_args, **_kwargs: {"profiles": profiles},
    )
    monkeypatch.setattr(_run, "_BASELINE_N", 2)

    baseline, hard_n = _run._baseline_selection(
        [{"code": p["code"]} for p in profiles],
        {"strategy": "post_market"},
    )

    assert [p["code"] for p in baseline] == ["600002", "600003"]
    assert hard_n == 0

    monkeypatch.setattr(_run, "_BASELINE_N", 3)
    baseline, _ = _run._baseline_selection(
        [{"code": p["code"]} for p in profiles],
        {"strategy": "post_market"},
    )
    assert [p["code"] for p in baseline] == ["600002", "600003", "600001"]


def test_baseline_selection_skips_hot_location_and_backfills(monkeypatch):
    """location=hot 票跳过并顺延补位；名额仍取满（与一字板剔除同语义）。"""
    profiles = [
        {"code": "600001", "hard": [], "yizi_hit": False,
         "tech": {"zt_streak": 3, "close_pos_in_day": 80, "location": "normal"}},
        {"code": "600002", "hard": [], "yizi_hit": False,
         "tech": {"zt_streak": 2, "close_pos_in_day": 80, "location": "hot"}},
        {"code": "600003", "hard": [], "yizi_hit": False,
         "tech": {"zt_streak": 1, "close_pos_in_day": 80, "location": "cool"}},
    ]
    monkeypatch.setattr(
        _run, "_profile_candidates",
        lambda *_args, **_kwargs: {"profiles": profiles},
    )
    monkeypatch.setattr(_run, "_BASELINE_N", 2)

    baseline, _ = _run._baseline_selection(
        [{"code": p["code"]} for p in profiles],
        {"strategy": "post_market"},
    )
    assert [p["code"] for p in baseline] == ["600001", "600003"]
