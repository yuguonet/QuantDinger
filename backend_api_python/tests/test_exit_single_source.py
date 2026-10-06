"""test_exit_single_source.py — 出场逻辑单一化门禁（可断言，禁静默）。

背景（2026-10-06）:
    出场引擎曾有第二份副本（展示层侧 `run_dragon_exit` / `run_trail_stop`），
    与 `core.exit_engines` 存在口径分叉。该副本随 `auto/slice/` 命名空间
    整体删除而消失 —— 出场逻辑自此**单一化**。本测试把这一事实固化下来：
    一旦有人再抄一份出场引擎，测试硬 FAIL。

判据（不做静默降级）:
    1. 四个出场引擎函数在**整个 backend_api_python 树**内各只有 1 处 `def`
       （排除 del/、node_modules、.venv、__pycache__）。
    2. 每个出场调用点都必须 import 自 `core.exit_engines`（不允许 `from ... import`
       别的模块后调用同名函数）。
    3. `auto/slice/` 命名空间不得复活。

易错点:
    - 只 grep `def <name>(` 而非任意出现 —— 否则调用点会被误判为第二份实现。
    - 别用 `pytest.importorskip` 之类的软跳过：删了实现反而"通过"正是本门禁要防的。
"""

import io
import os
import re

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EXIT_ENGINES = ("run_hold_stop", "run_trail_stop", "run_limit_seal", "defer_force_open")

SKIP_DIRS = {"del", "node_modules", ".venv", "__pycache__", ".git", ".pytest_cache",
             "venv", "build", "dist", "_archive"}

DEF_RE = {
    name: re.compile(r"^\s*def\s+%s\s*\(" % name, re.M) for name in EXIT_ENGINES
}


def _iter_py():
    for root, dirs, files in os.walk(BACKEND):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in files:
            if fn.endswith(".py"):
                yield os.path.join(root, fn)


def test_exit_engines_single_definition():
    """四个出场引擎各只有一处定义（第二份 = 口径分叉 = 硬 FAIL）。"""
    found = {name: [] for name in EXIT_ENGINES}
    for p in _iter_py():
        try:
            with io.open(p, encoding="utf-8", errors="ignore") as f:
                src = f.read()
        except OSError:
            continue
        for name in EXIT_ENGINES:
            if DEF_RE[name].search(src):
                found[name].append(os.path.relpath(p, BACKEND))
    for name in EXIT_ENGINES:
        paths = found[name]
        assert len(paths) == 1, (
            "出场引擎 %s 有 %d 处定义: %s —— 出场逻辑必须单一实现（core/exit_engines.py）"
            % (name, len(paths), paths)
        )
        assert paths[0].endswith(os.path.join("core", "exit_engines.py")), (
            "出场引擎 %s 定义于 %s, 应在 core/exit_engines.py" % (name, paths[0])
        )


def test_no_slice_namespace():
    """`auto/slice/` 命名空间不得复活（策略只在 auto/strategies/）。"""
    slice_dir = os.path.join(BACKEND, "app", "market_cn", "auto", "slice")
    assert not os.path.exists(slice_dir), "auto/slice/ 目录复活 —— 展示层已迁入 core/present/"
    self_path = os.path.abspath(__file__)
    for p in _iter_py():
        if os.path.abspath(p) == self_path:
            continue      # 本文件自身在注释里点名旧命名空间，跳过
        with io.open(p, encoding="utf-8", errors="ignore") as f:
            src = f.read()
        assert "auto.slice" not in src, "残留 auto.slice 引用: %s" % os.path.relpath(p, BACKEND)
        assert "SliceStrategyBase" not in src, \
            "残留 SliceStrategyBase（已并入 StrategyBase）: %s" % os.path.relpath(p, BACKEND)


def test_no_common_shim():
    """`auto/common/` 兼容 shim 不得复活（4 个纯转发文件已于 10-06 退役）。

    真实实现在 `core.{indicators,market,exec,filters}`；消费方只能直连 core.*。
    """
    common_dir = os.path.join(BACKEND, "app", "market_cn", "auto", "common")
    assert not os.path.exists(common_dir), \
        "auto/common/ 目录复活 —— 直连 core.{indicators,market,exec,filters}"
    self_path = os.path.abspath(__file__)
    for p in _iter_py():
        if os.path.abspath(p) == self_path:
            continue      # 本文件自身在注释里点名旧命名空间，跳过
        with io.open(p, encoding="utf-8", errors="ignore") as f:
            src = f.read()
        assert "auto.common" not in src, "残留 auto.common 引用: %s" % os.path.relpath(p, BACKEND)


def test_strategy_dir_is_only_home():
    """策略内容只在 auto/strategies/：树内不得有第二处 `class XxxStrategy`。"""
    strat_dir = os.path.join(BACKEND, "app", "market_cn", "auto", "strategies")
    cls_re = re.compile(r"^class\s+(\w*Strategy)\b", re.M)
    outside = []
    for p in _iter_py():
        if os.path.abspath(p).startswith(os.path.abspath(strat_dir)):
            continue
        with io.open(p, encoding="utf-8", errors="ignore") as f:
            src = f.read()
        for m in cls_re.finditer(src):
            outside.append((os.path.relpath(p, BACKEND), m.group(1)))
    assert not outside, "策略类出现在 strategies/ 之外: %s" % outside


@pytest.mark.parametrize("name", EXIT_ENGINES)
def test_callers_import_from_core(name):
    """出场引擎的调用方必须 import 自 core.exit_engines。"""
    call_re = re.compile(r"\b%s\s*\(" % name)
    for p in _iter_py():
        if p.endswith(os.path.join("core", "exit_engines.py")):
            continue
        with io.open(p, encoding="utf-8", errors="ignore") as f:
            src = f.read()
        if not call_re.search(src):
            continue
        assert "core.exit_engines" in src or "core import exit_engines" in src, (
            "%s 调用了 %s 但未从 core.exit_engines 导入: %s"
            % (os.path.relpath(p, BACKEND), name, src[:0])
        )
