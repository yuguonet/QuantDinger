# -*- coding: utf-8 -*-
"""执行修复代理（提智方案 二波 B4）——**单次修复、fail-open、可关停**。

设计（2026-09-24，承接 B2 失败记忆）：
  · 触发面：**只有** B2 定死的可修复 error_type（hallucinated_tool / wrong_column /
    wrong_frequency）才尝试；sandbox_unavailable / empty_result / self_check_failed 属
    "环境或数据问题"，重试代码没有意义，直接交给失败记忆与数据自检处置。
  · **只许一次**：`REPAIR_MAX_ONCE = 1` 写死在调用方（task_agent 的 `_repair_step`），
    本模块不维护跨步状态；每次调用即"这一次尝试"。
  · **fail-open**：任何异常（无网关 / 超时 / 返回不可解析）→ 返回 ok=False + note，
    绝不抛给主流程、绝不阻塞、绝不重试第二遍。
  · **默认关闭**：env `AGENT_CODE_REPAIR`（"1" 开启）。上线先关，观测到收益再开。
  · 复用 `agents.task_agent._LLMAdapter`（延迟 import，避免循环依赖）。

注：原方案提到的 `_inject_disclaimers` 全仓不存在（幻觉引用）；本模块的"降级声明"
由 B3 的 `missing_data` + verify_node 的 DEGRADE 承担，不另设注入符号。
"""
from __future__ import annotations

import os
import re
from typing import List, Optional, Sequence

__all__ = ["REPAIRABLE_ERROR_TYPES", "REPAIR_MAX_ONCE", "repair_enabled",
           "RepairResult", "repair_code"]

# 只修"代码写法问题"，不修"环境/数据问题"（与 failure_memory.ERROR_TYPES 对齐）
REPAIRABLE_ERROR_TYPES = ("hallucinated_tool", "wrong_column", "wrong_frequency")
REPAIR_MAX_ONCE = 1

_SYS = (
    "你是 Python 代码修复器。给你一段在受限沙箱中执行失败的代码、错误信息、以及可用工具名清单。\n"
    "只做**最小修改**让代码能跑通：\n"
    "- 名字不存在 → 换成清单里语义最接近的工具名；\n"
    "- 属性/取值方式错 → 按工具真实返回结构改（不要发明字段）；\n"
    "- 周期/频率非法 → 用合法取值。\n"
    "若无法确定正确写法，或错误属于环境受限/数据为空，直接回复 `NO_FIX`。\n"
    "只输出一个 ```python 代码块（或 NO_FIX），不要解释。"
)


class RepairResult:
    __slots__ = ("ok", "fixed_code", "note", "error_type")

    def __init__(self, ok: bool, fixed_code: Optional[str] = None,
                 note: str = "", error_type: str = ""):
        self.ok = ok
        self.fixed_code = fixed_code
        self.note = note
        self.error_type = error_type

    def __repr__(self) -> str:
        return f"RepairResult(ok={self.ok}, len={len(self.fixed_code or '')}, note={self.note[:40]!r})"


def repair_enabled() -> bool:
    return str(os.getenv("AGENT_CODE_REPAIR", "0")).strip().lower() in ("1", "true", "yes", "on")


_CODE_RE = re.compile(r"```(?:python)?\s*(.*?)```", re.S)


def _extract_code(text: str) -> str:
    m = _CODE_RE.search(str(text or ""))
    return (m.group(1) if m else "").strip()


def repair_code(
    code: str,
    error_text: str,
    *,
    error_type: str = "",
    available_tools: Optional[Sequence[str]] = None,
    adapter=None,
    timeout: float = 30.0,
    max_tools: int = 60,
) -> RepairResult:
    """单次修复尝试；fail-open。`adapter` 缺省时延迟构造 `_LLMAdapter`。"""
    if error_type and error_type not in REPAIRABLE_ERROR_TYPES:
        return RepairResult(False, note=f"error_type={error_type} 不在可修范围", error_type=error_type)
    if not str(code or "").strip() or not str(error_text or "").strip():
        return RepairResult(False, note="缺少代码或错误信息", error_type=error_type)

    tools: List[str] = [str(t) for t in (available_tools or []) if str(t).strip()][:max_tools]
    user = (
        f"# 失败代码\n```python\n{str(code)[:4000]}\n```\n\n"
        f"# 错误信息\n{str(error_text)[:1500]}\n\n"
        f"# 本阶段可用工具（只能用这些名字）\n{', '.join(tools) if tools else '（无数据工具）'}"
    )

    try:
        if adapter is None:
            from agents.task_agent import _LLMAdapter  # 延迟 import（避免循环依赖）
            from llm.factory import create_llm  # type: ignore
            adapter = _LLMAdapter(create_llm())
        client = adapter._get_sync_client()
        resp = client.chat.completions.create(
            model=adapter.model_id,
            messages=[{"role": "system", "content": _SYS},
                      {"role": "user", "content": user}],
            temperature=0.0,
            timeout=timeout,
        )
        text = (resp.choices[0].message.content or "").strip()
    except Exception as e:  # fail-open：任何异常都不抛出
        return RepairResult(False, note=f"调用失败: {type(e).__name__}: {str(e)[:120]}", error_type=error_type)

    if not text or text.upper().startswith("NO_FIX") or "NO_FIX" in text[:20].upper():
        return RepairResult(False, note="模型判定不可自动修复(NO_FIX)", error_type=error_type)

    fixed = _extract_code(text)
    if not fixed:
        return RepairResult(False, note="返回中无可解析代码块", error_type=error_type)
    if fixed.strip() == str(code).strip():
        return RepairResult(False, note="修复结果为原代码(无改动)", error_type=error_type)
    return RepairResult(True, fixed_code=fixed, note="ok", error_type=error_type)
