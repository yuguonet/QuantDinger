---
name: market-screener
version: 5.0.0
description: 从A股全市场筛选短线标的。用户问"今天买什么股""有什么好股票""短线选什么"时使用。不含个股分析。
tags: [market, screener, short_term, a_share]
tools:
  - get_fund_flow
  - get_indicator_snapshot
  - search_stocks
  - agent_get_kline
---

# 全市场短线选股 (market-screener)

## 使用场景

用户询问"今天买什么股""有什么好股票""短线选什么"时使用。不含个股分析。

如果用户指定了具体股票代码，不要调用本技能。

## 执行流程

**分阶段执行（任务被拆成多阶段时，按 `## stages` 的对应阶段走）：**
- 「预筛与筛选」：先 `pre_screen()` 拿候选与市场状态，再 `filter_candidates(prescreen_result)` 得到目标 codes；
- 「深入分析」：对 codes 调 `deep_analyze(codes)`（codes 来自上一阶段结果）；
- 「开盘建议」：基于已完成阶段结果直接撰写报告，无需工具。

**快捷路径（单阶段任务）：** 直接 `run()` 一键完成全流程，拿到文本后 `final_answer()` 输出。
`run()` 内部已完成：候选获取 → 筛选 → 深入分析 → 格式化。

## stages

```yaml
- name: 市场环境速览
  goal: 获取指数行情、主力资金流向、市场情绪、热门板块，评估开盘基调并给出选股方向
  tools: [get_market_indices, get_market_overview, get_market_fund_flow, get_hot_sectors]
  deliverable: 市场状态摘要（基调/资金/逆势净流入板块Top与选股方向提示）
  acceptance:
    - 含主要指数涨跌与主力资金净额
    - 给出选股方向提示
- name: 预筛与筛选
  goal: 调用 pre_screen 获取候选池，再用 filter_candidates 按市场方向筛出目标 codes（只筛不析）
  tools: [pre_screen, filter_candidates]
  deliverable: 目标 codes（逗号分隔）清单
  acceptance:
    - codes 非空（≥5 只）
- name: 深入分析
  goal: 对目标 codes 调用 deep_analyze，整理每只标的的关键指标（评分/方向/信号/支撑压力）
  tools: [deep_analyze]
  deliverable: 每只标的简表（代码/名称/评分/方向/关键信号），3~6 只
  acceptance:
    - 每只标的含评分与方向
- name: 开盘建议
  goal: 基于已完成阶段结果撰写最终输出——标的列表 + 开盘操作建议（仓位/板块/介入点/止损）+ 风险提示
  tools: []
  deliverable: 最终报告文本（含标的列表与操作建议）
  acceptance:
    - 含标的列表（代码/名称/评分）
    - 含仓位与风险提示
```


