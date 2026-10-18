# -*- coding: utf-8 -*-
"""F18 壳测试自身的守卫（2026-10-18）：证明壳**真能拦截**，且 marker 过滤真的生效。

四例（对齐批4_剩余项实施总方案 §6.1 测试清单）：
  ① slow/integration marker 已注册（`config.getini`）；
  ② 壳覆盖 qd_smoke 模块里**全部** `test_N_*`（自省对齐，防漏收）；
  ③ 人为注入一个 FAIL ⇒ 壳变红（证明不是空壳）；
  ④ `-m "not slow"` 收集不到、`-m slow` 收集得到（真子进程验证过滤口径）。
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
V30 = Path(__file__).resolve().parent / "test_v30_qd_smoke_suite.py"


def _load_v30():
    spec = importlib.util.spec_from_file_location("v31_v30_shell", V30)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_markers_registered(pytestconfig):
    markers = pytestconfig.getini("markers")
    assert any(str(m).startswith("slow:") for m in markers), \
        f"slow marker 未注册: {markers}"
    assert any(str(m).startswith("integration:") for m in markers), \
        f"integration marker 未注册: {markers}"


def test_shell_covers_all_module_cases():
    """壳的参数清单必须与 qd_smoke 模块的 test_N_* 全集一致（自省对齐）。"""
    from app.agent.scripts import qd_smoke
    mod = _load_v30()
    covered = {c.__name__ for c in mod._CASES}
    expect = {n for n in dir(qd_smoke)
              if re.match(r"^test_\d+_[a-z0-9_]+$", n)
              and callable(getattr(qd_smoke, n))
              and getattr(getattr(qd_smoke, n), "__module__", "") == qd_smoke.__name__}
    assert covered == expect, (
        f"壳漏收/多收: 漏={sorted(expect - covered)} 多={sorted(covered - expect)}")
    assert len(covered) >= 40, f"用例数异常偏少: {len(covered)}"


def test_shell_catches_injected_failure(monkeypatch):
    """注入一个 False ⇒ `_run_smoke_case` 必须断言失败（拦截逻辑实证）。"""
    from app.agent.scripts import qd_smoke
    mod = _load_v30()
    monkeypatch.setattr(qd_smoke, "PASS", [])        # 隔离，不污染真清单

    def fake_case():
        qd_smoke.check("注入的失败项", True)
        qd_smoke.check("注入的失败项2", False)

    with pytest.raises(AssertionError, match="1 项 FAIL"):
        mod._run_smoke_case(fake_case)


def test_marker_filters_collection():
    """真子进程：`-m "not slow"` 收集不到 v30（exit 5），`-m slow` 收集得到。"""
    base = [sys.executable, "-m", "pytest", "tests/test_v30_qd_smoke_suite.py",
            "--collect-only", "-q", "-p", "no:cacheprovider"]
    r_skip = subprocess.run(base + ["-m", "not slow"], cwd=str(BACKEND),
                            capture_output=True, text=True, timeout=180)
    assert r_skip.returncode == 5, (
        f"'not slow' 应收集不到任何用例（exit 5）: {r_skip.stdout[-400:]}")
    r_slow = subprocess.run(base + ["-m", "slow"], cwd=str(BACKEND),
                            capture_output=True, text=True, timeout=180)
    assert r_slow.returncode == 0, r_slow.stdout[-400:] + r_slow.stderr[-400:]
    assert "test_qd_smoke_case" in r_slow.stdout, "slow 过滤应收集到壳用例"
