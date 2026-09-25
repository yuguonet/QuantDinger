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

## 执行流程（严格按此顺序，不可跳过）

使用 `filter_candidates()` 辅助工具筛选，**不要自己写筛选代码**。输出结果由你自己生成，按 Phase 3 格式规则。

```python
# step1: 获取候选
result = pre_screen()

# step2: filter_candidates() → 自动按 strategy 筛选 → 返回 codes 字符串
codes = filter_candidates(result)

# step3: deep_analyze(codes=codes) — codes 是逗号分隔的字符串
if codes:
    deep_result = deep_analyze(codes=codes)
else:
    deep_result = {"analyzed": [], "strategy": result.get("strategy", ""), "score": 0, "direction": "neutral"}

# step4: 按 Phase 3 格式规则生成输出，用 final_answer 输出
final_answer(你生成的markdown文本)
```

## Phase 1: 获取候选

```python
result = pre_screen()
```

返回:
- `result["strategy"]` — 当前策略 (intraday/eod/post_market)
- `result["market"]` — 市场状态 (mood, fund_flow, zt_count, dt_count, broken_rate)
- `result["candidates"]` — 候选股列表
- `result["main_themes"]` — 主线题材

候选股结构: `{code, name, source, continuous_days, change_pct, turnover_pct, reason, ...}`

## Phase 2: 筛选 + 分析

调用 `filter_candidates(result)` 自动按策略筛选，返回 codes。

然后 `deep_analyze(codes=codes)` 做深入分析，返回 `{score, direction, confidence, signal, analyzed, strategy}`。

`analyzed` 每项含 `{code, name, score, direction, signal, signals, factors}`

## 筛选规则（已由 filter_candidates() 内部实现，勿手写代码）

**筛选已全部封装在 `filter_candidates(result)` 里——你只需调用它，不要自己写任何筛选/过滤代码。**
（下方规则仅用于帮助你理解工具行为、以及向用户解释筛选依据，**不是让你去实现它们**。）

### 通用排除规则

- ST 股排除（所有策略）
- 换手率 < 2% 排除（所有策略，活跃度不够）

### 涨停封板处理（策略相关）

- **盘中/尾盘（intraday/eod）**: 涨停封板股排除（change_pct 接近涨停幅度，买不进去）
- **盘后（post_market）**: 不排除涨停股（盘后复盘关注已涨停/大涨股，评估次日机会）

### 盘中策略 (intraday) 筛选规则

盘中关注主线题材 + 弱转强信号：

1. **主线题材优先**: main_themes 中的题材对应的候选股优先保留
2. **连板股保留**: source="连板" 的候选股保留（连续涨停，市场关注度高）
3. **龙回头保留**: source="龙回头" 且有弱转强信号的保留
4. **条件搜索结果**: 根据 market.mood_score 或 market.mood 取舍
   - mood="偏强" 或 mood_score >= 70 → 保留全部
   - mood="中性" 或 mood_score >= 50 → 保留有 reason 或 tags 的
   - mood="偏弱" 或 mood="弱势" → 只保留 source="连板" 或 "龙回头"
5. **最终数量**: 控制在 5-8 只，太多会分散分析资源

### 尾盘策略 (eod) 筛选规则

尾盘关注收盘形态 + 放量：

1. **收盘=最高价**: 优先保留（收盘价与最高价差 < 0.3%）
2. **大幅放量**: 量比 > 2.5 的优先
3. **涨幅适中**: 涨幅 4-6% 的优先
4. **最终数量**: 控制在 3-5 只

### 盘后策略 (post_market) 筛选规则

盘后复盘已收盘，关注次日机会。source 可能值: "热点题材" / "盘后筛选" / "4IN1(近期涨停)" / "龙回头"

1. **热点题材优先**: source="热点题材" 的候选股(有题材 reason)优先保留
2. **近期涨停活跃股补充**: source="4IN1(近期涨停)" 或 "龙回头" 的优先保留
3. **主线题材匹配**: reason 涉及 main_themes 中题材的优先
4. **换手率适中**: 5-15% 优先（换手率过低可能缩量，过高可能出货）
5. **涨幅参考**: 大涨股(>=9%)需有题材支撑；涨幅 < 5% 且调整充分的保留
6. **最终数量**: 控制在 5-10 只

### 市场状态判断

从 result["market"] 读取:
- `mood`: "偏强" / "中性" / "偏弱" / "弱势"
- `mood_score`: 情绪评分 0-100（>=70 偏强，>=50 中性，>=30 偏弱，<30 弱势）
- `fund_flow`: 资金流向（正=净流入，负=净流出）
- `zt_count` / `dt_count`: 涨停/跌停家数
- `broken_rate`: 炸板率

市场弱势（mood="偏弱" 或 "弱势"）：
- 减少候选数量（3-5只）
- **盘中（intraday）** 只保留确定性高的（连板、龙回头）；盘后（post_market）和尾盘（eod）按各自规则执行，不受此限制
- 降低评分预期

## 评分语义（2026-09-25 起）

- **score = P(下一交易日上涨)×100**，由已标定模型给出（复用自选股 T+1 预测，不是手拍技术分）
- 技术形态/MACD/资金流只作**解释与信号文案**，不再直接加减分
- `tech_score` 字段保留原始技术分，仅供对照
- 方向：p_up≥0.55 看多，≤0.45 看空，其余中性
- 综合评分 = 推荐列表 Top5 的 score 均值（反映最终选股质量）
- **情绪分桶**（filter + 排序共同生效）：
  - `weak`（mood_score<40 或 偏弱/弱势）：涨停活跃源无 reason 剔除；预测 p_up 门槛 ≥0.55；连板/4IN1/龙回头 p_up×0.92；最多 5 只
  - `neutral`：门槛 ≥0.50，最多 8 只
  - `strong`（≥70 或 偏强）：门槛 ≥0.48，最多 10 只
- 价格 <2 或 >300、ST、换手 <2% 一律排除

## Phase 3: 输出结果

直接把 markdown 文本传给 `final_answer(...)` 输出（如 `final_answer(md)`）。

**不要为拼装报告编写辅助函数**（如 buy_hint / risk_hint / fmt_pct 之类）：买点参考、风险提示等文案由你依据 `deep_result` 里的真实数据直接撰写，一次 `final_answer` 输出即可——这类表述属于灵活生成，不要在沙箱里用代码做条件分支拼装。

### 格式规则

1. **标题行**: `**{strategy}**`（策略名，如 post_market / intraday / eod）
2. **评分行**: ` 综合评分: {score}/100`
3. **方向行**: ` 方    向: {看多/看空/中性}`
4. **空行**
5. **表头**: `股票代码 股票名称 评分 方向 置信度 信号`
6. **数据行**: 每只股票一行，空格分隔
7. **置信度**: 0-1.0数值越高, 代表分析结果越可靠

### 示例

```
**post_market**
 综合评分: 68.8/100
 方    向: 看多
 置 信 度: 0.85

股票代码	股票名称	评分	方向	置信度	信号
301199	迈赫股份	70.0	看多	0.85	突破前高, 放量5.2倍
301379	天山电子	65.0	看多	0.85	突破前高, MA多头排列, 放量1.8倍
300129	泰胜风能	70.0	看多	0.85	突破前高, 放量3.3倍
```

如果无符合条件的股票，输出: `当前无符合条件的股票`。

表中「评分」列填 `score`（P(次日涨)×100）；信号列用 `deep_result` 里的真实 signals 撰写（突破/放量/金叉等），不要复述评分。

## 参考资料

按需查阅:
- `references/trading-logic.md` — 核心交易逻辑
- `references/market-sentiment.md` — 市场情绪判断标准
- `references/limit-rules.md` — 涨跌停规则
- `references/strategy-notes.md` — 已验证策略参考
