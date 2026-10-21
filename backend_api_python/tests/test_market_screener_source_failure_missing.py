# -*- coding: utf-8 -*-
"""market-screener 数据源故障显性化测试（2026-10-21，修「静默塌缩成合法空结果」缺陷）。

缺陷背景：zt/dt/broken/hot 四源内部 except 静默返回 []/{"error":...}，源故障与
「真空无候选」不可区分 ⇒ 主 agent 把「数据不可用」当「今日无候选」交付空结论。

修复后契约：
  1. 任一候选源失败 ⇒ candidate_pool.missing 有可读条目（「<源>源失败: 原因」）
  2. dt/broken 仅经 market_state ⇒ 以 source_errors 返回，pre_screen 合并进 missing
  3. 全部候选源失败 + 空池 ⇒ run() 空结果携带非空 missing/hard_reasons
  4. 四源成功但真空（交易日）⇒ missing 无源失败条目（现有行为不回退）
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

def test_zt_source_failure_recorded_in_missing(monkeypatch):
    """① zt 源抛错 ⇒ missing 含「涨停池源失败」可读条目，池不可静默为空。"""
    def boom(date):
        raise RuntimeError("eastmoney down")

    monkeypatch.setattr(_facts, "fetch_zt_pool", boom)
    _patch_calendar(monkeypatch, trading=True)

    pool = _facts.candidate_pool(strategy="eod", sources="zt",
                                 queries=None, date="2026-10-21")
    assert pool["pool_size"] == 0
    assert any("涨停池源失败" in str(m) and "eastmoney down" in str(m)
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
                "continuous_board": [], "dragon_pullback": [], "eod_zt": [],
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
    """③ 四候选源全失败 ⇒ run() 空结果携带非空 missing/hard_reasons，
    且 missing 首条是源失败（截断 [:6] 不吞故障信息）。"""
    def boom(*a, **k):
        raise RuntimeError("source down")

    monkeypatch.setattr(_facts, "fetch_zt_pool", boom)
    monkeypatch.setattr(_facts, "scan_dragon_pullback", boom)
    monkeypatch.setattr(_facts, "fetch_hot_stocks_with_reason", boom)
    monkeypatch.setattr(_facts, "call_tool",
                        lambda name, **kw: {"error": "eastmoney search down"})
    monkeypatch.setattr(_run, "_market_state",
                        lambda: {"mood": "中性", "mood_score": 50,
                                 "source_errors": ["跌停池源失败: RuntimeError: source down"]})
    _patch_calendar(monkeypatch, trading=True)

    result = _run.run(sources="zt,dragon,hot,search", queries="测试")
    assert not result.get("analyzed"), "全源失败不可能产出候选"
    assert result["funnel"]["pool_raw"] == 0
    assert result["missing"], "missing 必须非空——数据不可用 ≠ 今日无候选"
    assert "源失败" in str(result["missing"][0]), result["missing"]
    assert result["hard_reasons"], "hard_reasons 必须非空"
    assert any("候选源失败" in str(h) for h in result["hard_reasons"]), \
        result["hard_reasons"]


# ── 3. 真空池回归（现有行为不回退） ───────────────────────────────────

def test_all_sources_ok_vacuum_on_trading_day_no_false_failure(monkeypatch):
    """④ 四源全成功但真空 + 交易日 ⇒ missing 不含源失败/日历条目（真实空候选）。"""
    monkeypatch.setattr(_facts, "fetch_zt_pool", lambda date: [])
    monkeypatch.setattr(_facts, "scan_dragon_pullback", lambda date: [])
    monkeypatch.setattr(_facts, "fetch_hot_stocks_with_reason",
                        lambda date: {"stocks": [], "hot_tags": []})
    monkeypatch.setattr(_facts, "call_tool",
                        lambda name, **kw: {"stocks": []})
    _patch_calendar(monkeypatch, trading=True)

    pool = _facts.candidate_pool(strategy="post_market", sources="zt,dragon,hot,search",
                                 queries="测试", date="2026-10-21")
    assert pool["pool_size"] == 0
    assert pool["missing"] == [], f"真空池不应有缺口条目，实际: {pool['missing']}"


# ── 4. 空池兜底交易日历 ───────────────────────────────────────────────

def _vacuum_patches(monkeypatch):
    """四源全成功但真空的公共 mock。"""
    monkeypatch.setattr(_facts, "fetch_zt_pool", lambda date: [])
    monkeypatch.setattr(_facts, "scan_dragon_pullback", lambda date: [])
    monkeypatch.setattr(_facts, "fetch_hot_stocks_with_reason",
                        lambda date: {"stocks": [], "hot_tags": []})
    monkeypatch.setattr(_facts, "call_tool", lambda name, **kw: {"stocks": []})


def test_empty_pool_non_trading_day_annotated(monkeypatch):
    """⑤ 非交易日空池 ⇒ missing 标注「非交易日」（休市 ≠ 真实筛选结果）。"""
    _vacuum_patches(monkeypatch)
    _patch_calendar(monkeypatch, trading=False)

    pool = _facts.candidate_pool(strategy="post_market", sources="zt,dragon,hot,search",
                                 queries="测试", date="2026-10-21")
    assert pool["pool_size"] == 0
    assert any("非交易日" in str(m) for m in pool["missing"]), pool["missing"]


def test_empty_pool_calendar_unavailable_annotated(monkeypatch):
    """⑤ 日历不可确认 ⇒ missing 标注（不允许把无法核实的天空池当真空交付）。"""
    _vacuum_patches(monkeypatch)
    _patch_calendar(monkeypatch, raises=True)

    pool = _facts.candidate_pool(strategy="post_market", sources="zt,dragon,hot,search",
                                 queries="测试", date="2026-10-21")
    assert pool["pool_size"] == 0
    assert any("日历不可确认" in str(m) for m in pool["missing"]), pool["missing"]
