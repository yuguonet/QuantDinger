# -*- coding: utf-8 -*-
"""
Chain — 编排/决策层。

核心模块：
  schema.py    — EvalNode 三层统一数据结构 + SkillReport
  store.py     — qd_evaluations 持久化（save_tree / load_tree）
  contract.py  — SkillReport 解析契约（从 LLM 输出提取结构化数据）
  chains.py    — 链路定义
  executor.py  — 链路执行器（按链路定义调度 Skill，构建决策树）
  evaluator.py — 回溯评估引擎（T+N 验证 → 因子权重更新）
"""
from app.agent.chain.schema import (
    EvalNode, SkillReport, FactorItem,
    Layer, Status, Action, Direction,
    VETO_SCORE, COVERAGE_THRESHOLD, DIRECTION_THRESHOLD,
    classify_return, is_direction_correct,
)
from app.agent.chain.store import (
    save_tree, load_tree, query_roots, query_pending_verify,
    update_verify_results, update_skill_verify,
    get_skill_weights, get_factor_weights, get_eval_stats,
)
from app.agent.chain.contract import parse_skill_output, extract_tools_called
from app.agent.chain.tool_chains import (
    get_tool_chain, save_tool_chain,
    get_chain_stats, update_chain_stats,
    list_all_chains,
    detect_feedback_severity, penalize_chain,
)

__all__ = [
    "EvalNode", "SkillReport", "FactorItem",
    "Layer", "Status", "Action", "Direction",
    "VETO_SCORE", "COVERAGE_THRESHOLD", "DIRECTION_THRESHOLD",
    "classify_return", "is_direction_correct",
    "save_tree", "load_tree", "query_roots", "query_pending_verify",
    "update_verify_results", "update_skill_verify",
    "get_skill_weights", "get_factor_weights",
    "get_eval_stats",
    "parse_skill_output", "extract_tools_called",
    "get_tool_chain", "save_tool_chain",
    "get_chain_stats", "update_chain_stats",
    "list_all_chains",
    "detect_feedback_severity", "penalize_chain",
]
