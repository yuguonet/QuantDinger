# -*- coding: utf-8 -*-
"""幻觉调用纠正器 v2（2026-09-12）：allowed 名单延迟解析。

v2 修正：v1 在构造时要求传入 allowed_tool_names，但该名单在 _build_code_agent 里
构造点之后才完整（smol_tools 定义在 executor 构造之后）→ UnboundLocalError。
改为延迟解析：__call__ 出错时从 self.static_tools（send_tools 后的最终工具集）
动态收集真实可用名单——比构造期更准确。
"""
from __future__ import annotations

import re

from smolagents.local_python_executor import InterpreterError, LocalPythonExecutor

_FORBIDDEN_RE = re.compile(r"Forbidden function evaluation:\s*'([\w.]+)'")


class GuidedPythonExecutor(LocalPythonExecutor):
    """幻觉调用纠正执行器：错误信息带可用工具清单与修复指令。

    allowed_tool_names 可省略——默认延迟从 static_tools 解析（send_tools 后的
    最终工具集，含 agent tools + BASE_PYTHON_TOOLS + additional_functions）。
    """

    def __init__(self, *args, allowed_tool_names: list | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._extra_allowed = list(allowed_tool_names or [])

    def _allowed_names(self) -> list:
        names = set(self._extra_allowed)
        st = self.static_tools or {}
        # ?????????2026-09-12 ???repr ?"???"????smolagents
        # ???? BASE_BUILTIN_MODULES ???????????????
        # pre-check ????? Python builtins ?????
        import builtins as _builtins
        names.update(n for n in dir(_builtins) if not n.startswith("_"))
        # ?????????2026-09-12 ???repr ?"???"????smolagents
        # ???? BASE_BUILTIN_MODULES ???????????????
        # pre-check ????? Python builtins ?????
        import builtins as _builtins
        names.update(n for n in dir(_builtins) if not n.startswith("_"))
        names.update(k for k in st.keys() if not k.startswith("__"))
        # Python ??????"??"?2026-09-12 ???repr ????????
        # BASE_BUILTIN_MODULES???????????? static_tools ???
        try:
            from smolagents.local_python_executor import BASE_BUILTIN_MODULES
            names.update(BASE_BUILTIN_MODULES)
        except Exception:
            pass
        return sorted(names)

    def _rewrite(self, err_text: str) -> str:
        m = _FORBIDDEN_RE.search(err_text)
        if not m:
            am = re.search(r"has no attribute (\w+)", err_text)
            if am:
                return (err_text
                        + "\n[纠正提示] 该对象没有这个方法/属性。请检查变量类型与正确用法；"
                          "如需调用工具，只能使用下方可用工具清单中的名称。")
            return err_text
        name = m.group(1)
        avail = ", ".join(self._allowed_names()[:30]) or "（本阶段无数据工具，仅计算能力）"
        return (
            f"[幻觉调用拦截] 函数 '{name}' 不存在——它不在本阶段可用工具清单中，"
            f"任何调用它的尝试都会失败，请立即停止尝试该名称。\n"
            f"可用工具清单（仅限这些）：{avail}\n"
            f"处理方式（二选一）：\n"
            f"  1) 用清单内工具重新实现该步骤；\n"
            f"  2) 清单内无对应能力时，直接用纯 Python 计算实现，并在最终答复中说明该能力暂缺。"
        )

    def __call__(self, code_action: str):
        try:
            return super().__call__(code_action)
        except Exception as e:
            if "Forbidden function evaluation" in str(e) or "has no attribute" in str(e):
                new_text = self._rewrite(str(e))
                raise InterpreterError(new_text) from None
            raise