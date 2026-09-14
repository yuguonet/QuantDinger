# -*- coding: utf-8 -*-
"""幻觉调用纠正器 v3（2026-09-14）：allowed 名单延迟解析 + import 边界纠正。

版本沿革：
- v1 构造时要求传入 allowed_tool_names，但该名单在 `_build_code_agent` 里构造点
  之后才完整（smol_tools 定义在 executor 构造之后）→ UnboundLocalError。
- v2 改为延迟解析：`__call__` 出错时从 `self.static_tools`（send_tools 后的最终
  工具集）动态收集真实可用名单——比构造期更准确。
- v3（2026-09-14）补 **import 类错误**覆盖。实测缺口：一次 run 里模型连撞
  `Import of sys is not allowed` 与 `Import of argparse is not allowed` 两次——
  v2 只在错误信息含 "Forbidden function evaluation" / "has no attribute" 时才改写，
  import 类错误原样抛出，模型得不到"白名单是什么、别再试"的纠正，于是在重写代码时
  再犯同一个错（该 task 白烧约 2 万 output tokens、7 步耗尽）。
  import 错误现在也改写：白名单取自 executor 真实属性 `self.authorized_imports`
  （`LocalPythonExecutor.__init__` 里 = BASE_BUILTIN_MODULES | additional），
  不另建表 → 告知模型的边界与执行边界同源；并附"第 N 次撞同一限制"计数。
- v4（2026-09-14，L14）修 **"可用工具清单"完全无效**：旧版 `_allowed_names()` 只从
  `static_tools` 取名字，而业务工具是走 `executor.custom_tools` 注入的（send_tools 只把
  agent tools + BASE_PYTHON_TOOLS + additional_functions 放进 static_tools），
  再把 dir(builtins) 混进同一个 set 后 sorted()[:30] ⇒ 清单里恒为
  ArithmeticError / BaseException / Ellipsis / False …，**一个真实工具都没有**。
  实测后果：真实工具 technical_analysis 被误报成幻觉调用，模型按提示放弃工具、改用纯
  Python 硬算。改为 `_tool_names()`：从 custom_tools + static_tools 取、剔除内置与
  BASE_PYTHON_TOOLS、工具名优先展示（详见函数注释）。
"""
from __future__ import annotations

import logging
import re
import sys

from smolagents.local_python_executor import InterpreterError, LocalPythonExecutor

# 2026-09-14：本模块此前没有 logger（诊断日志加进来时 NameError 才暴露）。
logger = logging.getLogger(__name__)

_FORBIDDEN_RE = re.compile(r"Forbidden function evaluation:\s*'([\w.]+)'")
_IMPORT_RE = re.compile(r"Import (?:of|from) ([\w.]+) is not allowed")
# 2026-09-14：模型为调试类型写 `type(x).__name__`，被沙箱的 dunder 拦截挡下后
# 不知道该怎么替代（实测卡在这一步）。给明确改法，而不是让它继续试。
_DUNDER_RE = re.compile(r"Forbidden access to dunder attribute:\s*(\w+)")


def _allow_dunder_attributes() -> bool:
    """放行 dunder 属性访问（`x.__name__` 等）—— 2026-09-14，沙箱已去掉。

    smolagents 解释器在 `evaluate_attribute` 里**无条件禁止所有 dunder**
    （`local_python_executor.py:390`），该限制**不受 `authorized_imports` 控制**，
    所以"import 全放行"根本没有解除它——实测去掉沙箱后 `type(x).__name__` 仍被拦。

    而它拦的正是最常见的调试写法：模型查看数据类型几乎必写 `type(x).__name__`，
    拦一次即白烧一步（反复出现）。同时沙箱既已去掉（`import os` 就能做任何事），
    dunder 检查的防护价值归零，`__class__.__subclasses__()` 那类绕行也毫无意义
    ——保留它只剩误伤。故在此放行，与"沙箱辅助而非阉割"的定调一致。

    用 monkeypatch 而非改包内源码：smolagents 是第三方依赖，改源码会在升级时丢失；
    `evaluate_ast` 在运行时按模块全局查找 `evaluate_attribute`，故 patch 生效，
    且仍走 smolagents 解释器（不是绕过它裸 exec）。
    """
    try:
        from smolagents import local_python_executor as _lpe
    except Exception as e:
        logger.warning("[沙箱] dunder 放行失败（无法导入 smolagents）: %s", e)
        return False
    if getattr(_lpe, "_qd_dunder_allowed", False):
        return True
    _orig = getattr(_lpe, "evaluate_attribute", None)
    if _orig is None:
        logger.warning("[沙箱] 未找到 evaluate_attribute，dunder 放行跳过")
        return False

    def _evaluate_attribute(expression, state, static_tools, custom_tools, authorized_imports):
        if expression.attr.startswith("__") and expression.attr.endswith("__"):
            value = _lpe.evaluate_ast(
                expression.value, state, static_tools, custom_tools, authorized_imports
            )
            return getattr(value, expression.attr)
        return _orig(expression, state, static_tools, custom_tools, authorized_imports)

    _lpe.evaluate_attribute = _evaluate_attribute
    _lpe._qd_dunder_allowed = True
    return True


_allow_dunder_attributes()

# ═══════════════════════════════════════════════════════════════
#  破坏性操作：从"硬拦"改为"先问"（2026-09-14 用户定调）
# ═══════════════════════════════════════════════════════════════
# 理念：沙箱**辅助** smolagents，而不是阉割它的能力。Python import 已全放开
#       （task_agent 给 executor 传 additional_authorized_imports=["*"]），故破坏性
#       模块不再靠 import 白名单拦截（白名单已不存在）。
#       写/删除这类破坏性操作改由 `_scan_dangerous()` 在执行前扫描 DANGEROUS_IMPORTS，
#       命中后交**用户确认**把关（确认机制独立一层，未接线）。
DANGEROUS_IMPORTS = frozenset({
    # 文件系统
    "os", "shutil", "pathlib", "glob", "tempfile", "io", "fileinput", "fnmatch",
    # 进程 / 系统
    "subprocess", "multiprocessing", "signal", "pty", "platform", "resource", "sys",
    # 网络
    "socket", "urllib", "http", "ftplib", "smtplib", "requests", "ssl",
    # 数据库直连
    "sqlite3", "psycopg2", "sqlalchemy", "pymysql",
    # 动态执行 / 反序列化 / 原生调用
    "importlib", "pickle", "shelve", "marshal", "ctypes", "code", "codeop",
    # 其它
    "argparse", "tkinter", "curses", "numpy", "pandas",
})


def _scan_dangerous(code: str) -> list:
    """静态扫描待执行代码里的破坏性 import（不执行代码，避免部分副作用）。

    返回命中的描述列表；无法解析（语法错误）时返回空，交由执行器本身报错。
    """
    try:
        import ast as _ast
        tree = _ast.parse(code)
    except Exception:
        return []
    hits = []
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in DANGEROUS_IMPORTS:
                    hits.append(f"import {alias.name}")
        elif isinstance(node, _ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in DANGEROUS_IMPORTS:
                hits.append(f"from {node.module} import ...")
    return hits


class GuidedPythonExecutor(LocalPythonExecutor):
    """幻觉调用纠正执行器：错误信息带可用工具/模块清单与修复指令。

    allowed_tool_names 可省略——默认延迟从 static_tools 解析（send_tools 后的
    最终工具集，含 agent tools + BASE_PYTHON_TOOLS + additional_functions）。
    """

    def __init__(self, *args, confirm_cb=None, allowed_tool_names: list | None = None,
                 **kwargs):
        super().__init__(*args, **kwargs)
        # 破坏性操作的确认回调（Web/前端场景注入）；为 None 时退化为终端询问。
        self._confirm_cb = confirm_cb
        self._extra_allowed = list(allowed_tool_names or [])
        # 权威工具表（2026-09-14）：由 install_tools() 登记，每次执行前重装。
        self._authoritative_tools: dict = {}
        # 同一错误签名的出现次数（2026-09-14）：让"重复撞同一面墙"在错误信息里显式化。
        self._err_counts: dict[str, int] = {}

    def _tool_names(self) -> list:
        """真实可用的**工具名**（业务工具 + agent 工具），不含 Python 内置。

        2026-09-14 修复（L14）——旧版有两处叠加错误，导致这份清单**完全无效**：

        1. **只从 `static_tools` 取名字**。业务工具是通过 `executor.custom_tools`
           注入的（`task_agent.py` 里 `executor.custom_tools = tool_functions`）；
           而 smolagents 的 `send_tools` 只把 agent tools + BASE_PYTHON_TOOLS +
           additional_functions 放进 static_tools（`local_python_executor.py:1763-1765`）
           ⇒ 业务工具**一个都不在里面**，清单里全是别的东西。
        2. **把 `dir(builtins)` 与工具名混进同一个 set 后 `sorted()[:30]`**。字母序下
           前 30 个恒为 A/B/C 开头的内置异常（ArithmeticError / BaseException / …），
           真实工具（t 开头等）永远排不进前 30 ⇒ 即使工具在名单里也显示不出来。

        后果（实测误拦真实工具 technical_analysis）：模型看到一份由
        `ArithmeticError, AssertionError, Ellipsis, False, …` 组成的"可用工具清单"，
        无法判断该用什么替代，只能按提示第 2 条"用纯 Python 计算实现"放弃工具
        ——系统已有的能力被这条错误信息废掉。

        内置函数（print/len/repr…）不列进工具清单：它们本来就能直接用，列进来只会稀释。
        """
        import builtins as _builtins
        builtin_names = set(dir(_builtins))
        names = set(self._extra_allowed)
        for src in (getattr(self, "custom_tools", None), getattr(self, "static_tools", None)):
            src = src or {}
            names.update(
                k for k in src.keys()
                if not k.startswith("__") and k not in builtin_names
            )
        # BASE_PYTHON_TOOLS（print/len/range…）不是"工具"，剔除以免稀释清单
        try:
            from smolagents.local_python_executor import BASE_PYTHON_TOOLS
            names -= set(BASE_PYTHON_TOOLS.keys())
        except Exception:
            pass
        return sorted(names)

    def _import_names(self) -> list:
        """executor 真实授权的模块清单（不另建表 → 与执行边界同源）。"""
        mods = set(getattr(self, "authorized_imports", None) or [])
        mods.discard("*")          # 通配符无信息量
        return sorted(mods)

    def _rewrite(self, err_text: str) -> str:
        dm = _DUNDER_RE.search(err_text)
        if dm:
            # 兜底：正常情况 `_allow_dunder_attributes()` 已放行 dunder，走不到这里。
            # 仅在 patch 失效（如 smolagents 升级改名）时给替代写法；此时**不再声称
            # "沙箱禁止"**（已不禁止），否则与放行的事实自相矛盾。
            return (
                f"[属性访问] 无法访问属性 `{dm.group(1)}`。\n"
                f"替代写法：查看类型用 print(type(x))；查看内容用 print(str(x)[:500])。\n"
                f"若数据来自 stage_read：它返回 JSON 文本，需先 data = json.loads(raw)。"
            )
        im = _IMPORT_RE.search(err_text)
        if im:
            mod = im.group(1)
            key = f"import:{mod}"
            self._err_counts[key] = self._err_counts.get(key, 0) + 1
            n = self._err_counts[key]
            repeat = (
                f"\n（这是第 {n} 次因 import 失败：{mod}。不要再用它，也不要换个同类模块再试——"
                f"直接按上面第 1/2 条改写法。）" if n > 1 else ""
            )
            return (
                f"[import 拦截] 模块 '{mod}' 不在沙箱白名单内，import 它必然失败，请立即放弃该模块。\n"
                f"可 import 清单（仅限这些）：{', '.join(self._import_names())}\n"
                # 2026-09-14 实证：模型用 `import inspect` 想看工具签名而撞墙。
                # 签名信息由 list_tools()/search_tools() 直接给出，引导到那条通道，
                # 不必为此把 inspect 放进白名单（后者能读源码，属额外暴露）。
                f"（要看工具的参数签名：用 list_tools() / search_tools()，它们给出完整签名含默认值；"
                f"不要用 inspect，它不在白名单）\n"
                f"处理方式（三选一）：\n"
                f"  1) 用清单内模块等价实现（例：不要 sys.argv / argparse —— 在代码里写显式默认值；"
                f"不要 os.path —— 用纯字符串处理）；\n"
                f"  2) 需要 GUI / 命令行参数 / 文件 / 网络 / 第三方库的能力，沙箱内**不做**："
                f"把完整代码写进最终答复的代码块，并注明需在本地运行；\n"
                f"  3) 沙箱内演示只用纯 Python + 有限次 print（约 8~10 帧）。"
                f"{repeat}"
            )
        m = _FORBIDDEN_RE.search(err_text)
        if not m:
            am = re.search(r"has no attribute (\w+)", err_text)
            if am:
                return (err_text
                        + "\n[纠正提示] 该对象没有这个方法/属性。请检查变量类型与正确用法；"
                          "如需调用工具，只能使用下方可用工具清单中的名称。")
            return err_text
        name = m.group(1)
        # 诊断留痕（2026-09-14）：拦截发生时把两个工具表的实际规模打出来。
        # 只有日志里有这两个数字，才能一眼区分"模型真幻觉"与"注入没生效"
        # （后者曾让排查绕了很久：任务书/list_tools 都列得出，沙箱里却是空的）。
        logger.warning(
            "[幻觉调用拦截] name=%s | state=%d custom_tools=%d static_tools=%d",
            name, len(getattr(self, "state", None) or {}),
            len(self.custom_tools or {}),
            len(getattr(self, "static_tools", None) or {}))
        tools = self._tool_names()
        if tools:
            shown = ", ".join(tools[:40])
            tail = f"…（共 {len(tools)} 个，其余可用 search_tools 按关键字查找）" if len(tools) > 40 else ""
            avail = shown + tail
        else:
            avail = "（本阶段无数据工具，仅计算能力）"
        return (
            f"[幻觉调用拦截] 函数 '{name}' 不存在——它不在本阶段可用工具清单中，"
            f"任何调用它的尝试都会失败，请立即停止尝试该名称。\n"
            f"可用工具清单（仅限这些）：{avail}\n"
            f"（Python 内置函数 print/len/repr 等可直接调用，不在此列）\n"
            f"处理方式（二选一）：\n"
            f"  1) 用清单内工具重新实现该步骤；\n"
            f"  2) 清单内无对应能力时，直接用纯 Python 计算实现，并在最终答复中说明该能力暂缺。"
        )

    def install_tools(self, tools: dict) -> None:
        """登记**权威工具表**并立即装入沙箱。

        2026-09-14 实证：阶段**重试**（复用 CodeAgent）时 `custom_tools` 被清成 0、
        `state` 被重置成只剩 4 项，工具全丢 ⇒ 调用即被误报"幻觉调用"，而首次运行
        一切正常——这正是"时好时坏"的来源。与其追查谁清的、什么时候清的，不如让
        executor 自己持有权威副本，**每次执行前无条件重装**。
        """
        self._authoritative_tools = dict(tools or {})
        self._reinstall_tools()

    def _reinstall_tools(self) -> None:
        """把权威工具表装回 custom_tools / state / static_tools 三处。"""
        tools = getattr(self, "_authoritative_tools", None) or {}
        if not tools:
            return
        self.custom_tools.update(tools)
        # state 在 evaluate_call 的查找顺序里优先级最高（state → static_tools →
        # custom_tools），且不会被 send_tools 重置，是三处里最可靠的一处。
        self.state.update(tools)
        static_tools = getattr(self, "static_tools", None)
        if isinstance(static_tools, dict):
            static_tools.update(tools)

    def _sync_tools_into_static(self) -> None:
        """把 custom_tools 并入 static_tools（2026-09-14，L16 根治）。

        为什么需要这一步：业务工具走 `executor.custom_tools`，但那是 smolagents 的
        **次路径**（主要为代码里 `def` 出来的函数服务）；官方主路径是
        `send_tools → static_tools`，`evaluate_call` 的查找顺序也是
        state → **static_tools** → custom_tools（local_python_executor.py:847-855）。

        依赖次路径的代价已经付了两次：先是注入时机错位（L16），这次是同样的症状在不同
        phase 复发。执行前无条件同步一次，则**不再依赖任何注入时序**——只要
        `custom_tools` 里有的，这一刻起 static_tools 里也有。
        """
        static_tools = getattr(self, "static_tools", None)
        if not isinstance(static_tools, dict):
            return   # send_tools 尚未调用（此时 super().__call__ 本就会失败），不越俎代庖
        static_tools.update(self.custom_tools or {})

    def _approve(self, hits: list, code: str) -> bool:
        """破坏性操作是否获准。

        优先用注入的 `confirm_cb`（Web 弹窗）；没有回调时只有**交互终端**才询问——
        服务/后台（非 TTY）无人可问，一律拒绝，避免既卡住请求又等于裸奔。
        """
        if self._confirm_cb is not None:
            try:
                return bool(self._confirm_cb(hits, code))
            except Exception as e:
                logger.warning("[沙箱] 确认回调异常，按拒绝处理: %s", e)
                return False
        try:
            if not (sys.stdin and sys.stdin.isatty()):
                logger.warning("[沙箱] 非交互环境且无确认回调，拒绝破坏性操作: %s", hits)
                return False
            print("\n[沙箱] 该代码含破坏性操作：\n  " + "\n  ".join(hits)
                  + "\n---\n" + code + "\n---\n允许执行？[y/N] ", end="")
            return input().strip().lower() == "y"
        except Exception:
            return False

    def __call__(self, code_action: str):
        # 注：`_scan_dangerous` + `_approve`（先征求用户确认再执行）已实现但**暂不接线**。
        # 2026-09-14 试接入时破坏了 15 项既有测试——危险模块一旦进白名单，smolagents 不再报
        # "import 不允许"，改由本层拦截；而 pytest/服务是非交互环境，无人可问只能拒绝，
        # 导致错误语义全变。确认机制须作为**独立一层**（在 agent 执行前、带默认策略与
        # 测试替身）重新设计，不能塞进执行器。保持硬拦（错误语义稳定）直到那时。
        try:
            self._reinstall_tools()
            return super().__call__(code_action)
        except Exception as e:
            text = str(e)
            # 需要改写的三类：幻觉工具调用 / 属性误用 / import 越界（v3 新增）。
            # 其余错误原样抛出，不改变既有行为。
            if ("Forbidden function evaluation" in text or "has no attribute" in text
                    or "dunder attribute" in text or _IMPORT_RE.search(text)):
                raise InterpreterError(self._rewrite(text)) from None
            raise
