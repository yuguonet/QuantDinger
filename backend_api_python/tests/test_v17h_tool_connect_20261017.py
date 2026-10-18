# -*- coding: utf-8 -*-
"""T7 接线守卫：tool_connect 状态机 + 分层派生 + 快照验收字段（2026-10-17）。

背景：`tmp/t7_improvement_gaps_20261017.md` G1–G7。T7 方案（§8.3/§8.6）要求
tool_connect 三态开关是**被消费的**配置底座；改造前它是零调用方的裸读口，
改 `QD_TOOL_CONNECT` 对工具面零效果。本文件锁四件事：

  ① 非法 state 值必须 fail-loud（ERROR + 登记），不得静默兜底成 resident；
  ② 三分组（core/on_demand/disabled）由 tool_connect 派生，disabled 工具
     从面/目录消失但**仍在注册表**（execute_action 回退可救，§8.3）；
  ③ 默认 env 下派生结果与改造前逐位一致（回归铁律）；
  ④ `tool_face_snapshot` 带 §8.6 验收字段（connect_state_counts/disabled_count/
     deferred_count/connect_invalid），口径可对账。

测试范式沿用 `tests/test_v6_qd_agent_split.py::_tier_self`（ToolFaceMixin
stand + 假注册表），不建真 agent（免模型/网络冷启动）。
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).resolve().parents[1]
QD_CONFIG = BACKEND / "app" / "agent" / "qd_config.py"
QD_TOOL_FACE = BACKEND / "app" / "agent" / "qd_tool_face.py"
TRACE_COLLECTOR = BACKEND / "app" / "agent" / "trace_collector.py"


# ── stand 范式（与 v6 分层测试同款）────────────────────────────────────

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
        return self._t[n]

    def get_function_definitions(self):
        return [_def(n) for n in self._t]


def _face_stand(tools: dict, *, on_demand=(), extra_core=()) -> object:
    """ToolFaceMixin stand：config 用 SimpleNamespace（显式口径，不读 env 默认）。"""
    from app.agent.qd_tool_face import ToolFaceMixin

    class _S(ToolFaceMixin):
        pass

    s = _S()
    s.tool_registry = _Reg(tools)
    s.config = SimpleNamespace(
        on_demand_tools=list(on_demand), extra_core_tools=list(extra_core),
        tool_tier_mode="tiered", max_active_domain_tools=12)
    s._activated = []          # 真实 agent 在 __init__ 初始化（activate/FIFO 读它）
    return s


def _fn_tools(*specs):
    """specs: (name, domain)；mimo 原生用 object()（非 FnToolAdapter）。"""
    from app.agent.tools._fn_adapter import FnToolAdapter

    def _f() -> str:
        """[test] 占位。"""
        return ""

    out = {}
    for name, dom in specs:
        out[name] = object() if dom is None else FnToolAdapter(_f, domain=dom)
    return out


@pytest.fixture(autouse=True)
def _clean_connect_env(monkeypatch):
    """每个用例从"无 T7 env"的干净态开始；invalid 登记表逐例重置（进程级 set）。"""
    for var in ("QD_TOOL_CONNECT", "QD_ON_DEMAND_TOOLS", "QD_CAPABILITIES"):
        monkeypatch.delenv(var, raising=False)
    from app.agent import qd_config
    # raising=False：属性尚未实现时让红落在**断言**上（功能缺失的红），
    # 而不是 fixture setup 报错（那是测试写坏的红，两者要分开看）。
    monkeypatch.setattr(qd_config, "_TOOL_CONNECT_INVALID", set(), raising=False)
    yield


# ── ① fail-loud（G4）──────────────────────────────────────────────────

def test_invalid_state_is_loud_not_silent(caplog):
    """非法 state 必须留痕：ERROR 日志 + invalid 登记；行为保持 resident 但不静默。"""
    from app.agent import qd_config

    monkeypatch_env = {"QD_TOOL_CONNECT": "x=defered"}
    import os
    old = os.environ.get("QD_TOOL_CONNECT")
    os.environ.update(monkeypatch_env)
    try:
        with caplog.at_level(logging.ERROR, logger="app.agent.qd_config"):
            assert qd_config.tool_connect("x") == "resident", "非法条被忽略后回落派生"
        assert any("defered" in r.message or "x=defered" in r.message
                   for r in caplog.records), "非法 state 必须打 ERROR 日志"
        assert "x=defered" in qd_config.tool_connect_invalid(), \
            "非法条必须登记进 tool_connect_invalid() 供快照/审计"
    finally:
        if old is None:
            os.environ.pop("QD_TOOL_CONNECT", None)
        else:
            os.environ["QD_TOOL_CONNECT"] = old


def test_valid_states_roundtrip(monkeypatch):
    """三态（+大小写/空格容差）正常往返。"""
    from app.agent import qd_config

    monkeypatch.setenv("QD_TOOL_CONNECT", "a=disabled; b = DEFERRED ;c=resident")
    assert qd_config.tool_connect("a") == "disabled"
    assert qd_config.tool_connect("b") == "deferred"
    assert qd_config.tool_connect("c") == "resident"


def test_default_derivation_unchanged(monkeypatch):
    """无 env 时的派生口径：on_demand 默认名单 ⇒ deferred；其余 resident。

    ★ `QD_ON_DEMAND_TOOLS=""` 必须与 `QDAgentConfig.default_factory` 同口径
    （空串 ⇒ 代码默认 8 个，不是空集）——旧读口 None-vs-"" 双语义是 G1 的病根。
    """
    from app.agent import qd_config

    assert qd_config.tool_connect("run_skill") == "deferred"
    assert qd_config.tool_connect("edit") == "deferred"
    assert qd_config.tool_connect("get_realtime_quote") == "resident"
    monkeypatch.setenv("QD_ON_DEMAND_TOOLS", "")
    assert qd_config.tool_connect("edit") == "deferred", \
        "空串 env 必须回落到代码默认名单（与 config.default_factory 一致）"
    monkeypatch.setenv("QD_ON_DEMAND_TOOLS", "get_realtime_quote")
    assert qd_config.tool_connect("get_realtime_quote") == "deferred"
    assert qd_config.tool_connect("edit") == "resident"


# ── ② 三分组派生（G1）─────────────────────────────────────────────────

def test_disabled_excluded_but_registered(monkeypatch):
    """disabled：面/目录消失，但注册表与 defs 仍在（回退可救）。"""
    s = _face_stand(_fn_tools(("fin_tool", "finance"), ("meta_tool", "meta")))
    monkeypatch.setenv("QD_TOOL_CONNECT", "fin_tool=disabled")
    s._sync_tool_tiers()
    assert "fin_tool" not in s._core_names and "fin_tool" not in s._on_demand_names
    assert "fin_tool" in s._disabled_names, s._disabled_names
    assert "fin_tool" in s._defs_by_name, "defs 快照必须保留（execute_action 回退放行）"
    assert "fin_tool" not in s._active_tool_definitions()
    assert "fin_tool" not in s._deferred_names(), "目录（延迟层名单）不得出现 disabled"
    # meta 不受影响
    assert "meta_tool" in s._core_names


def test_partition_rules_unchanged_by_default():
    """默认 env 下三分组与改造前逐位一致（v6 分层口径回归）。"""
    s = _face_stand(_fn_tools(("bash", None), ("common_tool", "common"),
                              ("meta_tool", "meta"), ("fin_tool", "finance")),
                    on_demand=())
    s._sync_tool_tiers()
    assert set(s._core_names) == {"bash", "common_tool", "meta_tool"}, s._core_names
    assert s._on_demand_names == ["fin_tool"], s._on_demand_names
    assert s._disabled_names == [], s._disabled_names
    # 显式 config 覆盖（on_demand=[]）优先于 env 默认——回退保护
    s2 = _face_stand(_fn_tools(("edit", None)), on_demand=())
    s2._sync_tool_tiers()
    assert "edit" in s2._core_names, "非 FnToolAdapter 恒必注入，config=[] 时不被 deferred"


def test_ast_partition_consumes_tool_connect():
    """AST 不变量：_partition_tools 必须消费 tool_connect_overrides。

    防"好心改回去"——退回裸读 config.on_demand_tools 就是 G1 哑炮重现
    （改 QD_TOOL_CONNECT 零效果）。
    """
    src = QD_TOOL_FACE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    nodes = list(tree.body)
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            nodes.extend(node.body)
    fn = next((n for n in nodes
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "_partition_tools"), None)
    assert fn is not None, "qd_tool_face.py 找不到 _partition_tools（T7 接线方法）"
    body = ast.unparse(fn)
    assert "tool_connect_overrides" in body, (
        "_partition_tools 必须经 tool_connect_overrides 派生——"
        "禁止退回裸读 config.on_demand_tools（G1 零消费方事故）")


# ── ④ 快照验收字段（G5）───────────────────────────────────────────────

def test_snapshot_has_connect_fields():
    """§8.6「口径唯一」：快照必须带 connect_state_counts/disabled/deferred/invalid。

    ★ 状态计数 vs 层计数（两个视角都要能对账）：
      connect_state_counts = **状态**视角（声明+config 叠加后每工具生效态）；
      deferred_count = **层**视角（按需层全集，含 resident 态但按域规则
      落入按需层的子域工具）。本 stand 让 fin_tool 走 config.on_demand 基座
      ⇒ 两个视角都买到 1，差集路径由 docstring 说明（生产审计据此对账）。
    """
    from app.agent.trace_collector import tool_face_snapshot

    s = _face_stand(_fn_tools(("meta_tool", "meta"), ("fin_tool", "finance"),
                              ("dead_tool", "finance")),
                    on_demand=("fin_tool",))
    import os
    os.environ["QD_TOOL_CONNECT"] = "dead_tool=disabled"
    try:
        s._sync_tool_tiers()
        snap = tool_face_snapshot(s)
    finally:
        os.environ.pop("QD_TOOL_CONNECT", None)
    for key in ("connect_state_counts", "disabled_count", "deferred_count",
                "connect_invalid"):
        assert key in snap, f"tool_face_snapshot 缺 §8.6 字段 {key}: {sorted(snap)}"
    assert snap["disabled_count"] == 1 and snap["deferred_count"] == 1, snap
    counts = snap["connect_state_counts"]
    assert sum(counts.values()) == 3, "生效态计数之和必须等于注册工具数"
    assert counts.get("disabled") == 1 and counts.get("deferred") == 1, counts
    assert snap["connect_invalid"] == [], snap


def test_snapshot_invalid_surfaced(monkeypatch):
    """非法 state 必须透出到快照（审计可见），不是只藏在日志里。"""
    from app.agent.trace_collector import tool_face_snapshot

    s = _face_stand(_fn_tools(("fin_tool", "finance")))
    monkeypatch.setenv("QD_TOOL_CONNECT", "fin_tool=disable")
    s._sync_tool_tiers()
    snap = tool_face_snapshot(s)
    assert "fin_tool=disable" in snap["connect_invalid"], snap


def test_activate_tools_refuses_disabled(monkeypatch):
    """显式激活 disabled ⇒ 进 disabled 桶、不进 _activated（trace 不虚报）。

    但**直调回退仍放行**（§8.3）：`execute_action` fail-open 不依赖
    activate_tools 返回值，registry 持有实例 ⇒ 模型直接点名照样执行。
    """
    # 未被禁用的按需工具仍可正常激活（行为不回归）——先建先验，避免继承 env
    s2 = _face_stand(_fn_tools(("fin_tool", "finance")))
    s2._sync_tool_tiers()
    act2 = s2.activate_tools(["fin_tool"])
    assert act2["activated"] == ["fin_tool"], act2
    # ── 以上是回归栅栏；以下才上禁用 env ──
    s = _face_stand(_fn_tools(("fin_tool", "finance"), ("meta_tool", "meta")))
    monkeypatch.setenv("QD_TOOL_CONNECT", "fin_tool=disabled")
    s._sync_tool_tiers()
    act = s.activate_tools(["fin_tool"])
    assert act["disabled"] == ["fin_tool"], act
    assert "fin_tool" not in act["activated"] and "fin_tool" not in s._activated
    assert "fin_tool" not in s._active_tool_definitions()


def test_apply_tool_connect_refreshes_per_run(monkeypatch):
    """热生效（G2/§8.6「无重启」）：改 env 后 _apply_tool_connect 下一 run 即变。

    agent 按 session 缓存 ⇒ 配置在进程内冻结（G2 病根）；刷新点挂在
    `reset_run_activation()`（_run_inner 每 run 调用）⇒ 改 env 下一 run 生效。
    """
    s = _face_stand(_fn_tools(("fin_tool", "finance"), ("meta_tool", "meta")))
    s._sync_tool_tiers()
    assert "fin_tool" in s._on_demand_names and s._disabled_names == []
    # 模拟"运维在两次请求之间改了 env"：不重建 agent，只再跑一次每 run 刷新
    monkeypatch.setenv("QD_TOOL_CONNECT", "fin_tool=disabled")
    s.reset_run_activation()          # = _activated 清空 + _apply_tool_connect()
    assert "fin_tool" in s._disabled_names, "改 env 后必须在下一次刷新时生效"
    assert "fin_tool" not in s._on_demand_names
    assert "fin_tool" not in s._active_tool_definitions()
    assert "fin_tool" in s._defs_by_name, "刷新只改分组，不动 defs 快照"
    # 恢复置位 ⇒ 功能与成本回到原值（§8.6「断开可逆」）
    monkeypatch.delenv("QD_TOOL_CONNECT", raising=False)
    s.reset_run_activation()
    assert "fin_tool" in s._on_demand_names and s._disabled_names == []


# ── ⑤ 四旋钮对账（F17 / A5 / G5）───────────────────────────────────────

def test_partition_is_a_complete_covering_of_registry():
    """F17：分层三分组必须是对注册表的**完全划分**（不重不漏）。

    背景 A5/G5：「加法先于减法」偏多给工具、靠 `QD_MAX_ACTIVE_TOOLS` 硬闸兜底，
    但没有任何对账保证**注册表里的每个工具都被某个旋钮显式分派** ⇒ 工具可能
    静默滑进/滑出面而无人察觉。本断言把"面 == 注册表"钉成不变量：
    互斥（三层两两不相交）+ 全覆盖（并集 == registry）。
    """
    tools = _fn_tools(("meta_tool", "meta"), ("common_tool", "common"),
                      ("fin_a", "finance"), ("fin_b", "finance"),
                      ("bash", None), ("knowledge_tool", "knowledge"))
    s = _face_stand(tools, on_demand=("fin_a",))
    s._sync_tool_tiers()
    reg = set(s.tool_registry.list_tools())
    parts = [set(s._core_names), set(s._on_demand_names), set(s._disabled_names)]
    assert set().union(*parts) == reg, (
        "分层必须完全覆盖注册表（无暗工具）: "
        f"缺={reg - set().union(*parts)}")
    assert sum(len(p) for p in parts) == len(reg), (
        "三层必须互斥（不重不漏）: "
        f"core∩on_demand={set(s._core_names) & set(s._on_demand_names)}")


def test_must_have_is_meta_and_outside_registry():
    """F17：`_MUST_HAVE` 是元工具模块（走 get_meta），不进注册表、不参与分层。

    若哪天它出现在注册表里，分层对账口径（registry == core∪on_demand∪disabled）
    就要重定——本断言把这个前提显式钉住，避免口径漂移。
    """
    from app.agent.tools._provider import _MUST_HAVE

    s = _face_stand(_fn_tools(("fin_a", "finance"), ("common_tool", "common")))
    s._sync_tool_tiers()
    reg = set(s.tool_registry.list_tools())
    assert _MUST_HAVE, "_MUST_HAVE 不得为空（元工具清单是硬契约）"
    assert (_MUST_HAVE & reg) == set(), (
        f"元工具不得进入注册表分层对账: 命中={_MUST_HAVE & reg}")
