# -*- coding: utf-8 -*-
"""CPython 语义代码执行器（2026-09-20 重写，路线 C）。

作用：替换 smolagents 的 `LocalPythonExecutor`（AST 解释器），用**真 exec** 运行模型代码，
同时保留本项目长出来的全部执行期能力（工具注入、跨步/跨阶段变量、幻觉调用纠正）。

为什么换（根因，2026-09-20 定案）：
  `LocalPythonExecutor` 是"自己实现的 AST 解释器"，与 CPython 语义有系统性偏差——
  实测 vararg 绑参错（`def f(d, *keys)` 的 keys 拿到 `({'a':1}, 'a')`，上游 issue #2754，
  1.27.0.dev0 仍未修）、默认值/kwonly/posonly 绑参错、仅 52 个内置（无 eval/exec/open/
  dir/vars/globals/locals）、dunder 默认禁、函数体解释执行导致**错误行号指到调用点**。
  官方已声明 `LocalPythonExecutor` "is not a security boundary"；本项目 2026-09-14 起
  import 全放行、2026-09-18 起破坏性操作也不拦 ⇒ 解释器的"安全职能"已全部作废，
  只剩语义偏差带来的误伤。故换真 exec，从根上消掉"解释器 ≠ CPython"这一整类问题。
  真隔离需求应走官方 `executor_type="e2b"/"docker"/"modal"/"blaxel"`，不是自建解释器。

关键设计点：
  1. **官方扩展点**：继承 `PythonExecutor`（ABC），经 `CodeAgent(executor=...)` 注入
     （agents.py:1534 `executor or self.create_python_executor()`）。只需实现
     `send_tools` / `send_variables` / `__call__ -> CodeOutput` 三件套。
  2. **命名空间就是 `self.state`**：exec 的 globals 直接用 state ⇒ 模型赋值直接落 state，
     于是 `_wrap_stage_guard` 写 `_r_<工具名>`、`_promote_model_vars` 促升、跨阶段投影
     三处既有机制**一行都不用改**。
  3. **全量真内置**（用户 2026-09-20 决策）：`vars(builtins)` 整份给，只覆盖 `help`
     （pydoc 交互式 help 会读 stdin 阻塞）。eval/exec/open/dir/vars/globals/locals/
     `__import__` 全部按 CPython 语义可用 ⇒ 原 `_UNAVAILABLE_BUILTINS` 纠正层整段退役。
  4. **观测语义对齐上游**：最后一条语句若是表达式则 eval，复刻 "Last output from code
     snippet"；print 经 `PrintContainer` 收集并按 `max_print_outputs_length` 截断；
     `final_answer` 抛 `FinalAnswerException`（BaseException，模型的 `except Exception`
     吞不掉）⇒ `CodeOutput.is_final_answer=True`。

容易误解/易错点：
  · `state["_print_outputs"]` **必须存在**：CodeAgent 在异常路径要读它拼观测
    （agents.py:1734-1741），缺了会丢掉全部执行日志。
  · `state["__builtins__"]` 是 dict（不是 module）——CPython 允许，且它下划线开头，
    判官摘要与变量促升都会跳过它。**每次执行都重建**（内含 per-call 的 print 收集器）。
  · `_promote_model_vars` 的排除名单必须含 `__builtins__`（它是 dict 不是 callable，
    否则会被当数据变量促升进会话级存储，跨阶段反复投影一个大字典）。
  · 超时复用上游 `timeout()`（线程版，跨平台）。线程超时**无法强杀**，超时后该线程会
    继续跑完（上游同限制）；外层 `AGENT_RUN_WALL_TIMEOUT` 仍大于本超时，保证外层先降级。
  · 本执行器**不是安全边界**（与上游声明一致，也与本项目"import 全放行"的现状一致）：
    模型代码能读写文件、起进程。要真隔离请换官方远程 executor。
"""
from __future__ import annotations

import ast
import builtins
import logging
import re
import time

from smolagents.local_python_executor import (
    BASE_PYTHON_TOOLS,
    DEFAULT_MAX_LEN_OUTPUT,
    CodeOutput,
    FinalAnswerException,
    InterpreterError,
    PrintContainer,
    PythonExecutor,
    timeout,
    truncate_content,
)

logger = logging.getLogger(__name__)

# 未定义名字（CPython 措辞，与解释器的 "The variable 'x' is not defined" 不同）。
# 模型写错工具名/变量名时命中——错的是名字，不是"沙箱不允许"，纠正话术见 _rewrite。
_NAME_ERR_RE = re.compile(r"name '([\w.]+)' is not defined")


def _help_no_stdin(obj=None) -> str:
    """`help` 覆盖版：只渲染文档文本，不进交互模式（裸 `help()` 会读 stdin 阻塞进程）。"""
    try:
        import pydoc
        if obj is None:
            return "（本环境不提供交互式帮助；传入对象可查看其文档，如 help(str)）"
        return pydoc.render_doc(obj)[:3000]
    except Exception as e:
        return f"help 不可用: {e}"


def _make_print(container: PrintContainer):
    """构造本次执行的 print：写入容器（不进观测），语义与内置 print 一致。

    `file=` 指向别的对象时按 CPython 原样委派给真 print（本环境不做 I/O 拦截）。
    """
    def _print(*args, sep=" ", end="\n", file=None, flush=False):
        if file is not None and file is not container:
            print(*args, sep=sep, end=end, file=file, flush=flush)
            return
        container.append(sep.join(str(a) for a in args) + end)
    return _print


class GuidedCPythonExecutor(PythonExecutor):
    """真 exec + 幻觉调用纠正 + 跨阶段变量的代码执行器。

    构造参数对齐 smolagents 原生 `LocalPythonExecutor` 中本项目实际用到的三项
    （`additional_authorized_imports` 不再需要：真 exec 无 import 限制）。
    """

    # 促升时排除的 state 内部键（见文件头"易错点"）。
    _NON_VAR_STATE_KEYS = frozenset({"__name__", "__builtins__", "_print_outputs", "_qd_stats"})

    def __init__(self, max_print_outputs_length: int | None = None,
                 additional_functions: dict | None = None,
                 timeout_seconds: int | None = None):
        self.custom_tools: dict = {}
        self.static_tools: dict = {}
        self.state: dict = {"__name__": "__main__"}
        self.max_print_outputs_length = max_print_outputs_length or DEFAULT_MAX_LEN_OUTPUT
        self.additional_functions = dict(additional_functions or {})
        self.timeout_seconds = timeout_seconds
        # 权威工具表（2026-09-14）：由 install_tools() 登记，每次执行前重装。
        # 阶段重试会清空 custom_tools/state（复用 CodeAgent 时实测），故不依赖注入时序。
        self._authoritative_tools: dict = {}
        # 全局已知工具名（2026-09-22）：provider 全量名字（含本阶段未点名的）。
        # 仅用于未定义名字纠正话术——告诉模型"该工具存在但未列入本阶段白名单"，
        # 避免模型继续猜名字烧步数。不参与任何注入/执行判定。
        self._all_known_tools: set = set()
        # send_tools 给的 agent 工具（final_answer / list_tools / search_tools / …）。
        # 与 state 分开存：它们每次执行都要可调用，但不该被当成"数据变量"参与促升与摘要。
        self._agent_tools: dict = {}
        # 2026-09-15 两级统一：非空时，每次执行成功后把模型命名变量促升到该 scope 的
        # 会话级存储（跨阶段续承）。由 task_agent 在拿到 run_scope 后设置。
        self.session_vars_scope: str | None = None
        # 判官摘要用的诚实计数（原解释器的 _operations_count 已随解释器一起消失）。
        self._code_blocks = 0
        self._builtins_ns = {**vars(builtins), "help": _help_no_stdin}

    # ── 官方三件套 ────────────────────────────────────────────────────────

    def send_tools(self, tools: dict) -> None:
        """CodeAgent 每个 run 开头调一次（agents.py:492）。

        存两份：`static_tools` 保持上游同名属性（含 BASE_PYTHON_TOOLS）以备排查；
        `_agent_tools` 是真正要注入命名空间的（剔除 BASE_PYTHON_TOOLS——全量真内置已覆盖，
        重复注入只会污染 state 与判官摘要）。
        """
        tools = tools or {}
        self.static_tools = {**tools, **BASE_PYTHON_TOOLS.copy(), **self.additional_functions}
        self._agent_tools = {k: v for k, v in tools.items() if k not in BASE_PYTHON_TOOLS}

    def send_variables(self, variables: dict) -> None:
        """合并进 state（**合并**而非替换）——会话级变量投影依赖这一点。"""
        self.state.update(variables or {})

    def __call__(self, code_action: str) -> CodeOutput:
        try:
            tree = ast.parse(code_action)
        except SyntaxError as e:
            raise InterpreterError(
                f"Code parsing failed on line {e.lineno} due to: {type(e).__name__}: {e}\n"
                f"{e.text}{' ' * (e.offset or 0)}^"
            ) from None

        self._refresh_namespace()
        container = PrintContainer()
        self.state["_print_outputs"] = container
        self._code_blocks += 1
        self.state["_qd_stats"] = {"code_blocks": self._code_blocks}
        self.state["__builtins__"] = {**self._builtins_ns, "print": _make_print(container)}

        t0 = time.time()
        try:
            try:
                output, is_final_answer = self._run(tree, code_action)
            except InterpreterError as e:
                raise InterpreterError(self._rewrite(str(e))) from None
        finally:
            # 出错路径也要截断（上游同做法）：CodeAgent 会读 _print_outputs 拼观测。
            container.value = truncate_content(
                str(container), max_length=self.max_print_outputs_length
            )
        logger.debug("[CPythonExecutor] 代码块 #%d 执行 %dms（final=%s）",
                     self._code_blocks, int((time.time() - t0) * 1000), is_final_answer)
        try:
            self._promote_model_vars()
        except Exception as _pe:
            # 促升失败不得影响主流程，但必须留痕——本项目多次栽在"静默吞掉"。
            logger.warning("[CPythonExecutor] 会话级变量促升失败: %s", _pe)
        return CodeOutput(output=output, logs=str(container), is_final_answer=is_final_answer)

    # ── 执行内核 ──────────────────────────────────────────────────────────

    def _run(self, tree: ast.Module, code_action: str):
        """逐条执行 AST：前 n-1 条 exec，最后一条是表达式则 eval（复刻上游"最后输出"语义）。

        按**原 AST 节点**编译子树：节点自带 lineno，故行号与源码一致
        （这是本执行器相对旧解释器最直接的收益：报错行不再是调用点）。
        """
        body = list(tree.body)
        if not body:
            return None, False

        ns = self.state

        def _execute():
            result = None
            node = body[-1]
            try:
                for prev in body[:-1]:
                    exec(compile(ast.Module(body=[prev], type_ignores=[]), "<agent>", "exec"), ns)
                if isinstance(node, ast.Expr):
                    result = eval(compile(ast.Expression(body=node.value), "<agent>", "eval"), ns)
                else:
                    exec(compile(ast.Module(body=[node], type_ignores=[]), "<agent>", "exec"), ns)
            except FinalAnswerException as e:
                # final_answer(...) 的正常终止路径（BaseException，模型的 except Exception 吞不掉）
                return e.value, True
            except KeyboardInterrupt:
                raise
            except BaseException as e:
                # SystemExit/GeneratorExit 等 BaseException 若外泄会穿透 CodeAgent 的
                # `except Exception`（agents.py:1733）直到打死 worker，故在此统一转成
                # InterpreterError——错误可见、进程存活。
                seg = ast.get_source_segment(code_action, node) or ""
                raise InterpreterError(
                    f"Code execution failed at line '{seg}' due to: {type(e).__name__}: {e}"
                ) from None
            return result, False

        if self.timeout_seconds is not None:
            return timeout(self.timeout_seconds)(_execute)()
        return _execute()

    # ── 命名空间与工具注入 ────────────────────────────────────────────────

    def install_tools(self, tools: dict) -> None:
        """登记**权威工具表**并立即装入命名空间。

        2026-09-14 实证：阶段**重试**（复用 CodeAgent）时 `custom_tools` 被清成 0、
        `state` 被重置成只剩 4 项，工具全丢 ⇒ 调用即被误报"幻觉调用"，而首次运行一切正常
        ——这正是"时好时坏"的来源。故 executor 自己持有权威副本，**每次执行前无条件重装**。
        """
        self._authoritative_tools = dict(tools or {})
        self._refresh_namespace()

    def _refresh_namespace(self) -> None:
        """把工具与内置装进命名空间。每次 `__call__` 前调一次，不依赖任何注入时序。

        顺序即优先级：权威工具 → agent 工具 → custom_tools → additional_functions。
        `additional_functions` 放最后，保证 `final_answer` 不被同名工具顶掉。
        """
        ns = self.state
        if self._authoritative_tools:
            ns.update(self._authoritative_tools)
            self.custom_tools.update(self._authoritative_tools)
        if self._agent_tools:
            ns.update(self._agent_tools)
        if self.custom_tools:
            ns.update(self.custom_tools)
        if self.additional_functions:
            ns.update(self.additional_functions)

    def set_all_known_tools(self, names) -> None:
        """登记 provider 全量工具名（纠正话术用，见 _all_known_tools 注释）。"""
        self._all_known_tools = set(names or {})

    def _tool_names(self) -> list:
        """真实可用的**工具名**（业务工具 + agent 工具），不含 Python 内置。

        只列工具：内置函数（print/len/repr…）本来就能直接用，列进来只会稀释清单
        （2026-09-14 事故：清单里全是 ArithmeticError/BaseException 等内置异常名，
        真实工具一个都显示不出来，模型据此误判 technical_analysis 是幻觉调用）。
        """
        builtin_names = set(dir(builtins))
        names = set()
        for src in (self._authoritative_tools, self._agent_tools, self.custom_tools):
            names.update(k for k in (src or {}) if not k.startswith("__"))
        names -= builtin_names
        names -= set(BASE_PYTHON_TOOLS.keys())
        names -= set(self.additional_functions.keys())
        return sorted(names)

    def _rewrite(self, err_text: str) -> str:
        """把 CPython 原生错误改写成"能直接照做"的纠正话术。

        保留两类（对应两种真实误用）：
          · 未定义名字（NameError）——多为写错工具名/变量名；
          · 属性误用（AttributeError）——多为把 list 当 dict（`.get`）用。
        其余错误原样抛出：真 exec 下行号已准确，再包一层话术只会稀释信息。
        """
        m = _NAME_ERR_RE.search(err_text)
        if m:
            name = m.group(1)
            tools = self._tool_names()
            logger.warning(
                "[幻觉调用拦截] name=%s | state=%d custom_tools=%d authoritative=%d",
                name, len(self.state), len(self.custom_tools or {}), len(self._authoritative_tools or {}))
            if tools:
                shown = ", ".join(tools[:40])
                tail = f"…（共 {len(tools)} 个，其余可用 search_tools 按关键字查找）" if len(tools) > 40 else ""
                avail = shown + tail
            else:
                avail = "（本阶段无数据工具，仅计算能力）"
            # 2026-09-22：区分「存在但未点名」与「完全未知」——前者是 planner 白名单
            # 未覆盖（实测：技能文档/历史记忆让模型知道 calculate_ma 存在，引用即
            # NameError），明确告知比让模型猜省 1-2 步。
            known_hit = name in self._all_known_tools
            head = (
                f"[未定义名字] `{name}` 在当前命名空间里不存在。\n"
                f"{err_text}\n"
            )
            import re as _re2
            _is_cjk = bool(_re2.search(r"[\u4e00-\u9fff]", name))
            if _is_cjk:
                head += (
                    f"⚠ 未定义的名字 `{name}` 是**中文文本**——你很可能把报告/文档正文"
                    f"直接当 Python 代码提交了。正确做法：把报告文本作为字符串传给交付工具，"
                    f"例如 final_answer(\"报告正文…\")，或 final_answer(report_variable)。"
                    f"代码块里只能有合法 Python 语句。\n"
                )
            elif known_hit:
                head += (
                    f"⚠ `{name}` 是系统里真实存在的工具，但**未被列入本阶段的白名单**"
                    f"（planner 未点名），本阶段无法调用它。\n"
                    f"请立即停止尝试这个名字及其变体。\n"
                )
            else:
                head += (
                    f"两种可能，按序自查：\n"
                    f"  1) 拼写/大小写错误，或变量名写错（变量跨步保留，本阶段直接用名字引用即可）；\n"
                    f"  2) 该名字是想调用的工具，但不在本阶段可用清单里——"
                    f"请立即停止尝试这个名字及其变体。\n"
                )
            return (
                head
                + f"可用工具清单（仅限这些）：{avail}\n"
                f"（Python 内置函数 print/len/dir/type/… 可直接调用，不在此列）\n"
                f"处理方式（二选一）：\n"
                f"  1) 用清单内工具重新实现该步骤；\n"
                f"  2) 清单内无对应能力时，直接用纯 Python 计算实现，并在最终答复中说明该能力暂缺。"
            )
        if "has no attribute" in err_text:
            return (err_text
                    + "\n[纠正提示] 该对象没有这个方法/属性。请先确认变量类型"
                      "（print(type(x))）与正确用法；如需调用工具，只能使用可用工具清单中的名称。")
        return err_text

    # ── 跨阶段变量促升（2026-09-15 两级统一）──────────────────────────────
    # 两级共用同一个投递方式（state 里的 Python 变量），差别只在**能否越过快照边界**：
    # 本方法把"模型自己命名"的变量写进会话级存储，由它跨阶段存活。
    #
    # 排除三类，否则会把下一阶段的命名空间搞脏：
    #   ① 执行器内部项（__name__ / __builtins__ / _print_outputs / _qd_stats）；
    #   ② 注入进 state 的工具与函数本体（callable）——它们每阶段都会重装；
    #   ③ 2 级自动变量 `_r_*`：工具原始载荷量大且属本阶段过程数据，不该跨阶段堆积。
    #      模型若确实要留到下阶段，只需 `quotes = _r_quotes_1` 起自己的名字。
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
