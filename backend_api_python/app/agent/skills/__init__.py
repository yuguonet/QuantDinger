# -*- coding: utf-8 -*-
"""
Skills — 领域专用 Agent 技能包。

每个 skill 定义一个 Managed Sub-Agent 的身份、指令和工具需求。
通过 @skill 装饰器自动注册，agent.py 运行时自动发现。

15 个内置 Skill：
  technical / momentum / intelligence / screening / backtest / trading /
  policy / hot_money / lockup / concept / market_data / indicator /
  bull / bear / data_engineering
"""
