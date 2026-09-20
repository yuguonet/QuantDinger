# -*- coding: utf-8 -*-
"""
返回结构契约（Return-shape contracts）— 执行期提示词注入源（2026-09-20 改版）。

作用：
  把「每个工具的**真实返回结构**」拼进 CodeAgent 的 system_prompt，让模型取数后
  直接按键访问，而不是「取数 → print → 看类型 → 下一步再引用」的 REPL 式试探
  （6~11 步把 200K token 烧在类型探查上）。

  唯一真源 = **工具函数自己的 docstring 里的 `Returns:` 段**（Google 风格）。
  本模块只做**自动抽取**，不再维护任何平行契约表。

为什么改版（2026-09-20 根因）：
  旧版把契约集中登记在本文件的 `TOOL_RETURN_CONTRACTS` 大字典里（19 项）。
  实测注册工具 120 个 ⇒ 契约覆盖率 16%，101 个盲区；且**平行表会腐烂**——
  改工具返回结构时几乎没人记得回来改这张表，表里写的和代码里做的必然漂移。
  用户裁定：契约应当写在**工具描述**里、由程序自动匹配，未声明的走标准兜底。

与 OpenAI / MCP 标准的关系（查证结论，勿再走弯路）：
  · OpenAI Function Calling 的 tool 定义**没有返回结构字段**，只有
    `name` / `description` / `parameters`(JSON Schema) / `strict`；Structured Outputs
    约束的是**模型输出**，不是工具返回。⇒「把返回类型加进工具描述」正是官方口径内
    唯一可用的位置，本项目的 `tools/base.func_to_openai_schema` 已把 `Returns:` 段内容
    并入 schema 的 `description`（见该函数注释，2026-09-15 修复）。
  · 描述工具返回结构的既有标准是 MCP 的 `outputSchema`(JSON Schema) + `structuredContent`；
    本项目 `tools/mcp_bridge.py` 若将来要暴露工具给 MCP 客户端，可直接把同一个
    `Returns:` 段映射成 `outputSchema` 描述，无需另写一份。

易错点（改本文件前先读）：
  1. 抽取依赖 `inspect.getdoc()` 的**去缩进**结果：段头在 0 列、段体有缩进。
     故「段结束」判据 = **第一个 0 列非空行**（下一个段头，或段后的整段散文）。
     不要改成「遇到空行就结束」——段内空行是允许的；也不要只认「像段头的行」——
     本项目很多 docstring 在摘要后紧跟 0 列长注释，只认段头会把那段注释吞进契约
     （2026-09-20 实测：4 条契约被污染并触发 200 字符截断）。
  2. `Returns:` 段内容会被**折叠成单行**再拼进提示词（一工具一行，控 token）。
     段落里不要写需要保留换行的结构化内容。
  3. 未声明 `Returns:` 的工具**不是错误**：走 `_STANDARD_HANDLING` 标准兜底，
     不要为凑覆盖率编造返回结构（编造比没有更坏——模型会照错的键去访问）。
  4. 本模块只读 docstring，**不执行**任何工具，不在导入期做重活（导入期只编译正则）。
  5. 本模块内**所有函数一律下划线开头**：`tools/` 目录下的公开函数会被
     `ToolProvider._register_module_functions` 自动注册成**模型可调工具**。本模块是
     提示词构建器、不是工具，公开命名会让 `get_return_contract` 这类内部函数出现在
     模型工具表里（实测确实被注册过）。

工具 docstring 写法约定（新增/修改工具时遵守）：
  · `Returns:` 段放在**摘要段之后、长注释与 `Args:` 之前**。理由有三：
    ① 摘要后紧接返回结构，读代码的人第一眼就看到"这东西吐什么"；
    ② `func_to_openai_schema` 的 description 上限 1024 字符，放段尾会被长注释挤掉；
    ③ 排在 `Args:` 之前，`_parse_docstring_params` 不会把返回结构里的 `键: 说明`
       误当参数描述。
  · 内容格式：`类型: {顶层键, ...}。数据列表在 xx['二级键']（list）。`
    单/多参数返回结构不同的工具必须显式标注（如 get_realtime_quote）。
  · 长度控制在 ~120 字符内：这段每步都会重发，token 压力会从执行转移到提示词。
  · 成本实测（2026-09-20，`tmp/qclaw/audit_returns_contract_0920.py`）：典型阶段白名单
    8 工具 → 速查段 ~1.5K 字符；finance 全域 56 工具 → ~6.0K 字符（受 `max_declared=40`
    与单条 200 字符双重封顶）。**域模式此前完全不渲染**，这是本次新增的成本项，
    后续调 `max_declared` / `_MAX_CONTRACT_CHARS` 请以该脚本实测为准。
"""
from __future__ import annotations

import inspect
import re
from typing import Any, Callable, Iterable, Mapping

# 段头识别：Google 风格 + 中文变体。`Returns:` / `返回:` / `Yields:` 均视作返回结构段。
_RETURNS_HEADERS = frozenset({"returns", "return", "yields", "yield", "返回"})
# 0 列且形如 `Xxx: 内容` 的行 → 段头（段体有缩进，不会命中）
_SECTION_LINE_RE = re.compile(
    r"^([A-Za-z\u4e00-\u9fff][A-Za-z0-9 _\-]{0,24})\s*[:：]\s*(.*)$")


# 未声明返回结构的工具的标准处理口径（用户 2026-09-20 裁定："没描述的按标准处理"）。
# 目标不是禁止探查，而是把探查**限制成一次**并强制在同块内完成取值。
_STANDARD_HANDLING = (
    "未声明返回结构：调用后先 `isinstance(x, dict)` 判形，"
    "`list(x.keys())` **一次**确认键名（不要反复 print 试探），"
    "再在**同一代码块内**完成取值；列表通常在某个二级键下，勿对顶层 dict 直接切片。"
)

# 单工具契约的字符上限。超长（多为带字段注释的多行结构）会被截断——这是**故意的**：
# 本段每步重发，全量注册表按原样渲染要 8.7K 字符（≈4.4K token/步），会把"执行省下的
# token"原样搬到提示词里。超长结构的完整内容模型可随时用
# `print(tool_name.__doc__)` 就地读取（真 CPython 下这是一次普通调用，成本远低于每步重发）。
_MAX_CONTRACT_CHARS = 200
_TRUNCATED_HINT = " …（完整结构见 `print(<工具名>.__doc__)`）"



def _resolve_doc_target(tool: Any) -> Callable | None:
    """把「函数 / smolagents Tool 实例 / 类」统一解析到**承载 docstring 的可调用对象**。

    smolagents 的 `Tool` 实例把 docstring 写在 `forward()` 上（类本身通常只有说明），
    故实例优先取 `forward`；取不到再退回实例/类自身。
    """
    if tool is None:
        return None
    forward = getattr(tool, "forward", None)
    if callable(forward) and getattr(forward, "__doc__", None):
        return forward
    if callable(tool):
        return tool
    return None


def _extract_returns_section(tool: Any) -> str | None:
    """从工具 docstring 抽 `Returns:` 段，折叠成单行（超长按 `_MAX_CONTRACT_CHARS` 截断）。"""
    target = _resolve_doc_target(tool)
    if target is None:
        return None
    try:
        doc = inspect.getdoc(target) or ""
    except Exception:
        return None
    if not doc:
        return None

    body: list[str] = []
    in_returns = False
    for line in doc.split("\n"):
        if in_returns:
            if not line.strip():
                continue                           # 段内空行：跳过，不结束段
            if not line[0].isspace():
                break                              # **0 列非空行 = 段结束**
            body.append(line.strip())
            continue
        # 找段头：只认 0 列（`getdoc` 去缩进后，段头在 0 列、段体有缩进）
        if line[:1].isspace() or not line.strip():
            continue
        m = _SECTION_LINE_RE.match(line)
        if not m:
            continue
        if m.group(1).strip().lower() in _RETURNS_HEADERS:
            in_returns = True
            inline = m.group(2).strip()            # 支持 `Returns: dict: {...}` 同行写法
            if inline:
                body.append(inline)
    if not body:
        return None
    text = " ".join(body)
    if len(text) > _MAX_CONTRACT_CHARS:
        text = text[:_MAX_CONTRACT_CHARS].rstrip() + _TRUNCATED_HINT
    return text


def _get_return_contract(tool: Any) -> str | None:
    """取单个工具的返回结构契约（自动抽取）；未声明返回 None。

    Args:
        tool: 工具函数对象 / smolagents Tool 实例；传**工具名**（str）无法解析 docstring，
            恒返回 None —— 按名字取请改用 `_build_return_contract_block` 并传函数表。
    """
    if isinstance(tool, str):
        return None
    return _extract_returns_section(tool)


def _build_return_contract_block(tools: Mapping[str, Any] | Iterable[str],
                                 max_declared: int = 40,
                                 sampled: Mapping[str, str] | None = None) -> str:
    """拼出「工具返回结构速查」段（供 task_agent 注入 CodeAgent system_prompt）。

    两级真源，**采样优先、docstring 兜底**（2026-09-20 方案 D 接入）：
      · `sampled[name]`（来自 tools/returns_sampler.py 的真实调用采样）→ 首选，结构更准、
        覆盖率更高（实测 ~91% vs docstring ~64%）；
      · 未命中采样 → 回退工具 docstring 的 `Returns:` 段；
      · 两者都无 → 落 `_STANDARD_HANDLING` 标准兜底。

    Args:
        tools: **本阶段实际注入的工具表** `{name: 函数/Tool实例}`（首选，能读到 docstring）；
            也兼容只给名字的可迭代对象（此时全部落到标准兜底，仅用于兼容旧调用）。
        max_declared: 已声明契约的工具数上限（防某阶段工具极多时提示词膨胀）。
        sampled: `{name: 折叠后的返回结构单行}`（采样缓存命中项）。传 None = 纯 docstring 模式
            （采样未就绪 / 采样器不可用时自动走此路，零回归）。

    Returns:
        提示词段落；本阶段无工具时返回 ""。
    """
    if not tools:
        return ""
    if isinstance(tools, Mapping):
        items = [(str(k), v) for k, v in tools.items()]
    else:
        items = [(str(k), None) for k in tools]

    sampled = sampled or {}
    declared: list[tuple[str, str]] = []
    undeclared: list[str] = []
    for name, fn in items:
        if name in sampled:                     # ① 采样命中（最高优先）
            declared.append((name, sampled[name]))
            continue
        contract = _extract_returns_section(fn)  # ② docstring 兜底
        if contract:
            declared.append((name, contract))
        else:
            undeclared.append(name)

    if not declared and not undeclared:
        return ""
    declared.sort(key=lambda kv: kv[0])
    undeclared.sort()
    _n_sampled = sum(1 for n, _ in declared if n in sampled)

    head = ("【工具返回结构速查 — 取数后直接按键访问，勿再逐个 print 探查类型】"
            if not _n_sampled else
            "【工具返回结构速查 — 取数后直接按键访问，勿再逐个 print 探查类型】"
            "（结构取自真实调采样；obj{} 为对象、arr[] 为列表，深层元素默认泛化）")
    out = [head]
    if declared:
        for name, contract in declared[:max_declared]:
            out.append(f"- {name}() -> {contract}")
        if len(declared) > max_declared:
            rest = ", ".join(n for n, _ in declared[max_declared:])
            out.append(f"- （其余已声明工具：{rest}）")
    if undeclared:
        out.append("【未声明返回结构的工具 — 按标准处理】")
        out.append(f"- {', '.join(undeclared)}")
        out.append(f"  {_STANDARD_HANDLING}")
    out.append(
        "（凡返回含 error 键或 market_state='closed_today' 的工具，先判空/判 error 再使用；"
        "列表型结果一律通过上面标注的二级键访问，不要对顶层 dict 直接切片/迭代）"
    )
    return "\n".join(out)
