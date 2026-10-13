# -*- coding: utf-8 -*-
"""域分类学守卫（2026-10-14，来自 `app/agent/领域domain作用梳理_20261014.md`）。

钉的四件事，都是"改完容易悄悄退化"的那类：

  1. **数据域词典单源**：全仓只允许 `preselect/tool_preselect.py::DATA_DOMAINS` 一份。
     此前 `tools/finance/domain_meta.py::_DATA_DOMAINS` 是第二份，且**早已与活的
     分叉**（realtime_quote vs quote、fund_flow vs fundflow，还引用了已下线的
     index_daily / day_series）—— 同一件事两个事实源，改一处漏一处。
  2. **DomainSpec 字段只留活的**：`primary`/`intent_verbs`/`data_domains`/
     `list_producers`/`list_consumers` 五个字段连同 6 个零消费方函数已删。
     删它们的判据是「只被零消费方函数读」，不是「看着没用」。
  3. **每个 tools/*/domain_meta.py 必须装得进 SPEC**：`_load_from_tools` 对装载
     失败只 `logger.warning` ⇒ 字段一旦不匹配，该域会**静默消失**（实测：删字段后
     knowledge 域就是这样掉出去的，靠导入冒烟才抓到）。
  4. **不得再出现幻觉引用**：`domain_meta.py` 曾写着"已补断言 tests/test_wiring.py
     W20a/b/c"，而那个文件根本不存在。

存活的 domain_registry API（有消费方，删了才是真事故）：
  entity_to_domain（resolvers 实体→域）、trading_calendar_domains + word_domain_hints
  （resolvers/time.py 交易日口径）、classify_verb / normalize_chain_name /
  strip_entity（链名归一 → skill_brewer 与 utils/tracing）。
"""
from __future__ import annotations

import ast
import importlib
import logging
from pathlib import Path

import pytest

from app.agent.domain_registry import DomainSpec  # noqa: F401  (导入即冒烟)

_BACKEND = Path(__file__).resolve().parents[1]
_APP = _BACKEND / "app"
_AGENT = _APP / "agent"

# 本次删除的零消费方 API（防回流）
_DEAD_APIS = (
    "iter_data_domains", "list_producers", "list_consumers",
    "fallback_domain", "default_entity_type", "domain_intent_verbs",
)
# 本次删除的 DomainSpec 字段（防回流）：都只被上面的死函数读
_DEAD_FIELDS = ("primary", "intent_verbs", "data_domains",
                "list_producers", "list_consumers")
# 活下来的字段（有消费方）
_LIVE_FIELDS = ("name", "entity_types", "trading_calendar",
                "verb_nouns", "word_pattern")

_SKIP_DIRS = {"__pycache__", "tmp", "del", "node_modules", ".venv", "venv"}


def _iter_py(root: Path):
    for py in root.rglob("*.py"):
        rel = py.relative_to(_BACKEND).as_posix()
        if any(part in _SKIP_DIRS for part in rel.split("/")):
            continue
        yield py


def _assigned_names(py: Path) -> set:
    """源码里被**赋值**的顶层名（不含注释/docstring 里的同名字符串）。"""
    try:
        tree = ast.parse(py.read_text(encoding="utf-8-sig"))
    except (SyntaxError, UnicodeDecodeError):
        return set()
    out = set()
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for t in targets:
            if isinstance(t, ast.Name):
                out.add(t.id)
    return out


def test_data_domain_dict_has_exactly_one_source() -> None:
    """全仓只允许**一份**数据域词典，且必须在 `preselect/tool_preselect.py`。

    ★ 这是本次要根治的核心：`tools/finance/domain_meta.py` 里那份与活的早已分叉
      （域名拼法不同、还含已下线工具名），两套并行 = 同一个决策两个事实源。
    """
    hits = []
    for py in _iter_py(_BACKEND):
        names = _assigned_names(py)
        for n in ("DATA_DOMAINS", "_DATA_DOMAINS"):
            if n in names:
                hits.append((py.relative_to(_BACKEND).as_posix(), n))
    assert hits == [("app/agent/preselect/tool_preselect.py", "DATA_DOMAINS")], (
        f"数据域词典的定义处应为唯一一处 `preselect/tool_preselect.py::DATA_DOMAINS`，"
        f"实际：{hits}\n第二份词典迟早与活的漂移（历史上已经漂过："
        f"realtime_quote vs quote、fund_flow vs fundflow，还带已下线的 index_daily）")


def test_domain_spec_keeps_only_live_fields() -> None:
    """DomainSpec 只保留有消费方的字段；死字段回流会重新长出第二份词典。"""
    fields = set(DomainSpec.__dataclass_fields__)
    assert fields == set(_LIVE_FIELDS), (
        f"DomainSpec 字段集应为 {set(_LIVE_FIELDS)}，实际 {fields} —— "
        f"多出来的是已删字段（只被零消费方函数读），少掉的是活字段（会断真通道）")
    for f in _DEAD_FIELDS:
        assert f not in fields, f"死字段 {f} 回流了"


def test_dead_domain_apis_do_not_come_back() -> None:
    """6 个零消费方函数不得回流（它们会重新把第二份词典读进活链路）。"""
    import app.agent.domain_registry as R

    back = [n for n in _DEAD_APIS if hasattr(R, n)]
    assert not back, (
        f"零消费方的域 API 回流：{back} —— 它们的消费方（旧编排层 plan_linter / "
        f"selected_domain / task_agent）已退役，回流只会复活第二份数据域词典")


def test_live_domain_apis_still_resolve() -> None:
    """反向：4 条真通道必须仍然跑得通（删字段不能顺手删掉它们的粮）。

    ⚠ `trading_calendar_domains()` 非空是**时间解析**的命脉（决定"明天"按交易日
    还是自然日算）；它依赖 DomainSpec.trading_calendar 被正确搬运 —— 而
    `_load_from_tools` 里那段"字段逐个拷贝"正是删字段时最容易漏改的地方。
    """
    import app.agent.domain_registry as R

    assert R.entity_to_domain("stock") == "finance", "实体→域 通道断了"
    assert R.trading_calendar_domains(), "交易日口径域集为空 —— 相对日词解析会算错"
    assert R.word_domain_hints(), "语汇→域 兜底信号为空"
    assert R.classify_verb("screen") == {"domain": "finance", "noun": "stock"}
    assert R.normalize_chain_name("stock+analyze+600929") == "stock+analyze"


def test_every_domain_meta_loads_into_registry(caplog) -> None:
    """每个 `tools/*/domain_meta.py` 都必须装进注册表 —— 装载失败是**静默**的。

    `domain_registry._load_from_tools` 对装载异常只 `logger.warning` 就 continue，
    于是字段一不匹配的域会**悄悄消失**（实测：删 `primary` 字段后 knowledge 域
    就是这样掉出去的，全量 pytest 没抓到，靠导入冒烟才抓到）。
    这里把 `_LOADED` 复位后重新装载，用 caplog 断言无装载告警，并逐个文件直接
    import 验证 `SPEC` 真的是 DomainSpec。
    """
    import app.agent.domain_registry as R

    # ① 逐个文件直接 import：构造参数不匹配会当场 TypeError（确定性，不依赖时序）
    metas = sorted((_AGENT / "tools").glob("*/domain_meta.py"))
    assert metas, "没找到任何 tools/*/domain_meta.py（扫描口径变了？）"
    for meta in metas:
        mod_name = "app.agent.tools." + meta.parent.name + ".domain_meta"
        mod = importlib.import_module(mod_name)
        spec = getattr(mod, "SPEC", None)
        assert isinstance(spec, DomainSpec), (
            f"{meta.relative_to(_BACKEND).as_posix()} 的 SPEC 不是 DomainSpec：{spec!r} —— "
            f"多半是构造参数与 DomainSpec 字段不匹配，装载时会被 warning 吞掉")

    # ② 复位后重新装载，断言无告警（覆盖"装载期"而非"导入期"的失败）
    old_specs, old_loaded = dict(R._SPECS), R._LOADED
    try:
        R._SPECS.clear()
        R._LOADED = False
        with caplog.at_level(logging.WARNING):
            specs = R._ensure()
    finally:
        R._SPECS.clear()
        R._SPECS.update(old_specs)
        R._LOADED = old_loaded
    assert specs, "复位后重新装载得到空注册表"
    warns = [r.getMessage() for r in caplog.records
             if "DomainRegistry" in r.getMessage()]
    assert not warns, f"有 domain_meta 装载失败（会被静默跳过）：{warns}"
    assert set(specs) == {m.parent.name for m in metas}, (
        f"注册表域集 {sorted(specs)} 与 domain_meta 文件 {[m.parent.name for m in metas]} 不一致")


def test_no_comment_references_a_nonexistent_test_file() -> None:
    """**通用**幻觉引用守卫：注释/docstring 里写的 `tests/**.py` 路径必须真实存在。

    起因是 `tools/finance/domain_meta.py` 曾写"已补上真实断言 tests/test_wiring.py
    W20a/W20b/W20c"，而那个文件**根本不存在** —— 读注释的人会以为这块有守卫。

    开扫后实测共 6 处悬空引用（2026-10-14，均已订正）：
      · tests/test_wiring.py                  → 从未存在（幻觉引用）
      · tests/evals/runner.py                 → evals/ 目录整个已移除
      · tests/test_market_screener_pred.py    → .py 已删，只剩 __pycache__ 字节码
      · tests/test_v6_import_modernize.py     → 随 P16 归档到 del/agentcore_retired_20261013/
      · tests/test_v6_isolation.py            → 同上（且从未在 tests/ 下建立过）
      · tests/common.py                       → 文件已迁到 tests/present/，头注没跟着改

    ★ 为什么是通用扫而不是只禁 `test_wiring` 一个字符串：这一整类"注释指到不存在
      的文件"都同源（文件搬走/归档/从未建立，注释没跟着改），禁一个名字下次换个
      名字照样漏。代价是要保证全仓干净 —— 这正是守卫该干的事。
    """
    import re as _re

    pat = _re.compile(r"tests/[A-Za-z0-9_./\-]+\.py")
    bad: list = []
    for py in _iter_py(_BACKEND):
        if py.name == Path(__file__).name:      # 本文件自己的说明文字里含这些名字
            continue
        try:
            src = py.read_text(encoding="utf-8-sig")
        except (UnicodeDecodeError, OSError):
            continue
        for m in pat.finditer(src):
            target = _BACKEND / m.group(0)
            if not target.exists():
                bad.append(f"{py.relative_to(_BACKEND).as_posix()} → {m.group(0)}")
    assert not bad, (
        "以下注释引用了不存在的测试文件（幻觉引用，会让人以为有守卫其实没有）：\n  "
        + "\n  ".join(sorted(set(bad))))


@pytest.mark.parametrize("name", list(_DEAD_APIS))
def test_dead_api_names_are_not_referenced_anywhere(name: str) -> None:
    """全仓不得再**调用**这些死 API（源码级，AST 找 Call，不看注释）。

    ★ 反直觉：被禁的名字会写在本测试自己的字符串常量里，也会写进
      domain_registry 的删改说明注释 ⇒ 只能匹配 `ast.Call`，不能 `in src`。
    """
    hits = []
    for py in _iter_py(_BACKEND):
        if py.name == Path(__file__).name:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8-sig"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id == name:
                hits.append(f"{py.relative_to(_BACKEND).as_posix()}:{node.lineno}")
            elif isinstance(fn, ast.Attribute) and fn.attr == name:
                hits.append(f"{py.relative_to(_BACKEND).as_posix()}:{node.lineno}")
    assert not hits, f"死 API `{name}` 又被调用了：{hits}"
