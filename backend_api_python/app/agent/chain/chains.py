# -*- coding: utf-8 -*-
"""
Chain Definitions — 链路定义（动态注册）。

每条链路由多个步骤组成，每个步骤对应一个 Skill Agent。
链路由 Planner 动态生成并通过 register_chain() 注册。

数据结构：
  ChainStep — 链路中的一个步骤（name/agent/order/required）
  ChainDef  — 链路定义（chain_id/name/steps/trigger_verbs/trigger_nouns）

触发方式：
  agent._try_chain(verb, noun) → get_chain_for_intent() / Planner.plan() → _execute_plan()

公开接口：
  register_chain(chain_def) → None
  get_chain(chain_id) → Optional[ChainDef]
  get_chain_for_intent(verb, noun) → Optional[ChainDef]
  list_chains() → List[ChainDef]
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
    rules: str = ""             # 这一步的执行规则


@dataclass
class ChainDef:
    """链路定义。"""
    chain_id: str               # 如 "evaluate+stock"
    name: str                   # 显示名
    description: str            # 描述
    steps: List[ChainStep]      # 步骤列表
    trigger_verbs: List[str] = field(default_factory=list)   # 触发动词
    trigger_nouns: List[str] = field(default_factory=list)   # 触发对象
    context: Dict[str, Any] = field(default_factory=dict)    # Planner 传入的额外上下文（tips/focus/data_criticality 等）
    progressive: bool = True    # phase 间是否递进关系（后一步依赖前一步结论）


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
    """根据动词+对象查找匹配的链路（仅查动态注册的链路）。"""
    # 精确匹配 verb+noun
    if verb and noun:
        for chain in _CHAIN_REGISTRY.values():
            if verb in chain.trigger_verbs and noun in chain.trigger_nouns:
                return chain
    if verb:
        for chain in _CHAIN_REGISTRY.values():
            if verb in chain.trigger_verbs and not chain.trigger_nouns:
                return chain
    if noun:
        for chain in _CHAIN_REGISTRY.values():
            if noun in chain.trigger_nouns and not chain.trigger_verbs:
                return chain
    return None


def get_all_chains_for_noun(noun: str) -> List[ChainDef]:
    """按 noun 查找所有匹配的链路。"""
    return [c for c in _CHAIN_REGISTRY.values() if noun in c.trigger_nouns]


def list_chains() -> List[ChainDef]:
    """列出所有已注册链路。"""
    return list(_CHAIN_REGISTRY.values())


