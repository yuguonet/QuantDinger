# -*- coding: utf-8 -*-
"""P14 守卫：预选目录的能力层可达性 + 准入清单无幽灵条目（2026-10-13）。

两条缺陷都是**改回去很诱人**的那类，必须用测试锁住，否则下一轮"省 token"
又会把它们改掉：

① **能力层不进预选目录**（`qd_preselect.py` 调 `build_catalog_grouped` 第三参传 `[]`）
   诱人之处：改回 `[]` 立省 2,802 字符/轮（+67.6% 目录体积）。
   为什么不能改：`PRESELECT_SYSTEM` 明写「目录里找不到对应工具就删掉该交付要素」，
   而能力层是域内无覆盖时的**唯一兜底** ⇒ 不列名字 = 既无法按名调用、也会被整条
   删掉交付要素。省下的 token 换不来一条被误删的交付要素。

② **准入清单幽灵条目**（`get_signal_by_code` 函数从未存在）
   诱人之处：登记名字比实现函数容易得多——写 admission 条目的人不必验证函数存在。
   后果：每轮 loader 报一次"未注册"，账面上准入数虚高，且误导后来人以为该能力已就绪。

判据全部**可机检**，不依赖 LLM。
"""
from __future__ import annotations

import ast
import importlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
QDPRESELECT = ROOT / "app" / "agent" / "qd_preselect.py"
ADMISSION = ROOT / "app" / "agent" / "capabilities" / "admission.json"
CAP_MARK = "## 能力层（低优先级）"


# ──────────────────────────────────────────────────────────────
# ① 能力层必须进预选目录
# ──────────────────────────────────────────────────────────────
def _build_catalog_grouped_calls() -> list:
    """qd_preselect.py 里所有 build_catalog_grouped(...) 调用节点。"""
    tree = ast.parse(QDPRESELECT.read_text(encoding="utf-8-sig"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name == "build_catalog_grouped":
                out.append(node)
    return out


def test_preselect_passes_capability_names():
    """★ 核心守卫：第三参（capability_names）不得为空 —— 传 [] 即缺陷复发。

    静态判据用 AST 而非源码正则：源码里 `[]` 与 `[cap for ...]` 都能匹配正则，
    但语义完全不同；AST 直接读第三参的**表达式本身**。
    """
    calls = _build_catalog_grouped_calls()
    assert calls, "qd_preselect.py 里找不到 build_catalog_grouped 调用（函数被改名？）"

    for c in calls:
        assert len(c.args) >= 3, (
            f"build_catalog_grouped 只传了 {len(c.args)} 个位置参数，"
            f"缺第三参 capability_names ⇒ 能力层不进目录（P14 缺陷）")
        third = c.args[2]
        # 空列表字面量 / 空元组 / 空字符串 都算缺陷复发
        if isinstance(third, (ast.List, ast.Tuple)):
            assert third.elts, (
                "build_catalog_grouped 第三参是空容器 ⇒ 能力层不进预选目录。"
                "这是 2026-10-11「目录瘦身」的退法，2026-10-13 P14 已论证为功能性断裂："
                "PRESELECT_SYSTEM 会因此删掉只能由能力层支撑的交付要素。")
        assert not (isinstance(third, ast.Constant) and third.value in ("", None)), (
            "第三参为空常量 ⇒ 能力层不进目录")


def test_no_dangling_capability_pointer_line():
    """删掉的手工指针行不得回流。

    能力层已列进目录第二段后，那行「能力层另有 N 个…按名调用」既冗余又与
    PRESELECT_SYSTEM 冲突（它反而强调"不在下方目录"，而能力层现在就在下方）。
    """
    src = QDPRESELECT.read_text(encoding="utf-8-sig")
    assert "能力层另有" not in src, (
        "qd_preselect.py 里出现了手工拼的『能力层另有 N 个』指针行。"
        "能力层已由 build_catalog_grouped 生成第二段，这行是重复信息且与目录矛盾。")


def test_build_catalog_grouped_emits_second_section():
    """行为判据：传了 capability_names 就必须真的产出第二段，且排在域内之后。"""
    from app.agent.preselect.tool_preselect import build_catalog_grouped

    defs = {
        "dom_tool_a": {"function": {"name": "dom_tool_a",
                                    "description": "域内工具A",
                                    "parameters": {"properties": {"x": {}}}}},
        "cap_tool_b": {"function": {"name": "cap_tool_b",
                                    "description": "能力层工具B",
                                    "parameters": {"properties": {"y": {}}}}},
    }
    txt, names = build_catalog_grouped(defs, ["dom_tool_a"], ["cap_tool_b"])
    assert "cap_tool_b" in names, "能力层工具未进目录"
    assert CAP_MARK in txt, "能力层段落标记缺失"
    # 顺序：域内段在前
    assert txt.index("dom_tool_a") < txt.index(CAP_MARK) < txt.index("cap_tool_b")


def test_build_catalog_grouped_still_skips_empty_caps():
    """能力层为空时仍不得生成第二段（零准入/加载失败时别凭空给模型一段可幻觉空间）。"""
    from app.agent.preselect.tool_preselect import build_catalog_grouped

    defs = {"a": {"function": {"name": "a", "description": "A",
                               "parameters": {"properties": {}}}}}
    txt, names = build_catalog_grouped(defs, ["a"], [])
    assert CAP_MARK not in txt
    assert names == ["a"]


# ──────────────────────────────────────────────────────────────
# ② 准入清单不得有幽灵条目
# ──────────────────────────────────────────────────────────────
def _admitted_entries() -> list:
    data = json.loads(ADMISSION.read_text(encoding="utf-8-sig"))
    return [e for e in data["entries"] if e.get("admitted")]


def test_no_ghost_admission_entries():
    """★ 核心守卫：准入条目指向的函数必须**真实存在**。

    2026-10-13 查出 `app.market_cn.auto.store:get_signal_by_code` 是幽灵条目
    （函数从未存在），loader 每轮报"1 项未注册"、账面准入数虚高。
    登记名字比实现函数容易得多 ⇒ 这条守卫必须常驻。
    """
    ghosts = []
    for e in _admitted_entries():
        mod_name, fn_name = e.get("module"), e.get("name")
        if not mod_name or not fn_name:
            ghosts.append(f"{e!r} 缺 module/name 字段")
            continue
        try:
            mod = importlib.import_module(mod_name)
        except Exception as ex:
            ghosts.append(f"{mod_name}:{fn_name} 模块导入失败 {type(ex).__name__}: {ex}")
            continue
        if not hasattr(mod, fn_name):
            ghosts.append(f"{mod_name}:{fn_name} 模块无该属性")
    assert not ghosts, (
        "准入清单存在幽灵条目（函数不存在，但账面计入准入）：\n  - " + "\n  - ".join(ghosts))


def test_admission_entry_has_audit_fields():
    """人工审核的凭据：**所有条目**（含不准入）都要有 reviewed_by/reviewed_at/review_note。

    ★ 为什么连不准入的也要查：不准入同样是一次人工审核结论（如 `signal_row`
    的"内部转换器，agent 不该直接调"），没有凭据就分不清"审过不准入"和
    "随手登记"。2026-10-13 那条幽灵条目正是"随手登记"的产物。
    """
    data = json.loads(ADMISSION.read_text(encoding="utf-8-sig"))
    bad = [f"{e.get('module')}:{e.get('name')}"
           for e in data["entries"]
           if not all(e.get(k) for k in ("reviewed_by", "reviewed_at", "review_note"))]
    assert not bad, f"准入条目缺人工审核字段: {bad}"


def test_capability_registration_has_no_unregistered_warning():
    """端到端：能力层注册时不得出现『N 项未注册』——幽灵条目的运行时表征。"""
    import logging

    from app.agent.tools._base import ToolProvider
    from app.agent.qd_tool_face import _ensure_capabilities

    records = []

    class _Cap(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    h = _Cap()
    log = logging.getLogger("app.agent.capabilities.loader")
    log.addHandler(h)
    try:
        p = ToolProvider.get_or_build()
        _ensure_capabilities(p)
    finally:
        log.removeHandler(h)

    bad = [m for m in records if "项未注册" in m]
    assert not bad, f"能力层加载仍有未注册项（幽灵条目）：{bad}"
