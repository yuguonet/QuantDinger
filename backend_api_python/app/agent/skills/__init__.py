# -*- coding: utf-8 -*-
"""
Skills — 领域专用分析技能。

每个 Skill 是 BaseSkill 的子类，负责特定领域的分析：
  - 调用 Tools 获取原始数据
  - 分析数据并输出标准化 SkillReport
  - 自动记录入参出参到 EvalNode 子树

12 个内置 Skill（合并后）：
  technical_agent    — 技术面+动量（趋势/量价/均线/指标/形态/突破/择时）
  indicator_agent    — 指标信号（用户自定义指标策略）
  intelligence_agent — 情报+政策（新闻/事件驱动/概念催化/政策分析）
  hot_money_tracker  — 游资追踪（龙虎榜/主力资金/游资席位）
  lockup_watcher     — 解禁监控（限售股解禁/减持/质押）
  market_data_agent  — 行情+概念+资金（大盘/板块/概念热度/资金流向）
  screening_agent    — 选股筛选（条件选股/动量筛选/概念选股）
  backtest_agent     — 策略回测
  bull_researcher    — 多头论证
  bear_researcher    — 空头反驳
  trading_agent      — 交易执行
  data_engineer      — 数据工程

兼容别名（旧名仍可调用，自动路由到合并后的 Skill）：
  momentum_tracker   → technical_agent
  policy_analyst     → intelligence_agent
  concept_tracker    → market_data_agent

使用：
  from app.agent.skills.registry import skill_registry
  skill_registry.discover()  # 自动发现所有 BaseSkill 子类
  skill = skill_registry.get("technical_agent")
  report, node = skill.run(stock_code="600519", ...)
"""
