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

_CODE_FENCE_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)

_PROSE_RE = re.compile(
    r"^(?:[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]|(?:好的|例如|注意|现在|太好了|以上|这是|思路))"
)


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


def resilient_parse_code_blobs(text: str, code_block_tags: tuple[str, str]) -> str:
    """加固版 parse_code_blobs（签名兼容；v5：健全性校验 + 伪标签防线）。"""
    code = None
    try:
        code = _orig_parse(text, code_block_tags)
    except Exception:
        pass

    # 健全性校验（v5）：提取"成功"≠可用——伪标签匹配出的散文不是代码
    if code is not None:
        try:
            ast.parse(code)
        except SyntaxError:
            code = None  # 进入救援链

    if code is None:
        m = _CODE_FENCE_RE.findall(text)
        if m:
            code = "\n\n".join(x.strip() for x in m)
    if code is not None:
        try:
            ast.parse(code)
        except SyntaxError:
            code = None
    if code is None:
        code = _rescue_loose_code(text)

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

    # 最终校验（双保险）
    ast.parse(code)
    return code


def apply():
    """替换 smolagents.agents 的 parse_code_blobs 绑定（幂等）。"""
    _sm_agents.parse_code_blobs = resilient_parse_code_blobs
    return True
