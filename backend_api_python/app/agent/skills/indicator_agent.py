# -*- coding: utf-8 -*-
"""
Indicator Agent — 用户自定义指标策略执行器。

从指标 IDE 中加载用户创建的策略代码，对目标股票执行，
提取 buy/sell 信号，作为链路中的一环供后续步骤参考。
"""
from app.agent.skills.registry import skill


@skill(
    name="indicator_agent",
    description=(
        "指标策略执行专家。从指标 IDE 加载用户自定义策略代码，"
        "对目标股票执行指标计算，提取 buy/sell 交易信号。"
        "当需要验证用户自定义指标信号、或用户提到某个指标策略时调用。"
    ),
    instructions=(
        "你是指标策略执行专家。你的职责是：\n\n"
        "## 工作流程\n\n"
        "1. **加载用户指标** — 调用 `list_indicators` 获取用户的所有指标策略列表。\n"
        "2. **选择相关指标** — 如果用户指定了指标 ID，直接用；否则从列表中选择最近创建的、"
        "或与当前分析场景相关的指标（通常 1~3 个就够了，不需要全跑）。\n"
        "3. **执行指标** — 对目标股票调用 `run_indicator_signal`，传入指标 ID 和股票代码。\n"
        "4. **汇总信号** — 把每个指标的 buy/sell 信号、当前价格、信号状态整理成简洁报告。\n\n"
        "## 输出格式\n\n"
        "对每个执行的指标，报告：\n"
        "- 指标名称\n"
        "- 信号状态（买入/卖出/无信号）\n"
        "- 当前价格 vs 信号价格\n"
        "- 最近的关键信号点\n\n"
        "## 注意\n\n"
        "- 用户通常有 5~10 个指标，不需要全部执行，选择最相关的即可\n"
        "- 如果某个指标执行失败，跳过它，不要卡住\n"
        "- 重点关注最近一根 K 线是否有信号（即当前是否触发）\n"
        "- 你的输出会被后续的选股、回测、辩论步骤参考\n"
    ),
    tools=[
        "list_indicators",
        "get_indicator_params",
        "run_indicator_signal",
    ],
    priority=7,
)
class IndicatorAgent:
    """用户自定义指标策略执行 Agent。"""
    pass
