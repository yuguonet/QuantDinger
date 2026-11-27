---
name: position-cost-analysis
description: 针对已持仓的某只股票，结合用户给定的持仓成本价，拉取实时行情、日线、资金流、筹码分布与技术面评分，客观定位"成本 vs 现价 vs 市场平均成本"的差距并给出持有/减仓/止损/补仓的条件化处置选项；当用户问"我在 X 价买入/成本 X 元该怎么办""这只票被套了怎么处理""要不要割"这类持仓诊断问题时使用。
metadata:
  version: 0.1.0
  tags: 持仓诊断,成本分析,处置决策,技术面,筹码分布,资金流,风险控制
  tools: agent_get_kline, get_fund_flow, get_chip_distribution, get_capital_summary, bull_bear_research, analyze_pattern, daily_live, get_dividend, get_realtime_quote, technical_analysis, activate_tools
---
<!-- auto-brewed 2026-11-26 from root_id=5364 chain=agent | human-edited 后请去除 auto- 前缀接管 -->
# 持仓成本诊断与处置选项生成

## 使用场景

**适用**：用户已持有某只 A 股（或明确给出一个买入成本价），要求判断"现在该怎么处理"。典型问法：
- "我成本 X 元，现在怎么办？"
- "这只票套了 N%，要不要割 / 补仓 / 做 T？"
- "分析一下我的持仓，给出处置建议"

**不适用 / 边界**：
- 用户只是要选股、要标的推荐 —— 本技能不做选股，也不做标的筛选规则。
- 用户没有持仓、没有成本价 —— 归到纯行情/技术面分析类技能。
- 需要组合层仓位管理、跨标的资产配置 —— 本技能只诊断单个目标实体。
- 需要下单或修改账户 —— 本技能只输出客观事实与条件化选项，不执行交易。

## 执行流程

统一记目标标的为 **目标股票 / 目标代码**，用户给的买入价为 **成本价 C**。

### 1. 归一化输入
- 从用户话术里解析出：目标实体（名称或代码）、成本价 C（数值，单位元）、可选的持仓方向/比例、可选的持有天数。
- 若用户只给名称，用全市场代码表（`all_codes()`）或行情接口回填目标代码；解析不唯一时先向用户确认再继续。
- **成本价可能是除权前的名义价**：若目标股票近期有分红送转（`get_dividend(code)`），需说明名义成本与后复权成本的差异，后续一律用 `adj='qfq'` 口径的 K 线做对比，避免"假亏损"。

### 2. 拉实时快照，算浮盈浮亏
- 工具：实时行情接口（`get_realtime_quote`，按需层工具，若当前工具清单里没有就先 `activate_tools(names)` 点名激活）。
- 入参：目标代码（字符串）；支持批量时仍建议单只调用。
- 取字段：`last`（现价）、`changePercent`、`open`/`high`/`low`、`previousClose`、`volume`、`limit_up`/`limit_down`、`time`。
- 加工：
  - 浮动盈亏率 = (last − C) / C × 100%
  - 距涨停/跌停空间 = (limit_up − last)/last、(last − limit_down)/last
  - 记录 `time`，明确这是"某时刻"的快照，非收盘价。

### 3. 取日线与趋势结构
- 工具：`agent_get_kline(codes=<目标代码>, timeframe='1D', days=60~120, adj='qfq')`。
- 若当日 bar 尚未回填，改用 `daily_live(code, days=200)` 合成当日 bar。
- 输出是文本表格（列：`t o h l c v`，日期形如 `MM-DD`），需自行解析成序列。
- 从序列计算客观量：近 5/20/60 日均价与均线、区间最高/最低、成本价 C 在近 N 日成交区间中的分位、当前价相对各均线的位置、近 5 日与近 20 日量能比。
- 目的只有一个：回答"成本价 C 落在历史筹码区的哪个位置、现价离它多远"。

### 4. 资金流（带口径警告）
- 工具：`get_fund_flow(scope='stock', codes=<目标代码>, days=120, indicator='今日')`。
- 取 `total_main_net`、`recent_5d_main_net`、`recent_20d_main_net`，以及返回里 `approx` / `source` / `note` 三个元信息。
- **必须**在结论中标注：当 `approx: true` 且 `source: realtime_snapshot` 时，该净额是量价方向近似（涨计流入、跌计流出），**不是真实主力分类**，`small/mid/large/super` 通常为空，禁止据此断言"主力出货/吸筹"。

### 5. 筹码分布（成本对比的核心证据）
- 工具：`get_chip_distribution(codes=<目标代码>, lookback_days=60)`。
- 取：获利比例、平均成本、90% 筹码集中度、套牢/获利盘比例。
- 关键对比三条线：**用户成本 C** ↔ **市场平均成本** ↔ **现价**。
  - C 显著高于平均成本 → 用户处于筹码区上方，解套需越过密集区，压力位即平均成本/集中区上沿。
  - C 接近或低于平均成本 → 用户成本有优势，回撤容忍度更高。
  - 获利比例极低（如个位数）+ 现价远低于平均成本 → 客观上属"深套"状态，但**不直接等于应止损**。

### 6. 技术面打分与形态
- 工具：`technical_analysis`（按需层，缺失时先 `activate_tools` 激活）；可选 `analyze_pattern(codes=<目标代码>)`、`analyze_chart_patterns(codes=<目标代码>)`。
- 取：综合 `score`（0–100）、`direction`、`signal` 文本、分项（趋势/指标/量价/形态/筹码/流通盘）、BOLL 上中下轨、`falsifiable_conditions`。
- 这些是**客观评分与形态事实**，可直接引用数值与信号文本，但不要把它当买卖指令。

### 7.（可选）多空与基本面补充
- 单只标的的多空综合：`bull_bear_research(codes=<目标代码>)` → 多空评分与方向。
- 中长线视角：`get_capital_summary(codes=<目标代码>)` → 营收/利润增速、ROE、PE/PB、机构持仓变化。
- 仅在用户问题涉及"长期拿不拿得住"时才拉，避免噪音。

### 8. 组织输出：事实 → 关键位 → 条件化处置矩阵
输出结构固定为三段：

1. **事实快照**：现价、成本价 C、浮动盈亏率、距涨跌停空间、数据时间戳；均线/区间分位；资金流（含口径声明）；筹码三条线；技术面评分与信号。
2. **关键价位**：支撑（近 N 日低点/BOLL 下轨/平均成本下沿）、压力（平均成本/筹码密集区上沿/BOLL 中轨或上轨/前高）。用数值给，不模糊表述。
3. **条件化处置矩阵**（不写死阈值，阈值由用户风险偏好或上层策略给定）：
   - 持有：成立条件 = 现价站稳某支撑 + 技术面 direction 未转空 + 筹码未恶化。
   - 减仓：成立条件 = 反弹至压力位未放量突破 / 资金流转负。
   - 止损：成立条件 = 有效跌破支撑位（给出具体价位）且形态确认。
   - 补仓/摊薄：成立条件 = 现价靠近强支撑 + 技术面评分回升 + 用户仓位与风险预算允许；同时给出摊薄后的新成本价算式 `(C×股数 + P×加仓股数)/(股数+加仓股数)` 供用户自行代入。
   - 每条都附上来自第 6 步的 `falsifiable_conditions` 作为"判断失效"的观测点。
4. 结尾显式声明：以上为客观数据与条件罗列，非投资建议；决策与仓位由用户承担。

## 注意事项

- **禁止编造工具**：本技能只使用签名清单内工具；`get_realtime_quote`、`technical_analysis` 属按需层，若当前请求的工具清单中没有，先调 `activate_tools(names='get_realtime_quote,technical_analysis')` 激活再用，不要臆造接口名。
- **成本口径**：用户口头成本常是名义价，遇到近 60 日内除权除息必须换算到 `qfq` 口径再比较，否则会得出错误的亏损幅度。
- **资金流是近似值**：`get_fund_flow` 返回 `approx: true` + `source: realtime_snapshot` 时只有方向近似意义；`note` 字段会明确写"非真实主力分类"。不要把 `recent_5d_main_net` / `recent_20d_main_net` 当成主力净买额讲。
- **K 线是文本表格**：`agent_get_kline` 返回 `count/adj` 头 + 制表符分隔行，列序为 `t o h l c v`，日期为 `MM-DD` 无年份，跨年区间需自行补年份；解析失败时降级用 `daily_live`。
- **快照有时效**：实时行情带 `time` 字段，盘前/盘后可能停在上一交易日；结论里必须带时间戳，且明确浮亏是按快照价而非收盘价算的。
- **技术面评分非指令**：`score` 在 40–60 区间常为 neutral，`calibrated: false` 表示未校准；只陈述分数与信号原文，不要改写成"建议买入/卖出"。
- **避开低胜率工具**：`batch_valuation_compare`、`create_alert_job`、`get_fund_flow_daily`、`get_index_quote`、`get_market_flow_state`、`get_market_overview`、`search_resources` 近期链路胜率偏低，本流程不依赖它们；确需大盘背景时优先 `fetch_overview()` / `fetch_sentiment()` / `fear_greed_index()`。
- **不要写死取舍规则**：什么点位该止损、亏损多少必须砍、补仓比例上限，属于用户/上层策略的规则，技能只提供客观事实与条件表达式，绝不内置阈值。
- **缺数据时的降级顺序**：实时行情缺失 → 用 `agent_get_kline` 最后一根收盘价并标注"非实时"；筹码缺失 → 用 K 线成交密集区近似并标注为近似；技术面工具不可用 → 仅输出价格与均线事实，不给方向判断。
