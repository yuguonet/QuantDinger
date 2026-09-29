---
name: auto_stock-analyze-300497
version: 0.1.0
description: 对单只 A 股做"技术面+基本面+筹码+资金流+多空研判"的一站式综合分析。适用于用户提到"分析XXX(代码或名称)这只股票/个股/看看这只股/给个分析/技术面基本面一起看"等措辞时；输入一个股票代码或名称，输出多维度分析结论与买卖倾向。
tags: [stock, analyze, technical, fundamental, valuation, chip, fund-flow, bullbear, A股]
tools: [resolve_stock, get_stock_info, get_stock_sector_info, get_realtime_quote, agent_get_kline, analyze_chart_patterns, analyze_pattern, get_capital_summary, get_chip_distribution, get_fund_flow_daily, bull_bear_research]
---
<!-- auto-brewed 2026-09-29 from root_id=984 chain=stock+analyze+300497 | human-edited 后请去除 auto_ 前缀接管 -->
# 单股综合分析技能

## 使用场景

适用于：用户给出一个 A 股代码或名称（6 位数字代码或中文名均可），希望对该股做一次综合性的诊断，涵盖行情快照、K线走势、形态识别、估值基本面、筹码分布、资金流向以及多空研判。

典型触发措辞：
- "分析一下 300497"
- "看看这只股票 / 这支票怎么样"
- "给 XX(名称)做个技术面基本面一起看的分析"
- "综合分析一下 XX"

**不适用**：
- 多只股票批量筛选或横向对比（应使用 batch_valuation_compare / sector 类工具的批量技能）；
- 板块/指数/ETF 行情分析（应使用 get_sector_board / get_index_kline 等专属技能）；
- 仅查询某一项指标（如只想看 PE、只想看龙虎榜）——调用单项工具即可，无需走本技能。

## 执行流程

1. **解析标的**
   - 调用 `resolve_stock(keyword, market='CNStock', limit=10)`
   - 入参 `keyword`：用户给出的代码或名称（字符串）。
   - 用途：把名称解析为代码，或校验用户给的代码合法；输出取第一条匹配项的 `code` 字段作为后续所有调用的统一标的 `target_code`（字符串，例如 `"300497"`）。
   - 解析失败则终止并提示用户。

2. **拉取基础信息与所属行业/概念**
   - 调用 `get_stock_info(codes=target_code, detail=True)`
   - 调用 `get_stock_sector_info(codes=target_code)`
   - 用途：确认股票全称、所属行业、所属概念板块，为后续板块对照做铺垫。

3. **拉取实时行情快照**
   - 调用 `get_realtime_quote(codes=target_code)`
   - 用途：拿当前价、涨跌幅、换手率、量比、PE、PB、总市值等作为分析"当前状态"的基准。
   - 加工：把 `price / change_pct / turnover_rate / pe / pb / market_cap` 整理到结论摘要顶部。

4. **拉取日 K 线（近 30 日，前复权）**
   - 调用 `agent_get_kline(codes=target_code, timeframe='1D', days=30, adj='qfq')`
   - 入参 `codes` 为单元素列表（或直接传字符串，签名支持批量但单股时传字符串亦可；建议统一传字符串保持与后续工具一致）。
   - 用途：得到统一结构的 OHLCV 序列，供形态识别模块消费；同时自己计算近 N 日趋势方向（均价、振幅、连续阳/阴线天数）。

5. **图表形态识别（周/日级别）**
   - 调用 `analyze_chart_patterns(codes=target_code)`
   - 用途：识别头肩顶/底、双顶/双底、三角形、旗形、楔形、矩形、杯柄等经典形态。
   - 加工：保留命中的形态名称、方向（看多/看空）、关键价位。

6. **当日 K 线形态识别**
   - 调用 `analyze_pattern(codes=target_code)`
   - 用途：识别锤子线/十字星/吞没/三连阳等当日信号及含义。
   - 加工：与步骤 5 的结果合并展示，给出"形态 + 信号"组合解读（如"双底 + 锤子线"）。

7. **基本面摘要（中长线指标）**
   - 调用 `get_capital_summary(codes=target_code)`
   - 用途：营收/利润增速、ROE、PE/PB 估值、机构持仓变化。
   - 加工：标记增速是否为正、ROE 水平、估值相对位置（高/中/低）。

8. **筹码分布**
   - 调用 `get_chip_distribution(codes=target_code, lookback_days=120)`
   - 用途：获利比例、90% 筹码集中度、平均成本、套牢/获利盘比例。
   - 加工：以 90% 集中度 < 10% 视作高度集中；获利比例 < 30% 视作套牢偏重。

9. **个股资金流向（近 120 日）**
   - 调用 `get_fund_flow_daily(codes=target_code, days=120)`
   - 用途：每日主力/散户净流入金额。
   - 加工：汇总近 5 日 / 近 20 日主力净流入净额与方向（净流入为正=看多信号）。

10. **多空综合研判**
    - 调用 `bull_bear_research(codes=target_code, stock_name=<get_stock_info 返回的全称>)`
    - 用途：把技术面 + 筹码 + 情报三路综合，返回多空评分和方向判断。
    - 加工：该工具返回的方向结论作为整份分析的最终结论，叠加自有的步骤 3–9 细节作为论据。

11. **汇总输出**
    - 不再调任何工具，按以下顺序整理：
      1. 标的与板块（步骤 1–2）
      2. 行情快照（步骤 3）
      3. 趋势与形态（步骤 4–6）
      4. 估值与基本面（步骤 7）
      5. 筹码（步骤 8）
      6. 资金流（步骤 9）
      7. 多空结论与评分（步骤 10）
    - 末尾给"关注价位 / 风险点 / 后续观察指标"三条收尾。

## 注意事项

- **批量入参写法**：`agent_get_kline / analyze_chart_patterns / analyze_pattern / get_capital_summary / get_chip_distribution / get_fund_flow_daily / get_realtime_quote / get_stock_info / get_stock_sector_info / bull_bear_research` 均声明 `[支持批量]`，本技能是单股场景，`codes` 统一传字符串（如 `"300497"`），不要包成 `["300497"]`，以免触发批量返回结构与单股不一致的解析问题。
- **`resolve_stock` 的回退**：用户给的是名称时，先取返回列表第一条；若 `limit` 加大仍模糊匹配不到，需提示用户核对全称或直接提供 6 位代码。
- **`agent_get_kline` 的 `adj`**：固定传 `'qfq'`，与分析面（PE、形态）口径一致；不要传 `'hfq'` 或 `None`，避免价格台阶与估值信号冲突。
- **形态与信号的工具差异**：`analyze_chart_patterns` 是中长周期结构形态（双底/头肩顶等），`analyze_pattern` 是单日 K 线信号（锤子/吞没等），两者**不可互相替代**，必须都调用。
- **`bull_bear_research` 的依赖**：内部会自己拉技术面/筹码/情报，理论上可独立得出结论；但仍建议保留步骤 3–9 以便在自己的汇总里给出独立论据，避免结论与细节对不上。
- **`get_chip_distribution` 的 `lookback_days`**：固定 120，参数过短会高估集中度，过长则失真。
- **失败/熔断：任一步骤抛错时**，不要重试整链路；只对出错的那一步重试一次；若仍失败，在汇总里标注"该项数据缺失"，继续完成其余步骤，最后再统一提示。
- **避免使用 `python_interpreter`**：该工具近期链路胜率偏低，本技能全程不调用；如需本地数值计算，统一在汇总阶段用自然语言推理给出近似值（如"近 5 日均价≈XX"）。
- **不编造工具**：不要使用签名清单之外的工具名（如不要写 `get_pe` / `get_finance` 等）；如确需某项数据，先用 `list_tools / search_tools` 确认存在再调用。
