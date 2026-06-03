# -*- coding: utf-8 -*-
"""
Domain Registry — 领域注册中心。

每个领域定义：
- name: 唯一标识
- description: 给意图分析器看的领域描述（用于分类）
- tools: 该领域使用的工具名列表（None = 不过滤，用全部）
- managed_agent: 对应的子 agent 名称（None = 不使用子 agent）
- instructions: 领域专属指令（追加到 agent instructions）
- examples: 意图分析器的 few-shot 示例

新增领域只需要调用 register() 或在 DOMAINS 列表里加一项。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class DomainConfig:
    """单个领域的配置。"""
    name: str
    description: str
    tools: Optional[List[str]] = None          # None = 不过滤
    managed_agent: Optional[str] = None         # 对应子 agent 的 name
    instructions: str = ""                      # 领域专属指令
    examples: List[Dict] = field(default_factory=list)  # 意图分析 few-shot


# ═══════════════════════════════════════════════════════════════
# 注册表
# ═══════════════════════════════════════════════════════════════

_DOMAINS: Dict[str, DomainConfig] = {}


def register(domain: DomainConfig) -> None:
    """注册一个领域。重复注册会覆盖。"""
    _DOMAINS[domain.name] = domain
    logger.info("[DomainRegistry] 注册领域: %s", domain.name)


def get_domain(name: str) -> Optional[DomainConfig]:
    return _DOMAINS.get(name)


def get_all_domains() -> Dict[str, DomainConfig]:
    return dict(_DOMAINS)


def get_domain_names() -> List[str]:
    return list(_DOMAINS.keys())


def build_intent_prompt_domains() -> str:
    """为意图分析器生成领域的 prompt 片段。"""
    lines = []
    for d in _DOMAINS.values():
        lines.append(f"- **{d.name}**: {d.description}")
    return "\n".join(lines)


def build_intent_prompt_examples() -> str:
    """为意图分析器生成 few-shot 示例。"""
    examples = []
    for d in _DOMAINS.values():
        for ex in d.examples:
            examples.append(ex)
    if not examples:
        return ""
    import json
    parts = ["## 示例"]
    for ex in examples:
        parts.append(f"用户: {ex['message']}")
        parts.append(f"输出: {json.dumps(ex['output'], ensure_ascii=False)}")
        parts.append("")
    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════
# 内置领域
# ═══════════════════════════════════════════════════════════════

_BUILTIN_DOMAINS = [
    DomainConfig(
        name="finance",
        description="金融分析：股票行情查询、技术分析、资金流向、龙虎榜、涨停池、热点追踪、市场概览等",
        managed_agent=None,  # 由主 agent 根据复杂度决定是否委派给子 agent
        instructions="你是量化分析助手。所有金融数据必须通过工具获取，绝不编造。分析必须包含风险提示。",
        examples=[
            {"message": "帮我看看贵州茅台最近怎么样", "output": {"domain": "finance", "intent": "stock_analysis", "confidence": 0.9}},
            {"message": "看000001K线", "output": {"domain": "finance", "intent": "stock_analysis", "confidence": 0.95}},
            {"message": "今天涨停的股票有哪些", "output": {"domain": "finance", "intent": "market_scan", "confidence": 0.95}},
            {"message": "用双均线策略回测比亚迪", "output": {"domain": "finance", "intent": "backtest", "confidence": 0.95}},
            {"message": "什么是MACD金叉", "output": {"domain": "finance", "intent": "concept_explain", "confidence": 0.9}},
            {"message": "修改xxx文件的bug", "output": {"domain": "coding", "intent": "code_modify", "confidence": 0.9}},
        ],
    ),
    DomainConfig(
        name="coding",
        description="编程开发：代码编写、修改、调试、重构、代码审查、技术方案设计、项目结构分析等",
        managed_agent=None,
        instructions="你是编程助手。修改代码前先理解现有逻辑，修改后验证不破坏已有功能。",
        examples=[
            {
                "message": "把 self_modify_tools.py 里的路径解析逻辑改成支持 Docker",
                "output": {
                    "domain": "coding",
                    "intent": "code_modify",
                    "target": "self_modify_tools.py",
                    "task": "修改路径解析逻辑以支持 Docker 环境",
                    "aspects": [],
                },
            },
            {
                "message": "看看 agent.py 的结构，有没有性能问题",
                "output": {
                    "domain": "coding",
                    "intent": "code_review",
                    "target": "agent.py",
                    "task": "分析代码结构和性能问题",
                    "aspects": ["性能"],
                },
            },
        ],
    ),
    DomainConfig(
        name="chat",
        description="闲聊、打招呼、问天气、问时间、问身份等不涉及具体任务的对话",
        managed_agent=None,
        instructions="",
        examples=[
            {
                "message": "你好",
                "output": {
                    "domain": "chat",
                    "intent": "greeting",
                    "aspects": [],
                },
            },
        ],
    ),
]


_domains_initialized = False


def init_builtin_domains() -> None:
    """注册所有内置领域（幂等，只执行一次）。"""
    global _domains_initialized
    if _domains_initialized:
        return
    for d in _BUILTIN_DOMAINS:
        if d.name not in _DOMAINS:
            register(d)
    _domains_initialized = True
