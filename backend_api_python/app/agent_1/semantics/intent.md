---
# 快速通道正则（无需 LLM，直接匹配）
quick_patterns:
  greeting: '^(你好|hi|hello|嗨|hey|在吗|哈喽|嘿|yo)[\s\?\?\.\,\!\~\。\，\！\？\…]*$'
  farewell: '^(再见|拜拜|bye|88|886|晚安|回见)[\s\?\?\.\,\!\~\。\，\！\？\…]*$'
  thanks: '^(谢谢|感谢|多谢|thanks|thank\s*you|thx|3q)[\s\?\?\.\,\!\~\。\，\！\？\…]*$'

# 分类规则（结构化，可被代码和 LLM 共用）
rules:
  - match: "有股票名称或代码"
    result: { domain: finance, verb: analyze, noun: stock }
  - match: "怎么样/能买吗/跌了/涨了 + 股票"
    result: { domain: finance, intent: stock_analysis }
  - match: "K线/图表"
    result: { domain: finance, intent: chart_view, verb: view, noun: chart }
  - match: "涨停/大盘/板块"
    result: { domain: finance, intent: market_scan, verb: scan, noun: market }
  - match: "选股/推荐"
    result: { domain: finance, intent: screener, verb: filter, noun: stock }
  - match: "回测"
    result: { domain: finance, intent: backtest, verb: backtest, noun: stock }
  - match: "资金流向/主力/北向"
    result: { domain: finance, intent: fund_flow, verb: query, noun: fund_flow }
  - match: "MACD/RSI/指标"
    result: { domain: finance, intent: indicator, verb: query, noun: indicator }
  - match: "买入/卖出/持仓/启停策略"
    result: { domain: trading, intent: trading, verb: execute, noun: trading }
  - match: "市盈率/市值/基本面"
    result: { domain: finance, intent: stock_info, verb: query, noun: stock }
  - match: "概念/术语"
    result: { domain: finance, intent: concept_explain, verb: explain, noun: concept }
  - match: "设置提醒/定时/闹钟/倒计时"
    result: { domain: system, intent: reminder, verb: remind }
  - match: "查看/管理/取消定时任务"
    result: { domain: system, intent: cron_manage }
  - match: "修改系统设置/配置"
    result: { domain: system, intent: settings, verb: configure }
  - match: "闲聊/问候"
    result: { domain: chat }

# 意图 → 工具类别映射
intent_tool_categories:
  stock_analysis: [名称查询, 行情数据, 技术分析, 情报搜索]
  chart_view: [名称查询, 行情数据, K线图表]
  market_scan: [行情数据, 龙虎榜/热榜]
  screener: [名称查询, 选股, 指标策略]
  backtest: [名称查询, 行情数据, 回测, 指标策略]
  fund_flow: [名称查询, 行情数据]
  indicator: [名称查询, 行情数据, 技术分析, 指标策略]
  trading: [交易, 指标策略]
  stock_info: [名称查询, 行情数据]
  concept_explain: []
  reminder: []
  cron_manage: []
  settings: []
  unknown: []
  code_modify: [工作区]
  code_create: [工作区]
  project_scan: []
---

# 意图分类 Prompt

你是意图分类器。分析用户消息，输出 JSON。

## 用户消息
{message}

## 上轮对话摘要（如有）
{context_summary}

## 输出格式（只输出 JSON，不要其他内容）
```json
{{
  "domain": "finance | coding | trading | system | unknown | chat",
  "intent": "stock_analysis | chart_view | market_scan | screener | backtest | fund_flow | indicator | trading | stock_info | concept_explain | code_modify | code_create | project_scan | reminder | cron_manage | settings | unknown | general",
  "verb": "analyze | view | filter | backtest | execute | query | explain | modify | create | remind | schedule | configure",
  "noun": "stock | chart | market | screener | fund_flow | indicator | trading | concept | code | project | reminder | cron | settings",
  "stock_code": "6位代码或空",
  "stock_name": "股票名称或空",
  "confidence": 0.0-1.0,
  "context_summary": "本轮对话摘要，30字以内，用于下轮上下文。如果和上轮同话题则延续，否则重写。"
}}
```

## 规则
- domain: finance=金融分析/股票/行情/资金, coding=代码/项目/开发, trading=交易执行/持仓/策略启停, system=定时提醒/任务调度/设置, unknown=无法判断领域, chat=闲聊/问候
- 有股票名称或代码 → domain=finance, verb=analyze, noun=stock
- 用户说"怎么样/能买吗/跌了/涨了"等，且提到股票 → finance/stock_analysis
- 用户问K线/图表 → finance/chart_view
- 用户问涨停/大盘/板块 → finance/market_scan
- 用户要选股/推荐 → finance/screener
- 用户要回测 → finance/backtest
- 用户问资金流向/主力/北向 → finance/fund_flow
- 用户问MACD/RSI/指标 → finance/indicator
- 用户要买入/卖出/持仓/启停策略 → trading/trading
- 用户问市盈率/市值/基本面 → finance/stock_info
- 用户问概念/术语 → finance/concept_explain
- 纯闲聊/问候 → domain=chat
- 用户要设置提醒/定时/闹钟/倒计时/几分钟后 → system/reminder
- 用户要查看/管理/取消定时任务 → system/cron_manage
- 用户要修改系统设置/配置 → system/settings
- system 意图不需要调用分析工具，reminder 直接创建定时提醒
- confidence: 有明确信号=0.9+, 有关键词=0.7+, 不确定=0.5-, 猜测=0.3
- 意图不明确时（没有匹配到上述任何规则），intent 填 "unknown"，confidence 填 0.3，不要猜测
- context_summary: 压缩为一句话摘要，供下轮对话使用
- domain = chat，直接返回答案 — 打招呼、闲聊等直接给出结果并返回。