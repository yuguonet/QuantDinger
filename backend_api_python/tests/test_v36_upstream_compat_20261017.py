# -*- coding: utf-8 -*-
"""F15 守卫：上游契约收口（批 4c，2026-10-17）。

两类病灶（均实测）：

  **A. 入口死代码 + 误导注释**
  `run.py` 与 `gunicorn_config.py` 曾执行
  `sys.path.insert(0, <backend>/app)`，注释写「让 app/nanobot/ 遮蔽 pip 的
  nanobot-ai」。但 `app/nanobot/` 目录**已不存在**、pip 里也没有 nanobot
  （`app/__init__.py` 记着 2026-10-14 实测 `find_spec` 返回 None），且
  `app/__init__.py` 明令「本文件不得把任何目录塞进 sys.path」——两处自相矛盾。
  实测：删掉这两行后全量 pytest **1135 passed / 4 skipped** ⇒ 纯死代码。

  **B. 上游内部访问散落**
  对 mimoagent 内部的访问原本散落 10+ 处，绝大多数是 `getattr(obj, "字段", 默认)`
  形态 ⇒ 上游改名**不报错、只静默拿默认值**。两个已发生的实例：F10 的 cached
  （写库恒 NULL，缓存率算成 0%）与 F12 的轮界压缩（`hasattr(self, "_do_compact")`
  为假则静默不压缩）。现收口到 `app/agent/upstream_compat.py`。

  守卫范围：`app/agent/**` **除 `scripts/`**。scripts/ 下是测量/探针脚本——它们
  是观察上游契约的**人工窗口**（刻意保留裸访问），且 `qd_smoke.py` 的 mock 模型
  在 AST 上会误报（`self.token_stats.input_tokens += 50`）。
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
AGENT = BACKEND / "app" / "agent"

# 收口的上游内部契约（字段/方法名）。任何生产文件在 compat 之外读它们即违规。
_GUARDED_FIELDS = ("token_stats", "cache_read_tokens", "_do_compact")
# 收口的**符号**（不是整个模块）：`MIMOCODE_CORE_TOOL_NAMES` 若上游改名，
# `from ... import` 会直接 ImportError（**响亮**）——但那是在启动期；收口它
# 是为了让 qd_config/preselect_cooc 拿到统一的空表 + 告警而不是各自 try/except。
# ⚠ 只守这一个符号：`MimocodeToolRegistry`（类）等结构性依赖改名即 ImportError，
#   本就是响亮的，不在 F15b「静默失效」范围内（避免矫枉过正扩 scope）。
_GUARDED_IMPORT_NAME = "MIMOCODE_CORE_TOOL_NAMES"


# ────────────────────────────────────────────────────────── 工具

def _prod_sources():
    """生产源码（app/agent 下、除 scripts/ 与非 .py）。"""
    for p in sorted(AGENT.rglob("*.py")):
        rel = p.relative_to(AGENT)
        if rel.parts and rel.parts[0] == "scripts":
            continue
        if "__pycache__" in rel.parts:
            continue
        yield p


def _upstream_reads(tree: ast.AST):
    """检出对上游内部契约的**读取**：[(行号, 字段)]。

    只认两种真实读取形态，杜绝注释/docstring/字典键误报：
      · `getattr(obj, "field", ...)` / `hasattr(obj, "field")` —— 字符串字段名；
      · `obj.field`（Load 上下文）—— 属性访问。
    赋值目标（Store）不算读取（如 `self.token_stats = TokenStats()`）。
    """
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in ("getattr", "hasattr"):
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) \
                    and node.args[1].value in _GUARDED_FIELDS:
                hits.append((node.lineno, node.args[1].value))
        elif isinstance(node, ast.Attribute) and node.attr in _GUARDED_FIELDS \
                and isinstance(node.ctx, ast.Load):
            hits.append((node.lineno, node.attr))
    return hits


def _upstream_imports(tree: ast.AST):
    """检出对上游内部**符号**的导入（只看被收口的那个名字）：[(行号, 名称)]。

    不守整个 `mimoagent.tools.mimocode` 模块：其中的 `MimocodeToolRegistry` 等
    结构性依赖一旦改名会**直接 ImportError**（响亮），不属于 F15b 的「静默失效」。
    只守 `MIMOCODE_CORE_TOOL_NAMES` —— 它在两处被 try/except 吞掉，改名即静默空表。
    """
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == _GUARDED_IMPORT_NAME:
                    hits.append((node.lineno, a.name))
    return hits


# ─────────────────────────────────────────── A. 入口死代码 / 误导注释

def test_entry_scripts_do_not_insert_app_dir():
    """`run.py` / `gunicorn_config.py` 不得再把 `<backend>/app` 塞进 sys.path。"""
    for name in ("run.py", "gunicorn_config.py"):
        p = BACKEND / name
        tree = ast.parse(p.read_text(encoding="utf-8"))
        bad = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and fn.attr in ("insert", "append")):
                continue
            owner = fn.value
            if not (isinstance(owner, ast.Attribute) and owner.attr == "path"):
                continue
            src = " ".join(ast.unparse(a) for a in node.args)
            if '"app"' in src or "'app'" in src:
                bad.append((node.lineno, src))
        assert not bad, (
            f"{name} 仍在把 app/ 塞进 sys.path（行号/参数：{bad}）—— "
            f"该目录已不存在 app/nanobot，且 app/__init__.py 明令禁止注入")


def test_entry_scripts_reference_no_removed_nanobot():
    """注释里也不许再散播「app/nanobot 遮蔽 nanobot-ai」这套已消失的说法。"""
    for name in ("run.py", "gunicorn_config.py"):
        src = (BACKEND / name).read_text(encoding="utf-8")
        assert "nanobot" not in src.lower(), (
            f"{name} 仍提到 nanobot（app/nanobot 与 pip 的 nanobot-ai 均已不存在）")


# ───────────────────────────────────── B. upstream_compat 行为契约

@pytest.fixture(autouse=True)
def _reset_warnings():
    from app.agent import upstream_compat as uc
    uc.reset_warnings()
    yield uc


class _NoStats:
    pass


class _Stats:
    def __init__(self, cache=9357, total=20000):
        self.cache_read_tokens = cache
        self.total_tokens = total
        self.input_tokens = 123
        self.output_tokens = 45


class _Model:
    def __init__(self, stats):
        self.token_stats = stats


def test_missing_token_stats_returns_none_and_warns(_reset_warnings, caplog):
    """缺 token_stats ⇒ None + **WARNING**（不是静默 0 —— 闭环问责）。"""
    uc = _reset_warnings
    with caplog.at_level(logging.WARNING, logger="app.agent.upstream_compat"):
        assert uc.read_token_stats(_NoStats()) is None
        assert uc.read_cache_tokens(_NoStats()) is None
    assert any("token_stats" in r.message for r in caplog.records), caplog.text
    assert "token_stats" in uc.contract_warnings()


def test_cache_field_missing_is_none_not_zero(_reset_warnings, caplog):
    """stats 在但字段缺失 ⇒ None + WARNING（区别于「真实 0」）。"""
    uc = _reset_warnings

    class _Bare:
        total_tokens = 10

    with caplog.at_level(logging.WARNING, logger="app.agent.upstream_compat"):
        assert uc.read_cache_tokens(_Model(_Bare())) is None
    assert any("cache_read_tokens" in r.message for r in caplog.records)
    assert "cache_read_tokens" in uc.contract_warnings()


def test_present_fields_return_real_values(_reset_warnings, caplog):
    uc = _reset_warnings
    m = _Model(_Stats(cache=9357, total=20000))
    with caplog.at_level(logging.WARNING, logger="app.agent.upstream_compat"):
        assert uc.read_cache_tokens(m) == 9357
        assert uc.read_total_tokens(m) == 20000
    assert not caplog.records, "字段齐全时不得产生任何告警"
    assert uc.contract_warnings() == ()


def test_invoke_compaction_present_and_absent(_reset_warnings, caplog):
    uc = _reset_warnings
    calls = []

    class _A:
        def _do_compact(self):
            calls.append(1)

    assert uc.invoke_compaction(_A()) is True
    assert calls == [1]

    with caplog.at_level(logging.WARNING, logger="app.agent.upstream_compat"):
        assert uc.invoke_compaction(_NoStats()) is False
    assert any("_do_compact" in r.message for r in caplog.records)


def test_core_tool_names_is_nonempty_tuple(_reset_warnings, caplog):
    uc = _reset_warnings
    with caplog.at_level(logging.WARNING, logger="app.agent.upstream_compat"):
        names = uc.core_tool_names()
    assert isinstance(names, tuple) and names, "mimo 核心工具名应可正常取到"
    assert not caplog.records, f"正常路径不得告警: {caplog.text}"


# ───────────────────────────────── 生产调用点：只准经 compat（红线）

def test_production_code_reads_upstream_only_via_compat():
    """`app/agent`（除 scripts/）不得在 compat 之外读上游内部契约。

    ★ 用 AST 而非文本 `in src`：本文件自己的 docstring、以及源码注释里都写着
      这些字段名，文本匹配会**恒红**（假守卫）—— 本仓已有同类教训。
    """
    offenders = {}
    for p in _prod_sources():
        if p.name == "upstream_compat.py":
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        hits = _upstream_reads(tree)
        if hits:
            offenders[str(p.relative_to(BACKEND))] = hits
    assert not offenders, (
        "以下生产文件绕过了 upstream_compat 直接读上游内部契约（上游改名即静默失效）：\n"
        + "\n".join(f"  {k}: {v}" for k, v in offenders.items()))


def test_production_code_imports_upstream_symbol_only_via_compat():
    offenders = {}
    for p in _prod_sources():
        if p.name == "upstream_compat.py":
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        hits = _upstream_imports(tree)
        if hits:
            offenders[str(p.relative_to(BACKEND))] = hits
    assert not offenders, (
        "以下生产文件绕过 upstream_compat 直接导入 mimo 内部符号：\n"
        + "\n".join(f"  {k}: {v}" for k, v in offenders.items()))
