# -*- coding: utf-8 -*-
"""v52 — 统一发现层 resource_catalog 的 8 条守卫（2026-10-22，方案 §9 + v2 增补⑦⑧）。

被守对象：app/agent/tools/resource_catalog.py（list_resources / search_resources）。
  ① kind 五取值（""/all/skill/tool/capability）行为正确 + 非法值 fail-fast（P1-4）
  ② 空级联顺序：构造只 tool 命中的 query，验证级联落到第二层（skill→tool→capability）
  ③ 同名去重：tool 同名 capability 条目不列出（工具层>能力层，堵 admission 残留裂缝）
  ④ 激活纪律下沉：tool 命中进 agent._activated、source="auto"、_AUTO_ACTIVATE_MAX
    生效；重复 search 幂等不产生重复激活（P1-5）
  ⑤ 各层内部排序未被改写：对拍 provider.search 原序 / skills.list_skills priority 原序
  ⑥ 返回文本含 kind 标识 + next_action
  ⑦ 空 query 返回用法提示（fail-fast 不级联，P0-2）
  ⑧ 旧 5 动词输出快照不变（回归锁：格式关键行，防止未来误动旧口）

隔离约定：skill/capability 层数据源经 monkeypatch 控造（list_skills /
search_capabilities / list_capabilities），tool 层用真实 provider —— 级联与去重
判据不依赖 admission.json 是否在位，保证判定确定性。
"""
import inspect
import re

import pytest

from app.agent.tools import resource_catalog as _rc
from app.agent.tools.resource_catalog import list_resources, search_resources

# ── 造数与解析工具 ──────────────────────────────────────────────

_FAKE_SKILL = {"name": "fake-skill-a", "description": "普通技能描述甲",
               "tags": ["tag-a"], "type": "prompt", "tools": []}
_FAKE_CAP = {"name": "fake-cap-unique-xyz", "module": "m", "description": "能力描述"}
_FAKE_CAP_SHADOW = {"name": "list_resources", "module": "m", "description": "被工具层遮蔽的能力"}


def _patch_layers(monkeypatch, skills=None, caps=None, cap_matches=None):
    """控住 skill/capability 两层数据源（tool 层保持真实 provider）。

    【易错点·必须先建 provider 再打补丁】provider 首次 get_or_build 会扫描 tools/
    各模块的公开函数注册工具；若先 monkeypatch（如 _rc._list_skills 换成测试 lambda），
    扫描读到的是 lambda（__module__ 不属 tools 模块）⇒ 同名工具漏注册 ⇒ 同名去重
    判据失真（真实事故：本文件首跑 test③ 因先 patch 后建 provider 而 false 失败）。
    """
    _rc._provider()  # 先按真实口径构建 provider，再动模块属性
    skills = [_FAKE_SKILL] if skills is None else skills
    caps = [] if caps is None else caps
    cap_matches = [] if cap_matches is None else cap_matches
    monkeypatch.setattr(_rc, "_list_skills",
                        lambda: {"count": len(skills), "skills": skills})
    monkeypatch.setattr(_rc, "_list_capabilities",
                        lambda: {"count": len(caps), "capabilities": caps})
    monkeypatch.setattr(_rc, "_search_capabilities",
                        lambda query, count=8: {"count": len(cap_matches),
                                                "matches": cap_matches[:count]})


def _entries(text, kind):
    """按 kind 标识抽取条目名（统一格式：[kind] name(...或)— ...）。"""
    pat = r"^\[" + kind + r"\] (\S+?)(?:\(| — )"
    return re.findall(pat, text, flags=re.M)


def _tool_provider():
    return _rc._provider()


# ── ① kind 五取值 + 非法值 fail-fast（P1-4）────────────────────

def test_kind_five_values_and_invalid_fail_fast(monkeypatch):
    _patch_layers(monkeypatch, caps=[dict(_FAKE_CAP)], cap_matches=[dict(_FAKE_CAP)])
    # 单层 kind：只出该层条目
    for kind, tag in (("skill", "skill"), ("tool", "tool"), ("capability", "capability")):
        out = list_resources(kind=kind)
        names = _entries(out, tag)
        assert names, f"kind={kind} 应有该层条目:\n{out[:300]}"
        others = {"skill", "tool", "capability"} - {tag}
        for other in others:
            assert not _entries(out, other), f"kind={kind} 混入了 {other} 层"
    # "" 与 "all" 等价：三层合并（skill→tool→capability）
    for kind in ("", "all"):
        out = list_resources(kind=kind)
        assert _entries(out, "skill") and _entries(out, "tool") and _entries(out, "capability"), \
            f"kind={kind!r} 应三层都在:\n{out[:300]}"
        pos = [out.index(f"[{t}]") for t in ("skill", "tool", "capability")]
        assert pos == sorted(pos), "合并顺序应为 skill→tool→capability"
    # search_resources 同样接受五取值（kind 语义=只查该层/级联）
    out = search_resources("fake-cap-unique", kind="capability")
    assert _entries(out, "capability"), out[:200]
    # P1-4 非法值 fail-fast：返回合法值列表，白名单报错风格对齐 run_skill
    out = list_resources(kind="bogus")
    assert "未知 kind='bogus'" in out and "'skill'" in out and "'tool'" in out \
        and "'capability'" in out, out
    out2 = search_resources("x", kind="bogus")
    assert "未知 kind='bogus'" in out2, out2


# ── ② 级联顺序：只 tool 命中的 query 落到第二层 ─────────────────

def test_search_cascade_lands_on_tool_layer(monkeypatch):
    # skill 层给出一条与 query 不相关的技能；capability 层给空 ⇒ "fund" 只可能命中
    # 真实工具层（provider 里 fund 系工具名）。结果必须来自 tool 层 ⇒ 级联顺序成立。
    _patch_layers(monkeypatch, skills=[dict(_FAKE_SKILL)], caps=[])
    provider = _tool_provider()
    hit = [n for n in provider.get_tool_names() if "fund" in n]
    assert hit, "前提失效：provider 里应有 fund 系工具"
    out = search_resources("fund")
    assert _entries(out, "tool"), f"级联应落到 tool 层:\n{out[:300]}"
    assert not _entries(out, "skill") and not _entries(out, "capability")
    # 显式 kind="skill" 时只查该层，不逐级下探（单层语义）
    out2 = search_resources("fund", kind="skill")
    assert not _entries(out2, "tool"), "显式 kind=skill 不得下探到 tool 层"
    # skill 层先命中时级联停在第一层
    monkeypatch.setattr(_rc, "_list_skills",
                        lambda: {"count": 1, "skills": [dict(_FAKE_SKILL, name="fund-scan")]})
    out3 = search_resources("fund")
    assert _entries(out3, "skill") and not _entries(out3, "tool"), \
        "skill 层命中时应在第一层返回，不下探"


# ── ③ 同名去重：tool 同名 capability 不列出 ────────────────────

def test_capability_shadowed_by_tool_name_dropped(monkeypatch):
    # "list_resources" 是真实 provider 工具名 ⇒ 同名 capability 必须被丢弃；
    # 不同名的 fake-cap-unique-xyz 正常列出。
    _patch_layers(monkeypatch, caps=[dict(_FAKE_CAP_SHADOW), dict(_FAKE_CAP)])
    out = list_resources(kind="capability")
    names = _entries(out, "capability")
    assert "fake-cap-unique-xyz" in names, out[:300]
    assert "list_resources" not in names, "同名 capability 未被去重（工具层>能力层裂缝仍在）"
    # 搜索路径同样去重
    monkeypatch.setattr(_rc, "_search_capabilities",
                        lambda query, count=8: {"count": 2, "matches": [
                            dict(_FAKE_CAP_SHADOW), dict(_FAKE_CAP)]})
    out2 = search_resources("能力", kind="capability")
    names2 = _entries(out2, "capability")
    assert "list_resources" not in names2 and "fake-cap-unique-xyz" in names2, out2[:300]


# ── ④ 激活纪律下沉 + 幂等（P1-5）───────────────────────────────

class _FakeAgent:
    """qd_smoke 式假 agent：只复刻 activate_tools 的幂等语义（已激活进 already 桶）。"""

    def __init__(self, allowed):
        self.allowed = set(allowed)
        self.calls = []
        self._activated = []

    def activate_tools(self, names, source="named"):
        self.calls.append((list(names), source))
        new, already, unknown = [], [], []
        for n in names:
            if n not in self.allowed:
                unknown.append(n)
            elif n in self._activated:
                already.append(n)
            else:
                self._activated.append(n)
                new.append(n)
        return {"activated": new, "already": already, "unknown": unknown,
                "active_total": len(self._activated), "cap": 8}


def test_auto_activation_discipline_and_idempotent(monkeypatch):
    _patch_layers(monkeypatch, skills=[dict(_FAKE_SKILL)], caps=[])
    provider = _tool_provider()
    matched = [n for n, _ in provider.search("fund")][:8]
    assert matched, "前提失效：query=fund 应命中工具"
    agent = _FakeAgent(matched)
    ctx = {"agent": agent}

    out = search_resources("fund", _context=ctx)
    assert agent.calls and agent.calls[0][1] == "auto", f"激活来源应为 auto: {agent.calls}"
    assert set(matched[:_rc._AUTO_ACTIVATE_MAX]) == set(agent.calls[0][0]), \
        f"激活名单应与命中前 N 一致: {agent.calls[0][0]} vs {matched}"
    assert set(matched[:_rc._AUTO_ACTIVATE_MAX]) <= set(agent._activated), \
        f"命中工具应进入 agent._activated: {agent._activated}"
    first_snapshot = list(agent._activated)

    # P1-5：重复 search 不产生重复激活（activate_tools 幂等，上下文不膨胀）
    search_resources("fund", _context=ctx)
    search_resources("fund", _context=ctx)
    assert agent._activated == first_snapshot, \
        f"重复 search 产生了重复激活/上下文膨胀: {agent._activated}"
    assert all(c[0] == agent.calls[0][0] for c in agent.calls), "重复 search 激活名单应一致"

    # _AUTO_ACTIVATE_MAX 生效：上限调到 1 ⇒ 只激活 1 个
    monkeypatch.setattr(_rc, "_AUTO_ACTIVATE_MAX", 1)
    agent2 = _FakeAgent(matched)
    search_resources("fund", _context={"agent": agent2})
    assert len(agent2.calls[0][0]) == 1, f"上限未生效: {agent2.calls[0][0]}"

    # 无 _context：退化为只列不激活（与 search_tools 行为一致）
    out3 = search_resources("fund")
    assert _entries(out3, "tool"), out3[:200]


# ── ⑤ 各层内部排序未被改写（对拍）─────────────────────────────

def test_layer_order_not_rewritten(monkeypatch):
    _patch_layers(monkeypatch, skills=[dict(_FAKE_SKILL)], caps=[])
    provider = _tool_provider()
    # tool 层：provider.search 原序（截 count）逐一对拍
    expected = [n for n, _ in provider.search("fund")]
    out = search_resources("fund", count=8)
    assert _entries(out, "tool") == expected[:8], \
        f"tool 层序被改写:\n{[_entries(out, 'tool')]}\nvs\n{expected[:8]}"
    # skill 层：真实 skills priority 降序原样保留（不加 monkeypatch）
    real = [s["name"] for s in _rc._list_skills()["skills"]]
    out2 = list_resources(kind="skill")
    assert _entries(out2, "skill") == real, "skill 层 priority 序被改写"
    # tool 层浏览（list 路径）：list_by_domain 原序截 top-N
    all_names = list(provider.list_by_domain(""))
    out3 = list_resources(kind="tool")
    assert _entries(out3, "tool") == all_names[:_rc._LAYER_TOPN], "tool 层 list_by_domain 序被改写"


# ── ⑥ 返回文本含 kind 标识 + next_action ───────────────────────

def test_output_has_kind_and_next_action(monkeypatch):
    _patch_layers(monkeypatch, skills=[dict(_FAKE_SKILL)],
                  caps=[dict(_FAKE_CAP)],
                  cap_matches=[dict(_FAKE_CAP)])
    out = list_resources(kind="")
    assert "[skill] fake-skill-a" in out and "next_action: run_skill" in out, out[:400]
    assert "next_action: activate_and_call" in out, out[:400]
    assert "[capability] fake-cap-unique-xyz" in out and \
        "next_action: call_capability" in out, out[:400]
    # 搜索路径同样带标识
    out2 = search_resources("fund")
    assert "[tool]" in out2 and "next_action" in out2, out2[:300]
    out3 = search_resources("fake-cap-unique", kind="capability")
    assert "[capability]" in out3 and "next_action: call_capability" in out3, out3[:200]


# ── ⑦ 空 query fail-fast（P0-2）────────────────────────────────

def test_empty_query_returns_usage_hint_without_cascade(monkeypatch):
    touched = []

    def _boom(*a, **kw):
        touched.append(1)
        raise AssertionError("空 query 不得级联进任何一层")

    monkeypatch.setattr(_rc, "_list_skills", _boom)
    monkeypatch.setattr(_rc, "_search_capabilities", _boom)
    monkeypatch.setattr(_rc, "_list_capabilities", _boom)
    for q in ("", "   "):
        out = search_resources(q)
        assert "用法" in out and "search_resources" in out and "list_resources" in out, \
            f"空 query 应返回用法提示: {out!r}"
        assert not _entries(out, "skill") and not _entries(out, "tool") \
            and not _entries(out, "capability")
    assert not touched, "空 query 触发了层级联（违反 P0-2 fail-fast）"
    # kind 非法值优先于空 query 判定顺序不做要求，但两者都须 fail-fast
    assert "未知 kind" in search_resources("", kind="bogus")


# ── ⑧ 旧 5 动词输出快照不变（回归锁）──────────────────────────

def test_old_five_verbs_output_snapshot_unchanged():
    """旧 5 动词各调一次，断言格式关键行不变（统一口引入不得动旧口）。

    注意：resource_catalog 注册后 provider 工具数 +2，旧口输出里的**计数**会随之
    合法变化 ⇒ 快照锁只锁**格式**（头部形状/行模板/信封键），不锁具体数字。
    """
    # 1) _list_tools：头部 + 按需域概览 + 提示尾行
    out = _rc._list_tools()
    assert re.match(r"工具清单（\d+/\d+，domain='common'）：", out), out[:120]
    assert "按需域（默认未激活，需 search_resources/activate_tools）：" in out
    assert "提示：domain='all' 可看全部；按需层工具须 activate_tools 激活后才能调用。" in out
    # 2) _search_tools：命中头部 + 条目模板（无 _context ⇒ 不激活）
    out2 = _rc._search_tools("fund", count=3)
    m = re.match(r"命中 (\d+) 个，展示 (\d+) 个，已激活 (\d+) 个：", out2)
    assert m, out2[:120]
    assert m.group(3) == "0", "无 _context 时不得声称已激活"
    assert re.search(r"^\s+\[(已激活|未激活)\] \w+\(.*\) — ", out2, flags=re.M), out2[:300]
    assert "未自动激活：请用 activate_tools 点名激活后再调用。" in out2
    # 3) _list_skills（统一口内部第二层发现动词）：信封键与条目键
    data = _rc._list_skills()
    assert set(data) == {"count", "skills"}, set(data)
    assert all({"name", "description"} <= set(s) for s in data["skills"])
    # 4/5) _list_capabilities / _search_capabilities：信封键与条目键
    data2 = _rc._list_capabilities()
    assert set(data2) == {"count", "capabilities"}, set(data2)
    data3 = _rc._search_capabilities("行情")
    assert set(data3) == {"count", "matches"}, set(data3)
    assert all({"name", "module", "description"} <= set(x) for x in data3["matches"])
    # 旧动词签名未被改动（_context 兼容契约仍在）
    assert "_context" in inspect.signature(_rc._search_tools).parameters
    assert "_context" in inspect.signature(_rc._list_tools).parameters
