# -*- coding: utf-8 -*-
"""
Chain Orchestration — 链路决策评估系统。

架构：skill 输出 → 结构化解析 → 决策卡 → T+N 验证 → 权重迭代 → 门控决策

模块：
  chains.py          — 链路定义（ChainDef/ChainStep，verb+noun 触发）
  executor.py        — 链路执行器（ChainExecutor → DecisionCard）
  evaluator.py       — 闭环评估器（evaluate_pending → 因子权重 → 工具评估）
  schema.py          — 数据结构（DecisionCard/StepOutput/Blockers/...）
  skill_contract.py  — Skill 输出契约（JSON 格式 + 解析函数）

数据流：
  agent._try_chain() → ChainExecutor.execute() → 子 Agent × N 步
    → parse_skill_output() → StepOutput → DecisionCard
    → qd_agent_decisions + qd_agent_decision_steps（持久化）
    → [定时] evaluate_pending() → qd_agent_decision_results
    → [定时] update_factor_weights() → qd_agent_factor_weights
    → [下次执行] _load_step_weights() 读取新权重
"""
from app.agent.chain.schema import DecisionCard, StepOutput, StepStatus, Action, Confidence
from app.agent.chain.executor import ChainExecutor, ChainResult
from app.agent.chain.skill_contract import parse_skill_output
