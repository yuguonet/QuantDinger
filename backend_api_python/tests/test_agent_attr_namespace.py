"""QDAgent 实例属性命名空间守卫。

2026-10-12 血案：`QDAgent.__init__` 里写了 `self._run_tool_calls = 0`（int），
而 mimoagent `DefaultAgent._run_tool_calls(tool_calls)` 是**方法**
（default.py:245，mimocode_agent.py:161 调用它）。实例属性覆盖祖先方法后，
模型**第一次调用任何工具**即抛 `TypeError: 'int' object is not callable`，
整轮崩溃 PRESELECT/MAIN 这两次纯 LLM 调用能跑通，所以**单看前两步发现不了**。

红线：本项目自加的实例属性不得与祖先类的**可调用成员**同名。
（与祖先**方法**同名是正常覆写，属 intentional override，不在本守卫范围；
  与祖先**属性**同名也可能是故意覆盖配置，只作提示不强禁。）

规避约定：自加属性一律 `_qd_` 前缀，与框架命名空间隔离。
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

LEGACY_DIR = Path(__file__).resolve().parent.parent / "app" / "agent"

# ★ 2026-10-13 P3 拆分后必须扫**全部组成文件**：原先只扫 qd_agent.py 一个文件，
#   方法散到 mixin 后 `self.X = ...` 也跟着散走 —— 只扫一个文件等于把这张网
#   剪了个洞（正是本守卫存在的理由：2026-10-12 `_run_tool_calls` 血案）。
#   组成文件新增/改名必须同步这里，否则守卫静默失效。
QD_AGENT_SOURCES = (
    "qd_agent.py",      # 执行核主干（QDAgent 本体）
    "qd_tool_face.py",  # ToolFaceMixin
    "qd_preselect.py",  # PreselectMixin
    "qd_guards.py",     # GuardMixin
    "qd_budgeting.py",  # BudgetMixin
    "qd_aftercare.py",  # AftercareMixin
    "qd_grounding.py",  # GroundingMixin
    "qd_config.py",     # QDAgentConfig（无 mixin，兜底扫）
    "qd_hooks.py",      # 钩子表（无 mixin，兜底扫）
)


def _own_attributes() -> set[str]:
    """静态解析所有组成文件里 `self.X = ...` / `self.X += ...` 的属性名。"""
    names: set[str] = set()
    for fname in QD_AGENT_SOURCES:
        path = LEGACY_DIR / fname
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for sub in ast.walk(node):
                targets = []
                if isinstance(sub, ast.Assign):
                    targets = sub.targets
                elif isinstance(sub, (ast.AugAssign, ast.AnnAssign)):
                    targets = [sub.target]
                for t in targets:
                    if (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                            and t.value.id == "self"):
                        names.add(t.attr)
    return names


def test_all_split_sources_exist() -> None:
    """组成文件不能少：少一个就说明守卫扫不全（会静默放过命名空间冲突）。"""
    missing = [f for f in QD_AGENT_SOURCES if not (LEGACY_DIR / f).is_file()]
    assert not missing, (
        f"QDAgent 组成文件缺失 {missing} —— 本守卫会漏扫 `self.X` 赋值，"
        f"请同步更新 QD_AGENT_SOURCES")


def _ancestor_callables() -> dict[str, str]:
    """mimoagent 祖先类（Base→Default→Mimocode）中可被实例属性遮蔽的成员。"""
    from mimoagent.agents.base import BaseAgent
    from mimoagent.agents.default import DefaultAgent
    from mimoagent.agents.mimocode.mimocode_agent import MimocodeAgent

    out: dict[str, str] = {}
    for cls in (BaseAgent, DefaultAgent, MimocodeAgent):
        for key, val in vars(cls).items():
            if callable(val):
                # property / classmethod / staticmethod 也算可调用，一并纳入
                out.setdefault(key, cls.__name__)
    # 显式补：子类以字符串形式动态取用的钩子（静态 vars 抓不到的）
    out.setdefault("_run_tool_calls", "DefaultAgent")
    return out


def test_own_attributes_do_not_shadow_ancestor_methods():
    """自加实例属性不得覆盖祖先的可调用方法。"""
    own = _own_attributes()
    assert own, "未解析到任何 self.X 赋值——守卫失效，请检查 QDAgent 类名或解析逻辑"

    callables = _ancestor_callables()
    clash = sorted(n for n in own & set(callables))

    detail = "\n".join(
        "  self.%-28s 覆盖 %s.%s（可调用）" % (n, callables[n], n) for n in clash
    )
    assert not clash, (
        "QDAgent 的实例属性覆盖了 mimoagent 祖先方法——一旦模型走到该路径即崩溃"
        "（2026-10-12 血案：`_run_tool_calls`）。\n"
        "请把这些属性改成 `qd_` 前缀或换名：\n" + detail
    )


def test_ancestors_still_expose_expected_method():
    """正向锚：确认框架侧 `_run_tool_calls` 仍是方法（守卫本身的探测有效）。"""
    from mimoagent.agents.default import DefaultAgent

    member = vars(DefaultAgent).get("_run_tool_calls")
    assert member is not None, "框架改动后 _run_tool_calls 不见了，本守卫需重新对齐"
    assert callable(member), "_run_tool_calls 不再是方法，本守卫前提失效"


def test_own_attributes_prefer_qd_prefix():
    """新增的成本/台账类属性优先 `qd_` 前缀（防止未来再撞框架命名空间）。"""
    own = _own_attributes()
    # 只约束"明显是自加台账"的那批；既有历史属性（trace / interrupt_switch 等）不追溯
    budget_like = {n for n in own if "run_" in n or "budget" in n}
    offenders = sorted(n for n in budget_like if not n.startswith("_qd_"))
    assert not offenders, (
        "成本/台账类实例属性请用 `qd_` 前缀与框架隔离：%s" % offenders
    )
