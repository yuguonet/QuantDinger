# -*- coding: utf-8 -*-
"""
Chain Definitions — 链路定义。

每条链路由多个步骤组成，每个步骤对应一个 Skill Agent。
链路由 intent_analyzer 的 verb+noun 组合触发。

数据结构：
  ChainStep — 链路中的一个步骤（name/agent/order/required）
  ChainDef  — 链路定义（chain_id/name/steps/trigger_verbs/trigger_nouns）

内置链路：
  evaluate+stock  — 股票综合评估（10步：游资→解禁→情报→技术→指标→选股→行情→回测→多空辩论）
  screen+stock    — 选股筛选（3步：条件选股→技术验证→情报过滤）
  scan+market     — 市场全景扫描（3步：大盘指数→涨停池→资金流向）

触发方式：
  agent._try_chain(verb, noun) → get_chain_for_intent() → ChainExecutor.execute()

公开接口：
  register_chain(chain_def) → None
  get_chain(chain_id) → Optional[ChainDef]
  get_chain_for_intent(verb, noun) → Optional[ChainDef]
  list_chains() → List[ChainDef]
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
    # 1. 精确匹配 verb+noun
    if verb and noun:
        for chain in _CHAIN_REGISTRY.values():
            if verb in chain.trigger_verbs and noun in chain.trigger_nouns:
                return chain
    # 2. 只按 verb 匹配（noun 为空或未匹配到）
    if verb:
        for chain in _CHAIN_REGISTRY.values():
            if verb in chain.trigger_verbs and not chain.trigger_nouns:
                return chain
    # 3. 只按 noun 匹配（verb 为空或未匹配到）
    if noun:
        for chain in _CHAIN_REGISTRY.values():
            if noun in chain.trigger_nouns and not chain.trigger_verbs:
                return chain
    return None


def list_chains() -> List[ChainDef]:
    """列出所有已注册链路。"""
    return list(_CHAIN_REGISTRY.values())


# ═══════════════════════════════════════════════════════════════
# 内置链路定义
# ═══════════════════════════════════════════════════════════════

# ── 综合评估链（A股中短线特化：环境→技术→验证→辩论收尾）──
register_chain(ChainDef(
    chain_id="evaluate+stock",
    name="股票综合评估",
    description="游资追踪→解禁监控→情报/政策→技术面/动量→指标信号→选股验证→行情/概念/资金→回测→多空辩论",
    trigger_verbs=["analyze", "evaluate"],
    trigger_nouns=["stock"],
    steps=[
        # ── 第一优先级：环境判断 ──
        ChainStep(
            name="hot_money",
            agent="hot_money_tracker",
            order=1,
            description="游资追踪：龙虎榜、主力资金动态、游资席位动向（短线定价核心）",
            required=False,
        ),
        ChainStep(
            name="lockup",
            agent="lockup_watcher",
            order=2,
            description="解禁监控：限售股解禁、减持预警、质押风险（供给端风险）",
            required=False,
        ),
        # ── 第二优先级：分析验证 ──
        ChainStep(
            name="intelligence",
            agent="intelligence_agent",
            order=3,
            description="情报+政策分析：新闻搜索、事件驱动、概念催化、政策影响",
            required=False,
        ),
        ChainStep(
            name="technical",
            agent="technical_agent",
            order=4,
            description="技术面+动量综合判断：趋势、量价、指标、形态、筹码、突破、择时",
            required=True,
        ),
        ChainStep(
            name="indicator",
            agent="indicator_agent",
            order=5,
            description="用户指标信号验证：执行指标 IDE 中的自定义策略，获取 buy/sell 信号",
            required=False,
        ),
        ChainStep(
            name="screening",
            agent="screening_agent",
            order=6,
            description="选股验证：条件筛选、指标信号验证",
            required=False,
        ),
        ChainStep(
            name="market_data",
            agent="market_data_agent",
            order=7,
            description="行情+概念+资金流向：大盘、板块轮动、概念热度、主力态度",
            required=False,
        ),
        ChainStep(
            name="backtest",
            agent="backtest_agent",
            order=8,
            description="策略回测验证：历史绩效、胜率、盈亏比、最大回撤",
            required=False,
        ),
        # ── 第三优先级：多空辩论（决策收尾）──
        ChainStep(
            name="bull_bear_debate",
            agent="bull_researcher",
            order=9,
            description="多空辩论：多头研究员基于所有报告构建看涨论据",
            required=False,
            extract_fn="extract_bull_args",
        ),
        ChainStep(
            name="bear_rebuttal",
            agent="bear_researcher",
            order=10,
            description="多空辩论：空头研究员反驳多头论据并构建看跌论据",
            required=False,
            extract_fn="extract_bear_args",
        ),
    ],
))

# ── 选股筛选链 ──
register_chain(ChainDef(
    chain_id="screen+stock",
    name="选股筛选",
    description="条件选股→技术验证→情报过滤→综合排序",
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
            description="技术面+动量验证",
            required=False,
        ),
        ChainStep(
            name="intelligence",
            agent="intelligence_agent",
            order=3,
            description="新闻情报+政策过滤",
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
            description="大盘指数、板块排名、概念热度",
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
