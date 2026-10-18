# -*- coding: utf-8 -*-
"""tools 接线链 G1/G2/G6/G7②/G5 守卫（2026-10-14，来自 `tools接线审核_20261014.md`）。

这些用例**先红后绿**：写的时候对应缺陷都还在代码里，改完才转绿。
每条都标注了它钉的是报告里的哪一项，以及为什么这条规则值得钉死。

  G1  call_capability 的「让位」判据必须与注册期/R0 同口径（同名 + 近重名），
      否则被让位的能力能绕过工具层直接生效 —— 违反「工具层 > 能力层」裁定。
  G2  lint R4 的中间分支（remain<MIN_FACE 但未达 R4b）必须**留痕**，
      行为可以不变，但"发现了零相关却什么都不记"会让观测与行为脱钩。
  G6  parse_selection 兜底的裸子串匹配要加词边界（前缀误捞）。
  G7② call_capability 返回的标注键 name 不得被能力返回值覆盖（留痕失真）。
  G5  成本闸词表与域词典必须**单源派生**，否则同一个决策两个事实源。

★ 反直觉提醒（本仓第三次同类）：`_shadows_domain_tool` 返回 "(generic)" 也表示
  "应让位"。判空要用 truthiness，不要只比对真实工具名。
"""
from __future__ import annotations

import types

import pytest

from app.agent.capabilities import loader as _loader
from app.agent.preselect.tool_preselect import (
    DATA_DOMAINS,
    _DEFAULT_HINTS,
    lint_selection,
    parse_selection,
)
from app.agent.tools import capability_tools as _ct
from app.agent.tools._base import ToolProvider


# ────────────────────────────────────────────────────────────── G1 / G7② 工装


class _FakeProvider:
    """最小 ToolProvider 替身：够 `call_capability` 与 `_shadows_domain_tool` 用。"""

    def __init__(self, by_domain: dict):
        self._by = dict(by_domain)
        self._all = {n for names in self._by.values() for n in names}

    def __contains__(self, name: str) -> bool:
        return name in self._all

    def list_by_domain(self, domain: str):
        return list(self._by.get(domain, ()))

    def get_domains(self):
        return [d for d in self._by if d != "common"]


@pytest.fixture
def cap_env(monkeypatch):
    """把 call_capability 的三处外部依赖换成替身，返回登记假能力的钩子。"""
    admitted: list = []
    modules: dict = {}

    monkeypatch.setattr(ToolProvider, "get_default", lambda *_a, **_k: cap_env.provider)
    monkeypatch.setattr(_loader, "load_admitted", lambda *_a, **_k: list(admitted))
    monkeypatch.setattr(_loader, "_wrap_guards", lambda fn, *_a, **_k: fn)

    import app.agent.modpath as _modpath
    monkeypatch.setattr(
        _modpath, "import_module", lambda path: types.SimpleNamespace(**modules.get(path, {})))

    def register(name: str, fn, provider: _FakeProvider):
        admitted.append(("fake.mod", name, 5, 4000))
        modules.setdefault("fake.mod", {})[name] = fn
        cap_env.provider = provider

    cap_env.provider = _FakeProvider({"common": []})
    cap_env.register = register
    return cap_env


# ────────────────────────────────────────────────────────────────── G1


def test_call_capability_blocks_shadowed_capability(cap_env) -> None:
    """G1【高】近重名让位项不得被 `call_capability` 直接调用。

    注册期用「同名 + `_shadows_domain_tool` 近重名」判定让位（能力**不注册**）；
    派发期原先只判同名 ⇒ 名字不同但功能近重的能力（如 get_fund_flow_daily
    对上域内 get_fund_flow）仍能绕过工具层生效，让位裁定形同虚设。
    """
    cap_env.register(
        "get_fund_flow_daily",
        lambda **kw: {"leaked": True},
        _FakeProvider({"common": ["get_fund_flow"]}),
    )
    out = _ct.call_capability("get_fund_flow_daily", {})
    assert "error" in out, (
        f"让位能力被放行执行了（原样返回 {out}）—— 派发期判据仍是「仅同名」，"
        f"与注册期/R0 的「同名 + 近重名」不同口径，违反「工具层 > 能力层」")
    assert "get_fund_flow" in out["error"], (
        f"拦截信息应指回工具层同名/近重名工具，便于模型改用域内工具：{out['error']}")


def test_call_capability_still_allows_unrelated_capability(cap_env) -> None:
    """G1 反向：修复不得误伤——真正不重名的能力仍要能调（防过度拦截）。"""
    cap_env.register(
        "compute_sharpe_for_backtest",
        lambda **kw: {"sharpe": 1.23},
        _FakeProvider({"common": ["get_fund_flow"]}),
    )
    out = _ct.call_capability("compute_sharpe_for_backtest", {})
    assert "error" not in out, f"无关能力被误拦：{out}"
    assert out.get("sharpe") == 1.23


def test_call_capability_blocks_generic_name(cap_env) -> None:
    """G1 边界：`_shadows_domain_tool` 对通用名返回 "(generic)"，同样算让位。

    ★ 反直觉：它不是工具名，用 `if shadow` 判定即可；若写成
      `if shadow in provider` 之类就会漏掉这一档。
    """
    cap_env.register("daily", lambda **kw: {"leaked": True},
                     _FakeProvider({"common": ["get_fund_flow"]}))
    out = _ct.call_capability("daily", {})
    assert "error" in out, f"通用名能力被放行：{out}"


def test_call_capability_same_name_executes_tool_layer(cap_env) -> None:
    """F4（2026-10-17）：**同名**工具存在时，call_capability 直接执行工具层实现。

    原实现回一句「请直接调用该工具」，模型需再发一次 tool call（run10 实测绕 3 轮）。
    修法：派发层把同名工具执行掉、结果原样交给模型（工具层 > 能力层 = 用工具层那份）。
    反向护栏：标注键 name 仍不得被工具返回值覆盖（与 G7② 同口径）。
    """
    class _P(_FakeProvider):
        def get(self, nm):
            if nm == "get_fund_flow":
                return lambda **kw: {"from_tool_layer": True, "name": "覆盖名"}
            return None

    cap_env.register("get_fund_flow", lambda **kw: {"from_capability": True},
                     _P({"common": ["get_fund_flow"]}))
    out = _ct.call_capability("get_fund_flow", {})
    assert "error" not in out, f"同名工具应被代执行而非报错：{out}"
    assert out.get("from_tool_layer") is True, (
        f"同名时未执行工具层实现（实际 {out}）—— 仍让模型白跑一轮")
    assert out.get("from_capability") is None, "不得执行能力层那份"
    assert out["name"] == "get_fund_flow", "标注键 name 不得被工具返回值覆盖"


# ────────────────────────────────────────────────────────────── G7②


def test_call_capability_name_key_not_overwritten(cap_env) -> None:
    """G7②能力返回值里的 `name` 不得覆盖派发层标注的 name（留痕失真）。

    原实现 `{"name": fn_name, **result}` 的展开顺序让 result 里的 name **赢**，
    trace/追责里记到的就成了能力内部自报的名字。
    """
    cap_env.register(
        "unique_cap_xyz",
        lambda **kw: {"name": "能力内部自报的名字", "value": 42},
        _FakeProvider({"common": []}),
    )
    out = _ct.call_capability("unique_cap_xyz", {})
    assert out["name"] == "unique_cap_xyz", (
        f"标注键 name 被能力返回值覆盖成 {out['name']!r} —— 留痕/追责失真")
    assert out["value"] == 42, "其余字段应原样透传，不得因修复丢字段"


# ────────────────────────────────────────────────────────────────── G2


def test_lint_mid_branch_leaves_a_trace() -> None:
    """G2【中高】R4 中间分支（remain<MIN_FACE 但未达 R4b）必须留痕。

    构造：selected=4、受保护 2 个 ⇒ pruned=2、remain=2(<MIN_FACE=3)、
    ratio=0.5(<MISSELECT_RATIO=0.7) ⇒ 既不走 R4b 也不走裁剪，落到中间分支。
    原实现该分支什么都不做：不裁、不记、不告警 ⇒ `to_trace()` 里 pruned=[]
    会被读成"没发现零相关"，与实际（发现了但决策被丢弃）相反。
    """
    rep = lint_selection(
        query="把这份名单按拼音排个序",          # 中性：不触发任何数据域，避免 R1 补位
        selected=["sort_a", "sort_b", "sort_c", "sort_d"],
        available_names=["sort_a", "sort_b", "sort_c", "sort_d", "other_x"],
        index={"zzz_token": {"other_x"}},         # 必须非空（R4 段要求），但不命中正文
        plan_text="goal: 排序并输出名单",
        protected_names=["sort_a", "sort_b"],
        parsed=True,
    )
    assert any("R4" in w for w in rep.warnings), (
        f"R4 中间分支没有留痕（warnings={rep.warnings}）—— "
        f"发现了零相关工具却既不裁也不记，观测与行为脱钩")
    # 行为必须保持不变：中间分支是"不裁"，修复只补留痕
    assert rep.pruned == [], f"中间分支应维持不裁，实际裁了 {rep.pruned}"


# ────────────────────────────────────────────────────────────────── G6


def test_parse_selection_fallback_is_word_boundary_aware() -> None:
    """G6【低】兜底的目录逐字扫描必须按词边界匹配，不能裸子串。

    `get_fund_flow` 是 `get_fund_flow_daily` 的前缀 ⇒ 正文只提后者时两者都被捞进，
    噪声被推给下游 lint。
    """
    valid = ["get_fund_flow", "get_fund_flow_daily"]
    text = "建议用 get_fund_flow_daily 看主力资金"
    got = parse_selection(text, valid, max_tools=10)
    assert got == ["get_fund_flow_daily"], (
        f"兜底子串匹配把前缀名一起捞了：{got}（应为 ['get_fund_flow_daily']）")


def test_parse_selection_still_matches_plain_mention() -> None:
    """G6 反向：加词边界后，正文里的独立提及仍要捞得到（防过度收紧）。"""
    valid = ["get_fund_flow", "get_fund_flow_daily"]
    got = parse_selection("建议用 get_fund_flow 看资金", valid, max_tools=10)
    assert got == ["get_fund_flow"], f"独立提及捞不到了：{got}"


# ────────────────────────────────────────────────────────────────── G5


def test_gate_hints_are_derived_from_domain_dictionary() -> None:
    """G5【中低】成本闸词表必须覆盖域词典 —— 同一个决策不能有两个事实源。

    实测（2026-10-14）：域词典 129 词里有 100 个不在闸门词表里（现价/北向/收盘/
    形态/支撑/macd…），这些查询**根本进不了** plan+lint 质量路径，只吃盲兜底。
    修法是单源派生：闸门 = 通用词 ∪ 域词典，新增域词自动进闸门，不再手工并行维护。
    """
    domain_words = {w for _d, kws, _t in DATA_DOMAINS for w in kws}
    missing = sorted(domain_words - set(_DEFAULT_HINTS))
    assert not missing, (
        f"域词典里有 {len(missing)} 个词不在成本闸词表 ⇒ 这些查询走不到预选质量路径："
        f"{missing[:30]}{' …' if len(missing) > 30 else ''}")


def test_gate_hints_still_contain_generic_words() -> None:
    """G5 反向：派生后通用词（对比/统计/查一下…）仍要在，不能只剩域词。"""
    for w in ("对比", "统计", "查一下", "stock", "数据"):
        assert w in _DEFAULT_HINTS, f"通用闸门词 {w!r} 在派生后丢失了"
