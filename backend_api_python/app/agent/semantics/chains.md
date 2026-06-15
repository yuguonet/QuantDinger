---
chains:
  evaluate+stock:
    name: "股票综合评估"
    description: "游资追踪→解禁监控→情报/政策→技术面/动量→指标信号→选股验证→行情/概念/资金→回测→多空辩论"
    trigger_verbs: [analyze, evaluate]
    trigger_nouns: [stock]
    steps:
      - name: hot_money
        agent: hot_money_tracker
        order: 1
        description: "游资追踪：龙虎榜、主力资金动态、游资席位动向（短线定价核心）"
        required: false
      - name: lockup
        agent: lockup_watcher
        order: 2
        description: "解禁监控：限售股解禁、减持预警、质押风险（供给端风险）"
        required: false
      - name: intelligence
        agent: intelligence_agent
        order: 3
        description: "情报+政策分析：新闻搜索、事件驱动、概念催化、政策影响"
        required: false
      - name: technical
        agent: technical_agent
        order: 4
        description: "技术面+动量综合判断：趋势、量价、指标、形态、筹码、突破、择时"
        required: true
      - name: indicator
        agent: indicator_agent
        order: 5
        description: "用户指标信号验证：执行指标 IDE 中的自定义策略，获取 buy/sell 信号"
        required: false
      - name: screening
        agent: screening_agent
        order: 6
        description: "选股验证：条件筛选、指标信号验证"
        required: false
      - name: market_data
        agent: market_data_agent
        order: 7
        description: "行情+概念+资金流向：大盘、板块轮动、概念热度、主力态度"
        required: false
      - name: backtest
        agent: backtest_agent
        order: 8
        description: "策略回测验证：历史绩效、胜率、盈亏比、最大回撤"
        required: false
      - name: bull_bear_debate
        agent: bull_researcher
        order: 9
        description: "多空辩论：多头研究员基于所有报告构建看涨论据"
        required: false
        extract_fn: extract_bull_args
      - name: bear_rebuttal
        agent: bear_researcher
        order: 10
        description: "多空辩论：空头研究员反驳多头论据并构建看跌论据"
        required: false
        extract_fn: extract_bear_args

  screen+stock:
    name: "选股筛选"
    description: "条件选股→技术验证→情报过滤→综合排序"
    trigger_verbs: [filter, screen]
    trigger_nouns: [stock, screener]
    steps:
      - name: screening
        agent: screening_agent
        order: 1
        description: "条件选股，获取候选池"
        required: true
      - name: technical
        agent: technical_agent
        order: 2
        description: "技术面+动量验证"
        required: false
      - name: intelligence
        agent: intelligence_agent
        order: 3
        description: "新闻情报+政策过滤"
        required: false

  scan+market:
    name: "市场全景扫描"
    description: "大盘指数→板块排名→涨停池→龙虎榜→资金流向"
    trigger_verbs: [view, analyze, scan]
    trigger_nouns: [market]
    steps:
      - name: market_overview
        agent: market_data_agent
        order: 1
        description: "大盘指数、板块排名、概念热度"
        required: true
      - name: hotspots
        agent: screening_agent
        order: 2
        description: "涨停池、龙虎榜、热榜"
        required: false
      - name: fund_flow
        agent: market_data_agent
        order: 3
        description: "板块和概念资金流向"
        required: false
---

# 链路定义

每条链路由多个步骤组成，每个步骤对应一个 Skill Agent。
链路由 intent_analyzer 的 verb+noun 组合触发。

## 链路列表

| 链路 | 触发 | 步骤 | 说明 |
|------|------|------|------|
| evaluate+stock | analyze/evaluate + stock | 10 | 股票综合评估 |
| screen+stock | filter/screen + stock/screener | 3 | 选股筛选 |
| scan+market | view/analyze/scan + market | 3 | 市场全景扫描 |
