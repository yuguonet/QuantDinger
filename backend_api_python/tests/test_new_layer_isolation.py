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

另锁一条（2026-10-27 起改为接入点白名单）：生产模块可 import 新层，但仅限
test_new_layer_wiring_allowlist 登记的入口文件 —— 原「只实现、不接入」已随
Phase C（index.py 改走 coord_index）退役。
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


def test_new_layer_wiring_allowlist():
    """接入期（2026-10-26 Phase C 起）：生产允许 import 新层，但仅限登记的入口文件。

    原「只实现、不接入」契约已随 Phase C（index.py 改走 coord_index）退役；
    本用例改为**白名单**约束 —— 防止引用点无序扩散，新增接入须在此登记。
    """
    layer_parts = {"dispatch.py", "coord_pool.py", "coord_tape.py",
                   "coord_basic.py", "coord_misc.py", "coord_index.py",
                   "provider_pool", "provider_tape", "provider_basic",
                   "provider_misc", "provider_index"}
    layer_tokens = ("data_sources.dispatch", "data_sources.coord_pool",
                    "data_sources.coord_tape", "data_sources.coord_basic",
                    "data_sources.coord_misc", "data_sources.coord_index",
                    "data_sources.provider_pool", "data_sources.provider_tape",
                    "data_sources.provider_basic", "data_sources.provider_misc",
                    "data_sources.provider_index")
    # 允许引用新层的生产文件（接入点登记表；新增须评估后追加）
    allowed_refs = {
        "cn_hk_fundamentals.py",   # coord_basic 基本面
        "cn_stock_ext.py",         # coord_index 指数K线兜底
        "cn_stock_putdb.py",       # coord_index 5m/资金流/龙虎榜落库
        "dragon_limit.py",         # coord_pool 涨跌停池
        "index.py",                # coord_index 日K兜底（Phase C）
        "tape.py",                 # coord_tape 分笔
        "coordinator.py",          # 调用 dispatch 提交/异常分类原语（2026-10-27）
    }
    offenders = []
    for p in APP.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        if p.stem in layer_parts or any(x in p.parts for x in layer_parts):
            continue
        txt = p.read_text("utf-8", errors="ignore")
        for token in layer_tokens:
            if token in txt and p.name not in allowed_refs:
                offenders.append("%s 引用了 %s（未登记接入点）" % (p.relative_to(APP), token))
    assert not offenders, "新层接入点未登记：\n" + "\n".join(offenders)


def test_market_cn_is_read_only_source_of_copy():
    """复制是**生成期**行为：新层运行期不得 import market_cn（本用例即证明）。"""
    for path in NEW_FILES:
        mods = [m for m, _ in _imports(path)]
        assert not any(m.startswith("app.market_cn") for m in mods), path.name
