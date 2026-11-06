# -*- coding: utf-8 -*-
"""回归守卫：个股分析不得放大近似资金流或错配龙虎榜明细日期。"""
from app.agent.tools.finance import dragon_tools, fund_flow_tools, signal_tools


def test_flow_vote_names_approximate_caliber():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "agent"
        / "skills"
        / "stock-analyzer"
        / "run.py"
    ).read_text(encoding="utf-8")
    assert "量价方向净额近似（非主力分类）" in source


def test_stock_daily_flow_marks_legacy_main_net_as_approximate(monkeypatch):
    source = __import__("app.market_cn.tape", fromlist=["get_fund_flow_daily"])
    monkeypatch.setattr(
        source,
        "get_fund_flow_daily",
        lambda *_args, **_kwargs: {
            "data": [{"date": "2026-11-03", "main_net": -10, "turnover": 100}]
        },
    )
    monkeypatch.setattr(
        fund_flow_tools,
        "_batch_execute",
        lambda fn, codes: {"error": None, "data": {code: fn(code) for code in codes}},
    )

    result = fund_flow_tools.get_fund_flow("stock_daily", "000915")
    stock = result["data"]["000915"]

    assert stock["approx"] is True
    assert "非真实主力/机构" in stock["caliber"]
    assert stock["data"][0]["main_net"] == -10


def test_analyze_report_guide_owns_stock_specific_reporting_contract():
    from app.agent.modpath import import_module

    run = import_module("skills.stock-analyzer.run")
    guide = run._ANALYZE_REPORT_GUIDE

    assert "不是主力/机构资金分类" in guide["EVIDENCE"]
    assert "detail_date" in guide["EVIDENCE"]
    assert "命令式买卖" in guide["OUTPUT_FORM"]
    assert "最多检索 2 次" in guide["SEARCH"]
    assert "零成交" in guide["TIME_CALIBER"]


def _mock_dragon_tool(monkeypatch, records, detail):
    source = __import__("app.market_cn.dragon_limit", fromlist=["get_dragon_tiger"])
    monkeypatch.setattr(source, "get_dragon_tiger", lambda *_args: records)
    monkeypatch.setattr(signal_tools, "_get_dragon_tiger_detail", lambda *_args, **_kwargs: detail)
    monkeypatch.setattr(
        dragon_tools,
        "_batch_execute",
        lambda fn, codes: {"error": None, "data": {code: fn(code) for code in codes}},
    )


def test_dragon_detail_requires_matching_record_date(monkeypatch):
    _mock_dragon_tool(
        monkeypatch,
        [{"stock_code": "000915", "date": "2026-11-03"}],
        {
            "detail_date": "2026-11-02",
            "seats": {"buy": [], "sell": []},
            "institution": {"buy_wan": 0, "sell_wan": 923.8, "net_wan": -923.8},
        },
    )

    result = dragon_tools.get_dragon_tiger("000915", detail=True)
    stock = result["data"]["000915"]

    assert "institution" not in stock
    assert stock["detail_error"] == (
        "席位明细日期与上榜记录不一致（2026-11-02 vs 2026-11-03），已跳过席位明细"
    )


def test_dragon_detail_is_attached_when_record_date_matches(monkeypatch):
    _mock_dragon_tool(
        monkeypatch,
        [{"stock_code": "000915", "date": "2026-11-03"}],
        {
            "detail_date": "2026-11-03",
            "seats": {"buy": [], "sell": []},
            "institution": {"buy_wan": 5, "sell_wan": 2, "net_wan": 3},
        },
    )

    result = dragon_tools.get_dragon_tiger("000915", detail=True)
    stock = result["data"]["000915"]

    assert stock["detail_date"] == "2026-11-03"
    assert stock["institution"]["net_wan"] == 3


def test_dragon_detail_skips_when_no_record_date(monkeypatch):
    detail_calls = []
    _mock_dragon_tool(monkeypatch, [], {"detail_date": "2026-11-02"})
    monkeypatch.setattr(
        signal_tools,
        "_get_dragon_tiger_detail",
        lambda *_args, **_kwargs: detail_calls.append(True),
    )

    result = dragon_tools.get_dragon_tiger("000915", detail=True)

    assert not detail_calls
    assert result["data"]["000915"]["detail_error"] == "无可核验的上榜日期，已跳过席位明细"


def test_stock_analyzer_skill_sets_reporting_evidence_boundaries():
    from pathlib import Path

    skill = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "agent"
        / "skills"
        / "stock-analyzer"
        / "SKILL.md"
    )
    text = skill.read_text(encoding="utf-8")

    assert "量价方向净额近似" in text
    assert "最多 2 次检索" in text
    assert "不推断「托底」" in text
    assert "detail_date" in text
    assert "零成交" in text


def test_dragon_detail_reports_the_date_used_for_seat_aggregation(monkeypatch):
    def fake_datacenter(dataset, **_kwargs):
        if dataset == "RPT_DAILYBILLBOARD_DETAILSNEW":
            return [{"TRADE_DATE": "2026-11-03", "BILLBOARD_NET_AMT": 10000}]
        return []

    monkeypatch.setattr(signal_tools, "em_datacenter", fake_datacenter)
    monkeypatch.setattr(
        signal_tools,
        "_batch_execute",
        lambda fn, codes: {"error": None, "data": {code: fn(code) for code in codes}},
    )

    result = signal_tools._get_dragon_tiger_detail("000915")

    assert result["data"]["000915"]["detail_date"] == "2026-11-03"
