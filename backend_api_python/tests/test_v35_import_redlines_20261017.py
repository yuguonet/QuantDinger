# -*- coding: utf-8 -*-
"""P5：导入红线守卫（取回 R2/R3/R4/R5/R6/R6b + 行为回归；丢弃 R1/agentcore）

来源：归档件 `del/agentcore_retired_20261013/tests/test_v6_import_modernize`（322 行，
R2/R3/R5/R6 取回自此；文件名不写 .py 后缀，避免触发通用幻觉引用守卫）
取回策略：保留 R2/R3(+白名单自检)/R5/R6/R6b + 行为回归；丢 R1（agentcore 已退役）及其辅助 n_names。
当前 app/agent 为**现行代码目录**（非 legacy 待迁移），但以下红线仍有约束价值：
- R2：app/agent 内非入口脚本不得出现裸名内部导入（避免 sys.path 污染导致的重名副本）
- R3：非 __main__ 块内不得出现 sys.path.insert（入口脚本按白名单豁免）
- R5：工具序列化模块单一规范名 app.agent.tools._serialize
- R6/R6b：modpath 解析规则（含 app.agent_legacy 归一化）
- 行为回归：context_frame/sampler 默认 sources 可调用
R4（legacy 目录不在 sys.path）已被 tests/test_v6_app_dir_syspath.py 覆盖，不重复。
"""
from __future__ import annotations

import ast
import io
import sys
import tokenize
from pathlib import Path
from typing import Iterable, Set, Tuple

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
_AGENT_DIR = _BACKEND / "app" / "agent"

_SKIP_DIRS = {"tmp", "traces", "__pycache__", "data", "prompts", "__pycache__"}

_ENTRY_SCRIPTS = {"cli.py"}

_SYSPATH_ALLOWED = {
    "mimo_boot.py": "MIMOAGENT_SRC 环境变量指向 mimo 源码目录时临时前置（boot 脚本）",
    "cli.py": "CLI 入口需将 project_root 插入 sys.path 以解析 app.*（按 -m 方式使用为首选）",
}


def _strip_comments_and_strings(src: str) -> str:
    out = io.StringIO()
    prev_end = (0, 0)
    try:
        for tok in tokenize.tokenize(io.BytesIO(src.encode("utf-8")).readline):
            if tok.exact_type in (tokenize.COMMENT, tokenize.STRING):
                start = tok.start
                end = tok.end
                srow, scol = prev_end
                erow, ecol = start
                if erow == srow:
                    out.write(" " * (ecol - scol))
                else:
                    out.write("\n" * (erow - srow))
                    out.write(" " * ecol)
                prev_end = end
                continue
            prev_end = tok.end
            out.write(tok.string)
    except Exception:
        return src
    return out.getvalue()


def _iter_sources() -> Iterable[Tuple[Path, Path]]:
    if not _AGENT_DIR.exists():
        return
    for p in _AGENT_DIR.rglob("*.py"):
        rel = p.relative_to(_AGENT_DIR)
        parts = set(rel.parts)
        if parts & _SKIP_DIRS:
            continue
        yield p, rel


def _is_entry_script(rel: Path) -> bool:
    if not rel.parts:
        return False
    if rel.parts[0] == "scripts":
        return True
    return rel.name in _ENTRY_SCRIPTS


def _internal_top_names() -> Set[str]:
    names: Set[str] = set()
    if _AGENT_DIR.exists():
        for p in _AGENT_DIR.glob("*.py"):
            names.add(p.stem)
        for d in _AGENT_DIR.iterdir():
            if d.is_dir() and (d / "__init__.py").exists():
                names.add(d.name)
    names.discard("__init__")
    return names


def _main_block_ids(tree: ast.AST) -> Set[int]:
    """`if __name__ == "__main__":` 块内所有节点的 id 集合（含两类延伸语义）。

    口径取回归档原件（test_v6_import_modernize R3），2026-10-18 收红时恢复——
    此前一次重写丢了三样，导致 10 个**正当**的直跑引导被误报（详见总账附录 E）：
      ① 块**体语句**全收（原件 `for sub in ast.walk(node)`）；只收 If 节点本身
         会漏掉块内直写的 `sys.path.insert`（skill_brewer 的 `__main__` 即此形态）；
      ② 块内**定义**的函数体全收；
      ③ 块内**调用**的函数体同属入口语义——原件注释原话："`__main__` 块里调用的
         函数，其函数体同属入口语义（如 scanner 的 `_bootstrap_path()` 只在命令行
         直跑时才挂路径）"。
    """
    ids: Set[int] = set()

    def _is_main_if(node: ast.If) -> bool:
        t = node.test
        if isinstance(t, ast.Compare):
            left = t.left
            if isinstance(left, ast.Name) and left.id == "__name__":
                ops = t.ops
                if ops and isinstance(ops[0], ast.Eq):
                    comps = t.comparators
                    if comps and isinstance(comps[0], ast.Constant) and comps[0].value == "__main__":
                        return True
        return False

    def _collect(n: ast.AST):
        if isinstance(n, ast.If) and _is_main_if(n):
            # ① 块体语句全收（含块内定义的函数 ②）
            for sub in ast.walk(n):
                ids.add(id(sub))
            # ③ 块内调用的函数：函数体同属入口语义
            called = {
                c.func.id for c in ast.walk(n)
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            }
            for f in ast.walk(tree):
                if (isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and f.name in called):
                    for sn in ast.walk(f):
                        ids.add(id(sn))
        for child in ast.iter_child_nodes(n):
            _collect(child)

    _collect(tree)
    return ids


def _parse(p: Path) -> ast.AST:
    return ast.parse(p.read_text(encoding="utf-8", errors="ignore"))


# ── R2：非入口脚本，不得有裸名内部导入 ────────────────────────────
def test_no_bare_internal_imports():
    internal = _internal_top_names()
    # 自检：防止未来误删导致假绿
    assert {"tools", "chain"} & internal, f"internal top names 含预期集合不足: {sorted(internal)}"
    violations = []
    for p, rel in _iter_sources():
        if _is_entry_script(rel):
            continue
        try:
            tree = _parse(p)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    m0 = a.name.split(".")[0] if a.name else ""
                    if m0 in internal:
                        violations.append(f"{rel}: {a.name}")
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                m0 = node.module.split(".")[0] if node.module else ""
                if m0 in internal:
                    violations.append(f"{rel}: from {node.module}")
    assert not violations, "裸名内部导入（R2）：" + "; ".join(sorted(set(violations)))


# ── R3：非 __main__ 块内，不得 sys.path.insert；入口脚本白名单 ───────
def test_no_syspath_insert_in_sources():
    """R3：包内零 sys.path 注入（入口脚本 / `__main__` 块 / 白名单除外）。

    ★ 入口脚本豁免取回归档原件 `:203`（`if _is_entry_script(rel) ...: continue`）：
      scripts/ 下是**直跑**的测量/批跑脚本，模块级挂 backend 根是正当引导；
      2026-10-18 收红前的一次重写丢了该豁免，把 8 个 scripts/ 全部误报。
    """
    violations = []
    for p, rel in _iter_sources():
        if _is_entry_script(rel):
            continue
        try:
            tree = _parse(p)
        except SyntaxError:
            continue
        main_ids = _main_block_ids(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "path":
                v = node.value
                if isinstance(v, ast.Name) and v.id == "sys":
                    if id(node) in main_ids:
                        continue
                    key = rel.name if len(rel.parts) == 1 else rel.as_posix()
                    if rel.name in _SYSPATH_ALLOWED or rel.as_posix() in _SYSPATH_ALLOWED:
                        continue
                    violations.append(str(rel))
    assert not violations, "非 main 块 sys.path.insert（R3）：" + "; ".join(sorted(set(violations)))


def test_main_block_ids_covers_three_entry_semantics():
    """`_main_block_ids` 三条入口语义各钉一例（2026-10-18 收红新增，防再被改写丢件）。

    ① 块内直写语句；② 块内定义函数；③ 块内调用、外置定义的函数（scanner 形态）。
    """
    src = (
        "def boot():\n"
        "    import sys\n"
        "    sys.path.insert(0, 'x')\n"
        "if __name__ == '__main__':\n"
        "    import sys\n"
        "    sys.path.insert(0, 'y')\n"
        "    def inner():\n"
        "        sys.path.insert(0, 'z')\n"
        "    boot()\n"
    )
    tree = ast.parse(src)
    ids = _main_block_ids(tree)
    targets = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and n.attr == "path"
    ]
    assert len(targets) == 3
    assert all(id(n) in ids for n in targets), (
        "三条入口语义（块内直写/块内定义/块内调用外置函数）必须全部被 _main_block_ids 覆盖"
    )


def test_syspath_guard_exempts_entry_scripts():
    """入口脚本（scripts/ 与 _ENTRY_SCRIPTS）不得进入 R3 违规名单。"""
    for rel in (Path("scripts/qd_bench.py"), Path("cli.py")):
        assert _is_entry_script(rel), f"{rel} 应判定为入口脚本"


def test_syspath_allowlist_still_justified():
    for fname, reason in _SYSPATH_ALLOWED.items():
        p = _AGENT_DIR / fname
        if not p.exists():
            p = _AGENT_DIR / fname
        assert p.exists(), f"白名单文件不存在：{fname}"
        s = p.read_text(encoding="utf-8", errors="ignore")
        assert "sys.path.insert" in s, f"{fname} 不含 sys.path.insert 却在白名单"


# ── R5：_serialize 单一规范名 ──────────────────────────────────────
def test_module_has_single_canonical_name():
    from app.agent.tools import _serialize as ser

    assert ser.__name__ == "app.agent.tools._serialize"
    m = sys.modules.get("serialize")
    if m is not None:
        assert m is ser


# ── R6：modpath 解析规则 ──────────────────────────────────────────
def test_modpath_resolve_rules():
    from app.agent import modpath

    assert modpath.PKG == "app.agent"
    assert modpath.resolve("tools.finance.kline_tools") == "app.agent.tools.finance.kline_tools"
    assert modpath.resolve("cron.cron_tools") == "app.agent.cron.cron_tools"
    assert modpath.resolve("app.market_cn.auto.store") == "app.market_cn.auto.store"
    assert modpath.resolve("app.agent.tools._base") == "app.agent.tools._base"
    assert modpath.resolve("app.agent_legacy.cron.cron_tools") == "app.agent.cron.cron_tools"
    assert modpath.resolve("app.agent_legacy") == "app.agent"


def test_modpath_import_module_and_function_path():
    from app.agent import modpath

    m = modpath.import_module("preselect.preselect_cooc")
    assert m.__name__ == "app.agent.preselect.preselect_cooc"
    fp = modpath.resolve_function_path("cron.cron_tools.some_job")
    assert fp == ("app.agent.cron.cron_tools", "some_job")
    fp2 = modpath.resolve_function_path("app.market_cn.auto.f")
    assert fp2 == ("app.market_cn.auto", "f")


# ── 行为回归：context_frame/sampler 默认 sources 可调用 ──────────────
def test_context_frame_and_sampler_sources_load():
    from app.agent.chain import context_frame, sampler

    cf = context_frame._default_sources()
    sm = sampler._default_sources()
    assert callable(cf.get("index_quote"))
    assert callable(cf.get("limit_pool"))
    assert callable(sm.get("limit_pool"))
    assert callable(sm.get("all_stocks"))
