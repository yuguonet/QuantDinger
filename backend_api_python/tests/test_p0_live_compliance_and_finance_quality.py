# -*- coding: utf-8 -*-
"""P0 regressions: live answer compliance and explicit rejection of invalid market data."""
from __future__ import annotations

import math
from types import SimpleNamespace

import pytest


def _agent_with_responses(monkeypatch, responses):
    from mimoagent.agents.mimocode.mimocode_agent import MimocodeAgent
    from app.agent.qd_agent import QDAgent

    agent = QDAgent.__new__(QDAgent)
    agent.messages = []
    agent.trace = None
    agent.config = SimpleNamespace(grounding_gate=True, grounding_max_retries=1)
    agent._maybe_turn_compact = lambda: 0
    agent._preselect_prerun_done = lambda: True
    agent._make_collector = lambda task: None
    agent._dynamic_context_text = lambda task: task
    agent._spawn_tail = lambda message, status, trace: None
    answers = iter(responses)
    prompts = []

    def fake_run(_self, prompt, **kwargs):
        prompts.append(prompt)
        return "Idle", next(answers)

    monkeypatch.setattr(MimocodeAgent, "run", fake_run)
    return agent, prompts


def test_live_gate_retries_and_blocks_persistent_certainty_without_corpus(monkeypatch):
    agent, prompts = _agent_with_responses(monkeypatch, ["稳赚", "稳赚"])

    status, answer = agent._run_inner("分析一只股票")

    assert len(prompts) == 2
    assert "结论合规拦截" in prompts[1]
    from app.agent.qd_config import is_transient_control
    assert is_transient_control(prompts[1])
    assert status == "grounding_failed"
    assert "原答复未展示" in answer
    assert "稳赚" not in answer


def test_live_gate_allows_corrected_answer_without_corpus(monkeypatch):
    agent, prompts = _agent_with_responses(
        monkeypatch, ["保证收益", "收益存在不确定性，应结合数据评估"])

    status, answer = agent._run_inner("分析一只股票")

    assert len(prompts) == 2
    assert status == "Idle"
    assert answer == "收益存在不确定性，应结合数据评估"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("不保证收益", []),
        ("不一定保证收益", []),
        ("不保证收益，但仍有无风险套利", ["无风险套利"]),
        ("稳赚不赔", ["稳赚"]),
        ("不能不保证收益", ["保证收益"]),
    ],
)
def test_banned_phrase_check_respects_clause_and_negation(text, expected):
    from app.agent.utils.grounding import check_banned_phrases

    assert check_banned_phrases(text) == expected


def test_bench_certainty_and_compliance_share_negation_aware_rule():
    from app.agent.scripts.qd_bench import _chk_compliance, _chk_no_fake, _score_grounding

    assert _chk_compliance("不保证收益", {})
    assert _chk_no_fake("不保证收益", {})
    assert not _chk_compliance("保证收益", {})
    assert not _chk_no_fake("必然上涨", {})
    assert _score_grounding({}, "不保证收益") == 94.0


def test_fetch_closes_rejects_missing_close(monkeypatch):
    from app.agent.tools.finance import _analysis_utils as utils

    monkeypatch.setattr(utils, "_fetch_klines", lambda code, days: [{"open": 10}])
    with pytest.raises(ValueError, match="close"):
        utils._fetch_closes("600000")


@pytest.mark.parametrize(
    ("row", "field"),
    [
        ({"open": 10, "high": 11, "low": 9, "close": 10}, "volume"),
        ({"open": 10, "high": 11, "low": 9, "close": math.nan, "volume": 2}, "close"),
        ({"open": 10, "high": math.inf, "low": 9, "close": 10, "volume": 2}, "high"),
    ],
)
def test_fetch_ohlcv_rejects_missing_or_non_finite_fields(monkeypatch, row, field):
    from app.agent.tools.finance import _analysis_utils as utils

    monkeypatch.setattr(utils, "_fetch_klines", lambda code, days: [row])
    with pytest.raises(ValueError, match=field):
        utils._fetch_ohlcv("600000")


@pytest.mark.parametrize("value", [None, math.nan, math.inf, -math.inf])
def test_safe_round_rejects_missing_or_non_finite_values(value):
    from app.agent.tools.finance._analysis_utils import _safe_round

    with pytest.raises(ValueError):
        _safe_round(value)


def test_safe_round_preserves_valid_values():
    from app.agent.tools.finance._analysis_utils import _safe_round

    assert _safe_round(1.23456, 2) == 1.23
