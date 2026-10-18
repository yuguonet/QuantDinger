# -*- coding: utf-8 -*-
"""F18：qd_smoke 40 个函数级用例进 pytest（壳测试，2026-10-18）。

**设计（对齐批4_剩余项实施总方案 §6.1，DRV/YAGNI）**：
  · **一行不改 qd_smoke**：原样 `from app.agent.scripts import qd_smoke` 调用其
    `test_N_*` 函数；用例清单由**模块自省**产生（`test_\\d+_*` + `__module__` 校验），
    脚本里新增 test_41 这里自动跟上，不维护第二份名单。
  · 失败捕获：qd_smoke 的 `check()` 把结果追加进模块级 `PASS` 列表。壳在调用前
    记 `len(PASS)`、调用后只切片新增段 —— 有 False 即断言失败（stdout 里的
    `[FAIL] 明细` 随 pytest  captured output 一起呈现）。
  · 异常不吞：用例函数抛异常 ⇒ pytest 直接红（比脚本的 try/except 更响）。
  · SKIP（--with-env 缺件 / B.2 归档缺件直写）不算失败。
  · trace 自清：module 级 fixture 复刻脚本 `__main__` 的
    `_trace_baseline()` / `_cleanup_trace_rows()` 包裹语义。

**⚠️ 为什么壳里显式置 `WITH_ENV = True`（2026-10-18，不是兜底）**：
`tests/conftest.py` 在收集前把 psycopg2（及一批重依赖）整体 mock 成替身模块
（且替身缺 `OperationalError` 等属性 ⇒ `db_postgres.HAS_PSYCOPG2=False`）⇒
**pytest 进程里 PG/名录库结构性不可用**。qd_smoke 的 DB/名录断言按设计标了
`env=`（check() docstring 原话："缺件而非回归"；test_26 内注释同）——脚本直跑时
有真 PG 所以 PASS。壳在 pytest 进程里必须走模块**自带**的 --with-env 语义：
探针（真连 SELECT 1 / 真查名录库）证实缺件 ⇒ 记 SKIP 不进失败；环境真可用时
断言照常 FAIL。**探针诚实，故这不是掩盖回归**——脚本 `python app/agent/scripts/
qd_smoke.py`（无 mock、有真 PG）仍是更严口径，两者并存。

**⚠️ 进程全局判据与"外来污染"处置（2026-10-18 收红，全量跑出的真问题）**：
qd_smoke 有两条判据是**进程全局**断言（test_29 ① legacy 目录不在 sys.path、
test_30 ③ 同）。脚本直跑时进程 fresh 恒真；但 pytest **全量**里，收集期/运行期
排在前的测试文件会按 R3「入口脚本豁免」导入 `exp_grind` 等脚本——那些脚本的
模块级路径引导把 `app/agent` 插进 sys.path（P5 后 qd_smoke 自己已不插，见其
头部注释；其余 6 个入口脚本的插入是 R3 豁免下的 sanctioned 行为，**不在本批
改动范围**）。那是别的文件的既有行为，不是 qd_smoke 的回归，但不能让它把
本套件的进程全局判据打红。壳的处置（还原 fresh 前提，而非吞失败）：
  ① **导入期精确断言**：导入 qd_smoke 前后对 sys.path 做差集，qd_smoke 自身
     导入链若引入 legacy 目录 ⇒ 收集期直接报错（P5 真回归在这拦，绕不过）；
  ② **用例前清洗**：每例开跑前摘掉 sys.path 里的 legacy 目录——此时只可能是
     外来污染（① 已证本套件链不引入），摘除即还原脚本的 fresh 进程前提；
     模块级插入另有 test_v35 R3 全仓 AST 扫描把着，不会因摘除而失守。
两者相加 = 脚本语义在 pytest 里的忠实复现；**qd_smoke 脚本直跑仍是更严口径**。

**⚠️ 必须整文件按序跑**：40 个子用例共享进程内状态且**有序依赖**（qd_smoke
设计如此，脚本 `__main__` 同样是固定顺序 1→40：工具面的能力层由 test_16 的
服务装配注册，test_30 的数量判据依赖它）。单挑参数化用例（`-k test_30`）会
因缺前置装配误报——那不是回归，是脱离了脚本的执行契约。日常/CI 用
`-m "not slow"` 排掉本文件；完整冒烟整文件跑或直接跑脚本。
"""
from __future__ import annotations

import os
import re
import sys

import pytest

# ─────────────────────────────────────── 导入 qd_smoke（前后差集见下方断言）
_paths_before = {os.path.normcase(os.path.abspath(p)) for p in sys.path if p}

from app.agent.scripts import qd_smoke   # noqa: E402

# ① 导入期精确断言：qd_smoke **自身**导入链不得引入 legacy 目录（P5 口径）。
#    差集而非全量检查——收集期别的文件可能已污染（那是②的处置对象），这里只判
#    "是不是 qd_smoke 自己插的"：真回归必须红在收集期，且绕不过本壳。
_self_added = [p for p in sys.path
               if p and os.path.normcase(os.path.abspath(p)) not in _paths_before
               and os.path.normcase(os.path.abspath(p))
               == os.path.normcase(os.path.abspath(str(qd_smoke.AGENT_DIR)))]
if _self_added:
    raise AssertionError(
        "qd_smoke 自身导入链把 legacy 目录插进了 sys.path（P5 回归）："
        f"{_self_added}——去修 qd_smoke 头部的路径引导，别在本壳绕过")

# pytest 进程语义切到 --with-env（理由见模块 docstring）：conftest mock 掉 psycopg2
# ⇒ PG/名录库结构性缺件 ⇒ 带 env= 的断言经探针转 SKIP，不误报回归。
qd_smoke.WITH_ENV = True

# ─────────────────────────────────────────────────── 用例清单（自省，勿手抄）

_FN_RE = re.compile(r"^test_(\d+)_([a-z0-9_]+)$")


def _smoke_cases():
    """qd_smoke 里按编号排序的 `test_N_*` 函数（只收本模块定义的，防拾雅）。"""
    out = []
    for name in dir(qd_smoke):
        m = _FN_RE.match(name)
        if not m:
            continue
        fn = getattr(qd_smoke, name)
        if callable(fn) and getattr(fn, "__module__", "") == qd_smoke.__name__:
            out.append((int(m.group(1)), fn))
    out.sort(key=lambda x: x[0])
    return [fn for _, fn in out]


_CASES = _smoke_cases()

if os.environ.get("PYTEST_XDIST_WORKER"):
    raise pytest.UsageError(
        "test_v30_qd_smoke_suite 必须串行跑：qd_smoke 40 个子用例共享进程内状态"
        "（脚本同为顺序执行），禁用 xdist 并行；日常全量请用 `pytest tests/ -m 'not slow'`")


# ─────────────────────────────────────────────────── 壳

_AGENT_DIR_NORM = os.path.normcase(os.path.abspath(str(qd_smoke.AGENT_DIR)))
_scrubbed: list = []          # 累计摘掉的外来 legacy 条目（只记，不还原）
_scrub_noted = False          # 外来污染只广播一次（不刷屏）


def _scrub_foreign_legacy_path() -> None:
    """② 摘掉 sys.path 里的 legacy 目录，还原 qd_smoke 判据的 fresh 进程前提。

    只可能是外来污染：上方导入期断言已证 qd_smoke 自身链不引入，而 agent 包内
    模块的路径插入另有 test_v35 R3 全仓 AST 扫描把着（非入口脚本插入即红）。
    摘除 ≠ 掩盖：真回归（qd_smoke 链插 legacy / 包内模块插 legacy）分别在
    收集期断言与 test_v35 两条路上都是响的。
    """
    global _scrub_noted
    for p in list(sys.path):
        if p and os.path.normcase(os.path.abspath(str(p))) == _AGENT_DIR_NORM:
            sys.path.remove(p)
            _scrubbed.append(p)


def _run_smoke_case(fn) -> None:
    """跑一个 qd_smoke 用例；PASS 切片里出现 False ⇒ 断言失败（壳的核心拦截逻辑）。"""
    global _scrub_noted
    _scrub_foreign_legacy_path()
    if _scrubbed and not _scrub_noted:
        print(f"  [note] 已摘除外来 legacy sys.path 条目 ×{len(_scrubbed)}"
              f"（入口脚本导入所致，非本套件回归；qd_smoke 脚本直跑无此污染）")
        _scrub_noted = True
    before = len(qd_smoke.PASS)
    fn()                                   # 异常直接抛给 pytest（不吞）
    added = qd_smoke.PASS[before:]
    failed = [x for x in added if x is False]
    assert not failed, (
        f"{fn.__name__}: {len(failed)} 项 FAIL（[FAIL] 明细见上方 captured output）")


@pytest.fixture(scope="module", autouse=True)
def _smoke_trace_cleanup():
    """复刻脚本 __main__ 的 trace 自清包裹（只删 smoke 自己写的行）。"""
    base = qd_smoke._trace_baseline()
    yield
    qd_smoke._cleanup_trace_rows(base)


@pytest.mark.slow
@pytest.mark.parametrize("case", _CASES, ids=[c.__name__ for c in _CASES])
def test_qd_smoke_case(case):
    _run_smoke_case(case)
