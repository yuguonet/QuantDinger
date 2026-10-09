"""test_scheduler_imports.py — 调度器导入面静态防线（零 import、纯 AST）。

背景（2026-10-09 审计 B-1 修复）:
    `app/market_cn/scheduler.py:363` 的 `_dragon_strategy_monitor` 里
    `from app.market_cn.auto.monitor import run_monitor_safe`，
    而 `monitor.py` 只有 `run_monitor()` —— `run_monitor_safe` **从来不存在**。
    该 Task 每 60s 触发一次，每次在 import 处抛 ImportError，被 `_worker` 的
    `except Exception` 吞成一行 "执行失败" ⇒
    开盘 gap 买入 / 盘中止损 / 预确认 / 收盘出场 / 15:01 确认 / exit 平账 / 组对账
    **整条盘中链在生产上停摆**，且日志上看不出是代码问题。

为什么用静态解析而不是真 import:
    - 真 import 会连带 import 全链（DB / registry / store），慢且有副作用；
    - 静态解析足以回答唯一的问题 ——「被 import 的名字，在目标模块顶层定义过吗」。

覆盖范围（有意收窄）:
    只查 `app.market_cn.scheduler.py` **函数体内部**的 `from X import a, b`，
    即延迟导入（import 写在函数里，避开循环依赖）。
    模块顶层的 import 不查 —— 那些在 import scheduler 时就会炸，不需要门禁。

误报豁免:
    `try: import A except ImportError: A = None` 这类**有意降级**的写法，
    在本仓不存在于 scheduler.py；若将来引入，用显式白名单登记，不要放宽规则。
"""

import ast
import os

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCHEDULER = os.path.join(_ROOT, "app", "market_cn", "scheduler.py")
APP_PKG = os.path.join(_ROOT, "app")


def _module_path(dotted: str):
    """`app.market_cn.auto.monitor` → 源码绝对路径（None = 非本仓模块，跳过）。"""
    if not dotted.startswith("app."):
        return None
    rel = dotted.split(".")[1:]                 # ["market_cn", "auto", "monitor"]
    base = os.path.join(APP_PKG, *rel)          # <root>/app/market_cn/auto/monitor
    for cand in (base + ".py", os.path.join(base, "__init__.py")):
        if os.path.isfile(cand):
            return cand
    return None


def _top_level_names(path: str):
    """模块顶层定义的绑定名（函数 / 类 / 变量 / import / for 目标 / with-as / except-as）。

    刻意覆盖 import 别名：`from a import b as c` 之后 `c` 就是该模块的顶层绑定名。
    不做语义分析 —— `__getattr__` 动态导出这类花招本仓不使用。
    """
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)

    names = set()
    for node in tree.body:                       # 只扫顶层，不进函数
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            for t in ast.walk(node.target):
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    for n in ast.walk(item.optional_vars):
                        if isinstance(n, ast.Name):
                            names.add(n.id)
        elif isinstance(node, ast.Try):
            for h in node.handlers:
                if h.name:
                    names.add(h.name)
    return names


def _deferred_imports(path: str):
    """返回 [(模块, [名字…])] —— 只取函数体内的 `from X import a, b`。"""
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)

    found = []
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        for node in ast.walk(fn):
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                found.append((node.module, [a.name for a in node.names]))
    return found


DEFERRED = _deferred_imports(SCHEDULER)


def test_scheduler_has_deferred_imports():
    """前置：本门禁确实有东西可查（否则下面的空通过毫无意义）。"""
    assert DEFERRED, "scheduler.py 里没有函数体内 from-import —— 本门禁已失效，请重新评估"


@pytest.mark.parametrize("module,names", DEFERRED,
                         ids=[f"{m}:{','.join(n)}" for m, n in DEFERRED])
def test_deferred_import_target_exists(module, names):
    """被 import 的每个名字，必须在目标模块顶层存在。

    失败信息按 2026-10-09 事故的口径写：明确指出「该任务实际未执行、其能力停摆」，
    避免再次被当成一条普通业务失败日志。
    """
    path = _module_path(module)
    if path is None:
        pytest.skip(f"{module} 非本仓模块（外部依赖），静态不可查")

    top = _top_level_names(path)
    missing = [n for n in names if n not in top]
    assert not missing, (
        f"接线错误：scheduler.py 延迟 import 的 {module} 中不存在 {missing}。"
        f"调用该 import 的任务每次触发都会抛 ImportError ⇒ "
        f"该任务实际从未执行，其覆盖能力全部停摆，且日志被 _worker 降级为普通失败。"
        f"修法：在 {module} 补出契约名（语义别名），而非在调用点包 try/except。"
    )


def test_dragon_monitor_contract_name_resolvable():
    """点名防线：`run_monitor_safe` 必须真实可解析。

    单独立一条而非只靠参数化，是为了让这条事故的名字直接出现在失败输出里。
    """
    from app.market_cn.auto.monitor import run_monitor_safe, run_monitor
    assert run_monitor_safe is run_monitor, (
        "run_monitor_safe 必须是 run_monitor 的契约别名（语义恒等），"
        "不要包装 try/except —— 盘中链失败必须抛出并被调度器记账。"
    )


def test_worker_does_not_swallow_wiring_errors():
    """`_worker` 必须能区分「接线错误」与「业务失败」，不得一律降级为一行 error。"""
    src = open(SCHEDULER, "r", encoding="utf-8").read()
    tree = ast.parse(src)
    worker = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_worker")
    caught = [n for n in ast.walk(worker)
              if isinstance(n, ast.ExceptHandler) and n.type is not None]
    assert caught, "_worker 失去了异常处理分支 —— 失败不再被记账，请复核"

    # 接线错误必须走 logger.exception（带 traceback），而非普通 logger.error
    handler_src = ast.unparse(caught[-1])
    assert "ImportError" in handler_src or "ModuleNotFoundError" in handler_src, (
        "_worker 未对 ImportError/ModuleNotFoundError 做特殊处理："
        "代码缺陷会与业务失败混为一条日志，盘中链停摆会再次被静默。"
    )
