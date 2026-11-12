# -*- coding: utf-8 -*-
"""P2：claims 多周期抽取守卫（2026-10-23）——不碰 market-screener 决策链。

【钉什么】
  ① GitHub 体例「T+1看多 T+3看多 T+5中性」能抽出多档 direction claim；
  ② 中性/不适用不产 claim（无追责价值）；
  ③ 止损「跌破 X」抽成 level claim（全身而退可追责）；
  ④ 各档 due_date 按 label_spec.HOLDS 分日到期。

【纪律】纯规则抽取，零网络零 DB。
"""
from __future__ import annotations

import pytest

from app.agent.chain.claims import extract_claims, extract_horizon_directions
from app.agent.chain import label_spec as ls


def test_github_style_horizon_line_multi_claims() -> None:
    ans = (
        "301199  迈赫股份  68 | 主线人形机器人\n"
        "  周期: T+1看多 T+3看多 T+5中性 T+10/T+20不适用 | 支撑=MA10\n"
    )
    hz = extract_horizon_directions(ans)
    assert ("T+1", "bullish") in hz
    assert ("T+3", "bullish") in hz
    assert not any(lab == "T+5" for lab, _ in hz), "中性不产 claim"
    assert not any(lab in ("T+10", "T+20") for lab, _ in hz), "不适用不产 claim"

    cs = extract_claims("帮我选股", ans, "finance")
    dirs = {c["horizon"]: c["predicted"]["dir"] for c in cs if c["claim_type"] == "direction"}
    assert dirs.get("T+1") == "bullish" and dirs.get("T+3") == "bullish"
    assert "T+5" not in dirs


def test_stop_loss_is_level_claim() -> None:
    ans = "300129 看多。止损：跌破 12.80 离场。T+3看多 T+5看多"
    cs = extract_claims("选股", ans, "finance")
    lv = [c for c in cs if c["claim_type"] == "level"]
    assert lv and lv[0]["predicted"]["price"] == 12.8
    assert lv[0]["predicted"]["kind"] in ("跌破", "止损", "止损位")


def test_due_dates_follow_label_spec() -> None:
    ans = "002164 T+1看多 T+3看多 T+5看空"
    cs = extract_claims("选股", ans, "finance")
    by = {c["horizon"]: c for c in cs if c["claim_type"] == "direction"}
    assert ls.hold_days("T+1") == 1 and ls.hold_days("T+5") == 5
    assert by["T+1"]["due_date"] < by["T+5"]["due_date"], "到期必须分日"


def test_label_spec_has_t10_t20_for_mid_term_claims() -> None:
    """通用口径保留 T+10/T+20（≈1M）；screener 不输出但 claims 认。"""
    assert ls.hold_days("T+10") == 10
    assert ls.hold_days("T+20") == 21
    assert ls.hold_label(10) == "2W" and ls.hold_label(21) == "1M"


@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        ("未知/看多/看空", [("T+3", "bullish"), ("T+5", "bearish")]),
        ("看多/未知/看空", [("T+1", "bullish"), ("T+5", "bearish")]),
        ("看多/看空/未知", [("T+1", "bullish"), ("T+3", "bearish")]),
        ("中性/未知/中性", []),
    ],
)
def test_unknown_horizon_is_not_a_direction_and_preserves_positions(cell, expected):
    answer = f"| 代码 | T+1/T+3/T+5 |\n| 300129 | {cell} |"
    assert extract_horizon_directions(answer) == expected


def test_unknown_periods_do_not_fall_back_to_reason_text_for_direction():
    answer = "| 代码 | T+1/T+3/T+5 | 理由 |\n| 300129 | 未知/中性/未知 | 看多证据不足 |"
    claims = extract_claims("选股", answer, "finance")
    assert not [claim for claim in claims if claim["claim_type"] == "direction"]


def test_explicit_unknown_period_does_not_hide_known_periods():
    answer = "300129 T+1未知 T+3看多 T+5未知"
    assert extract_horizon_directions(answer) == [("T+3", "bullish")]
