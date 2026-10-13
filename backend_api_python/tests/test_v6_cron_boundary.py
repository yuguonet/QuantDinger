# -*- coding: utf-8 -*-
"""D-2：`app/agent/cron` 的**对外能力出口契约**守卫（2026-10-14）。

【D-2 原始记载】「`cron/` 被**旧系统**反向依赖 —— 删旧系统会断掉定时任务」。
2026-10-14 复核结论：**方向本身不是缺陷，是没被记录**。

  · 允许方向（外部 → agent）：`app/__init__.py`（启动 worker）、
    `app/routes/cron.py`（CRUD/手工执行）、`app/routes/health.py`（健康检查/订阅）
    都 import `app.agent.cron.*` —— 这是「方案代码只在 app/agent」的**预期形态**：
    agent 是能力提供方，旧系统是消费方。
  · 真正的风险是**反向**：若 cron 包反过来 import `app.routes` / `app.services`，
    就与消费方成环（routes → agent.cron → routes），删哪边都炸。本文件把它钉死。
  · 例外已确认并登记：`app/agent/tools/finance/*` 依赖 `app.services.*`
    （筹码唯一数学内核 `chip_service.compute_chip_core` 就在服务层），
    这是「复用现成方案、不造轮子」的产物，**不是**环（服务层不反向依赖 agent）。
    所以"禁 services"这条**只作用于 cron 子包**，不下到全 agent ——
    否则守卫会与既定的复用策略冲突，逼人去写第二份筹码内核。

【为什么用 AST 而不是 `in src`】注释/docstring 里必然出现 "app.services" 字样
（本文件就写了一大段），全文匹配会恒红。故一律解析 Import/ImportFrom 节点，
且 **相对 import 按文件所在包解析成绝对模块名** —— 只扫绝对形式会漏。

【TDD 说明】本文件里 ① 方向红线 ② 导出契约 都是"当前已成立的不变量"，
写出来本来就是绿的（RED 不可得）；它们靠 `tmp/mutate_d2_cron_20261014.py`
的变异检验证明有 bite。只有 ④ 契约注释是真正的 TDD（先红后绿）。
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
_AGENT_DIR = _BACKEND / "app" / "agent"
_CRON_DIR = _AGENT_DIR / "cron"

# 对外契约：**外部消费方 → 本包** 的导出名（改任一个名字 = 打破 routes/启动器）
_EXPORTS = (
    ("app.agent.cron.cron_worker", "start_cron_worker"),   # app/__init__.py:423 启动器
    ("app.agent.cron", "schedule_job_from_db"),            # app/routes/cron.py:107/154
    ("app.agent.cron", "unschedule_job"),                  # app/routes/cron.py:154/185
    ("app.agent.cron", "get_scheduled_jobs"),              # app/routes/health.py:43
    ("app.agent.cron", "subscribe"),                       # app/routes/health.py:81
    ("app.agent.cron", "unsubscribe"),                     # app/routes/health.py:81
    # 私有名被外部借用是**历史包袱**（routes/cron.py:214），未改；登记在此，
    # 将来若要收敛成公开名，改完必须同步本表与 routes 侧。
    ("app.agent.cron.cron_worker", "_execute_prompt_job"),
    ("app.agent.cron.cron_worker", "_execute_function_job"),
)

# 契约注释里必须列全的**消费方**（删旧系统/搬启动器时，这里先红 ⇒ 强制同步契约）
_CONSUMER_FILES = (
    _BACKEND / "app" / "__init__.py",
    _BACKEND / "app" / "routes" / "cron.py",
    _BACKEND / "app" / "routes" / "health.py",
)

# 扫描时排除的目录：tmp/ 是一次性探针脚本（设计机检的规模口径也把它单列），
# 里面的 import 不构成生产依赖。
_SKIP_DIRS = {"tmp", "__pycache__", "del"}


def _iter_py(root: Path):
    for p in root.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in p.relative_to(root).parts[:-1]):
            continue
        yield p


def _imported_abs_modules(py_file: Path, pkg_root: Path, base_pkg: str) -> set:
    """解析一个文件 import 到的**绝对模块名**（三形式：绝对 / from-import / 相对）。"""
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    rel_parts = list(py_file.relative_to(pkg_root).parts[:-1])   # 如 ['cron']
    pkg_parts = base_pkg.split(".") + rel_parts                  # ['app','agent','cron']
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # level=1 ⇒ 当前包本身；每 +1 上溯一层
                base = pkg_parts[: len(pkg_parts) - (node.level - 1)]
                out.add(".".join(base + ([node.module] if node.module else [])))
            elif node.module:
                out.add(node.module)
    return {m for m in out if m}


def _violations(root: Path, banned: tuple) -> list:
    bad = []
    for p in _iter_py(root):
        for mod in _imported_abs_modules(p, _AGENT_DIR, "app.agent"):
            for b in banned:
                if mod == b or mod.startswith(b + "."):
                    bad.append((str(p.relative_to(_BACKEND)), mod))
    return bad


# ── ① 对外导出契约：消费方用到的名字一个都不能少 ────────────────
@pytest.mark.parametrize("mod,name", _EXPORTS)
def test_cron_export_contract_still_importable(mod, name):
    obj = getattr(importlib.import_module(mod), name, None)
    assert callable(obj), f"`{mod}.{name}` 不可用 —— 消费方（routes/启动器）会 ImportError"


def test_cron_export_contract_list_is_not_stale():
    """契约表本身要有货（防止有人为了让测试变绿把表清空）。"""
    assert len(_EXPORTS) >= 8, "导出契约被删减了 —— 这正是 D-2「契约未记录」要防的事"


# ── ② 方向红线：cron 不得反向依赖 Web 层 / 服务层 ───────────────
def test_cron_does_not_import_web_or_service_layer():
    bad = _violations(_CRON_DIR, ("app.routes", "app.services"))
    assert not bad, (
        "cron 包反向依赖了 Web/服务层 ⇒ 与消费方成环（routes → agent.cron → routes）："
        f" {bad}")


def test_agent_does_not_import_web_layer():
    """全 agent 包不得 import `app.routes`（Web 路由层）。

    ⚠ 只禁 routes、**不禁 services**：finance 工具层复用 `app.services.*`
    是既定策略（筹码唯一内核在服务层），见本文件头。
    """
    bad = _violations(_AGENT_DIR, ("app.routes",))
    assert not bad, f"agent 反向依赖 Web 层 ⇒ 成环风险：{bad}"


# ── ③ 契约登记的消费方仍然成立 ─────────────────────────────────
def test_registered_consumers_still_import_cron():
    """三个消费方若不再依赖 cron（或新增了消费方），契约注释必须同步 —— 先红。"""
    for f in _CONSUMER_FILES:
        mods = _imported_abs_modules(f, _BACKEND / "app", "app")
        assert any(m.startswith("app.agent.cron") for m in mods), (
            f"{f.relative_to(_BACKEND)} 不再 import app.agent.cron —— "
            "D-2 的对外契约（消费方清单）需要同步更新")


# ── ④ 契约必须写在代码里（D-2 的根因：以前没记录）────────────────
def test_cron_package_docstring_records_the_contract():
    init = _CRON_DIR / "__init__.py"
    doc = ast.get_docstring(ast.parse(init.read_text(encoding="utf-8"))) or ""
    for marker in ("对外能力出口", "app/__init__.py", "app/routes/cron.py", "app/routes/health.py"):
        assert marker in doc, (
            f"cron 包 docstring 缺契约要素 {marker!r} —— "
            "D-2 的根因就是『依赖没被记录』，删了这段等于把 D-2 改回未做")
