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
# 用户说 “全面分析300497明天的走向” → depth=complete, period=T+1
# 用户说 “深度分析300129” → depth=deep
# 用户说 “快速看看000858” → depth=simple
```

### Step 2: 执行代码生成标准输出

```python
from skills.stock_evaluation.run import parse_user_input, evaluate_stock, evaluate_stocks

params = parse_user_input(user_input)

# 再执行（单股）
result = evaluate_stock(codes=params["codes"], depth=params["depth"], period=params["period"])

# 多股
result = evaluate_stocks(codes=params["codes"], depth=params["depth"], period=params["period"])
```

**⚠️ 必须先检查错误再使用结果：**
```python
if "error" in result:
    final_answer(f"评估失败: {result['error']}")
    return  # 错误时才 final_answer，成功时继续到 Step 3

# 成功时不要 final_answer，继续到 Step 3
report = result["report"]
llm_data = result["llm_data"]
verified = result["verified"]
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

### Step 3: 直接输出

report 已包含综合分析，直接输出即可：

```python
result = evaluate_stock(codes="300129", depth="standard", period="T+3")

if "error" in result:
    final_answer(f"评估失败: {result['error']}")
    return

# report 已含支撑位/压力位/信号/综合分析，直接输出
final_answer(result["report"])
```

**最终输出示例**：
```
**股票名称**: 多氟多 (002407)
**综合评分**: 56
**操作建议**: 持有
**方    向**: 中性
**置 信 度**: 高
**时间窗口**: T+3
**当 前 价**: 31.70
**支 撑 位**: 30.00
**压 力 位**: 32.31
**信号**: 偏离MA20达-17.9%，超跌反弹
**综合分析**: 形态:三连阳（多头排列） | 形态:红三兵（强烈看多），资金流向健康，指标健康。趋势向上，建议持有。
```

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
| **L1** | 快速 | technical_analysis + realtime_quote + fund_flow + capital | 盘中快速筛选 |
| **L2** | 标准 | +indicator_snapshot + volume_analysis + trend | 日常分析 |
| **L3** | 深度 | +stock_info + search_stock_intel + chip_distribution | 详细研究 |
| **L4** | 完整 | +web_search | 重要决策 |

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
