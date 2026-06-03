# -*- coding: utf-8 -*-
"""
Context Compressor — 跨轮上下文压缩。

agent.run() 结束后，把本轮结果（分析内容 + 工具调用）压缩成结构化 markdown，
存入 session store，下一轮作为上下文注入。

压缩后的格式示例：

    ## 上轮分析: 贵州茅台(600519)
    - 行情: 当前价 1850.00, 涨幅 +2.3%
    - 技术面: MACD 金叉, KDJ 超买
    - 资金流: 主力净流入 3.2 亿
    - 结论: 短期偏多, 关注 1900 压力位
    - 工具: get_realtime_quote, get_indicator_snapshot, get_fund_flow
"""
from __future__ import annotations

import logging
import os
from typing import List, Dict

logger = logging.getLogger(__name__)

_COMPRESS_PROMPT = """将以下 Agent 分析结果压缩为结构化 markdown 摘要。

## 要求
1. 保留关键数据（价格、涨跌幅、指标值、结论）
2. 保留涉及的股票代码和名称
3. 保留调用过的工具名列表
4. 去掉冗余描述，只留要点
5. 如果是代码相关任务，保留修改了哪些文件、做了什么改动
6. 控制在 300 字以内
7. 只输出 markdown，不要其他文字

## Agent 输出
{output}

## 工具调用记录
{tool_calls}
"""


def compress_context(
    output: str,
    tool_calls: List[Dict] = None,
    model: str = None,
    domain: str = "",
) -> str:
    """压缩 agent 本轮输出为 markdown 摘要。

    失败时返回 output 的前 500 字符（降级）。
    """
    if not output:
        return ""

    tool_text = ""
    if tool_calls:
        names = [tc.get("tool", "") for tc in tool_calls if tc.get("tool")]
        tool_text = ", ".join(names) if names else "（无）"
    else:
        tool_text = "（无）"

    # 太短不需要压缩
    if len(output) < 200:
        return output

    prompt = _COMPRESS_PROMPT.format(output=output[:3000], tool_calls=tool_text)

    try:
        from app.services.llm import LLMService
        import requests

        svc = LLMService(provider=None)
        api_key = svc.get_api_key()
        base_url = svc.get_base_url()
        compress_model = model or os.getenv("AGENT_COMPRESS_MODEL", "").strip() or svc.get_default_model()

        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": compress_model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 600,
            },
            timeout=60.0,
        )
        resp.raise_for_status()
        summary = resp.json()["choices"][0]["message"]["content"].strip()
        logger.info("[Compress] 原始 %d 字 → 压缩 %d 字", len(output), len(summary))
        return summary

    except Exception as e:
        logger.warning("[Compress] 压缩失败，降级截断: %s", e)
        return output[:500]
