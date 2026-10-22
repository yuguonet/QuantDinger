# -*- coding: utf-8 -*-
"""T7 group 单位 + 第四态 capability + 能力层目录折叠守卫（2026-10-17）。

对应 `tmp/t7_improvement_gaps_20261017.md` G3/G7/G8/G9 与
`tmp/t7_wiring_plan_20261017.md` P1：
  ① 组注册表三类成员（data 域静态 / coding 原生 / capability 动态）；
  ② 组级覆盖展开：单项优先、组间冲突 disabled 最严格胜出；
  ③ 第四态 capability 合法（方案 §8.3 示例第 4 行）且走按需路径；
  ④ 能力层目录折叠保 P14 不变量（代表名可见 + 检索指针可行动），
     env 可整体回退到全列格式。
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).resolve().parents[1]


# ── stand 范式（与 v17h / v6 同款）────────────────────────────────────

class _Reg:
    def __init__(self, tools: dict):
        self._t = tools

    def list_tools(self):
        return list(self._t)

    def get(self, n):
        return self._t[n]

    def get_function_definitions(self):
        return [{"function": {"name": n, "description": f"[test] {n}",
                              "parameters": {"properties": {}, "type": "object"}}}
                for n in self._t]


def _fn_tools(*specs):
    from app.agent.tools._fn_adapter import FnToolAdapter

    def _f() -> str:
        """[test] 占位。"""
        return ""

    out = {}
    for name, dom in specs:
        out[name] = object() if dom is None else FnToolAdapter(_f, domain=dom)
    return out


def _face_stand(tools: dict, *, on_demand=(), extra_core=()) -> object:
    from app.agent.qd_tool_face import ToolFaceMixin

    class _S(ToolFaceMixin):
        pass

    s = _S()
    s.tool_registry = _Reg(tools)
    s.config = SimpleNamespace(
        on_demand_tools=list(on_demand), extra_core_tools=list(extra_core),
        tool_tier_mode="tiered", max_active_domain_tools=12)
    s._activated = []
    return s


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("QD_TOOL_CONNECT", "QD_ON_DEMAND_TOOLS", "QD_CAPABILITIES",
                "QD_PRESELECT_CAP_FOLD"):
        monkeypatch.delenv(var, raising=False)
    from app.agent import qd_config
    monkeypatch.setattr(qd_config, "_TOOL_CONNECT_INVALID", set(), raising=False)
    yield


# ── ① 组注册表 ────────────────────────────────────────────────────────

def test_group_members_covers_three_kinds():
    """三类成员齐了：data 域（DATA_DOMAIN_TOOLS 派生）/ coding 原生 / capability 动态。"""
    from app.agent.preselect.tool_groups import group_members
    from app.agent.preselect import capability_domain

    reg = _Reg(_fn_tools(("get_realtime_quote", "finance"),
                         ("bash", None), ("edit", None),
                         ("cap_fn", capability_domain())))
    members = group_members(reg)
    assert members.get("quote") == ["get_realtime_quote"], members.get("quote")
    assert set(members.get("coding", [])) == {"bash", "edit"}, members.get("coding")
    assert members.get("capability") == ["cap_fn"], members.get("capability")
    # 未注册的域不出现在组表里（不能禁用不存在的东西）
    assert "dragon" not in members, members


def test_group_members_source_is_data_domain_tools():
    """★ 单一事实源：data 组成员必须从 DATA_DOMAIN_TOOLS 派生，不另维护名单。

    O2 锤实的病根就是两套域口径（_tool_domain 粗粒度 vs DATA_DOMAIN_TOOLS
    细粒度）——组表只准有一个来源。
    """
    from app.agent.preselect.tool_groups import group_members, GROUP_DESC
    from app.agent.preselect.tool_preselect import DATA_DOMAIN_TOOLS

    # 造一个"全注册"表：所有域工具 + 编码原生
    from app.agent.tools._fn_adapter import FnToolAdapter

    def _f() -> str:
        """[test] 占位。"""
        return ""

    tools = {n: FnToolAdapter(_f, domain="finance") for tools in DATA_DOMAIN_TOOLS.values()
             for n in tools}
    for n in ("bash", "edit", "read", "write", "glob", "grep", "task", "compact"):
        tools[n] = object()
    members = group_members(_Reg(tools))
    for dom, names in DATA_DOMAIN_TOOLS.items():
        assert members.get(dom) == list(names), f"组 {dom} 与 DATA_DOMAIN_TOOLS 漂移"
        assert dom in GROUP_DESC, f"组 {dom} 缺描述（检索面）"


def test_deferred_native_labels_keys_match_coding_group():
    """coding 组两个消费方（组展开用 CODING_NATIVE_TOOLS、系统提示地图用
    `_DEFERRED_NATIVE_LABELS`）键集必须一致——漂移即"组关了但地图还在报"。"""
    from app.agent.preselect.tool_groups import CODING_NATIVE_TOOLS
    from app.agent.qd_tool_face import ToolFaceMixin

    labels = ToolFaceMixin._DEFERRED_NATIVE_LABELS
    assert set(labels) == set(CODING_NATIVE_TOOLS), (
        f"漂移：labels={sorted(labels)} vs group={sorted(CODING_NATIVE_TOOLS)}")


# ── ② 组级覆盖展开 ────────────────────────────────────────────────────

def test_expand_overrides_group_to_members():
    from app.agent.preselect.tool_groups import expand_overrides

    out = expand_overrides({"coding": "disabled"},
                           {"coding": ["bash", "edit"], "quote": ["get_realtime_quote"]})
    assert out == {"bash": "disabled", "edit": "disabled"}, out


def test_per_name_wins_over_group():
    """单项条目优先于组级（显式 > 批量）。"""
    from app.agent.preselect.tool_groups import expand_overrides

    out = expand_overrides({"coding": "disabled", "edit": "resident"},
                           {"coding": ["bash", "edit"]})
    assert out.get("bash") == "disabled" and out.get("edit") == "resident", out


def test_group_conflict_strictest_wins():
    """一个工具属两个组、组间状态冲突 ⇒ disabled 最严格胜出（安全侧）。"""
    from app.agent.preselect.tool_groups import expand_overrides

    out = expand_overrides({"trend": "deferred", "volume": "disabled"},
                           {"trend": ["technical_analysis"], "volume": ["technical_analysis"]})
    assert out.get("technical_analysis") == "disabled", out


# ── ③ 第四态 capability ───────────────────────────────────────────────

def test_capability_state_is_valid():
    """第四态合法：`QD_TOOL_CONNECT="x=capability"` 不判非法、原样返回。"""
    from app.agent import qd_config

    os.environ["QD_TOOL_CONNECT"] = "x=capability"
    try:
        assert qd_config.tool_connect("x") == "capability"
        assert qd_config.tool_connect_invalid() == (), \
            "capability 是合法第四态（方案 §8.3 示例），不得记非法"
    finally:
        os.environ.pop("QD_TOOL_CONNECT", None)


def test_partition_capability_state_goes_on_demand():
    """capability 态与 deferred 同路径：注册、按需层、不占每轮 schema。"""
    s = _face_stand(_fn_tools(("meta_tool", "meta"), ("cap_tool", "finance")))
    os.environ["QD_TOOL_CONNECT"] = "cap_tool=capability"
    try:
        s._sync_tool_tiers()
    finally:
        os.environ.pop("QD_TOOL_CONNECT", None)
    assert "cap_tool" in s._on_demand_names, s._on_demand_names
    assert "cap_tool" not in s._core_names
    assert s._connect_states["cap_tool"] == "capability"


# ── ④ 能力层目录折叠（P14 不变量）─────────────────────────────────────

def _cap_defs(*names):
    return {n: {"function": {"name": n, "description": f"[test] 能力 {n}",
                             "parameters": {"properties": {}, "type": "object"}}}
            for n in names}


def test_capability_catalog_folded_keeps_p14_invariant(monkeypatch):
    """折叠 ≠ 2026-10-11 的裸指针行：组名+描述+**代表名**+可行动检索指针都在。"""
    from app.agent.preselect.tool_preselect import build_catalog_grouped

    defs = _cap_defs("cap_a", "cap_b", "cap_c", "cap_d", "cap_e", "cap_f", "cap_g", "cap_h")
    caps = sorted(defs)
    monkeypatch.setenv("QD_PRESELECT_CAP_FOLD", "1")
    text, kept = build_catalog_grouped(defs, ["get_realtime_quote"], caps, max_desc=60)
    assert "capability" in text, text
    assert "（8 个）" in text, "组计数必须在（P14：只报计数=回退，但计数仍要有）"
    for rep in caps[:6]:
        assert rep in text, f"代表名 {rep} 必须可见（P14 不可见即不可用）"
    assert "search_resources(query, kind='capability')" in text, "检索指针必须可行动"
    # 合法点名集不受折叠影响（lint/parse 仍认全量）
    assert set(kept) >= set(caps), "折叠只缩呈现，不缩合法集"


def test_capability_catalog_fold_env_off_restores_full_listing(monkeypatch):
    """QD_PRESELECT_CAP_FOLD=0 ⇒ 与原全列格式逐位一致（回退路径真实存在）。"""
    from app.agent.preselect.tool_preselect import build_catalog, build_catalog_grouped

    defs = _cap_defs("cap_a", "cap_b", "cap_c")
    caps = sorted(defs)
    monkeypatch.setenv("QD_PRESELECT_CAP_FOLD", "0")
    folded_text, _ = build_catalog_grouped(defs, [], caps, max_desc=60)
    full_text, _ = build_catalog(defs, only_names=caps, max_desc=60)
    assert folded_text.strip().endswith(full_text.strip()), (
        "关折叠后必须回到全列格式（与 build_catalog 单段输出一致）")


def test_capability_catalog_no_caps_unchanged(monkeypatch):
    """无能力层时不生成第二段（既有口径不动）。"""
    from app.agent.preselect.tool_preselect import build_catalog_grouped

    monkeypatch.setenv("QD_PRESELECT_CAP_FOLD", "1")
    defs = {"get_realtime_quote": {"function": {"name": "get_realtime_quote",
                                                "description": "行情",
                                                "parameters": {}}}}
    text, kept = build_catalog_grouped(defs, ["get_realtime_quote"], [], max_desc=60)
    assert "capability" not in text and kept == ["get_realtime_quote"], (text, kept)
