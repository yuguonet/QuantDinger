# -*- coding: utf-8 -*-
"""market-screener 分层漏斗测试（2026-10-20）。

覆盖：batch_realtime_quotes 分块、fill_quote 扩展回填、objective_preexclude、
score_pool 双保险软门槛（含 2026-10-21 缺分保送 / 打分护栏）、run() 分层编排。
包目录名含连字符，统一走 modpath.import_module。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.modpath import import_module

_common = import_module("app.agent.skills.market-screener.common")
_facts = import_module("app.agent.skills.market-screener._facts")
_helpers = import_module("app.agent.skills.market-screener._helpers")
_run = import_module("app.agent.skills.market-screener.run")


def test_batch_rt_quotes_chunks_beyond_20():
    """25 只票分 2 块（20+5），全部返回，不被 20 截断。"""
    import app.agent.tools.finance.data_tools as dt

    codes = [f"{600000 + i}" for i in range(25)]

    def fake(codes_str):
        part = [c.strip() for c in codes_str.split(",") if c.strip()]
        data = {c: {"stock_code": c, "last": 10.0, "changePercent": 1.0,
                    "previousClose": 9.9, "limit_up": 10.89, "limit_down": 8.91,
                    "name": f"N{c}", "time": "2026-10-20 10:00:00"} for c in part}
        return {"count": len(part), "error": None, "data": data}

    orig = dt.get_realtime_quote
    dt.get_realtime_quote = fake
    try:
        quotes, failed = _common.batch_realtime_quotes(codes, workers=2)
    finally:
        dt.get_realtime_quote = orig

    assert len(quotes) == 25, f"应返回25只，实际{len(quotes)}"
    assert failed == [], f"不应有失败块，实际{failed}"
    assert quotes["600024"]["name"] == "N600024"


def test_batch_rt_quotes_failed_chunk_recorded():
    """第二块抛异常 → 进 failed_chunks，第一块正常返回。"""
    import app.agent.tools.finance.data_tools as dt

    codes = [f"{600000 + i}" for i in range(25)]
    calls = {"n": 0}

    def fake(codes_str):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("eastmoney down")
        part = [c.strip() for c in codes_str.split(",") if c.strip()]
        data = {c: {"stock_code": c, "last": 10.0, "name": "X"} for c in part}
        return {"count": len(part), "error": None, "data": data}

    orig = dt.get_realtime_quote
    dt.get_realtime_quote = fake
    try:
        quotes, failed = _common.batch_realtime_quotes(codes, workers=2)
    finally:
        dt.get_realtime_quote = orig

    assert len(quotes) == 20, f"第一块20只应成功，实际{len(quotes)}"
    assert len(failed) == 1
    assert len(failed[0]) == 5


def test_batch_rt_quotes_empty():
    quotes, failed = _common.batch_realtime_quotes([])
    assert quotes == {} and failed == []


def test_fill_quote_extended_fields_and_full_coverage():
    """fill_quote 用分块回填，>20 只全覆盖，且带 name/limit_up/limit_down/previous_close。"""
    import app.agent.tools.finance.data_tools as dt

    codes = [f"{600000 + i}" for i in range(25)]

    def fake_call_tool(name, **kw):
        if name == "search_stocks":
            return {"stocks": [{"code": c, "name": f"票{c}", "reason": "测试",
                                "change_pct": 1.0, "turnover_pct": 3.0} for c in codes]}
        return {}

    def fake_rt(codes_str):
        part = [c.strip() for c in codes_str.split(",") if c.strip()]
        data = {c: {"stock_code": c, "last": 10.0 + i, "changePercent": 1.0,
                    "previousClose": 9.9, "limit_up": 10.99, "limit_down": 9.01,
                    "name": f"RT{c}", "time": "10:00"} for i, c in enumerate(part)}
        return {"count": len(part), "error": None, "data": data}

    orig_rt = dt.get_realtime_quote
    orig_ct = _facts.call_tool
    dt.get_realtime_quote = fake_rt
    _facts.call_tool = fake_call_tool
    try:
        pool = _facts.candidate_pool(strategy="post_market", sources="search",
                                     queries="测试", search_top_n=30)
    finally:
        dt.get_realtime_quote = orig_rt
        _facts.call_tool = orig_ct

    cands = pool["candidates"]
    assert len(cands) == 25, f"25只搜索结果应全在池中，实际{len(cands)}"
    filled = [c for c in cands if c.get("limit_up") is not None]
    assert len(filled) == 25, f"25只都应回填 limit_up，实际{len(filled)}"
    sample = next(c for c in cands if c["code"] == "600024")
    assert sample.get("name") == "RT600024", f"name 应被实时覆盖，实际{sample.get('name')}"
    assert sample.get("previous_close") == 9.9
    assert sample.get("limit_up") == 10.99


def test_query_default_expanded():
    """_QUERY_DEFAULT 扩词：各时段默认查询覆盖资金流/板块/连板/涨停/情绪等多维度。"""
    qd = _facts._QUERY_DEFAULT
    assert isinstance(qd, dict), f"_QUERY_DEFAULT 应为 dict（按 strategy 分词），实际: {type(qd)}"
    assert qd, "_QUERY_DEFAULT 不应为空"
    for strategy, qs in qd.items():
        joined = " ".join(qs)
        for kw in ("资金流", "板块", "涨停"):
            assert kw in joined, (
                f"{strategy} 的默认查询应包含「{kw}」，实际: {qs}")
        assert len(qs) >= 6, (
            f"{strategy} 扩词后默认查询应 >=6 条，实际{len(qs)}: {qs}")


def test_fetch_rt_map_beyond_20():
    """25 只票 fetch_rt_map 全覆盖，不被 20 截断。"""
    import app.agent.tools.finance.data_tools as dt

    codes = [f"{600000 + i}" for i in range(25)]

    def fake(codes_str):
        part = [c.strip() for c in codes_str.split(",") if c.strip()]
        data = {c: {"stock_code": c, "last": 10.0 + (int(c) - 600000),
                    "changePercent": 1.0,
                    "previousClose": 9.9, "limit_up": 10.99, "limit_down": 9.01,
                    "high": 10.5, "low": 9.5, "name": f"票{c}", "time": "10:00"}
                for c in part}
        return {"count": len(part), "error": None, "data": data}

    orig = dt.get_realtime_quote
    dt.get_realtime_quote = fake
    try:
        rt_map = _helpers.fetch_rt_map(codes)
    finally:
        dt.get_realtime_quote = orig

    assert len(rt_map) == 25, f"应25只全覆盖，实际{len(rt_map)}"
    assert rt_map["600024"]["price"] == 10.0 + 24
    assert rt_map["600024"]["limit_up_price"] == 10.99
    assert rt_map["600024"]["previous_close"] == 9.9


def test_objective_preexclude_intraday_limit_up():
    """intraday：涨停封板剔除；post_market：同样票仅保留（不剔）。"""
    cands = [
        {"code": "600001", "name": "涨停票", "price": 10.99, "previous_close": 9.99,
         "turnover_pct": 5.0, "limit_up": 10.99, "limit_down": 8.99},
        {"code": "600002", "name": "正常票", "price": 10.0, "previous_close": 9.9,
         "turnover_pct": 5.0, "limit_up": 10.89, "limit_down": 8.91},
    ]
    kept, stats = _helpers.objective_preexclude(cands, "intraday")
    assert [c["code"] for c in kept] == ["600002"]
    assert stats["limit_up"] == 1

    kept2, stats2 = _helpers.objective_preexclude(cands, "post_market")
    assert len(kept2) == 2, "post_market 不排涨停"
    assert stats2["limit_up"] == 0


def test_objective_preexclude_st_price_turnover():
    """所有时段：ST/退市、价格带 [2,300]、换手 <1% 剔除。"""
    cands = [
        {"code": "600001", "name": "ST股", "price": 5.0, "turnover_pct": 5.0},
        {"code": "600002", "name": "退市整理", "price": 1.0, "turnover_pct": 5.0},
        {"code": "600003", "name": "低价票", "price": 1.5, "turnover_pct": 5.0},
        {"code": "600004", "name": "高价票", "price": 350.0, "turnover_pct": 5.0},
        {"code": "600005", "name": "低换手", "price": 10.0, "turnover_pct": 0.5},
        {"code": "600006", "name": "正常", "price": 10.0, "turnover_pct": 5.0},
        {"code": "600007", "name": "停牌票", "price": 10.0, "turnover_pct": 5.0,
         "suspended": True},
    ]
    kept, stats = _helpers.objective_preexclude(cands, "post_market")
    assert [c["code"] for c in kept] == ["600006"]
    assert stats["st"] == 2, f"ST+退市共2只归 st 判定，实际{stats}"
    assert stats["price_band"] == 2
    assert stats["low_turnover"] == 1
    assert stats["suspended"] == 1


def test_objective_preexclude_quotes_missing_not_killed():
    """无行情、无昨收 → 无法判涨停，不剔除，计入 quotes_missing。"""
    cands = [
        {"code": "600001", "name": "无行情", "price": 10.0, "turnover_pct": 5.0},
    ]
    kept, stats = _helpers.objective_preexclude(cands, "intraday")
    assert len(kept) == 1, "缺行情不剔除"
    assert stats["quotes_missing"] == 1


def test_objective_preexclude_limit_down_intraday():
    """intraday：跌停封死剔除。"""
    cands = [
        {"code": "600001", "name": "跌停票", "price": 9.01, "previous_close": 10.01,
         "turnover_pct": 5.0, "limit_up": 11.01, "limit_down": 9.01},
    ]
    kept, stats = _helpers.objective_preexclude(cands, "intraday")
    assert kept == []
    assert stats["limit_down"] == 1


def test_objective_preexclude_no_limit_sentinel():
    """limit_up=0 哨兵（无涨跌幅限制）→ 不按涨停剔除。"""
    cands = [
        {"code": "600001", "name": "新股", "price": 50.0, "turnover_pct": 5.0,
         "limit_up": 0, "limit_down": 0},
    ]
    kept, stats = _helpers.objective_preexclude(cands, "intraday")
    assert len(kept) == 1, "哨兵 0 不应触发涨停剔除"
    assert stats["limit_up"] == 0


def _fake_bundle_factory(profiles):
    def fake(prescreen_result, limit=20, with_flow=True, with_pred=True):
        return {"strategy": prescreen_result.get("strategy"),
                "market": prescreen_result.get("market"),
                "mood_regime": "neutral",
                "themes": ["机器人"],
                "hints": {},
                "pool_size": len(prescreen_result.get("candidates") or []),
                "trimmed": 0,
                "profiles": profiles[:limit],
                "missing": [], "errors": []}
    return fake


def test_score_pool_dual_gate():
    """p_up<0.40 剔除 + 排名后30%剔除 + 保底回捞 theme.hit + 缺分保送（2026-10-21 修订）。"""
    profiles = [
        {"code": "A", "p_up": 0.60, "hard": [], "theme": {"hit": True, "tags": []}, "source": "热点题材"},
        {"code": "B", "p_up": 0.55, "hard": [], "theme": {"hit": False, "tags": []}, "source": "条件搜索"},
        {"code": "C", "p_up": 0.50, "hard": [], "theme": {"hit": False, "tags": []}, "source": "条件搜索"},
        {"code": "D", "p_up": 0.45, "hard": [], "theme": {"hit": False, "tags": []}, "source": "条件搜索"},
        {"code": "E", "p_up": 0.42, "hard": [], "theme": {"hit": False, "tags": []}, "source": "条件搜索"},
        {"code": "F", "p_up": 0.38, "hard": [], "theme": {"hit": True, "tags": []}, "source": "龙回头"},
        {"code": "G", "p_up": 0.30, "hard": [], "theme": {"hit": False, "tags": []}, "source": "条件搜索"},
        {"code": "H", "p_up": None, "hard": [], "theme": {"hit": False, "tags": []}, "source": "条件搜索"},
    ]
    orig = _helpers.profile_candidates
    _helpers.profile_candidates = _fake_bundle_factory(profiles)
    try:
        selected, stats, meta = _helpers.score_pool(
            [{"code": p["code"]} for p in profiles],
            strategy="post_market", market={}, main_themes=[("机器人", 3)],
            p_up_floor=0.40, rank_cut=0.30, floor_keep=4)
    finally:
        _helpers.profile_candidates = orig

    codes = {p["code"] for p in selected}
    # 8只有p_up的排后30% → cut_start=int(7*0.7)=4，即 E(0.42) 及之后被排名剔；
    # G(0.30) p_up 地板剔；F(0.38) 地板剔但 theme.hit+龙回头 → 保底回捞
    assert "A" in codes and "B" in codes and "C" in codes and "D" in codes
    assert "G" not in codes, "G p_up=0.30 无保底资格应剔"
    # 2026-10-21 用户裁定：缺分（模型盲区）≠ 低分 ⇒ H 走缺分保送（默认名额 2）
    assert "H" in codes, "H 无 p_up 但应缺分保送——模型盲区不是坏票"
    assert any(p["code"] == "H" and p.get("no_pred_kept") for p in selected), \
        "H 应标 no_pred_kept"
    assert stats["no_pred"] == 1 and stats["no_pred_kept"] == 1
    assert stats["p_up_hard"] >= 1
    assert any(p["code"] == "F" and p.get("floor_kept") for p in selected), \
        "F 应被保底回捞并标 floor_kept"
    assert meta["mood_regime"] == "neutral"

    # no_pred_keep=0 = 回到旧口径：缺分票不进 deep（旋钮可关）
    _helpers.profile_candidates = _fake_bundle_factory(profiles)
    try:
        selected2, stats2, _ = _helpers.score_pool(
            [{"code": p["code"]} for p in profiles],
            strategy="post_market", market={}, main_themes=[("机器人", 3)],
            p_up_floor=0.40, rank_cut=0.30, floor_keep=4, no_pred_keep=0)
    finally:
        _helpers.profile_candidates = orig
    assert "H" not in {p["code"] for p in selected2}, "no_pred_keep=0 时缺分票不进"
    assert stats2["no_pred_kept"] == 0


def test_score_pool_no_pred_priority_and_quota():
    """缺分保送按客观优先级（题材命中→稀缺来源→多源）占名额，配额外不进。

    排序键是纯计数分层（_objective_rank_key），不编造分数；有分票仍按 p_up
    排在前，缺分保送殿后。
    """
    profiles = [
        {"code": "P3", "p_up": None, "hard": [], "theme": {"hit": False}, "source": "条件搜索"},
        {"code": "P2", "p_up": None, "hard": [], "theme": {"hit": False}, "source": "连板+条件搜索"},
        {"code": "P1", "p_up": None, "hard": [], "theme": {"hit": False}, "source": "龙回头"},
        {"code": "P0", "p_up": None, "hard": [], "theme": {"hit": True}, "source": "条件搜索"},
        {"code": "W", "p_up": 0.55, "hard": [], "theme": {"hit": False}, "source": "条件搜索"},
    ]
    orig = _helpers.profile_candidates
    _helpers.profile_candidates = _fake_bundle_factory(profiles)
    try:
        selected, stats, _ = _helpers.score_pool(
            [{"code": p["code"]} for p in profiles],
            strategy="post_market", market={}, main_themes=[("机器人", 3)],
            p_up_floor=0.40, rank_cut=0.0, floor_keep=0, no_pred_keep=2)
    finally:
        _helpers.profile_candidates = orig

    codes = [p["code"] for p in selected]
    assert set(codes) == {"W", "P0", "P1"}, \
        f"配额2只应给题材命中P0+稀缺来源P1，实际{codes}"
    assert codes == ["W", "P0", "P1"], f"有分票在前、缺分保送殿后，实际{codes}"
    assert stats["no_pred"] == 4 and stats["no_pred_kept"] == 2


def test_score_pool_budget_guard_skips_with_objective_priority(monkeypatch):
    """超打分护栏（QD_SCREENER_SCORE_MAX）时按客观优先级让位，score_skipped 可复盘。

    2026-10-21 修「隐形截断」：旧 _SCORE_BATCH=80 只给池序前 160 打分，后排票
    永远没分；现改为全员打分（护栏 600，env 可调），真超护栏时才按纯计数
    优先级（稀缺来源→多源）让位，不再按任意池序截断。
    """
    monkeypatch.setenv("QD_SCREENER_SCORE_MAX", "2")
    seen = {}

    def fake_bundle(prescreen_like, limit=20, with_flow=True, with_pred=True):
        seen["codes"] = [c.get("code") for c in prescreen_like.get("candidates") or []]
        return _fake_bundle_factory([
            {"code": "B1", "p_up": 0.55, "hard": [], "theme": {"hit": False}, "source": "条件搜索"},
            {"code": "B2", "p_up": 0.50, "hard": [], "theme": {"hit": False}, "source": "条件搜索"},
        ])(prescreen_like, limit=limit, with_flow=with_flow, with_pred=with_pred)

    candidates = [
        {"code": "C1", "source": "条件搜索"},
        {"code": "C2", "source": "龙回头"},
        {"code": "C3", "source": "连板+条件搜索"},
        {"code": "C4", "source": "条件搜索"},
    ]
    orig = _helpers.profile_candidates
    _helpers.profile_candidates = fake_bundle
    try:
        selected, stats, _ = _helpers.score_pool(
            candidates, strategy="post_market", market={}, main_themes=[])
    finally:
        _helpers.profile_candidates = orig

    # 预算 2：稀缺来源 C2(tier1) + 多源 C3(tier2) 先拿 p_up，C1/C4 让位
    assert seen["codes"] == ["C2", "C3"], f"让位顺序错，实际{seen['codes']}"
    assert stats["score_skipped"] == 2
    assert stats["scored"] == 2


def test_score_pool_rank_cut_only():
    """全部 >= floor 时仅排名尾部被剔，无 p_up_hard。"""
    profiles = [
        {"code": f"C{i}", "p_up": 0.90 - i * 0.02, "hard": [],
         "theme": {"hit": False, "tags": []}, "source": "条件搜索"}
        for i in range(10)
    ]
    orig = _helpers.profile_candidates
    _helpers.profile_candidates = _fake_bundle_factory(profiles)
    try:
        selected, stats, _ = _helpers.score_pool(
            [{"code": p["code"]} for p in profiles],
            strategy="post_market", market={}, main_themes=[],
            p_up_floor=0.40, rank_cut=0.30, floor_keep=0)
    finally:
        _helpers.profile_candidates = orig

    # 10只全>=0.40；cut_start=int(10*0.7)=7 → C7/C8/C9 剔（3只）
    assert len(selected) == 7
    assert stats["p_up_hard"] == 0
    assert stats["rank_cut"] == 3


def test_run_funnel_two_rounds_when_low_alive():
    """Round0 存活 <4 → 自动 Round1 放宽参数并追加通用查询词。"""
    round_calls = {"n": 0, "queries": []}

    def fake_pre_screen(sources=None, queries=None, search_top_n=50,
                        min_turnover=None, limit=None):
        round_calls["n"] += 1
        round_calls["queries"].append(queries or "")
        assert search_top_n == 100, f"run() 应传 search_top_n=100，实际{search_top_n}"
        if round_calls["n"] == 1:
            # Round0：4只票全低分 → score_pool 存活 2 <4
            cands = [{"code": f"60000{i}", "name": f"票{i}", "price": 10.0,
                      "turnover_pct": 5.0, "source": "条件搜索"} for i in range(4)]
        else:
            # Round1：更多票
            cands = [{"code": f"60001{i}", "name": f"票{i}", "price": 10.0,
                      "turnover_pct": 5.0, "source": "条件搜索"} for i in range(8)]
        return {"strategy": "post_market", "market": {}, "main_themes": [],
                "candidates": cands, "missing": [], "errors": [], "pool_size": len(cands)}

    def fake_objective_preexclude(cands, strategy, quotes=None):
        return list(cands), {"limit_up": 0, "limit_down": 0, "st": 0, "suspended": 0,
                             "price_band": 0, "low_turnover": 0, "quotes_missing": 0}

    round_params = []

    def fake_score_pool(cands, strategy="", market=None, main_themes=None,
                        p_up_floor=0.40, rank_cut=0.30, floor_keep=4,
                        no_pred_keep=2, **kw):
        round_params.append({"p_up_floor": p_up_floor, "rank_cut": rank_cut,
                             "floor_keep": floor_keep, "no_pred_keep": no_pred_keep,
                             "w2s_keep": kw.get("w2s_keep")})
        if len(round_params) == 1:
            # Round0：只活 2 只
            selected = [{"code": "600000", "p_up": 0.55}, {"code": "600001", "p_up": 0.50}]
            stats = {"p_up_hard": 1, "rank_cut": 1, "no_pred": 0, "floor_kept": 0, "scored": 4}
        else:
            selected = [{"code": f"60001{i}", "p_up": 0.60 - i * 0.03} for i in range(6)]
            stats = {"p_up_hard": 1, "rank_cut": 1, "no_pred": 0, "floor_kept": 0, "scored": 8}
        return selected, stats, {"strategy": "post_market", "market": {},
                                 "mood_regime": "neutral", "themes": [],
                                 "hints": {}, "missing": [], "errors": []}

    def fake_deep_analyze(codes, limit=8, **kw):
        return {"report_guide": _run._REPORT_GUIDE, "analyzed": codes.split(","),
                "strategy": "post_market", "signal": "ok", "limit": limit}

    orig_pre = _run.pre_screen
    orig_obj = _run.objective_preexclude
    orig_score = _run.score_pool
    orig_deep = _run.deep_analyze
    _run.pre_screen = fake_pre_screen
    _run.objective_preexclude = fake_objective_preexclude
    _run.score_pool = fake_score_pool
    _run.deep_analyze = fake_deep_analyze
    try:
        result = _run.run()
    finally:
        _run.pre_screen = orig_pre
        _run.objective_preexclude = orig_obj
        _run.score_pool = orig_score
        _run.deep_analyze = orig_deep

    assert round_calls["n"] == 2, f"应两轮，实际{round_calls['n']}"
    assert round_params[0]["p_up_floor"] == 0.40
    assert round_params[0]["no_pred_keep"] == 2, "Round0 缺分保送名额 2"
    assert round_params[1]["p_up_floor"] == 0.35, "Round1 应放宽地板"
    assert round_params[1]["no_pred_keep"] == 3, "Round1 缺分保送名额应放宽到 3"
    assert "换手率大于3%" in round_calls["queries"][1], \
        f"Round1 queries 应含通用词，实际{round_calls['queries'][1]}"
    funnel = result["funnel"]
    assert funnel["rounds"] == 2
    assert funnel["deep_n"] == 6
    assert result["limit"] == 12, f"deep limit 应 12，实际{result.get('limit')}"
    # funnel 在 report_guide 之后
    keys = list(result.keys())
    assert keys[0] == "report_guide"
    assert keys[1] == "funnel", f"funnel 应紧跟 report_guide，实际键序{keys[:3]}"


def test_run_funnel_single_round_when_alive_enough():
    """Round0 存活 >=4 → 不进 Round1，单轮交付。"""
    calls = {"n": 0}

    def fake_pre_screen(**kw):
        calls["n"] += 1
        return {"strategy": "post_market", "market": {}, "main_themes": [],
                "candidates": [{"code": f"60000{i}", "name": f"票{i}", "price": 10.0,
                                "turnover_pct": 5.0} for i in range(6)],
                "missing": [], "errors": []}

    def fake_obj(cands, strategy, quotes=None):
        return list(cands), {"limit_up": 0, "limit_down": 0, "st": 0, "suspended": 0,
                             "price_band": 0, "low_turnover": 0, "quotes_missing": 0}

    def fake_score(cands, strategy="", market=None, main_themes=None, **params):
        selected = [{"code": f"60000{i}", "p_up": 0.60 - i * 0.02} for i in range(5)]
        return selected, {"p_up_hard": 0, "rank_cut": 1, "no_pred": 0,
                          "floor_kept": 0, "scored": 6}, {"mood_regime": "neutral"}

    def fake_deep(codes, limit=8, **kw):
        return {"report_guide": _run._REPORT_GUIDE, "analyzed": codes.split(","),
                "limit": limit}

    orig = (_run.pre_screen, _run.objective_preexclude, _run.score_pool, _run.deep_analyze)
    _run.pre_screen = fake_pre_screen
    _run.objective_preexclude = fake_obj
    _run.score_pool = fake_score
    _run.deep_analyze = fake_deep
    try:
        result = _run.run()
    finally:
        (_run.pre_screen, _run.objective_preexclude,
         _run.score_pool, _run.deep_analyze) = orig

    assert calls["n"] == 1, f"存活>=4应单轮，实际{calls['n']}轮"
    assert result["funnel"]["rounds"] == 1
    assert result["funnel"]["deep_n"] == 5


def test_run_funnel_empty_after_gates():
    """两轮后仍无存活 → 返回可汇报空结果（含 funnel）。"""
    def fake_pre_screen(**kw):
        return {"strategy": "post_market", "market": {}, "main_themes": [],
                "candidates": [{"code": "600001", "name": "ST票", "price": 10.0,
                                "turnover_pct": 5.0}],
                "missing": ["条件搜索[测试]: timeout"], "errors": []}

    def fake_obj(cands, strategy, quotes=None):
        return [], {"limit_up": 0, "limit_down": 0, "st": 1, "suspended": 0,
                    "price_band": 0, "low_turnover": 0, "quotes_missing": 0}

    def fake_score(cands, strategy="", market=None, main_themes=None, **params):
        return [], {"p_up_hard": 0, "rank_cut": 0, "no_pred": 0,
                    "floor_kept": 0, "scored": 0}, {"mood_regime": "weak"}

    orig = (_run.pre_screen, _run.objective_preexclude, _run.score_pool)
    _run.pre_screen = fake_pre_screen
    _run.objective_preexclude = fake_obj
    _run.score_pool = fake_score
    try:
        result = _run.run()
    finally:
        (_run.pre_screen, _run.objective_preexclude, _run.score_pool) = orig

    assert result["analyzed"] == []
    assert result["signal"] == "无符合软硬门槛的候选"
    assert result["funnel"]["pool_raw"] == 1
    assert result["funnel"]["excluded_obj"]["st"] == 1
    assert result["funnel"]["eastmoney_ok"] is False
    assert "report_guide" in result


def test_objective_preexclude_missing_price_not_killed():
    """缺价候选不被 price_band 误杀（raw None ≠ 0.0）。"""
    cands = [
        {"code": "600001", "name": "缺价票"},          # 无 price/close
        {"code": "600002", "name": "低价票", "price": 1.5},  # 真低价，应剔
    ]
    kept, stats = _helpers.objective_preexclude(cands, strategy="post_market")
    codes = [c["code"] for c in kept]
    assert "600001" in codes, f"缺价不应剔，实际kept={codes}"
    assert "600002" not in codes
    assert stats["price_band"] == 1


def test_fetch_rt_map_missing_last_skipped():
    """quote 无 last/price → 不进 rt_map（不以 0.0 混入）。"""
    import app.agent.tools.finance.data_tools as dt

    def fake_rt(codes_str):
        part = [c.strip() for c in codes_str.split(",") if c.strip()]
        data = {}
        for c in part:
            if c == "600001":
                data[c] = {"stock_code": c, "name": "无价票"}  # 无 last
            else:
                data[c] = {"stock_code": c, "last": 10.5, "name": "正常"}
        return {"count": len(part), "error": None, "data": data}

    orig = dt.get_realtime_quote
    dt.get_realtime_quote = fake_rt
    try:
        out = _helpers.fetch_rt_map(["600001", "600002"])
    finally:
        dt.get_realtime_quote = orig
    assert "600001" not in out, f"缺价票不应进rt_map，实际{list(out)}"
    assert out["600002"]["price"] == 10.5


def test_score_pool_hard_counted_in_stats():
    """hard 候选被剔但计入 stats['hard']，漏斗可复盘。"""
    def fake_bundle(prescreen_like, limit=80, with_flow=False, with_pred=True):
        return {
            "strategy": "post_market", "market": {}, "mood_regime": "neutral",
            "themes": [], "hints": {}, "missing": [], "errors": [],
            "profiles": [
                {"code": "600001", "p_up": 0.90, "hard": ["涨停封板"], "source": "条件搜索"},
                {"code": "600002", "p_up": 0.50, "hard": [], "source": "条件搜索",
                 "theme": {"hit": False}},
            ],
        }

    orig = _helpers.profile_candidates
    _helpers.profile_candidates = fake_bundle
    try:
        selected, stats, meta = _helpers.score_pool(
            [{"code": "600001"}, {"code": "600002"}], strategy="post_market")
    finally:
        _helpers.profile_candidates = orig

    codes = [p["code"] for p in selected]
    assert "600001" not in codes, f"hard票不应进deep，实际{codes}"
    assert stats["hard"] == 1, f"hard应计1，实际{stats.get('hard')}"
    assert "600002" in codes
