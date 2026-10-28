# -*- coding: utf-8 -*-
"""market-screener 数据源故障显性化测试（2026-10-21；2026-10-28 去连板/龙回头源）。

缺陷背景：hot/search 等源内部 except 静默返回 []/{"error":...}，源故障与
「真空无候选」不可区分 ⇒ 主 agent 把「数据不可用」当「今日无候选」交付空结论。

修复后契约：
  1. 任一候选源失败 ⇒ candidate_pool.missing 有可读条目（「<源>源失败: 原因」）
  2. dt/broken 仅经 market_state ⇒ 以 source_errors 返回，pre_screen 合并进 missing
  3. 全部候选源失败 + 空池 ⇒ run() 空结果携带非空 missing/hard_reasons
  4. 候选源全成功但真空（交易日）⇒ missing 无源失败条目（现有行为不回退）
  5. 空池兜底交易日历：非交易日/日历不可确认 ⇒ missing 显式标注「非真实空候选」
     （日历复用 app/utils/trading_calendar.py，项目唯一既有交易日历实现）

包目录名含连字符，统一走 modpath.import_module；日历打源模块属性（延迟导入生效）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.agent.modpath import import_module

_common = import_module("app.agent.skills.market-screener.common")
_facts = import_module("app.agent.skills.market-screener._facts")
_run = import_module("app.agent.skills.market-screener.run")


def _patch_calendar(monkeypatch, *, trading=True, raises=False):
    """钉死交易日历判定（_facts 内延迟导入 ⇒ 打 app.utils.trading_calendar 即生效）。"""
    from app.utils import trading_calendar as _cal

    def _boom(*a, **k):
        raise RuntimeError("akshare calendar down")

    monkeypatch.setattr(_cal, "is_trading_day",
                        _boom if raises else (lambda *a, **k: trading))


# ── 1. 源级失败必须进 missing ──────────────────────────────────────────

def test_search_source_failure_recorded_in_missing(monkeypatch):
    """① search 源抛错 ⇒ missing 含「条件搜索」可读条目，池不可静默为空。"""
    def boom(*a, **k):
        raise RuntimeError("eastmoney down")

    monkeypatch.setattr(_facts, "call_tool", boom)
    _patch_calendar(monkeypatch, trading=True)

    pool = _facts.candidate_pool(strategy="eod", sources="search",
                                 queries="测试", date="2026-10-21")
    assert pool["pool_size"] == 0
    assert any("条件搜索" in str(m) and "eastmoney down" in str(m)
               for m in pool["missing"]), pool["missing"]


def test_hot_source_failure_recorded_in_missing(monkeypatch):
    """① hot 源抛错 ⇒ missing 含「热点题材源失败」条目（旧版 error 键无人检查）。"""
    def boom(date):
        raise RuntimeError("同花顺错误: date invalid")

    monkeypatch.setattr(_facts, "fetch_hot_stocks_with_reason", boom)
    _patch_calendar(monkeypatch, trading=True)

    pool = _facts.candidate_pool(strategy="post_market", sources="hot",
                                 queries=None, date="2026-10-21")
    assert pool["pool_size"] == 0
    assert any("热点题材源失败" in str(m) and "同花顺错误" in str(m)
               for m in pool["missing"]), pool["missing"]


def test_market_state_pool_source_errors_collected(monkeypatch):
    """② dt/broken 仅经 market_state ⇒ 三池失败以 source_errors 显性返回，
    情绪计数降级为 0 但不再伪装成「情绪冰点」。"""
    def boom(date):
        raise RuntimeError("source down")

    monkeypatch.setattr(_facts, "fetch_zt_pool", boom)
    monkeypatch.setattr(_facts, "fetch_dt_pool", boom)
    monkeypatch.setattr(_facts, "fetch_broken_board", boom)
    monkeypatch.setattr(_facts, "fetch_hot_sectors",
                        lambda *a, **k: {"error": "x"})
    import app.agent.tools.finance.fund_flow_tools as ffmod
    monkeypatch.setattr(ffmod, "get_fund_flow", lambda **k: {"main_net": 0})

    out = _facts.market_state()
    errs = [str(x) for x in out.get("source_errors") or []]
    assert len(errs) == 3, errs
    assert any("涨停池源失败" in x for x in errs), errs
    assert any("跌停池源失败" in x for x in errs), errs
    assert any("炸板池源失败" in x for x in errs), errs
    assert out["zt_count"] == 0 and out["dt_count"] == 0
    assert "mood" in out and "mood_score" in out


def test_pre_screen_merges_market_source_errors(monkeypatch):
    """② pre_screen 必须把 market_state.source_errors 合并进 missing 收口。"""
    def fake_pool(**kw):
        return {"strategy": "post_market", "date": "2026-10-21",
                "sources_used": ["search"], "queries_used": [],
                "pool_size": 0, "main_themes": [], "candidates": [],
                "missing": []}

    monkeypatch.setattr(_run, "candidate_pool", fake_pool)
    monkeypatch.setattr(_run, "_market_state",
                        lambda: {"mood": "中性", "mood_score": 50,
                                 "source_errors": ["跌停池源失败: RuntimeError: down"]})

    out = _run.pre_screen(sources="search")
    assert any("市场状态" in str(m) and "跌停池源失败" in str(m)
               for m in out["missing"]), out["missing"]


# ── 2. 全源失败 → run() 空结果不可静默 ────────────────────────────────

def test_all_sources_failure_run_empty_result_not_silent(monkeypatch):
    """③ 候选源全失败 ⇒ run() 空结果携带非空 missing/hard_reasons，
    且 missing 首条是源失败（截断 [:6] 不吞故障信息）。"""
    def boom(*a, **k):
        raise RuntimeError("source down")

    monkeypatch.setattr(_facts, "fetch_hot_stocks_with_reason", boom)
    monkeypatch.setattr(_facts, "call_tool",
                        lambda name, **kw: {"error": "eastmoney search down"})
    monkeypatch.setattr(_run, "_market_state",
                        lambda: {"mood": "中性", "mood_score": 50,
                                 "source_errors": ["跌停池源失败: RuntimeError: source down"]})
    _patch_calendar(monkeypatch, trading=True)

    result = _run.run(sources="hot,search", queries="测试")
    assert not result.get("analyzed"), "全源失败不可能产出候选"
    assert result["funnel"]["pool_raw"] == 0
    assert result["missing"], "missing 必须非空——数据不可用 ≠ 今日无候选"
    assert "源失败" in str(result["missing"][0]) or "条件搜索" in str(result["missing"][0]), \
        result["missing"]
    assert result["hard_reasons"], "hard_reasons 必须非空"
    assert any("候选源失败" in str(h) for h in result["hard_reasons"]), \
        result["hard_reasons"]


# ── 3. 真空池回归（现有行为不回退） ───────────────────────────────────

def test_all_sources_ok_vacuum_on_trading_day_no_false_failure(monkeypatch):
    """④ 候选源全成功但真空 + 交易日 ⇒ missing 不含源失败/日历条目（真实空候选）。"""
    monkeypatch.setattr(_facts, "fetch_hot_stocks_with_reason",
                        lambda date: {"stocks": [], "hot_tags": []})
    monkeypatch.setattr(_facts, "call_tool",
                        lambda name, **kw: {"stocks": []})
    _patch_calendar(monkeypatch, trading=True)

    pool = _facts.candidate_pool(strategy="post_market", sources="hot,search",
                                 queries="测试", date="2026-10-21")
    assert pool["pool_size"] == 0
    assert pool["missing"] == [], f"真空池不应有缺口条目，实际: {pool['missing']}"


# ── 4. 空池兜底交易日历 ───────────────────────────────────────────────

def _vacuum_patches(monkeypatch):
    """候选源全成功但真空的公共 mock。"""
    monkeypatch.setattr(_facts, "fetch_hot_stocks_with_reason",
                        lambda date: {"stocks": [], "hot_tags": []})
    monkeypatch.setattr(_facts, "call_tool", lambda name, **kw: {"stocks": []})


def test_empty_pool_non_trading_day_annotated(monkeypatch):
    """⑤ 非交易日空池 ⇒ missing 标注「非交易日」（休市 ≠ 真实筛选结果）。"""
    _vacuum_patches(monkeypatch)
    _patch_calendar(monkeypatch, trading=False)

    pool = _facts.candidate_pool(strategy="post_market", sources="hot,search",
                                 queries="测试", date="2026-10-21")
    assert pool["pool_size"] == 0
    assert any("非交易日" in str(m) for m in pool["missing"]), pool["missing"]


def test_empty_pool_calendar_unavailable_annotated(monkeypatch):
    """⑤ 日历不可确认 ⇒ missing 标注（不允许把无法核实的天空池当真空交付）。"""
    _vacuum_patches(monkeypatch)
    _patch_calendar(monkeypatch, raises=True)

    pool = _facts.candidate_pool(strategy="post_market", sources="hot,search",
                                 queries="测试", date="2026-10-21")
    assert pool["pool_size"] == 0
    assert any("日历不可确认" in str(m) for m in pool["missing"]), pool["missing"]


def test_unknown_board_sources_rejected(monkeypatch):
    """⑥ 已删除的连板/龙回头源不得再进池：记入 missing 未知候选源。"""
    monkeypatch.setattr(_facts, "fetch_hot_stocks_with_reason",
                        lambda date: {"stocks": [], "hot_tags": []})
    monkeypatch.setattr(_facts, "call_tool", lambda name, **kw: {"stocks": []})
    monkeypatch.setattr(_facts, "fetch_hot_sectors",
                        lambda *a, **k: {"data": {"industry": [], "concept": []}})
    monkeypatch.setattr(_facts, "fetch_kline", lambda *a, **k: [])
    _patch_calendar(monkeypatch, trading=True)

    pool = _facts.candidate_pool(strategy="post_market", sources="zt,dragon,hot",
                                 queries=None, date="2026-10-21")
    assert pool["sources_used"] == ["hot"]
    assert any("未知候选源" in str(m) for m in pool["missing"]), pool["missing"]
    assert not any(k in pool for k in ("continuous_board", "dragon_pullback", "eod_zt"))


def test_multi_source_pool_not_eastmoney_only(monkeypatch):
    """⑦ 候选池必须多源并进：flow/sector/scan 各贡献票，search 不是唯一通道。"""
    monkeypatch.setattr(_facts, "fetch_hot_stocks_with_reason",
                        lambda date: {"stocks": [], "hot_tags": []})

    def fake_call(name, **kw):
        if name == "search_stocks":
            return {"stocks": [{"code": "600001", "name": "搜票", "change_pct": 1.0}]}
        if name == "get_hot_rank":
            return {"stocks": [{"code": "600002", "name": "人气票", "change_pct": 2.0}]}
        if name == "get_sector_stocks":
            return {"data": [{"code": "600003", "name": "板块票", "change_pct": 3.0}]}
        return {"stocks": []}

    monkeypatch.setattr(_facts, "call_tool", fake_call)
    monkeypatch.setattr(_facts, "fetch_hot_sectors",
                        lambda *a, **k: {"data": {"industry": [{"name": "测试板块"}],
                                                   "concept": []}})
    # 结构扫描宇宙里塞一只结构合格票
    bars = [{"open": 10, "high": 10.2, "low": 9.8, "close": 10.1, "volume": 100}
            for _ in range(30)]
    bars[-1] = {"open": 10, "high": 10.3, "low": 9.9, "close": 10.15, "volume": 120}

    def fake_kline(code, days=60):
        return bars if code == "600004" else []

    monkeypatch.setattr(_facts, "fetch_kline", fake_kline)

    import app.utils.basicinfo_db as bdb
    monkeypatch.setattr(bdb, "get_stock_basic_db",
                        lambda: type("B", (), {
                            "market_all_codes": staticmethod(lambda status=None: ["600004"])
                        })())
    _patch_calendar(monkeypatch, trading=True)

    pool = _facts.candidate_pool(strategy="post_market",
                                 sources="hot,search,flow,sector,scan",
                                 queries="测试", date="2026-10-21",
                                 fill_quote=False)
    codes = {c["code"] for c in pool["candidates"]}
    assert "600001" in codes, "search 应贡献候选"
    assert "600002" in codes, "flow/人气榜应贡献候选"
    assert "600003" in codes, "sector/板块应贡献候选"
    assert "600004" in codes, "scan/结构扫描应贡献候选"
    srcs = {c.get("source") for c in pool["candidates"]}
    assert any("人气" in (s or "") for s in srcs) and any("板块" in (s or "") for s in srcs)


def test_rule_prefilter_excludes_inactive_and_new():
    """⑧ 代码规则初筛：剔不活跃/新股，全量保留合格票（不硬截 top-N）。"""
    _helpers = import_module("app.agent.skills.market-screener._helpers")
    from datetime import datetime, timedelta
    old = (datetime.now() - timedelta(days=300)).strftime("%Y-%m-%d")
    new = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
    cands = [
        {"code": "600001", "name": "正常", "turnover_pct": 3.0, "list_date": old},
        {"code": "600002", "name": "N新股", "turnover_pct": 5.0, "list_date": new},
        {"code": "600003", "name": "C次新", "turnover_pct": 5.0, "list_date": new},
        {"code": "600004", "name": "不活跃", "turnover_pct": 0.4, "list_date": old},
        {"code": "600005", "name": "次新日期", "turnover_pct": 3.0, "list_date": new},
        {"code": "600006", "name": "停牌", "turnover_pct": 3.0, "suspended": True},
    ]
    kept, stats = _helpers.rule_prefilter(cands)
    codes = [c["code"] for c in kept]
    assert codes == ["600001"], f"只留活跃老股，实际{codes} stats={stats}"
    assert stats["new_stock"] == 3 and stats["inactive"] == 2
    assert stats["total_in"] == 6 and stats["total_kept"] == 1


def test_code_narrow_cuts_to_40_with_tiers():
    """⑨ 代码收窄：400+ → ≤40；买不进/极端追高降权，题材+结构优先。"""
    _helpers = import_module("app.agent.skills.market-screener._helpers")
    cands = []
    # 20 只题材+健康换手+非追高
    for i in range(20):
        cands.append({"code": f"6{i:05d}", "name": f"题材{i}", "turnover_pct": 5.0,
                      "change_pct": 1.0, "source": "热点题材+条件搜索", "reason": "机器人"})
    # 5 只封板（应被剔）
    for i in range(5):
        cands.append({"code": f"60{i:04d}", "name": f"封板{i}", "turnover_pct": 5.0,
                      "change_pct": 10.0, "source": "热点题材", "limit_up": True,
                      "reason": "机器人"})
    # 30 只健康换手
    for i in range(30):
        cands.append({"code": f"61{i:04d}", "name": f"活跃{i}", "turnover_pct": 4.0,
                      "change_pct": 1.5, "source": "条件搜索"})
    # 400 只低信息
    for i in range(400):
        cands.append({"code": f"62{i:04d}", "name": f"杂票{i}", "turnover_pct": 0.2,
                      "change_pct": 0.5, "source": "结构扫描"})
    kept, stats = _helpers.code_narrow(cands, main_themes=[("机器人", 5)], max_n=40)
    assert len(kept) == 40, f"必须收到 40，实际{len(kept)}"
    assert stats["unbuyable"] == 5, f"封板应剔，实际{stats}"
    assert stats["total_in"] == 455
    names = [k.get("name") for k in kept]
    assert not any(n and n.startswith("封板") for n in names), "封板不得进 40"
    assert all("题材" in (k.get("name") or "") for k in kept[:20]), \
        f"tier0 题材应排最前，实际{names[:5]}"


def test_enforce_buy_quality_theme_or_space_and_board_cap():
    """⑩ 质量闸：题材或 sr_gap≥8%；同板最多 2；双确认优先。"""
    _helpers = import_module("app.agent.skills.market-screener._helpers")
    profiles = [
        # 双确认 + 结构
        {"code": "600001", "theme": {"hit": True},
         "tech": {"sr_gap_pct": 12.0, "dist_ma20_pct": 2.0, "ma_bull_stack": True}},
        # 仅空间 + 结构（站上 MA20）
        {"code": "600002", "theme": {"hit": False},
         "tech": {"sr_gap_pct": 10.0, "dist_ma20_pct": 1.5, "ret_20d_pct": 5.0}},
        # 仅题材
        {"code": "300001", "theme": {"hit": True},
         "tech": {"sr_gap_pct": 3.0, "dist_ma20_pct": -2.0}},
        # 无题材无空间 → 剔
        {"code": "300002", "theme": {"hit": False},
         "tech": {"sr_gap_pct": 4.0, "dist_ma20_pct": 0.0, "ret_20d_pct": 1.0}},
        # 超跌反弹：距 MA20 -8% 且 20 日 -20% → 即使 gap 大也不算趋势空间
        {"code": "600003", "theme": {"hit": False},
         "tech": {"sr_gap_pct": 55.0, "dist_ma20_pct": -8.0, "ret_20d_pct": -22.0}},
        # 创业板后续 → 板性上限 2
        {"code": "301001", "theme": {"hit": True},
         "tech": {"sr_gap_pct": 15.0, "dist_ma20_pct": 2.0, "ma_bull_stack": True}},
        {"code": "301002", "theme": {"hit": True},
         "tech": {"sr_gap_pct": 11.0, "dist_ma20_pct": 2.0, "ma_bull_stack": True}},
        {"code": "301003", "theme": {"hit": True},
         "tech": {"sr_gap_pct": 9.0, "dist_ma20_pct": 2.0, "ma_bull_stack": True}},
        {"code": "000001", "theme": {"hit": True},
         "tech": {"sr_gap_pct": 20.0, "dist_ma20_pct": 3.0, "ma_bull_stack": True}},
    ]
    kept, stats = _helpers.enforce_buy_quality(
        profiles, main_themes=[], max_n=12, max_per_board=2, min_sr_gap_pct=8.0)
    codes = [p["code"] for p in kept]
    assert "300002" not in codes, "无题材无空间应剔"
    assert "600003" not in codes, "超跌反弹不得靠大 gap 过关"
    assert stats["oversold_bounce"] == 1
    assert stats["no_theme_no_space"] == 1
    cyb = [c for c in codes if c.startswith(("300", "301"))]
    assert len(cyb) <= 2, f"创业板应≤2且不回填，实际{cyb}"
    assert codes[0] == "000001", f"空间最大的双确认应最前，实际{codes}"
