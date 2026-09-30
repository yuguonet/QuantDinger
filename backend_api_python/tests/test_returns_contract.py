# -*- coding: utf-8 -*-
"""Returns 契约覆盖测试（2026-09-30 提智批 5，AGENT_DESIGN §7.13/§7.17.4 配套）。

背景：「工具返回结构速查」的真源是 docstring 的 `Returns:` 段——没写 Returns 的
工具，模型只能 print 探结构（REPL 式多步 + token 爆炸，§7.13 根因）。且提示词
引用不存在的工具名属幻觉诱导（§7.17.4）。本测试对 plan_linter R1 数据域词典的
**全部候选工具**做静态断言：
  1. 每个候选名都能在 tools/** 或 market_cn/** 解析到函数（含 TOOL_ALIAS 归一）；
  2. 该函数 docstring 必须含 `Returns:`（返回结构契约）。

纯静态 AST 扫描，不 import 任何工具模块（零重依赖）。
"""
import ast
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "app" / "agent"))

from domain_registry import iter_data_domains  # noqa: E402

try:
    from tools.base import TOOL_ALIAS
except Exception:  # pragma: no cover
    TOOL_ALIAS = {}

_SCAN_ROOTS = (BACKEND / "app" / "agent" / "tools", BACKEND / "app" / "market_cn")


def _collect_docs() -> dict:
    docs = {}
    for root in _SCAN_ROOTS:
        if not root.is_dir():
            continue
        for p in root.rglob("*.py"):
            try:
                tree = ast.parse(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    docs.setdefault(node.name, ast.get_docstring(node) or "")
    return docs


_DOCS = _collect_docs()


def test_data_domain_candidates_resolve_with_returns():
    seen = set()
    missing_fn, missing_ret = [], []
    for _name, _kws, tools in iter_data_domains():
        for t in tools:
            if t in seen:
                continue
            seen.add(t)
            real = TOOL_ALIAS.get(t, t)
            doc = _DOCS.get(real)
            if doc is None:
                missing_fn.append(t)
            elif "Returns:" not in doc and "Returns：" not in doc:
                missing_ret.append(t)
    assert seen, "数据域词典为空（装载链断）"
    assert not missing_fn, f"词典引用了不存在的工具名（幻觉诱导）: {missing_fn}"
    assert not missing_ret, f"热点工具缺 Returns 契约（模型只能 print 探结构）: {missing_ret}"
