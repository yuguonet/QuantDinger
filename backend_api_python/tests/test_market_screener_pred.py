# -*- coding: utf-8 -*-
"""market_screener 预测分重排 + 情绪分桶契约（2026-09-25 选股质量）。"""
import sys
from types import ModuleType


def _patch_pred(monkeypatch, p_up_by_code_order):
    from skills.market_screener import _helpers
    import skills.market_screener.common as common

    def fake_fetch(code, days=120):
        return [{"close": 10.0 + i * 0.01, "high": 10.1 + i * 0.01,
                 "low": 9.9 + i * 0.01, "volume": 1000 + i} for i in range(80)]

    seq = list(p_up_by_code_order)
    order = {"i": 0}

    def fake_predict(bars, mk):
        p = seq[order["i"]]
        order["i"] += 1
        return {"score": p * 100, "p_up": p, "exp_ret_bp": 0,
                "beta": {"beta": 1.0, "regime": "follow"},
                "zhuang": {"score": 0}, "market": {"p_up": 0.5}}

    fake_mod = ModuleType("app.watchlist.predict")
    fake_mod.predict_next_day = fake_predict
    fake_mod.load_market_klines = lambda d=120: []
    monkeypatch.setitem(sys.modules, "app.watchlist.predict", fake_mod)
    monkeypatch.setattr(common, "fetch_kline", fake_fetch)
    return _helpers


def test_enrich_predictive_ranks_by_p_up(monkeypatch):
    _helpers = _patch_pred(monkeypatch, [0.3, 0.7, 0.5])
    raw = [
        {"code": "A", "score": 70, "name": "a"},
        {"code": "B", "score": 40, "name": "b"},
        {"code": "C", "score": 55, "name": "c"},
    ]
    # strong 放宽门槛，三只都过
    out = _helpers.enrich_predictive(raw, market={"mood": "偏强", "mood_score": 75})
    kept = [x for x in out if x.get("selected")]
    assert [x["code"] for x in kept] == ["B", "C"]  # A p=0.3 被门槛裁掉
    assert kept[0]["score"] == 70.0
    assert kept[0]["direction"] == "bullish"


def test_weak_mood_haircut_and_threshold(monkeypatch):
    _helpers = _patch_pred(monkeypatch, [0.56, 0.70])
    raw = [
        {"code": "LB", "score": 80, "source": "连板", "name": "x"},
        {"code": "OK", "score": 60, "source": "热点题材", "name": "y"},
    ]
    out = _helpers.enrich_predictive(raw, market={"mood": "弱势", "mood_score": 25})
    by = {x["code"]: x for x in out}
    # 连板 0.56*0.92=0.515 < 0.55 被裁；热点 0.70 保留
    assert by["OK"]["has_pred"] is True
    assert by["OK"]["score"] == 70.0
    assert by["LB"]["selected"] is False
    assert by["OK"]["selected"] is True


def test_filter_weak_mood_drops_unreasoned_dragon(monkeypatch):
    from skills.market_screener import _helpers
    res = {
        "strategy": "post_market",
        "market": {"mood": "弱势", "mood_score": 20},
        "main_themes": [],
        "candidates": [
            {"code": "111111", "source": "连板", "change_pct": 9.9,
             "turnover_pct": 10, "reason": "", "name": "A"},
            {"code": "222222", "source": "热点题材", "change_pct": 5,
             "turnover_pct": 8, "reason": "机器人", "name": "B"},
            {"code": "333333", "source": "盘后筛选", "change_pct": 6,
             "turnover_pct": 10, "reason": "", "name": "C"},
        ],
    }
    codes = _helpers.filter_candidates(res).split(",")
    assert "111111" not in codes  # 弱势+连板+无 reason
    assert "222222" in codes


def test_filter_price_band():
    from skills.market_screener import _helpers
    res = {
        "strategy": "post_market",
        "market": {"mood": "中性", "mood_score": 50},
        "main_themes": [],
        "candidates": [
            {"code": "1", "source": "热点题材", "change_pct": 5,
             "turnover_pct": 8, "reason": "x", "price": 1.2, "name": "仙"},
            {"code": "2", "source": "热点题材", "change_pct": 5,
             "turnover_pct": 8, "reason": "x", "price": 50, "name": "正"},
        ],
    }
    codes = _helpers.filter_candidates(res).split(",")
    assert "1" not in codes
    assert "2" in codes


def test_build_report_uses_top_avg():
    from skills.market_screener._helpers import build_report
    results = [{"score": s, "direction": "bullish", "confidence": 0.8}
               for s in (90, 80, 70, 10, 10, 10, 10)]
    rep = build_report(results)
    assert abs(rep.score - 52.0) < 0.1
