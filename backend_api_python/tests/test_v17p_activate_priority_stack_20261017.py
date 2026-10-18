# -*- coding: utf-8 -*-
"""F24 激活优先级栈守卫（2026-10-17）。

背景：按需层激活的淘汰点全仓库唯一（`qd_tool_face.activate_tools` 末尾
`while len(self._activated) > cap: self._activated.pop(0)`），是**纯 FIFO**。
而激活来源有 8 类（模型点名 / 技能声明补位 / 预选 / 域兜底 / search_tools 自动）
却**同权** —— 后果：`search_tools` 中途检索自动激活的 top-N 会把模型**刚点名**
的工具从 `_activated` 挤掉，表现为「点了却没生效」= 降智。

本文件锁：淘汰改为**优先级栈**（序数越大越晚淘汰）
  `auto < fallback < preselect < declared < named`
同序数退回 FIFO；`QD_ACTIVATE_PRIORITY=0` 一键回退纯 FIFO。

stand 范式沿用 `tests/test_v17h_tool_connect_20261017.py`（ToolFaceMixin +
假注册表 + SimpleNamespace config），不建真 agent（免模型/网络冷启动）。
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).resolve().parents[1]
QD_TOOL_FACE = BACKEND / "app" / "agent" / "qd_tool_face.py"


# ── stand 范式 ──────────────────────────────────────────────────────────

def _def(name: str) -> dict:
    return {"function": {"name": name, "description": f"[test] {name}",
                         "parameters": {"properties": {}, "type": "object"}}}


class _Reg:
    def __init__(self, tools: dict):
        self._t = tools

    def list_tools(self):
        return list(self._t)

    def get(self, n):
        return self._t[n]

    def get_function_definitions(self):
        return [_def(n) for n in self._t]


def _fn_tools(*names, domain: str = "finance"):
    """FnToolAdapter（非 CORE 域 ⇒ 落按需层，可被 activate_tools 管理）。"""
    from app.agent.tools._fn_adapter import FnToolAdapter

    def _f() -> str:
        """[test] 占位。"""
        return ""

    return {n: FnToolAdapter(_f, domain=domain) for n in names}


def _face_stand(tools: dict, *, cap: int = 2) -> object:
    from app.agent.qd_tool_face import ToolFaceMixin

    class _S(ToolFaceMixin):
        pass

    s = _S()
    s.tool_registry = _Reg(tools)
    s.config = SimpleNamespace(
        on_demand_tools=[], extra_core_tools=[],
        tool_tier_mode="tiered", max_active_domain_tools=cap)
    s._activated = []          # 真实 agent 在 __init__ 初始化
    s._sync_tool_tiers()
    return s


# ── ① 高优先项不被后到的低优先项挤掉（FIFO 会挤掉）────────────────────

def test_named_survives_auto_eviction():
    """先点名的工具（named）不得被后到的 search_tools 自动激活（auto）挤掉。

    这是 F24 的核心病灶：旧 FIFO 淘汰**最早**的 ⇒ 恰好淘汰掉模型刚点名的那个。
    """
    s = _face_stand(_fn_tools("a", "b", "c"), cap=2)
    s.activate_tools(["a"], source="named")           # _activated=[a]
    s.activate_tools(["b", "c"], source="auto")       # 追加 b,c ⇒ 超 cap 淘汰 1
    assert "a" in s._activated, s._activated
    assert "b" not in s._activated, "auto 里最早插入的应先淘汰（同序数 FIFO）"
    assert len(s._activated) == 2


def test_declared_outranks_fallback():
    """技能声明补位（declared）优先于域兜底（fallback）。"""
    s = _face_stand(_fn_tools("a", "b", "c"), cap=2)
    s.activate_tools(["a"], source="fallback")
    s.activate_tools(["b", "c"], source="declared")
    assert "a" not in s._activated, "fallback 最低优先，先淘汰"
    assert set(s._activated) == {"b", "c"}


def test_reactivation_upgrades_protection():
    """既激活项被更高优先来源再次点名 ⇒ 保护级升级（点名 > 一切）。"""
    s = _face_stand(_fn_tools("a", "b"), cap=1)
    s.activate_tools(["a"], source="auto")            # a 保护级=auto
    s.activate_tools(["a"], source="named")           # 升级 a→named
    s.activate_tools(["b"], source="auto")            # 追加 b ⇒ 淘汰应命中 b
    assert s._activated == ["a"], s._activated


# ── ② 同序数退回 FIFO ───────────────────────────────────────────────────

def test_same_source_is_fifo():
    """同优先级按插入序淘汰最早者（稳定性）。"""
    s = _face_stand(_fn_tools("a", "b", "c"), cap=2)
    s.activate_tools(["a", "b", "c"], source="preselect")
    assert s._activated == ["b", "c"]


# ── ③ 缺省来源 = named（向后兼容）───────────────────────────────────────

def test_source_defaults_to_named():
    """不传 source 与显式 `source="named"` 行为逐位一致（旧调用点零改）。"""
    s_def = _face_stand(_fn_tools("a", "b", "c"), cap=2)
    s_def.activate_tools(["a"])                        # 无 source
    s_def.activate_tools(["b", "c"], source="auto")
    s_named = _face_stand(_fn_tools("a", "b", "c"), cap=2)
    s_named.activate_tools(["a"], source="named")
    s_named.activate_tools(["b", "c"], source="auto")
    assert s_def._activated == s_named._activated == ["a", "c"]


# ── ④ 回退旋钮 ──────────────────────────────────────────────────────────

def test_priority_knob_restores_fifo(monkeypatch):
    """`QD_ACTIVATE_PRIORITY=0` ⇒ 退回纯 FIFO（淘汰首个）。"""
    monkeypatch.setenv("QD_ACTIVATE_PRIORITY", "0")
    s = _face_stand(_fn_tools("a", "b", "c"), cap=2)
    s.activate_tools(["a"], source="named")
    s.activate_tools(["b", "c"], source="auto")
    assert s._activated == ["b", "c"], "旋钮关闭 ⇒ FIFO 淘汰首个 a"


# ── ⑤ 容量不变量 ────────────────────────────────────────────────────────

def test_cap_still_enforced_across_sources():
    """任意来源混用后：`_activated` 仍为 list 且长度 ≤ cap。"""
    s = _face_stand(_fn_tools("a", "b", "c", "d"), cap=2)
    s.activate_tools(["a", "b"], source="auto")
    s.activate_tools(["c"], source="named")
    s.activate_tools(["d"], source="fallback")
    assert isinstance(s._activated, list)
    assert len(s._activated) <= 2, s._activated


# ── ⑥ fail-loud：非法来源不得静默归 named ───────────────────────────────

def test_invalid_source_fails_loud():
    s = _face_stand(_fn_tools("a"), cap=2)
    with pytest.raises(ValueError):
        s.activate_tools(["a"], source="typo")


# ── ⑦ 契约守卫（AST）───────────────────────────────────────────────────

def test_activate_tools_has_keyword_only_source():
    """源码守卫：`activate_tools` 必须带**关键字**参数 `source`（防回退成裸 FIFO）。"""
    tree = ast.parse(QD_TOOL_FACE.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "activate_tools")
    kw = [a.arg for a in fn.args.kwonlyargs]
    assert kw and kw[0] == "source", kw
