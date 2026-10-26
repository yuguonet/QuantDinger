# -*- coding: utf-8 -*-
"""新层隔离约束（用户裁定 2026-10-26）。

允许依赖：
  · `app.utils.*`        —— 基础接口（日历、日志…），明确允许；
  · `app.data_sources.dispatch` / `provider_pool.*` —— 新层自身；
  · `app.data_sources.normalizer` / `rate_limiter` —— 纯转换与全局限速（**非上游取数接口**）。

禁止依赖：
  · `app.market_cn.*`            —— 生产业务模块，替换期要删的就是它；
  · `app.services.*` / `app.data_providers.*` / `app.agent.*` —— 其它上游/上层；
  · `app.data_sources.coordinator` / `provider`(legacy) / `cn_stock` / `base` —— 旧数据源层。
    （`provider_pool` 前缀与 legacy `provider` 不同，比较时按“模块段”匹配，不会误伤。）

另锁一条：新层**不得被生产模块 import**（本阶段只实现、不接入）。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

APP = pathlib.Path(__file__).resolve().parents[1] / "app"
NEW_FILES = [
    APP / "data_sources" / "dispatch.py",
    APP / "data_sources" / "coord_pool.py",
    APP / "data_sources" / "coord_tape.py",
    APP / "data_sources" / "coord_basic.py",
    APP / "data_sources" / "coord_misc.py",
    APP / "data_sources" / "coord_index.py",
    APP / "data_sources" / "provider_pool" / "__init__.py",
    APP / "data_sources" / "provider_pool" / "akshare.py",
    APP / "data_sources" / "provider_tape" / "__init__.py",
    APP / "data_sources" / "provider_tape" / "mootdx.py",
    APP / "data_sources" / "provider_tape" / "tencent.py",
    APP / "data_sources" / "provider_basic" / "__init__.py",
    APP / "data_sources" / "provider_basic" / "akshare.py",
    APP / "data_sources" / "provider_misc" / "__init__.py",
    APP / "data_sources" / "provider_misc" / "sina.py",
    APP / "data_sources" / "provider_index" / "__init__.py",
    APP / "data_sources" / "provider_index" / "akshare.py",
]

ALLOWED = (
    "app.utils",
    "app.data_sources.dispatch",
    "app.data_sources.coord_pool",
    "app.data_sources.coord_tape",
    "app.data_sources.provider_pool",
    "app.data_sources.provider_tape",
    "app.data_sources.provider_basic",
    "app.data_sources.provider_misc",
    "app.data_sources.provider_index",
    "app.data_sources.normalizer",
    "app.data_sources.rate_limiter",
)
FORBIDDEN = (
    "app.market_cn",
    "app.services",
    "app.data_providers",
    "app.data_sources.coordinator",
    "app.data_sources.provider",     # legacy 扁平 provider（注意：不是 provider_pool）
    "app.data_sources.cn_stock",
    "app.data_sources.base",
    "app.agent",
)


def _imports(path: pathlib.Path):
    tree = ast.parse(path.read_text("utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            yield node.module, node.lineno
        elif isinstance(node, ast.Import):
            for a in node.names:
                yield a.name, node.lineno


def _hits(mod: str, prefix: str) -> bool:
    return mod == prefix or mod.startswith(prefix + ".")


@pytest.mark.parametrize("path", NEW_FILES, ids=lambda p: p.name)
def test_no_forbidden_imports(path):
    bad = []
    for mod, lineno in _imports(path):
        if not mod.startswith("app."):
            continue
        if any(_hits(mod, f) for f in FORBIDDEN):
            bad.append("%s:%d -> %s" % (path.name, lineno, mod))
        elif not any(_hits(mod, a) for a in ALLOWED):
            bad.append("%s:%d -> %s（不在允许清单，需先评估）" % (path.name, lineno, mod))
    assert not bad, "新层出现越界依赖：\n" + "\n".join(bad)


def test_new_layer_not_wired_into_production():
    """本阶段“只实现、不接入”：app/ 下除新层自身外，不得 import 它们。"""
    layer_parts = {"dispatch.py", "coord_pool.py", "coord_tape.py",
                   "coord_basic.py", "coord_misc.py", "coord_index.py",
                   "provider_pool", "provider_tape", "provider_basic",
                   "provider_misc", "provider_index"}
    offenders = []
    for p in APP.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        if p.stem in layer_parts or any(x in p.parts for x in layer_parts):
            continue
        txt = p.read_text("utf-8", errors="ignore")
        for token in ("data_sources.dispatch", "data_sources.coord_pool",
                      "data_sources.coord_tape", "data_sources.coord_basic",
                      "data_sources.coord_misc", "data_sources.coord_index",
                      "data_sources.provider_pool", "data_sources.provider_tape",
                      "data_sources.provider_basic", "data_sources.provider_misc",
                      "data_sources.provider_index"):
            if token in txt:
                offenders.append("%s 引用了 %s" % (p.relative_to(APP), token))
    assert not offenders, "新层被接入了生产：\n" + "\n".join(offenders)


def test_market_cn_is_read_only_source_of_copy():
    """复制是**生成期**行为：新层运行期不得 import market_cn（本用例即证明）。"""
    for path in NEW_FILES:
        mods = [m for m, _ in _imports(path)]
        assert not any(m.startswith("app.market_cn") for m in mods), path.name
