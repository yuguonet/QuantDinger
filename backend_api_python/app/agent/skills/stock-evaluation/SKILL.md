---
name: stock-evaluation
version: 3.0.0
description: 个股综合评估系统。用户问"分析XX股票""XX股票怎么样""评估XX"时使用。
tags: [stock, evaluation, analysis, technical, fundamental]
tools:
  - technical_analysis
  - get_fund_flow
  - get_realtime_quote
  - get_stock_info
  - search_stock_intel
  - get_capital_summary
  - get_indicator_snapshot
  - web_search
  - stock_report
---

# 个股综合评估 (stock-evaluation)

## 使用场景

用户询问"分析XX股票""XX股票怎么样""评估XX""XX值得买吗"时使用。

## 执行流程

### Step 1: 解析用户需求

从用户输入中提取参数：
- `codes`: 股票代码（支持多股，逗号分隔）
- `period`: 分析周期（T+1/T+3/T+5/1W/1M，默认 T+3）
- `depth`: 分析深度（simple/standard/deep/complete，默认 standard）

```python
# 示例：用户输入 "分析300129 T+3深度分析"
codes = "300129"
period = "T+3"
depth = "deep"
```

### Step 2: 执行代码生成标准输出

调用 `evaluate_stock()` 一趟水获取数据，生成 stock_report：

```python
from skills.stock_evaluation.run import evaluate_stock, evaluate_stocks

# 单股
result = evaluate_stock(codes="300129", depth="standard", period="T+3")

# 多股
result = evaluate_stocks(codes="600519,300129,000858", depth="standard", period="T+3")
```

返回：
```python
{
    "code": "300129",
    "name": "泰胜风能",
    "score": 72,
    "direction": "看涨",
    "action": "持有",
    "report": "**stock_report 标准输出**",  # 格式化报告
    "summary": {...},  # 结构化摘要
    "tool_results": {...},  # 中间数据
    "verified": ["⭐ 技术面+资金面双重看多"],  # 交叉验证
    "llm_data": {...},  # LLM 需要的数据
}
```

### Step 3: LLM 综合分析

将 `report` + `summary` + `tool_results` + `llm_data` 注入到任务上下文，由 CodeAgent 做综合分析：

```python
task_parts.append(f"【stock_report 结果】\n{result['report']}")
task_parts.append(f"【结构化摘要】\n{result['summary']}")
task_parts.append(f"【交叉验证】\n{result['verified']}")
task_parts.append(f"【LLM分析数据】\n{result['llm_data']}")
```

LLM 根据以上数据生成：
- 核心逻辑（为什么涨/跌）
- 风险点（需要注意什么）
- 操作建议（具体策略）
- 综合评估（一句话总结）

### Step 4: 输出

用 `final_answer()` 输出最终报告。

## 深度级别

| 级别 | 名称 | 工具调用 | 适用场景 |
|------|------|----------|----------|
| **L1** | 快速 | technical_analysis | 盘中快速筛选 |
| **L2** | 标准 | +get_fund_flow+get_realtime_quote | 日常分析 |
| **L3** | 深度 | +get_stock_info+search_stock_intel | 详细研究 |
| **L4** | 完整 | +web_search+LLM深度分析 | 重要决策 |

## 周期配置

| 周期 | 技术面 | 资金面 | 基本面 | 新闻面 |
|------|--------|--------|--------|--------|
| T+1 | 50% | 25% | 10% | 15% |
| T+3 | 40% | 20% | 15% | 10% |
| 1W | 30% | 20% | 25% | 10% |
| 1M | 25% | 15% | 30% | 10% |

## 交叉验证（加星 ⭐）

两个不同来源验证同一结论时加星：
- 技术面 + 资金面双重看多 → ⭐
- 趋势 + 指标双重确认 → ⭐
- 资金面 + 新闻面双重验证 → ⭐

## 数据兜底

```python
fallback_chain = {
    "get_fund_flow": ["get_capital_summary", "web_search"],
    "get_stock_info": ["get_realtime_quote", "web_search"],
    "search_stock_intel": ["web_search"],
}
```
