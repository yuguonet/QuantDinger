# -*- coding: utf-8 -*-
"""
Chain Definitions — 链路定义。

每条链路由多个步骤组成，每个步骤对应一个技能 Agent。
链路由 verb+noun 组合触发。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ChainStep:
    """链路中的一个步骤。"""
    name: str                   # 步骤名，如 "screening"
    agent: str                  # 对应的 skill agent 名
    order: int                  # 执行顺序
    description: str = ""       # 步骤描述
    required: bool = True       # 是否必须成功（False 则失败可跳过）
    extract_fn: str = ""        # 结果提取函数名（从 agent 输出中提取关键结论）


@dataclass
class ChainDef:
    """链路定义。"""
    chain_id: str               # 如 "evaluate+stock"
    name: str                   # 显示名
    description: str            # 描述
    steps: List[ChainStep]      # 步骤列表
    trigger_verbs: List[str] = field(default_factory=list)   # 触发动词
    trigger_nouns: List[str] = field(default_factory=list)   # 触发对象


# ═══════════════════════════════════════════════════════════════
# 链路注册表
# ═══════════════════════════════════════════════════════════════

_CHAIN_REGISTRY: Dict[str, ChainDef] = {}


def register_chain(chain_def: ChainDef):
    """注册一条链路。"""
    _CHAIN_REGISTRY[chain_def.chain_id] = chain_def


def get_chain(chain_id: str) -> Optional[ChainDef]:
    """获取链路定义。"""
    return _CHAIN_REGISTRY.get(chain_id)


def get_chain_for_intent(verb: str, noun: str) -> Optional[ChainDef]:
    """根据动词+对象查找匹配的链路。"""
    key = f"{verb}+{noun}"
    if key in _CHAIN_REGISTRY:
        return _CHAIN_REGISTRY[key]
    # 降级：只按 verb 匹配
    for chain in _CHAIN_REGISTRY.values():
        if verb in chain.trigger_verbs and not chain.trigger_nouns:
            return chain
    return None


def list_chains() -> List[ChainDef]:
    """列出所有已注册链路。"""
    return list(_CHAIN_REGISTRY.values())


# ═══════════════════════════════════════════════════════════════
# 内置链路定义
# ═══════════════════════════════════════════════════════════════

# ── 综合评估链 ──
register_chain(ChainDef(
    chain_id="evaluate+stock",
    name="股票综合评估",
    description="对个股进行全面评估：指标选股→新闻情报→资金流向→回测验证→技术面→综合判断",
    trigger_verbs=["analyze", "evaluate"],
    trigger_nouns=["stock"],
    steps=[
        ChainStep(
            name="screening",
            agent="screening_agent",
            order=1,
            description="指标选股，筛选候选池",
            required=False,
        ),
        ChainStep(
            name="intelligence",
            agent="intelligence_agent",
            order=2,
            description="新闻搜索，综合情报分析",
            required=False,
        ),
        ChainStep(
            name="fund_flow",
            agent="market_data_agent",
            order=3,
            description="资金流向分析，板块动向",
            required=False,
        ),
        ChainStep(
            name="backtest",
            agent="backtest_agent",
            order=4,
            description="策略回测验证",
            required=False,
        ),
        ChainStep(
            name="technical",
            agent="technical_agent",
            order=5,
            description="技术面综合判断",
            required=True,
        ),
    ],
))

# ── 选股筛选链 ──
register_chain(ChainDef(
    chain_id="screen+stock",
    name="选股筛选",
    description="条件选股→指标验证→情报过滤→综合排序",
    trigger_verbs=["filter", "screen"],
    trigger_nouns=["stock", "screener"],
    steps=[
        ChainStep(
            name="screening",
            agent="screening_agent",
            order=1,
            description="条件选股，获取候选池",
            required=True,
        ),
        ChainStep(
            name="technical",
            agent="technical_agent",
            order=2,
            description="指标信号验证",
            required=False,
        ),
        ChainStep(
            name="intelligence",
            agent="intelligence_agent",
            order=3,
            description="新闻情报过滤",
            required=False,
        ),
    ],
))

# ── 市场扫描链 ──
register_chain(ChainDef(
    chain_id="scan+market",
    name="市场全景扫描",
    description="大盘指数→板块排名→涨停池→龙虎榜→资金流向",
    trigger_verbs=["view", "analyze", "scan"],
    trigger_nouns=["market"],
    steps=[
        ChainStep(
            name="market_overview",
            agent="market_data_agent",
            order=1,
            description="大盘指数和板块排名",
            required=True,
        ),
        ChainStep(
            name="hotspots",
            agent="screening_agent",
            order=2,
            description="涨停池、龙虎榜、热榜",
            required=False,
        ),
        ChainStep(
            name="fund_flow",
            agent="market_data_agent",
            order=3,
            description="板块和概念资金流向",
            required=False,
        ),
    ],
))
