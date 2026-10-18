# -*- coding: utf-8 -*-
"""skill 进预选层守卫（2026-10-17）。

背景：技能选择与工具选择原本是**两条互不知情的平行链**——
  技能 = `_prefetch._rank_skills` 关键词 top-1（qd_service）
  工具 = 预选器 LLM 从目录点名（qd_preselect，输入刻意剥掉 prefetch 注入块）
结果不是并列而是**技能被工具面过滤**：run8 实证 auto-agent 步骤 1 要
`agent_get_kline`，预选没给 ⇒ 该步骤被静默跳过；且 7 个技能里有 3 个都声明
适用「分析某只股票」，关键词 top-1 在真歧义上只能靠词频硬挑。

本文件锁六件事（对应 tmp/llm_io_content_analysis_run8_20261017.md §1.4 缺口）：

  ① 技能目录（名|场景|声明工具）进预选输入，且**排在工具目录之前**；
  ② 技能声明工具（frontmatter `tools:`，方法论合同）**加法补位**进工具选择，
     有上限、只补按需层合法名、陈旧声明 fail-loud 留痕；
  ③ 预选选出的技能经 `skill_hint` 覆盖 `_rank_skills` 的关键词 top-1，
     并**绕过关键词阈值**（阈值防的是弱关键词误注，不是显式语义点名）；
  ④ 非法/幻觉技能名 = 丢弃回落关键词（fail-open，不猜、不静默）；
  ⑤ 关停旋钮 `QD_PRESELECT_SKILL=0` ⇒ 与改造前**逐位一致**（零痕迹）；
  ⑥ 接线不变量（AST）：预选必须先于 prefetch；`_run_inner` 必须能跳过重跑
     （否则它的 reset 会清掉预选刚激活的工具）。

stand 范式沿用 tests/test_v17h_*（ToolFaceMixin + 假注册表 + 假模型），不建真
agent（免模型/网络冷启动）。
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).resolve().parents[1]
QD_AGENT = BACKEND / "app" / "agent" / "qd_agent.py"
QD_SERVICE = BACKEND / "app" / "agent" / "qd_service.py"
QD_PRESELECT = BACKEND / "app" / "agent" / "qd_preselect.py"


# ── stand 范式 ──────────────────────────────────────────────────────────

def _def(name: str) -> dict:
    return {"function": {"name": name, "description": f"[test] {name}",
                         "parameters": {"properties": {}, "type": "object"}}}


class _Reg:
    """假注册表：list_tools/get/get_function_definitions 三件。"""

    def __init__(self, tools: dict):
        self._t = tools

    def list_tools(self):
        return list(self._t)

    def get(self, n):
        return self._t.get(n)

    def get_function_definitions(self):
        return [_def(n) for n in self._t]


def _fn_tools(*names, domain: str = "finance"):
    """FnToolAdapter 实例（非 CORE 域 ⇒ 落按需层）；原生工具用 object()。"""
    from app.agent.tools._fn_adapter import FnToolAdapter

    def _f() -> str:
        """[test] 占位。"""
        return ""

    return {n: FnToolAdapter(_f, domain=domain) for n in names}


class _FakeModel:
    """记录预选请求、返回固定文本（不联网、不调真模型）。"""

    def __init__(self, text: str):
        self.text = text
        self.calls: list = []

    def query(self, messages, **kw):
        self.calls.append(messages)
        return self.text


class _FakeSkills:
    """假技能适配器：`list_skills()` 形态与 skills_adapter 一致（含 tools）。"""

    def __init__(self, items):
        self._items = list(items)

    def list_skills(self):
        return [dict(it) for it in self._items]

    def get(self, name):
        for it in self._items:
            if it.get("name") == name:
                return SimpleNamespace(name=name)
        return None

    def load_body(self, name):
        return "## 步骤\n1. 用工具取数\n2. 出结论\n"

    def get_section_headings(self, name):
        return ["步骤"]

    def list_resources(self, name):
        return []


def _stand(tools: dict, *, skills=None, response: str = "",
           on_demand=(), extra_core=()) -> object:
    """PreselectMixin + ToolFaceMixin stand（含分层台账与假模型）。"""
    from app.agent.qd_preselect import PreselectMixin
    from app.agent.qd_tool_face import ToolFaceMixin

    class _S(PreselectMixin, ToolFaceMixin):
        def _text_of(self, resp):        # 真 agent 由 mimo 基类提供
            return str(resp)

    s = _S()
    s.tool_registry = _Reg(tools)
    s.config = SimpleNamespace(
        tool_preselect=True, tool_tier_mode="tiered",
        preselect_gate=True, preselect_hints=None,
        preselect_max_tools=8, extra_core_tools=list(extra_core),
        on_demand_tools=list(on_demand), max_active_domain_tools=12)
    s._activated = []
    s._defs_by_name = {}
    s._all_defs = []
    s.skills = skills
    s.model = _FakeModel(response)
    s._sync_tool_tiers()
    return s


_SKILLS = [
    {"name": "auto-agent", "type": "markdown",
     "description": "个股资本面尽调：K线走势、筹码分布、融资融券与资金流向画像",
     "tools": ["agent_get_kline", "get_chip_distribution", "get_capital_summary"]},
    {"name": "auto-backtest", "type": "markdown",
     "description": "策略回测与参数对照", "tools": []},
]


def _plan(skill=None, tools=("get_realtime_quote",)) -> str:
    p = {"goal": "贵州茅台综合诊断", "deliverables": ["现价"], "tools": list(tools)}
    if skill is not None:
        p["skill"] = skill
    return json.dumps(p, ensure_ascii=False)


@pytest.fixture(autouse=True)
def _clean_skill_env(monkeypatch):
    """每个用例从"无技能旋钮 env"的干净态开始（默认开 ⇒ 走新路径）。"""
    for var in ("QD_PRESELECT_SKILL", "QD_PRESELECT_SKILL_TOOLS_MAX",
                "QD_PRESELECT_CATALOG_MAX", "QD_TOOL_CONNECT", "QD_ON_DEMAND_TOOLS"):
        monkeypatch.delenv(var, raising=False)
    yield


# ── ① 技能目录：形状 / 排位 / fail-open ────────────────────────────────

def test_skill_catalog_carries_declared_tools():
    """目录每行 = 技能名 | 场景 | 声明工具；声明工具必须可见（补位的依据）。"""
    from app.agent.preselect import build_skill_catalog

    txt, tmap = build_skill_catalog(_FakeSkills(_SKILLS))
    assert "【技能候选" in txt, "技能段必须有自己的标题（模型要知道这是候选集）"
    assert "auto-agent" in txt and "auto-backtest" in txt
    assert tmap["auto-agent"] == ["agent_get_kline", "get_chip_distribution",
                                 "get_capital_summary"], "声明工具映射 = 补位依据"
    for t in tmap["auto-agent"]:
        assert t in txt, f"声明工具 {t} 必须出现在目录行里（LLM 据此判方法论）"
    assert "（未声明）" in txt, "无声明工具的技能也要明示，不得留空让人猜"


def test_skill_catalog_empty_is_no_section():
    """skills 缺失/取不到 ⇒ ("", {}) ⇒ **整段不出现**（不是空标题占位）。"""
    from app.agent.preselect import build_skill_catalog

    assert build_skill_catalog(None) == ("", {})
    assert build_skill_catalog(_FakeSkills([])) == ("", {})

    def _boom(name=None):
        raise RuntimeError("技能系统不可用")

    assert build_skill_catalog(SimpleNamespace(list_skills=_boom)) == ("", {}), \
        "技能系统不可用 ≠ 预选不可用（fail-open，与 coding_hint 同哲学）"


def test_skill_section_precedes_tool_catalog():
    """用户诉求：skill 排在 tools 前面 —— 方法论先行，工具后随。"""
    tools = _fn_tools("get_realtime_quote", "technical_analysis")
    s = _stand(tools, skills=_FakeSkills(_SKILLS), response=_plan("auto-agent"))
    s._maybe_preselect_tools("分析600519")
    body = s.model.calls[0][1]["content"]
    assert body.index("【技能候选") < body.index("候选工具目录"), \
        "技能目录必须排在工具目录之前（用户裁定：skill 优先于 tool）"


# ── ② 声明工具补位：加法 / 上限 / 合法名 / 陈旧声明 ────────────────────

def test_declared_tools_merge_is_additive_and_covers_declaration(monkeypatch):
    """补位规则：只加不撤、**覆盖技能全部声明（F6：不按固定值截断）**、只补按需层合法名。"""
    from app.agent.preselect import _preselect_skill_on
    assert _preselect_skill_on() is True, "默认开（开启即生效，与 CAP_FOLD 同口径）"

    # F6：即便把补位下限基数压到 1，room 也必须覆盖「该技能声明数」⇒ 声明不截断
    monkeypatch.setenv("QD_PRESELECT_SKILL_TOOLS_MAX", "1")
    tools = _fn_tools("get_realtime_quote", "agent_get_kline",
                      "get_chip_distribution", "get_capital_summary")
    s = _stand(tools, skills=_FakeSkills(_SKILLS),
               response=_plan("auto-agent", tools=["get_realtime_quote"]))
    rep = s._maybe_preselect_tools("分析600519")
    assert rep["skill"] == "auto-agent"
    assert rep.get("skill_declared_added") == ["agent_get_kline",
                                              "get_chip_distribution",
                                              "get_capital_summary"], \
        "声明工具按声明顺序全补（F6：room=max(下限基数, 声明数)，不截断）"
    assert not rep.get("skill_declared_skipped"), \
        "声明全部装下 ⇒ 无 skipped（该键仅在非空时留痕；容量截断改由 activate_tools 的 QD_MAX_ACTIVE_TOOLS 硬闸兜底）"
    assert agent_in("get_realtime_quote", rep), "补位不得撤掉模型已点的工具"


def agent_in(tool: str, report: dict) -> bool:
    """工具是否进了最终工具面（激活或常驻）。"""
    return tool in (report.get("selected") or [])


def test_declared_stale_name_is_loud(caplog):
    """技能声明了注册表里没有的工具（frontmatter 陈旧）⇒ 留痕，不得静默 pass。"""
    import logging
    stale = [{"name": "old-skill", "type": "markdown",
              "description": "旧技能：仍写着已改名的工具",
              "tools": ["get_fund_flow_daily", "agent_get_kline"]}]
    tools = _fn_tools("agent_get_kline")
    s = _stand(tools, skills=_FakeSkills(stale), response=_plan("old-skill"))
    with caplog.at_level(logging.WARNING, logger="app.agent.qd_preselect"):
        rep = s._maybe_preselect_tools("分析600519")
    assert rep.get("skill_declared_unknown") == ["get_fund_flow_daily"], \
        "未注册名必须进 unknown（人肉/脚本可查 ⇒ frontmatter 可修）"
    assert any("未注册工具" in r.message for r in caplog.records), \
        "必须打 WARNING：静默则「技能在喊一个不存在的工具」永远发现不了"


def test_declared_core_tool_not_double_counted():
    """声明工具在**常驻层**（不在按需合法集）⇒ 既不加也不报 unknown。"""
    tools = _fn_tools("agent_get_kline")            # 按需
    tools["get_realtime_quote"] = object()          # 非 FnToolAdapter ⇒ 常驻层
    skills = [{"name": "auto-agent", "type": "markdown", "description": "个股尽调",
               "tools": ["get_realtime_quote", "agent_get_kline"]}]
    s = _stand(tools, skills=_FakeSkills(skills), response=_plan("auto-agent"))
    rep = s._maybe_preselect_tools("分析600519")
    added = rep.get("skill_declared_added") or []
    assert "get_realtime_quote" not in added, "常驻层已有，不必占激活名额"
    assert "get_realtime_quote" not in (rep.get("skill_declared_unknown") or []), \
        "已注册但在常驻层 ≠ 陈旧声明，不得误报"


# ── ③/④ skill 字段解析：四态 + 幻觉丢弃 ────────────────────────────────

def test_skill_field_parsing_four_states():
    """解析四态：逐字命中 / 幻觉丢弃 / 显式 null 不扫正文 / 字段缺失才扫正文。"""
    from app.agent.preselect import parse_plan

    valid = ["auto-agent", "auto-backtest"]
    names = ["get_realtime_quote"]
    assert parse_plan(_plan("auto-agent"), names, 8, valid_skills=valid)["skill"] \
        == "auto-agent"
    assert parse_plan(_plan("no-such-skill"), names, 8, valid_skills=valid)["skill"] == "", \
        "幻觉技能名必须丢弃（回落 _rank_skills），不得硬塞"
    explicit_null = '{"goal":"纯问答","tools":[],"skill":null}'
    assert parse_plan(explicit_null, names, 8, valid_skills=valid)["skill"] == "", \
        "显式 null 必须尊重：不得从模型自己写的 goal 里捞名字硬塞"
    body_only = '{"goal":"按 auto-agent 的方法看","tools":[],"skill":null}'
    assert parse_plan(body_only, names, 8, valid_skills=valid)["skill"] == "", \
        "显式 null 优先于正文扫描"
    missing = '{"goal":"按 auto-agent 的方法看","tools":[]}'
    assert parse_plan(missing, names, 8, valid_skills=valid)["skill"] == "auto-agent", \
        "字段整体缺失才回退正文逐字扫描（容错链，与 parse_selection 同哲学）"


def test_v9_shape_untouched_without_valid_skills():
    """不传 valid_skills ⇒ 返回字典**不含 skill 键**（v9 基线逐键对拍的依赖）。"""
    from app.agent.preselect import parse_plan
    assert "skill" not in parse_plan(_plan(), ["get_realtime_quote"], 8)


# ── ⑤ 关停旋钮：与改造前逐位一致 ───────────────────────────────────────

def test_env_off_leaves_preselect_untouched(monkeypatch):
    """QD_PRESELECT_SKILL=0 ⇒ 无技能段、无 skill、无补位（回退路径铁律）。"""
    monkeypatch.setenv("QD_PRESELECT_SKILL", "0")
    tools = _fn_tools("get_realtime_quote", "agent_get_kline")
    s = _stand(tools, skills=_FakeSkills(_SKILLS), response=_plan("auto-agent"))
    rep = s._maybe_preselect_tools("分析600519")
    body = s.model.calls[0][1]["content"]
    assert "【技能候选" not in body, "关停时目录里不得出现技能段（零痕迹）"
    assert rep["skill"] == "" and rep["skill_catalog"]["size"] == 0
    assert not rep.get("skill_declared_added")
    assert agent_in("get_realtime_quote", rep), "关停不影响工具选择本身"


# ── ⑥ skill_hint：覆盖 top-1 / 绕过阈值 / 非法回落 ─────────────────────

def _skill_block(report):
    """取 prefetch 留痕里的技能块（报告是 dict：blocks/injected_kinds/…）。"""
    for b in (report or {}).get("blocks") or []:
        if b.get("kind") == "skill":
            return b
    raise AssertionError("留痕里没有技能块")


def _service(skills):
    from app.agent.qd_service import QDAgentService
    return QDAgentService(skills=skills)


def test_hint_overrides_keyword_top1():
    """预选点名的技能覆盖关键词 top-1（三个技能撞同一触发词的解法）。"""
    skills = _FakeSkills([
        {"name": "auto-backtest", "type": "markdown",
         "description": "股票策略回测与参数对照，标的的历史表现验证", "tools": []},
        {"name": "auto-agent", "type": "markdown",
         "description": "股票资本面尽调：K线走势、筹码分布、融资融券与资金流向画像",
         "tools": ["agent_get_kline"]},
    ])
    svc = _service(skills)
    # 关键词面：两者都含"股票"，auto-backtest 排在 priority 前 ⇒ 无提示时它胜
    _, rep = svc._prefetch("分析600519 做个诊断", "s1")
    base = _skill_block(rep)
    task, rep2 = svc._prefetch("分析600519 做个诊断", "s2", skill_hint="auto-agent")
    hinted = _skill_block(rep2)
    assert hinted["name"] == "auto-agent" and hinted["hint_used"] is True
    assert "[相关技能：auto-agent]" in task, "注入的正文必须是**被选中**的技能"
    assert base is not None and base["hint_used"] is False


def test_hint_bypasses_keyword_threshold():
    """阈值（_SKILL_INJECT_MIN=2）防的是弱关键词误注；显式语义点名不属于它。"""
    skills = _FakeSkills([
        {"name": "cold-skill", "type": "markdown",
         "description": "回测框架的参数网格与样本外验证", "tools": []},
    ])
    svc = _service(skills)
    _, rep = svc._prefetch("分析600519 做个诊断", "s1")
    assert _skill_block(rep)["injected"] is False, \
        "无提示时关键词不达标 ⇒ 不注入（阈值语义不变）"
    task, rep2 = svc._prefetch("分析600519 做个诊断", "s2", skill_hint="cold-skill")
    hinted = _skill_block(rep2)
    assert hinted["injected"] is True and "[相关技能：cold-skill]" in task, \
        "显式点名 = 语义判断，证据强于阈值要防的弱关键词"


def test_invalid_hint_falls_back_to_keywords(caplog):
    """幻觉技能名 ⇒ 丢弃回落关键词选择（不猜、不静默、不注入空正文）。"""
    import logging
    skills = _FakeSkills(_SKILLS)
    svc = _service(skills)
    with caplog.at_level(logging.DEBUG, logger="app.agent.qd_service"):
        task, rep = svc._prefetch("分析600519 做个诊断", "s1",
                                  skill_hint="no-such-skill")
    blk = _skill_block(rep)
    assert blk["hint_used"] is False and blk["hint"] == "no-such-skill"
    assert any("不在目录" in r.message for r in caplog.records), "必须留痕"
    assert blk["name"] != "no-such-skill", "不得注入幻觉技能（正文里没有它）"


# ── ⑦ 接线不变量（AST）────────────────────────────────────────────────

def _func(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 里找不到 {name}")


def test_agent_skips_rerun_when_prerun():
    """`_run_inner` 必须能跳过 reset+预选 —— 否则它的 reset 清掉预选激活成果。"""
    src = QD_AGENT.read_text(encoding="utf-8")
    assert "_preselect_prerun_done()" in src, "接线被摘 ⇒ 改造静默失效（白写）"
    node = _func(QD_AGENT, "_run_inner")
    calls = {n.func.attr for n in ast.walk(node)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "_preselect_prerun_done" in calls
    assert "reset_run_activation" in calls and "_maybe_preselect_tools" in calls, \
        "跳过的**必须**是这两步（reset 与预选是不可分的：顺序反了就清掉成果）"


def test_service_runs_preselect_before_prefetch():
    """预选必须先于 prefetch：技能要当 hint 传进去，晚了就没效果。"""
    node = _func(QD_SERVICE, "_run_sync")
    pos = {}
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            fn = n.func
            nm = getattr(fn, "attr", None) or getattr(fn, "id", None)
            if nm in ("preselect_prerun", "_prefetch"):
                pos[nm] = n.lineno
    assert "preselect_prerun" in pos and "_prefetch" in pos, "两侧接线都在"
    assert pos["preselect_prerun"] < pos["_prefetch"], \
        "顺序反了 ⇒ hint 永远为空 ⇒ 功能静默失效（同类事故：shrink_catalog 白写一年）"


def test_prefetch_accepts_skill_hint():
    """`_prefetch` 必须有 skill_hint 形参且默认空（老调用方零改动）。"""
    node = _func(QD_SERVICE, "_prefetch")
    args = [a.arg for a in node.args.args]
    assert "skill_hint" in args
    defaults = {a.arg: d for a, d in zip(node.args.args[-len(node.args.defaults):],
                                         node.args.defaults)}
    assert isinstance(defaults.get("skill_hint"), ast.Constant) \
        and defaults["skill_hint"].value == "", "默认空 = 旧路径逐位不变"


def test_prerun_flag_is_one_shot():
    """一次性标记：读即清（防 run 中途异常让标记跨 run 残留）。"""
    tools = _fn_tools("get_realtime_quote")
    s = _stand(tools, skills=_FakeSkills(_SKILLS), response=_plan("auto-agent"))
    s.preselect_prerun("分析600519")
    assert getattr(s, "_preselect_prerun", False) is True
    assert s._preselect_prerun_done() is True, "第一次读 ⇒ True（跳过重跑）"
    assert s._preselect_prerun_done() is False, "读即清 ⇒ 第二次不得再 True"
    assert s._preselect_prerun_done() is False


# ── ⑧ F1：正文注入技能 ↔ 工具面同源补位（2026-10-17）────────────────────
#  run10 D1 实证：prefetch 注入 auto-stock-analyze 正文，但声明工具
#  `get_chip_distribution` 未进工具面（preselect 补位只认 planner 的 skill
#  字段，而注入走了关键词回落）⇒ 模型 call_capability 绕 3 轮。

def test_prefetch_reports_resolved_skill():
    """`_prefetch` 必须回报**实际注入**的技能名（补位的判定源）。"""
    svc = _service(_FakeSkills(_SKILLS))
    _, rep = svc._prefetch("分析600519 做个诊断", "s1", skill_hint="auto-agent")
    assert rep.get("skill_resolved") == "auto-agent", \
        "注入成功 ⇒ skill_resolved = 注入名（供 _run_sync 补声明工具）"
    assert _skill_block(rep)["injected"] is True

    svc2 = _service(_FakeSkills(_SKILLS))
    _, rep0 = svc2._prefetch("今天天气怎么样", "s2")
    assert rep0.get("skill_resolved") == "", "未注入 ⇒ 空串（不得误补）"


def test_supplement_declared_tools_adds_for_injected_skill():
    """按**实际注入的技能**补声明工具：加法、去重、受上限。"""
    tools = _fn_tools("get_realtime_quote", "agent_get_kline",
                      "get_chip_distribution", "get_capital_summary")
    s = _stand(tools, skills=_FakeSkills(_SKILLS), response=_plan("auto-agent"))
    s._activated = ["get_realtime_quote"]     # 预选已点名一个（在声明集外？不在）
    added = s.supplement_skill_declared("auto-agent")
    assert added == ["agent_get_kline", "get_chip_distribution",
                     "get_capital_summary"], \
        "声明工具全部补入（都合法、都不在已选里）"

    # 去重：extra_selected 已在面里的不重复补
    s2 = _stand(tools, skills=_FakeSkills(_SKILLS), response=_plan("auto-agent"))
    s2._activated = ["agent_get_kline"]
    added2 = s2.supplement_skill_declared(
        "auto-agent", extra_selected=["get_chip_distribution"])
    assert added2 == ["get_capital_summary"], "已激活/已强激活的不得重复补"

    # F6：下限基数压到 1 也不截断 —— room 覆盖该技能声明数
    import os
    os.environ["QD_PRESELECT_SKILL_TOOLS_MAX"] = "1"
    try:
        s3 = _stand(tools, skills=_FakeSkills(_SKILLS), response=_plan("auto-agent"))
        assert s3.supplement_skill_declared("auto-agent") == [
            "agent_get_kline", "get_chip_distribution", "get_capital_summary"], \
            "F6：声明是合同 —— room=max(下限基数, 声明数)，基数=1 也不截断声明"
    finally:
        os.environ.pop("QD_PRESELECT_SKILL_TOOLS_MAX", None)

    # 未知技能名 / 无 skills ⇒ 空
    assert s.supplement_skill_declared("no-such-skill") == []
    s_ns = _stand(tools, skills=None, response=_plan("auto-agent"))
    assert s_ns.supplement_skill_declared("auto-agent") == []


def test_run_sync_wires_supplement_declared():
    """接线不变量：`_run_sync` 必须按 skill_resolved 调 supplement_skill_declared。"""
    src = QD_SERVICE.read_text(encoding="utf-8")
    assert "supplement_skill_declared" in src, "接线被摘 ⇒ F1 静默失效"
    assert "skill_resolved" in src, "判定源必须来自 prefetch 的实际上报"
    node = _func(QD_SERVICE, "_run_sync")
    calls = {n.func.attr for n in ast.walk(node)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "supplement_skill_declared" in calls, \
        "必须在 _run_sync 里挂 agent.prefetch_force_tools 之后调用（否则补的工具未激活）"


# ── ⑨ F2 返工：时间/实体解析结果接线（2026-10-17）────────────────────────
#  根因：TimeResolver 一直把相对日词**就地标注**进 `effective_input`
#  （如「后天(2026-10-19)」），但 `_prefetch` 拼 task 时用的是**原始 message**，
#  `effective_input` 全链路无人消费 ⇒ 模型只拿原始问句自算相对日 ⇒ 幻觉（run10 H1）。
#  修法：`_prefetch` 直接用 `resolve_info["effective_input"]` 作 [用户问题] 基底。

def test_prefetch_uses_effective_input_as_user_question():
    """解析器的 effective_input（含就地时间标注）必须成为**模型看到的用户问题**。"""
    svc = _service(None)
    info = {"ran": True, "effective_input": "后天(2026-10-19)买什么股",
            "clarify": "", "entity_code": ""}
    task, rep = svc._prefetch("后天买什么股", "s1", info)
    assert task.endswith("后天(2026-10-19)买什么股"), \
        "必须先接线 effective_input —— 否则模型自算相对日 ⇒ 幻觉（H1）"
    assert not task.endswith("后天买什么股"), "原始问句不得绕过解析结果"
    assert isinstance(rep, dict)


def test_prefetch_falls_back_to_raw_message():
    """无 resolve_info / 无 effective_input ⇒ 回落原始 message（老路径逐位不变）。"""
    svc = _service(None)
    task, _ = svc._prefetch("后天买什么股", "s1")
    assert task.endswith("后天买什么股"), "无解析信息 ⇒ 原样透传"
    task2, _ = svc._prefetch("后天买什么股", "s1", {"ran": True, "effective_input": ""})
    assert task2.endswith("后天买什么股"), "effective_input 为空不得让用户问题变空"


def test_prefetch_reports_use_raw_message_for_matching():
    """AST 不变量：`_user_q` 只用于 [用户问题] 下发，技能匹配仍用原始 message。"""
    node = _func(QD_SERVICE, "_prefetch")
    src = ast.get_source_segment(QD_SERVICE.read_text(encoding="utf-8"), node) or ""
    assert "effective_input" in src, "必须从 resolve_info 取 effective_input"
    assert "or message" in src, "空值必须回落原始 message（fail-open）"

