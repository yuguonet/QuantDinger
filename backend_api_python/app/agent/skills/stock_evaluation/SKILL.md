---
name: stock_evaluation
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

# 个股综合评估 (stock_evaluation)

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

**⚠️ 必须先检查错误再使用结果：**
```python
if "error" in result:
    final_answer(f"评估失败: {result['error']}")
else:
    report = result["report"]
    # ... 继续处理
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

### Step 3: 综合分析 + 输出

从 result 中提取数据，生成综合分析，最终用 `final_answer()` 输出：

```python
result = evaluate_stock(codes="300129", depth="standard", period="T+3")

if "error" in result:
    final_answer(f"评估失败: {result['error']}")
    return

report = result["report"]      # 标准化报告（含注意栏）
llm_data = result["llm_data"]  # 技术因子/资金面/指标详情
verified = result["verified"]  # 交叉验证
```

基于 llm_data 生成综合分析（150字以内），拼在 report 后面：

```python
analysis = "你的分析内容..."  # 核心逻辑 + 风险点 + 操作建议
final_answer(report + "\n**综合分析**: " + analysis)
```

**最终输出顺序**：报告 → 注意 → 综合分析（注意由 evaluate_stock 自动生成，你只需追加综合分析）

**综合分析要求：**
- 核心逻辑：为什么涨/跌（基于技术面+资金面数据）
- 风险点：需要注意什么
- 操作建议：具体策略（持有/买入/卖出的时机）
- 控制在 150 字以内

**错误示范：**
- ❌ `final_answer(str(result))` — 输出原始 dict
- ❌ `final_answer("综合分析报告内容")` — 占位符

### Step 3b: 多股分析

多股时用 `evaluate_stocks()`，返回每只股的标准化报告 + 对比排名：

```python
result = evaluate_stocks(codes="600519,300129,000858", depth="standard", period="T+3")

if "error" in result:
    final_answer(f"评估失败: {result['error']}")
    return

# result["comparison"] 已包含每只股的标准化报告 + 对比排名表
comparison = result["comparison"]

# 为每只股生成综合分析（可选）
analyses = []
for s in result["stocks"]:
    llm_data = s.get("llm_data", {})
    # 基于 llm_data 生成该股的综合分析
    analysis = f"{s['name']}: 你的分析..."
    analyses.append(analysis)

# 输出：对比报告 + 各股综合分析
final_answer(comparison + "\n\n**综合分析**:\n" + "\n".join(analyses))
```

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
