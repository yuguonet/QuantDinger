# -*- coding: utf-8 -*-

"""
Intent Analyzer — 轻量级前置意图分析。

在 agent 执行前，用一次低成本 LLM 调用分析用户消息，
输出结构化的领域 + 意图 + 参数，供后续路由和工具过滤使用。

设计原则：
- 独立于 agent，不依赖工具目录，token 消耗极小
- 输出严格 JSON，便于程序化处理
- 失败时降级到默认流程（不阻塞主流程）
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.agent.domain_registry import (
    get_domain,
    get_all_domains,
    init_builtin_domains,
)

logger = logging.getLogger(__name__)


@dataclass
class IntentResult:
    """意图分析结果。"""
    domain: str = "chat"                       # 领域标识
    intent: str = ""                           # 具体意图
    params: Dict[str, Any] = field(default_factory=dict)  # 提取的参数
    confidence: float = 0.0                    # 置信度 0-1
    raw_response: str = ""                     # LLM 原始返回

    @property
    def domain_config(self):
        return get_domain(self.domain)

    @property
    def tool_filter(self) -> Optional[List[str]]:
        """该领域限定的工具列表，None 表示不限制。

        TODO: 当前未启用工具过滤（所有工具始终可用）。
        未来可根据 intent 结果过滤传给 agent 的工具集。
        """
        dc = self.domain_config
        return dc.tools if dc else None

    @property
    def domain_instructions(self) -> str:
        dc = self.domain_config
        return dc.instructions if dc else ""


# ═══════════════════════════════════════════════════════════════
# Prompt 模板
# ═══════════════════════════════════════════════════════════════
# 基于 Dify 生产级路由 Prompt 模式改造
# 参考: https://dify.ai + 企业级 Agent 意图路由最佳实践

_INTENT_PROMPT = """# Role
你是 QuantDinger 量化助手的意图分类器。核心职责：接收用户原始输入，精准匹配至预定义意图类别，仅输出分类结果 JSON，不额外解读或回答问题。

# Intent Definitions（意图定义）

## 1. [finance]（金融分析）
定义：用户询问股票行情、技术分析、资金流向、龙虎榜、涨停池、选股、回测、交易策略、市场概览等金融相关需求。
子意图与示例：
- stock_analysis: "帮我看看贵州茅台最近怎么样"、"600519什么情况"、"比亚迪技术面分析"
- market_scan: "今天涨停的股票有哪些"、"最近热门板块"、"龙虎榜数据"
- backtest: "用双均线策略回测一下比亚迪"、"测试RSI策略的历史表现"
- stock_screener: "帮我找低估值的银行股"、"筛选近5日涨幅超10%的股票"
- fund_flow: "主力资金流向"、"北向资金今天买了什么"
- indicator: "布林带指标怎么用"、"MACD金叉选股"
- trading: "启动网格交易策略"、"查看持仓情况"

## 2. [coding]（编程开发）
定义：用户要求编写、修改、调试、重构代码，分析项目结构，或讨论技术方案。
子意图与示例：
- code_modify: "把 self_modify_tools.py 里的路径解析改成支持 Docker"
- code_review: "看看 agent.py 有没有性能问题"
- code_create: "写一个数据清洗脚本"
- code_debug: "这段代码报错了，帮我看看"
- project_scan: "项目结构分析一下"、"有哪些 Python 文件"

## 3. [chat]（通用闲聊）
定义：问候、寒暄、感谢、告别，或不属于上述两类的通用对话。
子意图与示例：
- greeting: "你好"、"hi"、"早上好"
- farewell: "再见"、"拜拜"
- thanks: "谢谢"、"感谢"
- general: "你是谁"、"你会做什么"、"今天天气怎么样"

# Constraints（输出约束）
1. 严格输出一个 JSON 对象，不要 markdown 包裹，不要任何其他文字
2. 输出格式：{{"domain": "finance|coding|chat", "intent": "子意图", "params": {{}}, "confidence": 0.0~1.0}}
3. params 中提取关键参数：stock（股票代码）、stock_name（股票名称）、target（目标文件）、aspects（分析维度）、timeframe（时间范围）等
4. 股票名称必须转为代码（贵州茅台→600519，比亚迪→002594），不确定就留空
5. aspects 根据用户意图推断（如"最近怎么样"→["行情","技术面","资金流"]）
6. 根据对话历史解析代词（如"它"="贵州茅台"、"上一只"=之前提到的股票）
7. 无法判断时归 chat，confidence 给低值（<0.5）

# Conversation History（对话历史）
{history}

# User Input
请分析以下用户消息的意图，严格按上述格式输出 JSON，不要输出其他内容：
---
{message}
---"""


# ═══════════════════════════════════════════════════════════════
# 分析器
# ═══════════════════════════════════════════════════════════════

def analyze_intent(
    message: str,
    model: str = None,
    provider: str = None,
    history: List[Dict[str, str]] = None,
) -> IntentResult:
    print(f"[DEBUG-INTENT] >>> analyze_intent loaded from: {__file__}", flush=True)
    """分析用户消息的意图。

    Args:
        message: 当前用户消息
        model: 模型名（可选）
        provider: provider 名（可选）
        history: 最近对话历史 [{"role": "user/assistant", "content": "..."}, ...]

    失败时返回默认的 chat 领域（降级不阻塞）。
    """
    # 确保内置领域已注册（幂等，多次调用无副作用）
    init_builtin_domains()

    if not message or not message.strip():
        return IntentResult(domain="chat", intent="empty", confidence=1.0)

    # 格式化对话历史
    history_text = "（无）"
    if history:
        lines = []
        for msg in history[-6:]:  # 最近 3 轮
            role = "用户" if msg.get("role") == "user" else "助手"
            content = (msg.get("content") or "")[:200]
            lines.append(f"{role}: {content}")
        if lines:
            history_text = "\n".join(lines)

    # 构建 prompt（模板已内置领域定义和示例，无需动态生成）
    prompt = _INTENT_PROMPT.format(
        history=history_text,
        message=message.strip(),
    )

    # 直接调 LLM API（不经过 smolagents）
    try:
        from app.services.llm import LLMService
        svc = LLMService(provider=provider)
        api_key = svc.get_api_key()
        base_url = svc.get_base_url()
        model_id = model or svc.get_default_model()

        import requests
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model_id,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 500,
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()

        # 清理可能的 markdown 包裹
        if raw.startswith("```"):
            lines = raw.split("\n")
            # 去掉首尾的 ``` 行
            lines = [l for l in lines if not l.strip().startswith("```")]
            raw = "\n".join(lines).strip()

        logger.debug("[Intent] LLM raw response: %s", raw[:500])

        # 尝试解析 JSON（兼容 LLM 在 JSON 前后加了废话的情况）
        result = None
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            # 从响应中提取第一个 JSON 对象
            import re as _re
            match = _re.search(r'\{[^{}]*"domain"[^{}]*\}', raw, _re.DOTALL)
            if match:
                try:
                    result = json.loads(match.group())
                except json.JSONDecodeError:
                    pass
            if result is None:
                raise  # 让外层 except 捕获

        # 校验返回结构：必须是 dict 且包含 domain 字段
        if not isinstance(result, dict):
            logger.warning("[Intent] LLM 返回非 dict 类型: %s | raw: %s", type(result).__name__, raw[:200])
            return IntentResult(domain="chat", intent="bad_format", confidence=0.0, raw_response=raw)

        if "domain" not in result:
            logger.warning("[Intent] LLM 返回缺少 'domain' 字段: %s", raw[:200])
            return IntentResult(domain="chat", intent="no_domain", confidence=0.0, raw_response=raw)

        domain = result.get("domain", "chat")
        # 校验领域是否存在
        if domain not in get_all_domains():
            logger.warning("[Intent] 未知领域 '%s'，降级到 chat | raw: %s", domain, raw[:200])
            domain = "chat"

        return IntentResult(
            domain=domain,
            intent=result.get("intent", ""),
            params=result.get("params", {}),
            confidence=float(result.get("confidence", 0.5)),
            raw_response=raw,
        )

    except json.JSONDecodeError as e:
        logger.warning("[Intent] JSON 解析失败: %s | raw: %s", e, raw[:500])
        return IntentResult(domain="chat", intent="parse_error", confidence=0.0, raw_response=raw)
    except Exception as e:
        import traceback
        logger.warning("[Intent] 分析失败，降级到默认: %s\n%s", e, traceback.format_exc())
        return IntentResult(domain="chat", intent="error", confidence=0.0)


def format_intent_for_agent(intent: IntentResult, original_message: str) -> str:
    """将意图分析结果格式化为 agent 可用的上下文。

    返回空字符串表示不需要额外上下文（如简单闲聊）。
    """
    # 低置信度的 chat 不加额外上下文，直接走默认流程
    if intent.domain == "chat" and intent.confidence < 0.5:
        return ""

    # 高置信度的 chat（如打招呼）给一个简短提示
    if intent.domain == "chat":
        return f"[意图] {intent.intent}（直接回复即可，无需调用工具）"

    parts = [f"[意图分析] 领域={intent.domain}，意图={intent.intent}"]

    if intent.params:
        params_str = json.dumps(intent.params, ensure_ascii=False)
        parts.append(f"参数: {params_str}")

    if intent.confidence < 0.6:
        parts.append(f"⚠️ 置信度较低({intent.confidence})，请结合原始消息判断")

    return "\n".join(parts)
