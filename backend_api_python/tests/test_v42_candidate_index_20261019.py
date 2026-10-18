# -*- coding: utf-8 -*-
"""阶段 2 守卫：统一类型化候选索引（2026-10-19）。

对应 `tmp/skill_system_rework_implementation_plan_20261018.md` §3.1/§3.2/§5.2/§14.5：
  · 技能与工具统一候选抽象（kind=skill|tool），但**契约分开**：技能候选不带工具
    schema，工具候选不带技能正文；分池排序、分池截断，禁止混排后截断。
  · 硬约束过滤：停用/验证失败技能不进候选；未注册工具不进候选；registry 不可用
    ⇒ fail-open 回既有全量技能集（不静默返回空）。
  · 身份统一：skill 候选的 id/display/version 经 skill_registry 解析（§14.5）。
  · 无匹配不强配：技能池无达标候选 ⇒ 空技能候选（调用方走通用路径）。

本阶段只建索引与排序框架，**不接线**预选主链（阶段 3 影子对照后切换）。
"""
from __future__ import annotations

import pytest

from app.agent.preselect import candidate_index as ci
from app.agent.chain import skill_registry as sr


class _FakeAdapter:
    def __init__(self, infos):
        self._infos = {i["name"]: i for i in infos}

    def list_skills(self):
        return [
            {"name": i["name"], "description": i.get("description", ""),
             "tags": i.get("tags", []), "type": "markdown",
             "tools": i.get("tools", [])}
            for i in self._infos.values()
        ]

    def get(self, name):
        return self._infos.get(name)


_SKILL_INFOS = [
    {"name": "market-screener", "description": "从A股全市场筛选短中线标的，用户问今天买什么股时使用",
     "tools": ["get_fund_flow", "technical_analysis"], "tags": ["market", "screener"]},
    {"name": "auto-finance-query-stock", "description": "查询A股个股最新实时行情与涨跌幅信息",
     "tools": ["get_realtime_quote"], "tags": ["finance", "query"]},
    {"name": "auto-stock-analyze", "description": "对A股目标股票开展技术形态基本面筹码资金流综合诊断分析",
     "tools": ["agent_get_kline"], "tags": ["finance", "analyze"]},
]


class _FakeProvider:
    def __init__(self, schemas, domains=None):
        self._schemas = list(schemas)
        self._domains = dict(domains or {})

    def get_schemas(self):
        return list(self._schemas)

    def get_domain(self, name):
        return self._domains.get(name, "common")


_TOOL_SCHEMAS = [
    {"name": "get_realtime_quote", "description": "获取股票实时行情", "parameters": {"type": "object"}},
    {"name": "agent_get_kline", "description": "获取K线数据", "parameters": {"type": "object"}},
    {"name": "technical_analysis", "description": "技术指标分析", "parameters": {"type": "object"}},
]


# ═══════════════════════════════════════════════════════════════
# 一、类型化候选构建
# ═══════════════════════════════════════════════════════════════

def test_skill_candidates_typed():
    cands = ci.build_skill_candidates(_FakeAdapter(_SKILL_INFOS))
    assert len(cands) == 3
    for c in cands:
        assert c.kind == "skill"
        assert c.source == "skills_adapter"
        assert c.schema is None, "技能候选不得携带工具 schema（契约分开）"
        assert c.body_uri is None or isinstance(c.body_uri, str)
    names = {c.name for c in cands}
    assert names == {"market-screener", "auto-finance-query-stock", "auto-stock-analyze"}


def test_tool_candidates_typed():
    cands = ci.build_tool_candidates(_FakeProvider(_TOOL_SCHEMAS, {"agent_get_kline": "finance"}))
    assert len(cands) == 3
    for c in cands:
        assert c.kind == "tool"
        assert c.source == "tool_provider"
        assert c.schema is not None, "工具候选必须带 schema"
        assert c.body_uri is None, "工具候选不得带技能正文引用"
        assert not c.declared_tools, "工具候选不得携带技能声明工具"
    dom = {c.name: c.tool_domain for c in cands}
    assert dom["agent_get_kline"] == "finance"


def test_no_cross_contamination_between_pools():
    skills = ci.build_skill_candidates(_FakeAdapter(_SKILL_INFOS))
    tools = ci.build_tool_candidates(_FakeProvider(_TOOL_SCHEMAS))
    assert all(c.kind == "skill" for c in skills)
    assert all(c.kind == "tool" for c in tools)


def test_skill_candidate_carries_declared_tools_and_facets():
    cands = ci.build_skill_candidates(_FakeAdapter(_SKILL_INFOS))
    ms = next(c for c in cands if c.name == "market-screener")
    assert ms.declared_tools == ["get_fund_flow", "technical_analysis"]
    assert isinstance(ms.facets, dict)


# ═══════════════════════════════════════════════════════════════
# 二、硬约束过滤（§3.2：停用/验证失败不进候选）
# ═══════════════════════════════════════════════════════════════

def test_filter_excludes_non_active_skill(monkeypatch):
    cands = ci.build_skill_candidates(_FakeAdapter(_SKILL_INFOS))
    states = {"market-screener": "disabled", "auto-stock-analyze": "archived"}
    monkeypatch.setattr(ci, "_skill_states", lambda names: states)
    kept = ci.filter_available(cands)
    assert [c.name for c in kept] == ["auto-finance-query-stock"]


def test_filter_registry_unavailable_fail_open(monkeypatch):
    """registry 不可用 ⇒ 不静默返回空，回既有全量技能集（fail-open）。"""
    cands = ci.build_skill_candidates(_FakeAdapter(_SKILL_INFOS))
    monkeypatch.setattr(ci, "_skill_states", lambda names: None)
    kept = ci.filter_available(cands)
    assert len(kept) == 3


def test_filter_excludes_unregistered_tool():
    tools = ci.build_tool_candidates(_FakeProvider(_TOOL_SCHEMAS))
    tools[0].registered = False
    kept = ci.filter_available(tools)
    assert all(c.registered for c in kept) and len(kept) == 2


# ═══════════════════════════════════════════════════════════════
# 三、分池排序与截断（§3.1：禁止混排后截断）
# ═══════════════════════════════════════════════════════════════

def test_pools_ranked_and_capped_separately(monkeypatch):
    monkeypatch.setenv("QD_PRESELECT_SKILL_TOPK", "2")
    monkeypatch.setenv("QD_TOOL_TOPM", "2")
    skills = ci.filter_available(ci.build_skill_candidates(_FakeAdapter(_SKILL_INFOS)))
    tools = ci.filter_available(ci.build_tool_candidates(_FakeProvider(_TOOL_SCHEMAS)))
    top_s, top_t = ci.rank_pools("分析贵州茅台的技术面", skills, tools)
    assert len(top_s) <= 2 and len(top_t) <= 2
    assert all(c.kind == "skill" for c in top_s)
    assert all(c.kind == "tool" for c in top_t)
    # 技能 cap 不得吞掉工具候选（工具全集合法性不被技能截断改变）
    assert len(ci.filter_available(ci.build_tool_candidates(_FakeProvider(_TOOL_SCHEMAS)))) == 3


def test_no_skill_match_returns_empty_skill_pool(monkeypatch):
    """无达标技能 ⇒ 空技能候选，不得最低填充（§5.2.5/§14.1 无匹配不强配）。"""
    monkeypatch.setenv("QD_SKILL_MIN_SCORE", "99")
    skills = ci.filter_available(ci.build_skill_candidates(_FakeAdapter(_SKILL_INFOS)))
    top_s, _ = ci.rank_pools("今天天气怎么样", skills, [])
    assert top_s == []


def test_scores_are_deterministic():
    skills = ci.filter_available(ci.build_skill_candidates(_FakeAdapter(_SKILL_INFOS)))
    a = ci.rank_pools("查询茅台最新行情", skills, [])[0]
    b = ci.rank_pools("查询茅台最新行情", skills, [])[0]
    assert [c.name for c in a] == [c.name for c in b]
    assert [round(c.base_score, 6) for c in a] == [round(c.base_score, 6) for c in b]


def test_rank_metadata_explainable():
    skills = ci.filter_available(ci.build_skill_candidates(_FakeAdapter(_SKILL_INFOS)))
    top_s, _ = ci.rank_pools("查询茅台最新行情", skills, [])
    if top_s:
        assert isinstance(top_s[0].rank_metadata, dict)
        assert "reasons" in top_s[0].rank_metadata


# ═══════════════════════════════════════════════════════════════
# 四、身份统一（§14.5：索引消费 registry 的解析入口）
# ═══════════════════════════════════════════════════════════════

def test_index_uses_registry_identity(monkeypatch):
    monkeypatch.setattr(ci, "_active_versions",
                        lambda names: {"market-screener": "abc123"})
    cands = ci.build_skill_candidates(_FakeAdapter(_SKILL_INFOS))
    ms = next(c for c in cands if c.name == "market-screener")
    assert ms.id == "market-screener"                 # 稳定 ID
    assert ms.skill_version == "abc123"               # 版本来自 registry
    # display_name/version 统一解析入口仍以 registry 为单一真源
    assert sr.resolve_skill_id(_FakeAdapter(_SKILL_INFOS), "market-screener") == "market-screener"


def test_index_version_lookup_fail_soft(monkeypatch):
    monkeypatch.setattr(ci, "_active_versions", lambda names: None)
    cands = ci.build_skill_candidates(_FakeAdapter(_SKILL_INFOS))
    assert all(c.skill_version is None for c in cands)
