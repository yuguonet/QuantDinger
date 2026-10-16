# -*- coding: utf-8 -*-
"""_ast_guard — AST 级源码守卫 helper 单一真源（backtest_bridge_plan.md 改进 N）。

作用：
  只匹配**真实** import / 调用语法节点做禁入检查，供回测桥双泳道守卫复用：
    - Task 7（tests/test_backtest_bridge.py）：桥不 import auto 写者/扫描/重建模块、
      工具文件零 import svc 引擎（services/backtest）；
    - Task 8（svc 泳道，待实施）：守 backtest_svc_cli 只准调 run_strategy_snapshot，
      绝不 persist_run / ensure_storage_schema。

关键设计点：
  - 守卫必须看语法树：文本 `in src` 匹配会被 docstring 里的解释文字误伤恒红
    （AGENT_DESIGN_v3 坑表教训）——docstring 才能正常解释"为什么禁止调 XXX"；
  - 三种静态 import 形态全闭环（2026-10-15 批次D 盲区审查）：
      ① 绝对 `from X.Y import Z` —— module 前缀直中；
      ② 经父包 `from X import Y` —— module 不含子模块名，须把 names[].name
        （原始名，非 asname；asname 只是本地别名不影响解析）拼成 X.Y 再匹配；
      ③ 相对 `from ...M import N` —— level>0 须用调用方传入的 package（被守
        文件的点分包名）解析真实模块名再匹配；解析规则：level=1 当前包，
        每多一个点上一级，base = package.split(".")[: len - (level-1)]；
  - 前缀匹配一律**边界匹配**（`name == p or name.startswith(p + ".")`）：
    裸 startswith 会误伤兄弟模块（如禁 "app.services.backtest" 误伤
    "app.services.backtest_types"）；
  - fail-closed：level>0 但调用方未传 package（或 level 超出包深度）无法解析
    时**按违规报告**而非静默跳过 —— 守卫的职责是拦截，沉默即洞。

易错点：
  - package 必须传被守文件真实所在包（如 backtest_bridge.py 传
    "app.agent.chain"），传错 = 相对 import 解析到错误层级；
  - 已知边界（改进 N 范围，不扩 scope）：动态导入（importlib.import_module /
    __import__）与调用别名（`from x import f as g` 后调 g()）不在守卫能力内；
  - 返回违规描述列表（空 = 干净），调用方 `assert not _ast_violations(...)`。

文件名以 `_` 开头：pytest 不收集；跨文件消费 `from tests._ast_guard import ...`
（先例：tests/_p8_indicator_baseline.py）。
"""
from __future__ import annotations

import ast


def _ast_violations(source: str, *, import_prefixes: tuple = (),
                    call_names: tuple = (), package: str = "") -> list:
    """AST 级源码守卫（改进 N）：返回 source 中违反禁入规则的描述列表。

    import_prefixes: 禁止的 import 模块名前缀（边界匹配）；
    call_names: 禁止的调用名（Attribute 调用的 attr 或 Name 调用的 id）；
    package: 被守文件的点分包名，相对 import（level>0）解析必需。
    """
    bad = []
    pkg_parts = package.split(".") if package else []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            if node.level > 0:
                # 相对导入：level=1 当前包，每多一个点上一级（ast 语义）。
                # 合法域 level <= len(pkg_parts)（level=len 恰好回到顶层包名）；
                # 切片前必须 fail-closed —— level-1 > len 时切片索引变负，
                # Python 负切片会静默丢尾部得到错误 base（漏报洞）。
                if node.level > len(pkg_parts):
                    bad.append(f"relative import level={node.level} 无法解析"
                               f"（package={'未传' if not pkg_parts else package!r}）")
                    continue
                base = pkg_parts[: len(pkg_parts) - (node.level - 1)]
                module = ".".join(base) + (f".{node.module}" if node.module else "")
            else:
                module = str(node.module or "")
            if _hits_prefix(module, import_prefixes):
                bad.append(f"from {module} import ...")
            else:
                for a in node.names:
                    if _hits_prefix(f"{module}.{a.name}", import_prefixes):
                        bad.append(f"from {module} import {a.name}")
        elif isinstance(node, ast.Import):
            for a in node.names:
                if _hits_prefix(a.name, import_prefixes):
                    bad.append(f"import {a.name}")
        elif isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr in call_names:
                bad.append(f".{f.attr}(...)")
            elif isinstance(f, ast.Name) and f.id in call_names:
                bad.append(f"{f.id}(...)")
    return bad


def _hits_prefix(name: str, prefixes: tuple) -> bool:
    """边界匹配：全等或"前缀 + 点"开头，裸 startswith 的兄弟模块误伤禁绝。"""
    return any(name == p or name.startswith(p + ".") for p in prefixes)
