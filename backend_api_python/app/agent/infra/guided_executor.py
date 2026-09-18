# -*- coding: utf-8 -*-
"""幻觉调用纠正器 v3（2026-09-14）：allowed 名单延迟解析 + import 边界纠正。

版本沿革：
- v1 构造时要求传入 allowed_tool_names，但该名单在 `_build_code_agent` 里构造点
  之后才完整（smol_tools 定义在 executor 构造之后）→ UnboundLocalError。
- v2 改为延迟解析：`__call__` 出错时从 `self.static_tools`（send_tools 后的最终
  工具集）动态收集真实可用名单——比构造期更准确。
- v3（2026-09-14，**已废弃，v5 删除**）补 **import 类错误**覆盖：把
  `Import of sys is not allowed` 改写为"白名单清单 + 第 N 次撞同一限制"计数。
- v5（2026-09-18）删除上述 import 分支及其专用计数 `_err_counts`。原因：
  沙箱 import 已全放行（task_agent 传 `additional_authorized_imports=["*"]`；
  smolagents `check_import_authorized` 首层 `"*" in current_node` 即 return True）
  ⇒ 该错误**永不触发**，分支是死代码；且分支文案仍写"不在白名单内 /
  可 import 清单（仅限这些）"，而清单经 `discard("*")` 后**为空** ⇒ 万一触发
  会输出与真实能力矛盾的误导（本项目 §7.1 prompt 面变体：教错比不教更糟）。
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

from smolagents.local_python_executor import InterpreterError, LocalPythonExecutor

# 2026-09-14：本模块此前没有 logger（诊断日志加进来时 NameError 才暴露）。
logger = logging.getLogger(__name__)

_FORBIDDEN_RE = re.compile(r"Forbidden function evaluation:\s*'([\w.]+)'")
# 2026-09-14：模型为调试类型写 `type(x).__name__`，被沙箱的 dunder 拦截挡下后
# 不知道该怎么替代（实测卡在这一步）。给明确改法，而不是让它继续试。
_DUNDER_RE = re.compile(r"Forbidden access to dunder attribute:\s*(\w+)")

# 2026-09-18：沙箱**实际可用**的内置函数只有 52 个（smolagents BASE_PYTHON_TOOLS）。
# 下面这些常见内置**不在其中**，模型写它们会得到 `The variable 'x' is not defined`
# ——措辞像"自己写错变量名"，模型于是改名重试、反复烧步数（§8.4 剩余缺口的可触发实例）。
# 显式纠正：告诉它这个名字在本环境根本没有，而不是"你拼错了"。
# 名单取自实测的 BASE_PYTHON_TOOLS 键集；升级 smolagents 后须复核（教错比不教更糟，§7.1）。
_UNAVAILABLE_BUILTINS = {
    "eval": "直接写表达式（写成 `x + 1`，不要 `eval('x+1')`）",
    "exec": "直接写语句，不要用字符串执行",
    "compile": "不需要预编译，直接写可执行代码",
    "globals": "直接用变量名引用（变量在 executor.state，跨步/跨阶段都保留）",
    "locals": "直接用变量名引用",
    "__import__": "用 `import 模块名` 直接导入（import 已全放行）",
    "open": "本环境不做文件读写：结果用 final_answer 交付，不要落盘",
    "input": "本环境无交互输入（非 TTY），需要的值直接写成字面量或变量",
    "dir": "查工具返回结构用 list_tools()/search_tools()，或按「工具返回结构速查」直接按键访问",
    "vars": "同 dir：按速查表直接访问键，不要探查",
}
_UNDEF_VAR_RE = re.compile(r"The variable [`']?(\w+)[`']? is not defined")


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
#  破坏性操作：不做拦截（2026-09-18 用户定调）
# ═══════════════════════════════════════════════════════════════
# 沙箱已全删：import 全放行，本执行器**不再做任何破坏性拦截**。
# 曾有一层 `_scan_dangerous()` + `_approve()`（扫描 DANGEROUS_IMPORTS 后交用户确认），
# 但从未接线——非交互环境（pytest/服务）无人可问只能拒绝，接上会改变既有错误语义。
# 2026-09-18 连同 DANGEROUS_IMPORTS 一并删除：留着是"看起来有防护、实际零作用"。


class GuidedPythonExecutor(LocalPythonExecutor):
    """幻觉调用纠正执行器：错误信息带可用工具/模块清单与修复指令。

    allowed_tool_names 可省略——默认延迟从 static_tools 解析（send_tools 后的
    最终工具集，含 agent tools + BASE_PYTHON_TOOLS + additional_functions）。
    """

    def __init__(self, *args, allowed_tool_names: list | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._extra_allowed = list(allowed_tool_names or [])
        # 权威工具表（2026-09-14）：由 install_tools() 登记，每次执行前重装。
        self._authoritative_tools: dict = {}
        # 2026-09-15 两级统一：非空时，**每次执行后**把模型命名变量促升到该 scope 的
        # 会话级存储（跨阶段续承）。由 task_agent 在拿到 run_scope 后设置。
        self.session_vars_scope: str | None = None

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

    def _rewrite(self, err_text: str) -> str:
        um = _UNDEF_VAR_RE.search(err_text)
        if um and um.group(1) in _UNAVAILABLE_BUILTINS:
            name = um.group(1)
            return (
                f"[沙箱不可用] `{name}` 在本执行环境里**没有定义**——它不在可用的内置函数表内，"
                f"任何调用都会报同一个错，请立即停止尝试这个名字及其变体。\n"
                f"替代写法：{_UNAVAILABLE_BUILTINS[name]}\n"
                f"（本沙箱可用内置仅 52 个：print/len/range/list/dict/tuple/set/enumerate/zip/"
                f"sorted/sum/min/max/abs/round/int/str/float/bool/type/isinstance/getattr/"
                f"hasattr/setattr/map/filter/iter/next 等；"
                f"eval/exec/compile/open/input/dir/vars/globals/locals/__import__ 一律不可用）"
            )
        dm = _DUNDER_RE.search(err_text)
        if dm:
            # 兜底：正常情况 `_allow_dunder_attributes()` 已放行 dunder，走不到这里。
            # 仅在 patch 失效（如 smolagents 升级改名）时给替代写法；此时**不再声称
            # "沙箱禁止"**（已不禁止），否则与放行的事实自相矛盾。
            return (
                f"[属性访问] 无法访问属性 `{dm.group(1)}`。\n"
                f"替代写法：查看类型用 print(type(x))；查看内容用 print(str(x)[:500])。\n"
                f"若数据来自变量（工具返回值）：非字符串内容返回的是**活对象**，直接当 dict/list 用"
                f"（无需 json.loads）；只有文本类内容才是字符串。"
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
        self._ensure_tools_available()

    def _ensure_tools_available(self) -> None:
        """确保权威工具表在 custom_tools / state / static_tools 三处可用。

        合并原 _reinstall_tools（权威表恢复）和 _sync_tools_into_static（custom→static 同步）。
        每次 __call__ 前调用一次，不再依赖任何注入时序。
        """
        tools = getattr(self, "_authoritative_tools", None) or {}
        if tools:
            self.custom_tools.update(tools)
            self.state.update(tools)
        static_tools = getattr(self, "static_tools", None)
        if isinstance(static_tools, dict):
            static_tools.update(self.custom_tools or {})

    # ── 两级统一的促升（2026-09-15）────────────────────────────────────────
    # 两级共用同一个投递方式（executor.state 里的 Python 变量），差别只在**能否越过
    # 快照边界**：本方法负责把"模型自己命名"的变量写进会话级存储，由它跨阶段存活。
    #
    # 排除三类，否则会把下一阶段的命名空间搞脏：
    #   ① 解释器内部项（__name__ / _print_outputs）；
    #   ② 注入进 state 的工具与函数本体（callable）——它们每阶段都会重装；
    #   ③ 2 级自动变量 `_r_*`：工具原始载荷量大且属本阶段过程数据，不该跨阶段堆积。
    #      模型若确实要留到下阶段，只需 `quotes = _r_quotes_1` 起自己的名字。
    _NON_VAR_STATE_KEYS = frozenset({"__name__", "_print_outputs"})

    def _promote_model_vars(self) -> int:
        """把本次执行后 state 中的模型命名变量促升到会话级。返回促升个数。"""
        scope = getattr(self, "session_vars_scope", None)
        if not scope:
            return 0
        try:
            from infra.staging import stage_put_obj as _put
        except Exception:
            return 0
        promoted = 0
        for name in list(self.state.keys()):
            if name in self._NON_VAR_STATE_KEYS or name.startswith("_r_"):
                continue
            try:
                value = self.state[name]
            except Exception:
                continue
            if callable(value):          # 工具/函数本体不是数据变量
                continue
            try:
                if _put(scope, name, value):
                    promoted += 1
            except Exception:
                continue
        return promoted

    def __call__(self, code_action: str):
        # 只在**执行成功**后促升：失败的代码块不产出可信变量。
        try:
            self._ensure_tools_available()
            out = super().__call__(code_action)
        except Exception as e:
            text = str(e)
            # 需要改写的四类：幻觉工具调用 / 属性误用 / dunder 拦截 /
            # 沙箱未提供的内置函数（eval/exec/open/... 未定义，2026-09-18 新增）。
            # （import 越界改写分支已于 2026-09-18 删除——沙箱 import 全放行后该错误
            #   永不触发，留着只会输出"可 import 清单（空）"的误导，见文件头 v5。）
            # 其余错误原样抛出，不改变既有行为。
            if ("Forbidden function evaluation" in text or "has no attribute" in text
                    or "dunder attribute" in text or _UNDEF_VAR_RE.search(text)):
                raise InterpreterError(self._rewrite(text)) from None
            raise
        try:
            self._promote_model_vars()
        except Exception as _pe:
            # 促升失败不得影响主流程，但必须留痕——本项目多次栽在"静默吞掉"。
            logger.warning("[GuidedPythonExecutor] 会话级变量促升失败: %s", _pe)
        return out
