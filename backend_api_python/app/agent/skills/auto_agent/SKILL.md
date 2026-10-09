---
name: auto_agent
version: 0.1.0
description: 股票多维度资本面尽调流水线——一次性拉取 K线走势、筹码分布、融资融券/大宗交易摘要、主力/超大单资金流向，用于个股或批量股票的复盘、调研、资金面画像。当用户表述类似"分析/复盘/调研/梳理/拆解XX股票"、"XX资金面怎么样"、"看一下XX筹码结构"、"XX主力流向"时优先匹配本技能。
tags:
  - stock-analysis
  - kline
  - chip-distribution
  - fund-flow
  - capital-summary
  - multi-dimensional-analysis
tools:
  - activate_tools
  - agent_get_kline
  - get_chip_distribution
  - get_capital_summary
  - get_fund_flow
---
<!-- auto-brewed 2026-10-09 from root_id=398 chain=agent | human-edited 后请去除 auto_ 前缀接管 -->
# 股票多维度资本面尽调（auto_agent）

## 使用场景

适用于：
- 对**单只或批量（多只）目标股票**做中长线资本面综合尽调，组合维度包括：K线价格序列、筹码成本与集中度、融资融券/大宗交易边际变化、主力/超大单资金净流入。
- 用户给出具体股票名/股票代码，希望"分析、复盘、调研、梳理、拆解、看一看"等情况。
- 资金面与筹码结构画像——用于判断杠杆资金动向、套牢/获利盘比例、主力是否连续流入。

不适用：
- 短线打板/涨停板数追踪、炸板回封、龙虎榜/题材归因：改用 `get_limit_pool` / `get_broken_board` / `get_dragon_tiger` / `get_hot_stocks_with_reason`。
- 单纯技术形态识图（头肩顶、双底、三角形、杯柄等）：改用 `analyze_chart_patterns` / `analyze_pattern`。
- K线单根形态识别（锤子线/吞没/三连阳等）：改用 `analyze_pattern`。
- 跨市场指数、ETF行情：改用 `get_index_quote` / `get_index_kline`。
- 市场情绪/温度判断（贪恐指数、情绪历史、情绪快照）：改用 `get_fear_greed` / `get_emotion_history` / `get_emotion_latest`。
- 个股基础财务、估值横向对比、F10 详情：改用 `get_finance` / `batch_valuation_compare` / `get_f10_*`。
- 全市场扫雷、活跃代码表扫描：改用 `all_codes` / `get_hot_rank` / `get_hot_sectors`。
- 策略回测历史查询：改用 `get_backtest_history`。

## 执行流程

按以下顺序串行调用，输出按目标代码分组统一汇总（最新一期排前）。

### 步骤 1：按需激活工具

- 工具：`activate_tools`
- 参数：`names: str`（**字符串**，逗号分隔）
- 调用示例：`activate_tools(names="agent_get_kline,get_chip_distribution,get_capital_summary,get_fund_flow")`
- 加工要点：检查返回中 `新激活 N / 未找到 N / 当前已激活按需工具 N/12`。若任一名未找到，立即停止后续步骤并要求用户确认名称。

### 步骤 2：拉取 K 线 OHLCV

- 工具：`agent_get_kline`
- 参数：
  - `codes: str`（**字符串**，单只或多只逗号分隔，支持批量）
  - `timeframe: str`（默认 `'1D'`，常用 `'1D' / '1W'`）
  - `days: int`（默认 30，1D/1W 走本地库直连时可放大至 60~120）
  - `adj: str`（默认 `'qfq'` 前复权）
- 返回结构：`{"count": N, "data": {code: [{t, o, h, l, c, v, ...}, ...]}}`
- 加工要点：
  - 提取每只目标代码每日 OHLCV 序列，注意 `t` 字段为短格式 `'MM-DD'`。
  - 批量调用时 `data` 为 `code → 序列` 的字典，需按 code 索引再聚合。

### 步骤 3：拉取筹码分布

- 工具：`get_chip_distribution`
- 参数：
  - `codes: str`（**字符串**，支持批量）
  - `lookback_days: int`（默认 120）
- 关键字段：
  - `avg_cost`：平均持仓成本
  - `current_price`：当前股价
  - `profit_ratio` / `loss_ratio`：获利盘 / 套牢盘比例（0~1）
  - `concentration_90` / `concentration_90_lower` / `concentration_90_upper` / `concentration_90_width_pct`：90% 筹码集中度（定性标签 + 上下界绝对价位 + 宽度）
  - `concentration_70_*`：70% 筹码集中度（同上结构）
- 加工要点：
  - `profit_ratio` / `loss_ratio` 转换为百分比字符串便于阅读。
  - `concentration_*_width_pct` 是上下界差的相对比，**不是盈亏百分比**；解读时要结合 `upper / lower` 的绝对价位与当前股价对比下结论。
  - 把当前价相对均价的偏离方向（上方套牢 vs 下方获利）给出定性结论。

### 步骤 4：拉取资金面摘要（融资融券 + 大宗交易）

- 工具：`get_capital_summary`
- 参数：`codes: str`（**字符串**，支持批量）
- 关键字段（来自返回 `summary`）：
  - `margin`：`rz_balance` 融资余额、`rz_5d_change_pct` 5日变化、`rq_balance` 融券余额、`signal` 信号文本（如"融资净流入"）
  - `block_trade`：`recent_count` 近期大宗交易次数、`avg_premium_pct` 平均溢价率、`inst_buy` 机构买入次数
- 加工要点：结合步骤 3 的成本区间，判断融资余额边际变化方向（流入 = 杠杆资金加仓，流出 = 减仓）。`signal` 字段为字符串，给出方向但需结合数值复核。

### 步骤 5：拉取资金流向（主力 / 大单 / 超大单）

- 工具：`get_fund_flow`
- 参数：
  - `scope: str`（默认 `'stock'`，本技能固定用 `'stock'`）
  - `codes: str`（**字符串**，支持批量）
  - `days: int`（默认 120）
  - `indicator: str`（默认 `'今日'`）
- 关键字段：
  - `points`：数据点数
  - `total_main_net`：主力净流入合计（绝对金额）
  - `data[]`：按时段或按日的 `main_net / small_net / mid_net / large_net / super_net`，带 `time` 字段
- 加工要点：
  - 把 `data` 按 `time` 升序或倒序排列后再聚合。
  - `small_net / mid_net / large_net / super_net` 可能为 `null`（表示当日该分项未采集到），按"数据缺失"处理，**不能当作 0**——否则会误判净流入方向。
  - 取 `total_main_net` 与 `data[]` 单日 `main_net` 累加交叉验证，避免总量与明细不一致。

### 步骤 6：汇总呈现

将上述 4 类数据按目标代码分组，输出一份结构化报告：

1. **价格走势**：K线区间、振幅、最新一期收盘。
2. **筹码结构**：均价成本、当前价偏离、获利盘/套牢盘占比、90%/70% 集中度区间。
3. **资本面**：融资余额 + 5日变化 + 融券余额 + 大宗交易次数/溢价/机构买入。
4. **资金流向**：主力净流入总额、近期分项方向（结合 null 缺失说明）。

## 注意事项

1. **批量参数类型**：所有"支持批量"工具的入参（`codes`）一律为**字符串**（逗号分隔），不要写成 `List[str]`。
2. **null ≠ 0**：`get_fund_flow` 在某些时段未采集大单/超大单等分项时会返回 `null`，必须按"数据缺失"处理，不可视为 0；同样地，`get_capital_summary` 的 `inst_buy` 等字段也可能是 `null`，需明确说明。
3. **筹码集中度宽度易误读**：`concentration_*_width_pct` 是上下界差相对值，不是盈亏百分比；务必读 `concentration_*_lower` 与 `concentration_*_upper` 的**绝对价位**再与当前股价对比。
4. **K线短日期格式**：`agent_get_kline` 在 `1D` 频次下 `t` 字段为 `'MM-DD'`（不带年份），跨年度回测需自行补年份上下文，否则无法对齐到具体交易日。
5. **按需工具有上限**：系统按需工具槽位为 12 个，超额激活会失败。执行前先看 `activate_tools` 返回中的 `当前已激活按需工具 N/12`；如已接近上限，先 `activate_tools` 替换再继续，避免重复激活造成浪费。
6. **批量 vs 单只语义一致性**：`agent_get_kline` / `get_chip_distribution` / `get_capital_summary` / `get_fund_flow` 均支持批量，但返回结构均为 `data → {code: ...}` 字典映射，**单只调用也是同样的字典结构**，下游逻辑不能假设是数组。
7. **数据时效性**：`get_capital_summary` 的融资融券、块交易为日频截面；`get_fund_flow` 在 `indicator='今日'` 下为分时级、`days>1` 时为日频级——下游呈现时要按数据点的 `time` 颗粒度分别说明，不能把分时数据按日频误读。
8. **复权口径**：默认 `adj='qfq'` 前复权。切换至不复权或后复权会改变历史价位与后续计算的输入，需在最终报告中显式注明复权方式。
9. **回测/短线/情绪类工具的边界**：本技能不覆盖涨停池、龙虎榜、情绪指数、个股 K 线形态、技术评分等维度；用户提出这些子问题时，应主动切换到对应工具，而不是强行在本流水线内做扩展。
10. **激活失败熔断**：若 `activate_tools` 任一名字未找到，立即停止后续步骤并要求确认；不要试图在未激活工具上继续调用，会失败并浪费一轮调用预算。
