# -*- coding: utf-8 -*-
"""代码提取加固层 v5（2026-09-12）：v4 原则 + 伪标签防线。

v5 新增（CLI 实测 run7）：原生正则 <code>(.*?)</code> 会被【模型散文中引用的字面
标签】误匹配（如"以 <code> 开头，以 </code> 结尾"这句话本身构成一对伪标签），
提取出半截散文并"成功"返回 → SyntaxError。

v5 原则（在 v4 最小干预基础上收紧一条）：
  - 原生提取成功后做【健全性校验】：结果必须 ast 可解析；
    解析失败 → 视为提取失败，进入救援链（散文跳过 + 最长可解析区段）；
  - 救援结果同样必须 ast 可解析，否则抛结构化错误（含原文与格式示范）；
  - 结构化错误提示中明确："散文中提到的标签对会被忽略，代码必须用真实标签包裹"。
"""
from __future__ import annotations

import ast
import re

import smolagents.agents as _sm_agents
from smolagents.utils import parse_code_blobs as _orig_parse

# v6（2026-09-19）：兼容模型输出的不同代码块语言标签。
# 实测 glm 系模型用 ````python，部分国产模型（如 XingChenAGI）用 ````code / ````text
# 标记代码块。原生 smolagents fallback 仅认 `python|py`，故 ````code 块会被彻底漏掉
# （解析失败 → AgentParsingError）。此处追加 code/text/txt/plaintext 等通用代码标签，
# 让两类写法都能被提取；非 Python 内容由后续 ast 健全性校验兜底。
_CODE_FENCE_RE = re.compile(r"```(?:python|py|code|text|txt|plaintext)?\s*\n(.*?)```", re.DOTALL)

_PROSE_RE = re.compile(
    r"^(?:[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]|(?:好的|例如|注意|现在|太好了|以上|这是|思路))"
)


_INVOKE_RE = re.compile(r'<invoke\s+name="([^"]+)"\s*>(.*?)</invoke>', re.DOTALL)
_PARAM_RE = re.compile(r'<parameter\s+name="([^"]+)"\s*>(.*?)</parameter>', re.DOTALL)


def _translate_invoke_xml(text: str) -> str | None:
    """把 <invoke name="tool">…</invoke> XML 工具调用语法转成 Python 调用。

    模型偶尔用 XML 工具调用语法（而非 <code> 块内的 Python）表达工具调用，
    例如 Step1 实测：`<execute><invoke name="get_market_overview"></invoke></execute>`。
    本函数做 best-effort 转译，让一次格式漂移不至于直接烧掉整步：
      - `<invoke name="get_market_overview"></invoke>` → `get_market_overview()`
      - `<invoke name="foo"><parameter name="a">1</parameter><parameter name="b">'x'</parameter></invoke>`
        → `foo(a=1, b='x')`
    多个 invoke 串行拼接为多行调用。转译结果必须 ast 可解析，否则返回 None
    （交给后续救援链 / 结构化错误）。
    """
    invokes = _INVOKE_RE.findall(text)
    if not invokes:
        return None
    lines: list[str] = []
    for name, body in invokes:
        params = _PARAM_RE.findall(body)
        if params:
            args = []
            for pname, pval in params:
                pval = pval.strip()
                # 尝试解析为 Python 字面量（数字/bool/None/列表/字典），失败则按字符串 repr 包裹
                try:
                    ast.literal_eval(pval)
                    arg = f"{pname}={pval}"
                except (ValueError, SyntaxError):
                    arg = f"{pname}={pval!r}"
                args.append(arg)
            lines.append(f"{name}({', '.join(args)})")
        else:
            lines.append(f"{name}()")
    code = "\n".join(lines)
    try:
        ast.parse(code)
    except SyntaxError:
        return None
    return code


def _rescue_loose_code(text: str) -> str | None:
    """无标签抢救：跳过散文行，提取最长【可解析】的 Python 区段。"""
    lines = text.split("\n")
    n = len(lines)
    best = None
    i = 0
    while i < n:
        if _PROSE_RE.match(lines[i].strip()):
            i += 1
            continue
        for j in range(n, i, -1):
            snippet = "\n".join(lines[i:j]).strip()
            if len(snippet) < 30:
                break
            try:
                ast.parse(snippet)
            except SyntaxError:
                continue
            if best is None or len(snippet) > len(best):
                best = snippet
            break
        i += 1
    return best


def _looks_like_final_answer(text: str) -> bool:
    low = text.lower()
    return "final_answer" in low or ("最终" in text and "答复" in text)


def _usable(code: str | None) -> str | None:
    """候选代码可用性校验：必须非空白且 ast 可解析。

    2026-09-19（v6）：旧实现只校验 ast，而 ast.parse("") 对空模块**合法**，
    导致「多个空 <code> 块 join 成空白串」被当成有效代码返回 → 沙箱执行空代码（Out: None）
    → agent 静默空转到超时（XingChenAGI 实测每步输出约 30 个空 `<code></code>`）。
    空/纯空白一律判为不可用，进入救援链或最终报错，让"模型没写代码"尽早暴露。
    """
    if not code or not code.strip():
        return None
    try:
        ast.parse(code)
    except (SyntaxError, ValueError):
        return None
    return code


def resilient_parse_code_blobs(text: str, code_block_tags: tuple[str, str]) -> str:
    """加固版 parse_code_blobs（签名兼容；v6：非空校验 + 标签兼容 + 伪标签防线）。"""
    code = None
    try:
        code = _orig_parse(text, code_block_tags)
    except Exception:
        pass
    code = _usable(code)  # 空/伪标签散文不是代码

    if code is None:
        m = _CODE_FENCE_RE.findall(text)
        code = _usable("\n\n".join(x.strip() for x in m)) if m else None
    if code is None:
        # XML 工具调用语法兜底（如 `<invoke name="tool">…</invoke>`）：转译为 Python 调用
        code = _usable(_translate_invoke_xml(text))
    if code is None:
        code = _usable(_rescue_loose_code(text))

    if code is None:
        if _looks_like_final_answer(text):
            raise ValueError(
                "[代码提取失败] 你的输出像最终答复但没有用工具调用表达。\n"
                f"原文（截断）：{text[:600]}\n"
                f"请用如下格式输出最终结果：\n{code_block_tags[0]}\n"
                'final_answer("你的最终答复文本")\n'
                f"{code_block_tags[1]}"
            )
        raise ValueError(
            "[代码提取失败] 输出中未找到有效代码块。\n"
            f"原文（截断）：{text[:600]}\n"
            "注意：说明文字中提到的 <code> 等标签对会被忽略——"
            f"代码必须用真实的 {code_block_tags[0]}…{code_block_tags[1]} 包裹，且块内只能是纯 Python。\n"
            "代码内不要插入解释性文字；若代码含三引号请改用 # 注释。"
        )

    return code


def apply():
    """替换 smolagents.agents 的 parse_code_blobs 绑定（幂等）。"""
    _sm_agents.parse_code_blobs = resilient_parse_code_blobs
    return True
