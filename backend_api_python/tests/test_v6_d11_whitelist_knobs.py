# -*- coding: utf-8 -*-
"""D-11 残余：元工具名单下沉 + 执行型白名单单一真源收敛（2026-10-14）。

【背景】设计文档 D-11 记「`run_skill` 白名单硬编码，违反"规则不在代码里"」，
2026-10-13 复核已订正：原指控的那份名单已不存在（真源 = `scripts/skill_run.py`
的 `EXECUTABLE_SKILLS`，由 `qd_service._executable_skills()` 动态读取）。
**残余两处**由 O-11 打包收口，本文件把它们钉住：

  ① `chain/skill_brewer._META_TOOLS`：写盘前剔除的"元工具"名单是**硬编码集合**。
     工程红线（阈值/口径走 env 或配置层）⇒ 下沉为 `QD_META_TOOLS`，默认沿用原 6 个。
  ② `qd_service._executable_skills()`：用 `spec_from_file_location` 按文件路径
     加载 skill_run，会造出**第二个模块实例**（`_qd_skill_run_wl`）—— 与 P5 根治的
     "双模块实例"是同一类隐患。P5 后 `app/agent` 已不进 sys.path、全包统一
     `app.agent.*`，包内相对导入即可，不再需要按路径加载这套机械。

【为什么 ① 不能"推导"出来而只能配置】
元工具 = "查工具的工具"（list_tools/search_tools）与沙箱内置（final_answer /
python_interpreter），它们不由 `ToolProvider` 注册 ⇒ `provider.get_meta_names()`
只覆盖 format_result/web_search 这类，**盖不全**。所以只能显式配置，不能假装推导。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from app.agent import qd_service as qs
from app.agent.chain import skill_brewer as sb

DEFAULT_META = {"list_tools", "search_tools", "format_result",
                "final_answer", "python_interpreter", "web_search"}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("QD_META_TOOLS", raising=False)
    yield


# ── ① 元工具名单走 env ────────────────────────────────────────
def test_meta_tools_default_keeps_original_six():
    """默认必须等于原硬编码的那 6 个 —— 下沉不是改名，别悄悄换口径。"""
    assert sb._meta_tools() == DEFAULT_META, (
        f"默认元工具名单变了：{sorted(sb._meta_tools() ^ DEFAULT_META)}")


def test_meta_tools_is_read_from_env(monkeypatch):
    monkeypatch.setenv("QD_META_TOOLS", "aaa,bbb")
    assert sb._meta_tools() == {"aaa", "bbb"}


def test_meta_tools_env_drops_empty_tokens(monkeypatch):
    """脏 token（空串/纯空格）不能落进名单，否则会误伤同名空串工具。"""
    monkeypatch.setenv("QD_META_TOOLS", "aaa,, ,bbb,  ")
    assert sb._meta_tools() == {"aaa", "bbb"}


@pytest.mark.parametrize("dirty", ["", "   ", ",,", " , "])
def test_meta_tools_empty_env_falls_back_to_default(monkeypatch, dirty):
    """空 env = 未配置 ⇒ 回落默认（不是"空名单"，空名单会让元工具写进白名单）。"""
    monkeypatch.setenv("QD_META_TOOLS", dirty)
    assert sb._meta_tools() == DEFAULT_META


class _FakeProvider:
    def __init__(self, names):
        self._names = list(names)

    def get_tool_names(self):
        return self._names


_SKMD = """---
name: demo
description: d
tools: [get_stock_quote, list_tools, bogus_tool]
---
正文
"""


def test_validate_tools_strips_meta_and_fabricated():
    """行为守卫：元工具 + 编造名都被剔除，真实工具留下。"""
    out = sb._validate_tools(_SKMD, _FakeProvider(
        ["get_stock_quote", "list_tools", "search_tools"]))
    assert "tools: [get_stock_quote]" in out, f"应只留真实工具：{out!r}"
    assert "list_tools" not in out, "元工具必须剔除（写进白名单只是噪音）"
    assert "bogus_tool" not in out, "编造名必须剔除（执行期必踩坑）"


def test_validate_tools_honors_env_override(monkeypatch):
    """env 改了名单，剔除行为必须跟着变（否则 env 是摆设）。"""
    monkeypatch.setenv("QD_META_TOOLS", "get_stock_quote")
    out = sb._validate_tools(_SKMD, _FakeProvider(["get_stock_quote", "list_tools"]))
    assert "get_stock_quote" not in out, "被 env 指定为元工具后必须剔除"
    assert "list_tools" in out, "不再是元工具且已注册 ⇒ 保留"


def test_validate_tools_provider_none_is_noop():
    """provider 缺失时原样返回（原有 fail-open 行为不变）。"""
    assert sb._validate_tools(_SKMD, None) == _SKMD


# ── ② 白名单单一真源：包内相对导入，不造第二个模块实例 ──────────
@pytest.fixture()
def _reset_cache():
    qs._EXECUTABLE_SKILLS_CACHE = None
    # 同时清掉包内模块：否则别的用例先 import 过，本用例就变成"恒真"的空守卫
    sys.modules.pop("app.agent.scripts.skill_run", None)
    yield
    qs._EXECUTABLE_SKILLS_CACHE = None
    sys.modules.pop("app.agent.scripts.skill_run", None)


def test_executable_skills_matches_single_source(_reset_cache):
    from app.agent.scripts.skill_run import EXECUTABLE_SKILLS as WL
    got = qs._executable_skills()
    assert got == dict(WL), "取到的白名单必须等于真源（不是抄一份）"
    assert "market-screener" in got and "strategy-debug" in got


def test_executable_skills_creates_no_duplicate_module(_reset_cache):
    """按文件路径加载会在 sys.modules 里塞一个影子模块 ⇒ 双实例隐患。"""
    qs._executable_skills()
    # ⚠ 必须按**末段模块名**精确匹配，不能用 `"skill_run" in k` 子串：
    #   全量跑时 sys.modules 里有 `tests.test_skill_run_exits` 这个测试模块，
    #   子串匹配会把它当成影子模块 ⇒ 假红（本用例单独跑是绿的，全量才红）。
    shadows = [k for k in sys.modules
               if (k == "skill_run" or k.endswith(".skill_run")) and not k.startswith("app.")]
    assert not shadows, f"出现影子模块实例（应按包路径导入）: {shadows}"
    assert "app.agent.scripts.skill_run" in sys.modules, "应走包内相对导入"


def test_no_spec_from_file_location_in_qd_service():
    """源码守卫：不许回退到按文件路径加载。

    ⚠ 必须是 **AST 找调用**，不能用 `in src` 全文匹配：本节 docstring 里
    恰好写了 "spec_from_file_location" 这几个字（解释为什么废弃它）⇒
    全文匹配会恒红。教训同 P3 的 c3p 守卫（全文匹配会被注释骗成假绿/假红）。
    """
    tree = ast.parse(Path(qs.__file__).read_text(encoding="utf-8"))
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and fn.attr == "spec_from_file_location":
            bad.append(node.lineno)
        elif isinstance(fn, ast.Name) and fn.id == "spec_from_file_location":
            bad.append(node.lineno)
    assert not bad, (
        f"qd_service.py 第 {bad} 行又在按文件路径加载模块（spec_from_file_location）—— "
        "P5 后包内相对导入即可，按路径加载会造第二个模块实例（D-11 回归）")


# ── ③ 显示名 → 目录名归一化（2026-10-14）────────────────────────
# 背景：`_prefetch` 用 list_skills() 的**显示名**直查 EXECUTABLE_SKILLS（键是
# **目录名**）。历史遗留里显示名（连字符，market-screener）与目录名（下划线，
# market_screener）不同口径 ⇒ 直查恒 None ⇒ 执行型技能永远进不了 `_dispatch_card`
# 分支，退化成灌 2529 字方法论正文（实测）。修复：查白名单前先归一化（复用
# adapter 的 display→name 映射，与 skill_tools.run_skill 同口径）。
# 2026-10-14 kebab 改名后显示名/目录名统一为连字符，归一化退化为幂等，但仍需
# 保留（存量 DB/旧技能可能残留下划线显示名）。
class _FakeSkillInfo:
    def __init__(self, name, display):
        self.name = name
        self.display_name = display


class _FakeSkillsNames:
    """最小 skills 桩：只需 get() 返回带 .name 的对象（list_skills 用不上）。"""

    def __init__(self, mapping):
        self._infos = {d: _FakeSkillInfo(c, d) for d, c in mapping.items()}

    def get(self, name):
        return self._infos.get(name)


def test_canonical_skill_name_maps_display_to_dirname():
    fk = _FakeSkillsNames({"market-screener": "market_screener",
                           "strategy-debug": "strategy_debug"})
    assert qs._canonical_skill_name(fk, "market-screener") == "market_screener"
    assert qs._canonical_skill_name(fk, "strategy-debug") == "strategy_debug"


def test_canonical_skill_name_unknown_returns_input():
    """取不到（非技能名/无映射）⇒ 原样返回（fail-open，不抛）。"""
    fk = _FakeSkillsNames({"market-screener": "market_screener"})
    assert qs._canonical_skill_name(fk, "no-such-skill") == "no-such-skill"


def test_canonical_skill_name_tolerates_none_info():
    class _NoneSkills:
        def get(self, name):
            return None

    assert qs._canonical_skill_name(_NoneSkills(), "x") == "x"


def test_executable_lookup_hits_after_normalization():
    """核心回归：白名单键与显示名同口径 kebab；归一化幂等且必命中。

    改名（2026-10-14 kebab）后键==显示名，原"直查必 miss"前提已失效；本守卫改为
    钉住：①白名单键就是连字符口径；②归一化后的名字仍是白名单里的有效键。
    """
    wl = qs._executable_skills()
    assert "market-screener" in wl and "strategy-debug" in wl, (
        "EXECUTABLE_SKILLS 键应为连字符口径（与显示名/目录名统一）")
    fk = _FakeSkillsNames({"market-screener": "market-screener"})
    canon = qs._canonical_skill_name(fk, "market-screener")
    assert canon == "market-screener", "归一化应幂等（显示名==目录名）"
    assert wl.get(canon) is not None, (
        "归一化后未命中 EXECUTABLE_SKILLS ⇒ _prefetch 会退化为灌正文（分发卡片功能失效）")


# ── ④ 技能卡片 ↔ 工具激活解耦（2026-10-14）────────────────────────
# 回归背景：prefetch 注入执行型技能时，分发卡片点名 `run_skill`，但 `run_skill`
# 是**按需工具**（QD_ON_DEMAND_TOOLS 默认含它，见 qd_config），而工具预选看的是
# **剥掉注入后的用户原始问题**（qd_preselect._preselect_query）⇒ 预选看不到技能、
# 不会点 run_skill ⇒ 模型实测白花一轮 `search_tools` 才发现（trace step1 第 4 个
# tool_call）。修复：`_prefetch` 把卡片点名的工具写进 `report["skill_force_tools"]`，
# 由 agent 在预选后激活（见 qd_agent._run_inner）。
class _FakeSkillsForPrefetch:
    """`_prefetch` 技能分支所需的最小 skills 桩（比 _FakeSkillsNames 多几个方法）。"""

    def __init__(self, cands, mapping, bodies=None):
        self._cands = list(cands)
        self._infos = {d: _FakeSkillInfo(c, d) for d, c in mapping.items()}
        self._bodies = dict(bodies or {})

    def list_skills(self):
        return list(self._cands)

    def get(self, name):
        return self._infos.get(name)

    def load_body(self, name):
        return self._bodies.get(name, "")

    def get_section_headings(self, name):
        return []

    def list_resources(self, name):
        return []


def _prefetch_report(monkeypatch, svc, message="明天买什么股", session="s"):
    import app.agent.utils.case_memory as _cm
    monkeypatch.setattr(_cm, "case_memory_enabled", lambda: False)
    _, report = svc._prefetch(message, session, None)
    return report


def test_prefetch_dispatch_card_forces_run_skill(monkeypatch):
    skills = _FakeSkillsForPrefetch(
        [{"name": "market-screener", "description": "全市场选股"}],
        {"market-screener": "market_screener"})
    monkeypatch.setattr(qs, "_rank_skills", lambda *a, **k: [(4.0, 4, "market-screener")])
    monkeypatch.setattr(qs, "_executable_skills",
                        lambda: {"market_screener": ("m", ("run",), "run")})
    svc = qs.QDAgentService(skills=skills)
    report = _prefetch_report(monkeypatch, svc)
    assert report["skill_force_tools"] == ["run_skill"], (
        "执行型技能注入分发卡片后，必须把卡片点名的 run_skill 传给 agent 激活")


def test_prefetch_non_executable_skill_does_not_force(monkeypatch):
    skills = _FakeSkillsForPrefetch(
        [{"name": "auto-agent", "description": "通用"}],
        {"auto-agent": "auto_agent"},
        bodies={"auto-agent": "正文"})
    monkeypatch.setattr(qs, "_rank_skills", lambda *a, **k: [(3.0, 3, "auto-agent")])
    monkeypatch.setattr(qs, "_executable_skills", lambda: {})
    svc = qs.QDAgentService(skills=skills)
    report = _prefetch_report(monkeypatch, svc)
    assert report["skill_force_tools"] == [], (
        "知识型技能注正文（不派 run_skill）⇒ 不应强激活 run_skill")


def test_prefetch_pointer_hit_also_forces_run_skill(monkeypatch):
    skills = _FakeSkillsForPrefetch(
        [{"name": "market-screener", "description": "全市场选股"}],
        {"market-screener": "market_screener"})
    monkeypatch.setattr(qs, "_rank_skills", lambda *a, **k: [(4.0, 4, "market-screener")])
    monkeypatch.setattr(qs, "_executable_skills",
                        lambda: {"market_screener": ("m", ("run",), "run")})
    svc = qs.QDAgentService(skills=skills)
    _prefetch_report(monkeypatch, svc)                    # 首次：注入分发卡片
    report2 = _prefetch_report(monkeypatch, svc)          # 二次：命中一行指针
    assert report2["skill_force_tools"] == ["run_skill"], (
        "二次命中只注指针时，指针同样点名 run_skill ⇒ 仍需激活")


def test_run_inner_consumes_prefetch_force_tools():
    """源码守卫：agent 必须消费 `prefetch_force_tools`，否则上面的报告是断头路。"""
    from app.agent import qd_agent as qa
    src = Path(qa.__file__).read_text(encoding="utf-8")
    assert "prefetch_force_tools" in src, (
        "qd_agent._run_inner 未消费 prefetch_force_tools —— 技能卡片点名的 run_skill "
        "不会被激活（解耦修复失效）")
    assert "activate_tools(_force)" in src.replace(" ", "").replace("\t", ""), (
        "未在 _run_inner 里对 force 工具调用 activate_tools")
