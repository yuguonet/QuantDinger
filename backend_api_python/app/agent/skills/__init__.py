# -*- coding: utf-8 -*-
"""
Skills — 领域专用分析技能。

每个 Skill 是 BaseSkill 的子类，负责特定领域的分析：
  - 调用 Tools 获取原始数据
  - 分析数据并输出标准化 SkillReport
  - 自动记录入参出参到 EvalNode 子树

16 个内置 Skill：
  technical_agent    — 技术分析（趋势/量价/均线/指标/形态）
  momentum_tracker   — 动量追踪（趋势强度/突破/择时）
  indicator_agent    — 指标信号
  intelligence_agent — 情报分析（新闻/事件驱动/概念催化）
  policy_analyst     — 政策面分析
  hot_money_tracker  — 游资追踪
  lockup_watcher     — 解禁监控
  concept_tracker    — 概念追踪
  market_data_agent  — 市场数据
  screening_agent    — 选股筛选
  backtest_agent     — 策略回测
  bull_researcher    — 多头论证
  bear_researcher    — 空头反驳
  trading_agent      — 交易执行
  data_engineer      — 数据工程
  analysis_agent     — 通用分析

使用：
  from app.agent.skills.registry import skill_registry
  skill_registry.discover()  # 自动发现所有 BaseSkill 子类
  skill = skill_registry.get("technical_agent")
  report, node = await skill.run(stock_code="600519", ...)
"""
