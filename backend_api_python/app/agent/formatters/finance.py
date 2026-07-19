# -*- coding: utf-8 -*-
"""
formatters/finance.py — 金融领域格式化

样板实现：将金融数据汇总为简洁分析报告。

适用场景：
  - 个股分析（技术面、基本面、资金面）
  - 大盘/指数分析
  - 板块分析
  - 筛选结果汇总

输出规范：
  - 操作建议：买入/持有/卖出/观望/跳过
  - 评分：0-100
  - 方向：看多/看空/中性
  - 核心结论 + 关键数据（列表）
  - 控制在300字以内
"""

from __future__ import annotations

import logging

from llm.base import ChatMessage
from .base import BaseFormatter, register_formatter

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
你是 QuantDinger 的金融分析报告模块。

将原始金融数据整理为简洁的分析报告。

## 输出格式

**标的**: 名称(代码) 或 "大盘/板块"
**操作建议**: 买入/持有/卖出/观望/跳过
**评分**: 0-100
**方向**: 看多/看空/中性

## 核心结论
（2-3句话概括关键发现）

## 关键数据
- 涨跌幅、成交量、资金流向等核心指标
- 用列表而非表格，省 token

## 风险提示
（如有）

规则：
1. 不要编造数据，只基于原始数据
2. 价格保留2位小数，百分比保留2位
3. 涨用↑，跌用↓
4. 控制在300字以内
"""


@register_formatter("finance")
class FinanceFormatter(BaseFormatter):
    """金融领域格式化器。

    将金融原始数据汇总为结构化分析报告。
    """

    async def format(self, raw_result: str, context: dict) -> str:
        """汇总为金融分析报告。"""
        llm = context.get("_llm")
        if not llm:
            logger.warning("[FinanceFormatter] 无 LLM 实例，跳过格式化")
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
                logger.info("[FinanceFormatter] 格式化完成: %d 字符 → %d 字符",
                            len(raw_result), len(result))
                return result
            else:
                logger.warning("[FinanceFormatter] LLM 返回空，使用原始数据")
                return raw_result
        except Exception as e:
            logger.error("[FinanceFormatter] 格式化失败: %s", e)
            return raw_result
