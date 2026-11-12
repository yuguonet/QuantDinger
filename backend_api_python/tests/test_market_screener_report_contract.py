# -*- coding: utf-8 -*-
"""Market-screener output and capital-flow evidence contracts."""

from pathlib import Path

from app.agent.modpath import import_module


_RUN = import_module("app.agent.skills.market-screener.run")
_SKILL = Path(__file__).resolve().parents[1] / "app" / "agent" / "skills" / "market-screener" / "SKILL.md"


def test_output_contract_omits_observation_section_but_allows_empty_recommendations():
    guide = _RUN._REPORT_GUIDE
    skill = _SKILL.read_text(encoding="utf-8")
    assert "## 不买/观察" not in guide["OUTPUT_FORM"]
    assert "## 不买/观察" not in skill
    assert "今日无符合标的" in guide["OUTPUT_FORM"]
    assert "今日无符合标的" in skill
    assert "不进「买什么」" in guide["strength_rule"]


def test_fund_flow_remains_core_evidence_and_rank_preserves_flow_fields():
    guide = _RUN._REPORT_GUIDE
    assert "核心证据" in guide["fund_trend"]
    assert "不得用位置或新增提示稀释、替代资金判断" in guide["strength_rule"]
    assert "不能当作零或流入" in guide["fund_trend"]
    assert "不是东财四档主力净额" in guide["fund_trend"]
    card = {
        "code": "000001",
        "flow": {
            "recent": {"net_5d_wan": -20, "net_20d_wan": 30, "streak_dir": "outflow"},
            "flow_rel": -1.2,
        },
        "intraday": {"flow_rel_same_t": 0.4},
    }
    row = _RUN._rank_row(card)
    assert row["net5d_wan"] == -20
    assert row["net20d_wan"] == 30
    assert row["streak_dir"] == "outflow"
    assert row["flow_rel"] == -1.2
    assert row["flow_rel_st"] == 0.4


def test_horizon_contract_distinguishes_neutral_from_unknown():
    guide = _RUN._REPORT_GUIDE
    skill = _SKILL.read_text(encoding="utf-8")
    assert "看多/中性/看空/未知" in guide["OUTPUT_FORM"]
    assert "中性是有充分证据支持" in guide["horizon"]
    assert "无法相互验证" in guide["horizon"]
    assert "证据矛盾" in guide["horizon"]
    assert "中性是证据充分支持" in skill
    assert "无法相互验证" in skill
