---
name: market-screener
version: 2.0.0
description: 自包含选股技能,从A股全市场筛选短线标的。用户问"今天买什么股""有什么好股票""短线选什么"时使用。
tags: [market, screener, short_term, a_share]
tools:
  - get_fund_flow_realtime
  - get_indicator_snapshot
  - search_stocks
---

# 全市场短线选股 (market-screener)

## 描述

根据当前交易时间自动切换策略,从A股全市场筛选短线标的。

## 使用场景

- 用户询问"今天买什么股""有什么好股票""短线选什么"等全市场选股问题
- 用户要求盘后复盘、尾盘隔夜选股、盘中热点追踪选股
- 需要对全市场做系统性筛选,而非分析单只股票

注意:如果用户指定了具体股票代码,请使用其他个股分析技能,不要调用本技能。

## 策略调度

本技能根据交易时间自动选择策略:

| 时间窗口 | 策略 | 核心逻辑 |
|---|---|---|
| 09:30-14:29 | 盘中短线 | 涨停池连板 + 主线题材龙头 + 龙回头弱转强 |
| 14:30-15:00 | 尾盘隔夜 | 条件初筛 + 尾盘特征验证 + 尾盘封板 |
| 15:00+ / 非交易日 | 盘后复盘 | 全市场技术形态扫描 + 介入点计算 + 次日计划 |

## 执行流程

### Phase 1 - Python 预筛选

从涨停池、跌停池、炸板池、强势股题材归因、热门板块等数据源获取原始数据,按策略规则筛选候选标的。

```python
from app.agent.skills.market_screener.run import pre_screen
result = pre_screen()
# result["candidates"] → 候选股列表
# result["market"] → 市场情绪
# result["main_themes"] → 主线题材
```

### Phase 2 - 逐只深入分析

对 Phase 1 的候选股逐只调用工具获取实时数据,计算综合评分并生成操作建议。

```python
from app.agent.skills.market_screener.run import deep_analyze
final = deep_analyze(result)
# final["output_data"]["analyzed"] → 深入分析结果
# final["score"] → 综合评分
# final["analysis"] → Markdown 报告
```

调用的工具(Phase 2 内部自动调用,无需手动):
- `get_fund_flow_realtime` - 实时资金流向
- `get_indicator_snapshot` - 技术指标快照(MACD/KDJ/BOLL等)
- `search_stocks` - 条件选股(尾盘/盘后策略使用)


### 失败处理

- Phase 1 执行失败 → 不重试,直接告知用户"选股执行失败"
- Phase 2 单只分析失败 → 跳过该股,继续下一只
- 不要重复调用 read_skill,读一次就够

## 输入参数

无参数。

## 输出结构

```json
  {
  "skill": "market-screener",
  "strategy_used": "intraday | eod | post_market",
  "score": 0-100, // 综合评分
  "direction": "bullish | neutral | bearish",
  "confidence": 0-1, // 置信度
  "signal": "...", // 核心信号摘要(一句话)
  "analysis": "...", // Markdown 格式完整报告
  "factors": [...], // 评分因子列表
  "status": "ok | failed",
  "output_data": {
    "market": {...}, // 市场情绪(涨停/跌停/炸板率)
    "main_themes": [...], // 主线题材
    "candidates": [...], // 候选股列表
    "analyzed": [ // 深入分析结果(盘后策略)
      {
        "code": "002816",
        "name": "xxx",
        "score": 100,
        "direction": "bullish",
        "signal": "...",
        "patterns": ["MACD金叉", "突破前高"],
        "entry": {"price_low": 6.23, "stop_loss": 5.92, "target_1": 6.77}
      }
    ],
    "pattern_distribution": {...} // 形态分布统计
  }
  "tools_called": [...] // 实际调用的工具列表
  }
```

**注意**:深入分析结果在 `output_data.analyzed`,不在顶层。访问方式:
```python
best = max(result['output_data']['analyzed'], key=lambda x: x['score'])
```

### 输出示例(盘后复盘)

analysis 字段包含:
- 市场概况(扫描池大小、形态命中数)
- 主线题材识别
- 形态分布统计
- 候选标的列表,每只含:评分、方向、技术形态、信号
- 介入计划:入场区间、止损位、目标价、盈亏比

### 输出示例(盘中短线)

analysis 字段包含:
- 市场情绪判断(涨停/跌停/炸板率)
- 连板龙头列表
- 龙回头弱转强标的
- 主线题材强势股
- 候选标的列表及评分

## 市场情绪判断标准

| 涨停数 | 跌停数 | 情绪 |
|---|---|---|
| ≥50 | ≤10 | 亢奋 |
| ≥30 | ≤20 | 偏暖 |
| <20 | >30 | 冰点 |
| 其他 | | 中性 |

当情绪为冰点时，盘中策略直接返回“空仓观望”建议。

## 涨跌停规则

| 板块 | 涨跌停幅度 | 代码特征 |
|------|-----------|----------|
| 主板/中小板 | ±10% | 60xxxx / 00xxxx |
| ST 股 | ±5% | 名称含 ST |
| 创业板 | ±20% | 300xxx / 301xxx |
| 科创板 | ±20% | 688xxx |
| 北交所 | ±30% | 8xxxxx / 4xxxxx |

## 过滤规则

- **涨停封板股自动排除**：涨幅接近涨停的股票买不进去，不纳入候选
- **Phase 2 低分淘汰**：评分 < 60 或方向为 bearish/neutral 的候选股自动抛弃
- **最终排序**：通过的候选股按评分从高到低排列

## 注意事项

- 市场冰点时不推荐短线操作
- 尾盘策略关注“收盘=最高价”的强势特征
- 盘后策略会计算具体介入点（入场价、止损、目标价）
