# -*- coding: utf-8 -*-
"""
formatters/default.py — 通用兜底格式化

纯 LLM 自适应：不预设格式，让 LLM 根据数据类型自动组织输出。
适用于没有领域 formatter 的情况。
"""

from __future__ import annotations

import logging

from llm.base import ChatMessage
from .base import BaseFormatter

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
你是 QuantDinger AI 助手的数据分析模块。

你的任务是将原始工具返回数据整理成一份清晰、易读的分析报告。

规则：
1. 保留所有关键数据，不要遗漏重要信息
2. 用 Markdown 格式组织，使用表格、列表等增强可读性
3. 数据精度：价格保留2位小数，百分比保留2位，成交量用万/亿单位
4. 如果数据包含涨跌，用 ↑/↓/→ 标注方向
5. 在报告末尾给出简短的总结（1-3句话）
6. 不要编造数据，只基于提供的原始数据

输出格式：
- 先给出核心结论/摘要
- 再展开详细数据
- 最后总结
"""


class DefaultFormatter(BaseFormatter):
    """通用兜底格式化器。

    将原始数据交给 LLM，由 LLM 自适应组织输出。
    """

    async def format(self, raw_result: str, context: dict) -> str:
        """LLM 自适应汇总。"""
        llm = context.get("_llm")
        if not llm:
            logger.warning("[DefaultFormatter] 无 LLM 实例，跳过格式化")
            return raw_result

        system_prompt, user_content = self._build_prompt(raw_result, context, _SYSTEM_PROMPT)

        try:
            messages = [
                ChatMessage(role="system", content=system_prompt),
                ChatMessage(role="user", content=user_content),
            ]
            resp = await llm.generate(messages=messages)
            result = (resp.content or "").strip()
            if result:
                logger.info("[DefaultFormatter] 格式化完成: %d 字符 → %d 字符",
                            len(raw_result), len(result))
                return result
            else:
                logger.warning("[DefaultFormatter] LLM 返回空，使用原始数据")
                return raw_result
        except Exception as e:
            logger.error("[DefaultFormatter] 格式化失败: %s", e)
            return raw_result
