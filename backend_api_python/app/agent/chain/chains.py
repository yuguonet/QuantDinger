# -*- coding: utf-8 -*-
"""
Chain Definitions — 链路定义（Phase 2: 从 chains.md 加载）。

每条链路由多个步骤组成，每个步骤对应一个 Skill Agent。
链路由 intent_analyzer 的 verb+noun 组合触发。

Phase 2 变更（SEMANTICS_REFACTOR）：
  - 链路定义从 chains.md 加载（单一信源）
  - 改链路只改 YAML，不再改 Python 代码
  - 保留 register_chain() 接口，支持运行时动态注册

数据结构：
  ChainStep — 链路中的一个步骤（name/agent/order/required）
  ChainDef  — 链路定义（chain_id/name/steps/trigger_verbs/trigger_nouns）

内置链路（从 chains.md 加载）：
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
  load_chains_from_yaml() → None（从 chains.md 加载，幂等）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


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
_yaml_loaded = False


def register_chain(chain_def: ChainDef):
    """注册一条链路。"""
    _CHAIN_REGISTRY[chain_def.chain_id] = chain_def


def get_chain(chain_id: str) -> Optional[ChainDef]:
    """获取链路定义。"""
    return _CHAIN_REGISTRY.get(chain_id)


def get_chain_for_intent(verb: str, noun: str) -> Optional[ChainDef]:
    """根据动词+对象查找匹配的链路。"""
    # 确保 YAML 已加载
    load_chains_from_yaml()
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
    load_chains_from_yaml()
    return list(_CHAIN_REGISTRY.values())


# ═══════════════════════════════════════════════════════════════
# 从 chains.md 加载（Phase 2: 单一信源）
# ═══════════════════════════════════════════════════════════════

def load_chains_from_yaml():
    """从 semantics/chains.md 加载链路定义（幂等，只加载一次）。"""
    global _yaml_loaded
    if _yaml_loaded:
        return
    _yaml_loaded = True

    from app.agent.semantics import get_all_chain_metas
    chain_metas = get_all_chain_metas()

    if not chain_metas:
        logger.warning("[Chain] chains.md 为空")
        return

    for chain_id, meta in chain_metas.items():
        steps = []
        for s in meta.steps:
            steps.append(ChainStep(
                name=s.name,
                agent=s.agent,
                order=s.order,
                description=s.description,
                required=s.required,
                extract_fn=s.extract_fn,
            ))
        # 按 order 排序
        steps.sort(key=lambda x: x.order)
        register_chain(ChainDef(
            chain_id=chain_id,
            name=meta.name,
            description=meta.description,
            trigger_verbs=meta.trigger_verbs,
            trigger_nouns=meta.trigger_nouns,
            steps=steps,
        ))

    logger.info("[Chain] 从 chains.md 加载 %d 条链路: %s",
                len(chain_metas), list(chain_metas.keys()))


