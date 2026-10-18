# -*- coding: utf-8 -*-
"""预选目录预过滤的守卫测试（2026-10-17，用户点破"所有工具+描述都喂 LLM，300 个爆掉"）。

背景：`shrink_catalog` 自 2026-10-16 写好后在 app/ 全仓**零调用**——预选每次把
87 个工具的全量目录塞进 prompt（实测 7197 字符）。本测试锁三件事：
  ① `single_target_default_tools`：治「分析600519」三路确定性信号全失效
     （域词典无"分析"、倒排 min_hits=3 短查询恒空、快路由要求单域）；
  ② `shrink_catalog` 新行为：细粒度域映射（DATA_DOMAIN_TOOLS 反转，不再依赖
     没人传对的 `_tool_domain` 粗粒度域）+ defaults 路径 + fail-open 原样返回；
  ③ 接线不丢失：qd_preselect.py 必须在 build_catalog_grouped 前调 shrink_catalog，
     且 parse_plan/lint_selection 的合法点名集是全网 valid 而非目录 names
     （缩目录只缩呈现，不缩可激活性——否则 lint R1 补位/正文捞名全被裁死）。

作者: QuantDinger / 2026-10-17
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PRESELECT = ROOT / "app" / "agent" / "preselect" / "tool_preselect.py"
QD_PRESELECT = ROOT / "app" / "agent" / "qd_preselect.py"

# 合成 defs：工具名取自真实 DATA_DOMAINS 词典（保证域映射命中真实），
# 与 tests/_p9_preselect_baseline.py 同源（确定性，不依赖注册表/网络）。
from tests._p9_preselect_baseline import _DEFS, NAMES  # noqa: E402

from app.agent.preselect import (  # noqa: E402
    shrink_catalog, single_target_default_tools, _preselect_catalog_limit,
    DATA_DOMAIN_TOOLS,
)


# ── ① single_target_default_tools ────────────────────────────────────────

def test_single_target_defaults_for_code_query():
    """「分析600519」⇒ 单股十域首选代表集（非空、全可用、数量有界）。"""
    got = single_target_default_tools("分析600519", NAMES)
    assert got, "单标的+无域词问法必须给默认代表集（否则目录全量 87 行）"
    assert all(n in NAMES for n in got)
    assert len(got) <= 10, got
    # 单股分析四梁八柱必须在场（域词典首选）
    for must in ("get_realtime_quote", "agent_get_kline", "technical_analysis",
                 "batch_valuation_compare", "get_chip_distribution",
                 "get_capital_summary"):
        assert must in got, f"{must} 应在单标的默认集里: {got}"


def test_single_target_defaults_for_name_query():
    """「分析一下贵州茅台」（标的名锚点）同样命中——与代码锚点同权。"""
    # 600519 的注册名是"贵州茅台"；直接用代码+名称混合形态验锚点即可，
    # 名称名单来自本地库（测试环境可能无库）⇒ 这里验代码锚点路径。
    got = single_target_default_tools("分析一下 600519 谢谢", NAMES)
    assert got and "get_realtime_quote" in got


def test_single_target_defaults_empty_for_multi_or_none():
    """非单标的（多锚点/无锚点）必须返回 [] —— 调用方走原路径，不硬塞。"""
    assert single_target_default_tools("分析600519和000001哪个好", NAMES) == []
    assert single_target_default_tools("今天北京天气怎么样", NAMES) == []
    assert single_target_default_tools("帮我看看大盘", NAMES) == []


def test_single_target_defaults_env_override(monkeypatch):
    """QD_PRESELECT_SINGLE_DOMAINS 可覆盖域清单（禁止硬编码的兑现）。"""
    monkeypatch.setenv("QD_PRESELECT_SINGLE_DOMAINS", "quote")
    got = single_target_default_tools("分析600519", NAMES)
    assert got == ["get_realtime_quote"], got


def test_single_target_defaults_filter_unavailable():
    """默认集只收 available_names 里的（词典引用了注册表没有的 ⇒ 静默过滤）。"""
    got = single_target_default_tools("分析600519", ["technical_analysis"])
    assert got == ["technical_analysis"], got


# ── ② shrink_catalog ─────────────────────────────────────────────────────

def test_shrink_uses_defaults_when_no_domain_hit():
    """无域词 + 有 defaults ⇒ 返回 defaults（顺序保持、≤limit）。

    这正是「分析600519」的收窄路径：旧行为原样返回 87 个全量。
    """
    defaults = single_target_default_tools("分析600519", NAMES)
    got = shrink_catalog("分析600519", NAMES, _DEFS, limit=24, defaults=defaults)
    assert got == defaults, got


def test_shrink_fine_grained_domain_map():
    """细粒度域映射：「600519 筹码」只收 chip 域（get_chip_distribution）。

    2026-10-17 前 `_tool_domain` 返回粗粒度 finance，与细粒度 quote/kline
    对不上 ⇒ hit 恒 False ⇒ 不缩。现默认走 DATA_DOMAIN_TOOLS 反转映射。
    """
    got = shrink_catalog("600519 筹码分布", NAMES, _DEFS, limit=24)
    assert "get_chip_distribution" in got
    # 未命中域的工具不进（除非 limit 富余且 per_domain_keep>0，此处 0）
    assert "search_stocks" not in got, got


def test_shrink_domain_hit_collects_whole_domain():
    """域词命中 ⇒ 该域候选全收（宁多不可断），且保持原顺序。"""
    got = shrink_catalog("对比这几只股票的估值和财报业绩", NAMES, _DEFS, limit=24)
    assert "batch_valuation_compare" in got and "get_stock_info" not in got
    # 顺序保持（相对 NAMES 的原序）
    idx = [NAMES.index(n) for n in got]
    assert idx == sorted(idx), got


def test_shrink_fallback_open_returns_all():
    """无域词且无 defaults（天气类）⇒ 原样返回，不缩（fail-open：空目录更致命）。"""
    got = shrink_catalog("今天北京天气怎么样", NAMES, _DEFS, limit=24)
    assert got == list(NAMES)


def test_shrink_limit_zero_disables():
    """limit<=0（QD_PRESELECT_CATALOG_MAX=0）⇒ 原样返回，行为与改造前一致。"""
    defaults = single_target_default_tools("分析600519", NAMES)
    assert shrink_catalog("分析600519", NAMES, _DEFS, limit=0, defaults=defaults) == list(NAMES)


def test_shrink_respects_explicit_domain_of():
    """显式传 domain_of 仍生效（向后兼容：调用方可覆写域映射）。"""
    got = shrink_catalog("600519 筹码分布", NAMES, _DEFS, limit=24,
                         domain_of=lambda n: "chip" if n == "get_chip_distribution" else "other")
    assert got == ["get_chip_distribution"], got


def test_shrink_cap_truncates_and_keeps_order():
    """命中域超过 limit ⇒ 截断且保持 names 原序。"""
    many = list(NAMES) + ["tool_x", "tool_y"]
    got = shrink_catalog("对比这几只股票的估值和财报业绩", many, _DEFS, limit=3)
    assert len(got) <= 3
    idx = [many.index(n) for n in got]
    assert idx == sorted(idx), got


def test_preselect_catalog_limit_default():
    """目录上限默认 24（env 可覆盖；解析失败回落 24）。"""
    old = os.environ.pop("QD_PRESELECT_CATALOG_MAX", None)
    try:
        assert _preselect_catalog_limit() == 24
        os.environ["QD_PRESELECT_CATALOG_MAX"] = "12"
        assert _preselect_catalog_limit() == 12
        os.environ["QD_PRESELECT_CATALOG_MAX"] = "abc"
        assert _preselect_catalog_limit() == 24
    finally:
        os.environ.pop("QD_PRESELECT_CATALOG_MAX", None)
        if old is not None:
            os.environ["QD_PRESELECT_CATALOG_MAX"] = old


# ── ③ 接线不丢失（AST 守卫）─────────────────────────────────────────────

def _find_func(path: Path, func: str):
    """取文件里某个函数的 AST 节点（顶层 def 或 class 内方法都扫）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = list(tree.body)
    for node in nodes:                      # 下钻 class 体（PreselectMixin）
        if isinstance(node, ast.ClassDef):
            nodes.extend(node.body)
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func:
            return node
    raise AssertionError(f"{path.name} 找不到函数 {func}")


def _func_src(path: Path, func: str) -> str:
    """函数源码文本（文本断言用）。"""
    src = path.read_text(encoding="utf-8")
    node = _find_func(path, func)
    return "\n".join(src.splitlines()[node.lineno - 1: node.end_lineno])


def test_shrink_is_wired_before_build_catalog():
    """qd_preselect._maybe_preselect_tools 必须调 shrink_catalog（防再掉线）。

    2026-10-17 事故形态：函数写好了（连 env 开关都有）却零调用，预选照旧全量。
    AST 判据：函数体内 shrink_catalog 调用出现在 build_catalog_grouped 调用之前。
    """
    src = _func_src(QD_PRESELECT, "_maybe_preselect_tools")
    assert "shrink_catalog(" in src, "目录预过滤未接线（shrink_catalog 无调用）"
    assert "_preselect_catalog_limit" in src, "目录上限开关未接线"
    assert src.index("shrink_catalog(") < src.index("build_catalog_grouped("), \
        "shrink_catalog 必须在 build_catalog_grouped 之前（先缩后建）"


def test_valid_set_not_shrunk_for_parse_and_lint():
    """parse_plan 与 lint_selection 必须吃全网 valid，不是目录 names。

    缩目录只缩呈现；若合法点名集也被缩，模型从正文/历史捞到的目录外工具名
    会被判非法丢弃，lint R1 补位也只能补目录内的 ⇒ 静默丢工具。
    """
    src = _func_src(QD_PRESELECT, "_maybe_preselect_tools")
    tree = ast.Module(body=[_find_func(QD_PRESELECT, "_maybe_preselect_tools")], type_ignores=[])
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        fn = call.func
        name = getattr(fn, "id", None) or getattr(fn, "attr", None)
        if name == "parse_plan":
            arg = call.args[1] if len(call.args) > 1 else None
            assert isinstance(arg, ast.Name) and arg.id == "valid", \
                f"parse_plan 的 valid_names 实参不是 valid: {ast.dump(arg)}"
        if name == "lint_selection":
            for kw in call.keywords:
                if kw.arg == "available_names":
                    assert isinstance(kw.value, ast.Name) and kw.value.id == "valid", \
                        f"lint_selection.available_names 实参不是 valid: {ast.dump(kw.value)}"


def test_shrink_catalog_docstring_keeps_wiring_note():
    """shrink_catalog docstring 必须保留「2026-10-17 接线修复」段（防好心改回去）。

    与 v17e 同款防删守卫：本函数曾因没人看懂接线史而差点被当死代码清理。
    """
    src = PRESELECT.read_text(encoding="utf-8")
    assert "2026-10-17 接线修复" in src, "shrink_catalog 的接线修复说明被删了"


def test_single_stock_domains_registered():
    """单股十域必须是 DATA_DOMAINS 的真实子集（防登记错域名）。"""
    from app.agent.preselect.tool_preselect import _SINGLE_STOCK_DOMAINS
    assert set(_SINGLE_STOCK_DOMAINS) <= set(DATA_DOMAIN_TOOLS), \
        f"未登记的域: {set(_SINGLE_STOCK_DOMAINS) - set(DATA_DOMAIN_TOOLS)}"
