# -*- coding: utf-8 -*-
"""F13 守卫：域闸门漏判降级为「轻预选」+ 漏判落盘（2026-10-17）。

背景（R2 / D-8）：`_maybe_preselect_tools` 原本在闸门未命中时**直接 return**，
后果有二：
  ① 跳过零成本确定性快路由（`fast_route_tools`）——白白放弃一次免费的提智；
  ② 漏判既不报警、`preselect_stats` 计数器也拿不到 ⇒ 长期漏判率无法观测。

修法：闸门判定移到快路由**之后**，漏判时只跳 LLM plan（省 ≈3k 目录 token），
仍走确定性域兜底，并把每次漏判写入 `record_gate_miss`。

本文件锁三件事：
  ① 漏判 ⇒ 不调 LLM（省 token；提智不降智）；
  ② 漏判 ⇒ 记进 `preselect_stats`（闭环问责，漏判率可测）；
  ③ 结构不变量：闸门判定在 `fast_route_tools(` 调用**之后**（防「好心改回去」）。
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

BACKEND = Path(__file__).resolve().parents[1]
QD_PRESELECT = BACKEND / "app" / "agent" / "qd_preselect.py"


# ── stand 范式（与 test_v17j / test_v17h 同款）─────────────────────────

class _Reg:
    """假注册表：list_tools/get/get_function_definitions 三件。"""

    def __init__(self, tools: dict):
        self._t = tools

    def list_tools(self):
        return list(self._t)

    def get(self, n):
        return self._t[n]

    def get_function_definitions(self):
        return [{"function": {"name": n, "description": f"[t] {n}",
                              "parameters": {"properties": {}, "type": "object"}}}
                for n in self._t]


class _FakeModel:
    def __init__(self, text: str = ""):
        self.text = text
        self.calls: list = []

    def query(self, messages, **kw):
        self.calls.append(messages)
        return self.text


def _fn_tools(*specs):
    """specs: (name, domain)；domain=None ⇒ mimo 原生 object()。"""
    from app.agent.tools._fn_adapter import FnToolAdapter

    def _f() -> str:
        """[t] 占位。"""
        return ""

    out = {}
    for name, dom in specs:
        out[name] = object() if dom is None else FnToolAdapter(_f, domain=dom)
    return out


def _stand(tools: dict, *, response: str = "", on_demand=()) -> object:
    from app.agent.qd_preselect import PreselectMixin
    from app.agent.qd_tool_face import ToolFaceMixin

    class _S(PreselectMixin, ToolFaceMixin):
        def _text_of(self, resp):
            return str(resp)

    s = _S()
    s.tool_registry = _Reg(tools)
    s.config = SimpleNamespace(
        tool_preselect=True, tool_tier_mode="tiered",
        preselect_gate=True, preselect_hints=None,
        preselect_max_tools=8, extra_core_tools=[],
        on_demand_tools=list(on_demand), max_active_domain_tools=12)
    s._activated = []
    s._defs_by_name = {}
    s._all_defs = []
    s.skills = None
    s.model = _FakeModel(response)
    s._sync_tool_tiers()
    return s


_NO_HINT_Q = "今天天气不错"      # 无域词、无代码、无股票名 ⇒ 闸门必漏判


# ── ① 漏判 ⇒ 跳过 LLM（省 token）──────────────────────────────────────

def test_gate_miss_skips_llm_call():
    """闸门漏判必须跳过那次 LLM plan（≈3k 目录 token），且标记 gate_miss。"""
    from app.agent.preselect import preselect_stats as PS
    PS.reset()
    s = _stand(_fn_tools(("get_realtime_quote", "finance"),
                         ("fin_tool", "finance")),
               response="SHOULD-NOT-BE-CALLED", on_demand=("get_realtime_quote", "fin_tool"))
    rep = s._maybe_preselect_tools(_NO_HINT_Q)
    assert rep.get("gate_miss") is True, rep
    assert s.model.calls == [], "漏判必须跳过 LLM plan（否则白烧 ≈3k 目录 token）"
    assert "兜底" in str(rep.get("reason") or ""), rep
    PS.reset()


# ── ② 漏判 ⇒ 落盘（闭环问责）──────────────────────────────────────────

def test_gate_miss_is_recorded():
    """每次漏判都进 preselect_stats（含域兜底救回的），漏判率可观测。"""
    from app.agent.preselect import preselect_stats as PS
    PS.reset()
    s = _stand(_fn_tools(("get_realtime_quote", "finance")),
               on_demand=("get_realtime_quote",))
    s._maybe_preselect_tools(_NO_HINT_Q)
    s._maybe_preselect_tools(_NO_HINT_Q)
    rep = PS.report()
    assert rep["gate_miss"] == 2, rep
    assert rep["gate_miss_rate"] > 0, rep
    assert rep["by_route"].get("gate_miss") == 2, rep
    PS.reset()


def test_gate_miss_not_counted_as_empty_face():
    """漏判 ≠ 空域：不污染 empty_rate（域兜底可能已救回）。"""
    from app.agent.preselect import preselect_stats as PS
    PS.reset()
    r = PS.record_gate_miss("闲聊一句")
    assert r["gate_miss"] == 1
    assert PS.empty_rate() == 0.0, "漏判不得进空域率分母"
    assert PS.report()["empty"] == 0
    PS.reset()


# ── ③ 结构不变量：闸门在快路由之后（防回退）─────────────────────────────

def _func_src(src: str, name: str) -> str:
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.unparse(node)
    raise AssertionError(f"找不到函数 {name}")


def test_gate_ordered_after_fast_route():
    """`_maybe_preselect_tools` 里闸门的**动作**必须在快路由调用之后。

    闸门的 `has_domain_hint()` 求值可以提前（纯字符串、成本≈0），但它触发的
    「跳过 LLM plan」分支必须排在快路由之后——否则漏判绕过确定性快路由。
    回退到「闸门先 return」= F13 哑炮重现（绕过快路由 + 不落盘）。
    """
    src = QD_PRESELECT.read_text(encoding="utf-8")
    body = _func_src(src, "_maybe_preselect_tools")
    i_fast = body.index("fast_route_tools(")
    i_act = body.index("if gate_miss:")
    assert i_fast < i_act, (
        "闸门动作必须排在 fast_route_tools() 之后（F13：漏判不得跳过确定性快路由）")
    assert "record_gate_miss" in body, "漏判必须落盘（record_gate_miss）"
    assert i_act < body.index("record_gate_miss"), "落盘必须在闸门分支内"
