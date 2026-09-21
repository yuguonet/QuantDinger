<!-- auto-brewed 2026-09-21 from root_id=2540 chain=finance+analysis+stock | human-edited 后请去除 auto_ 前缀接管 -->
---
name: auto_finance-analysis-stock
version: 0.1.0
description: 对单只A股标的开展T+1/T+3/1W/1M多周期技术面、资金面、基本面综合诊断，输出短期走势参考结论。适用于用户要求"分析/诊断/解读某只股票"、"看下600519怎么样"、"贵州茅台短期走势如何"、"XX标的近期该关注什么"等场景。
tags: [finance, stock, A股, 多周期分析, 技术面, 资金面, 基本面, 综合诊断]
tools: [agent_get_kline, calculate_ma, get_realtime_quote, get_stock_info, analyze_trend, get_capital_summary, get_indicator_snapshot, get_fund_flow, get_chip_distribution]
---

# 单只A股多周期综合诊断

## 使用场景

当用户给出一个明确的A股标的（股票代码或名称），并要求对其近期走势进行分析、诊断或解读时，使用本技能。典型触发措辞包括但不限于："分析XXX"、"看看600519怎么样"、"贵州茅台短期走势"、"XXX（股票代码）最近能持吗"。

**不适用边界**：
- 不适用于期货、外汇、加密货币、港股、美股等非A股标的。
- 不适用于多只股票的横向对比（请使用估值对比/选股类技能）。
- 不适用于历史策略回测、实盘交易信号生成、或未给出明确标的的开放式投资问答。

## 执行流程

### Step 1：实体识别与代码规范化
- 从用户输入中提取股票名称或代码，统一转换为交易所标准代码格式（如 `600519` → `600519.SH`，创业板为 `.SZ`）。
- 确认分析周期（默认 T+1/T+3/1W/1M）与数据口径（默认日线级别）。

### Step 2：并行拉取基础行情与K线
并行调用以下工具，**codes 参数传字符串**（单标的场景）：
- `agent_get_kline(codes, timeframe='1D', days=30)`：获取近30个交易日OHLCV，用于计算多周期涨跌幅、阶段高低点、量比（当日成交量/5日均量）。
- `get_realtime_quote(codes)`：获取最新价、涨跌幅、换手率、PE/PB、总市值。若返回异常，以 `agent_get_kline` 或 `calculate_ma` 的最新收盘价兜底。
- `calculate_ma(codes, periods='5,10,20,60,120')`：获取MA值、斜率、趋势方向，用于均线多空排列判断。
- `get_stock_info(codes, detail=False)`：获取标的基础信息，辅助确认实体与行业归属。

### Step 3：拉取技术指标与筹码数据
- `analyze_trend(codes)`：获取MA/MACD/BOLL/RSI/KDJ/OBV/MFI/CMF/ATR等综合趋势信号。
- `get_indicator_snapshot(codes)`：获取MACD/RSI/BOLL/KDJ/KD最新数值及金叉/死叉/超买超卖状态。
- `get_chip_distribution(codes, lookback_days=120)`：获取获利比例、平均成本、90%筹码集中度。

### Step 4：拉取基本面与资金面数据
- `get_capital_summary(codes)`：获取营收/利润增速、ROE、PE/PB估值分位、机构持仓变化。
- `get_fund_flow(codes)`：获取主力/散户净流入金额及近期资金趋势；如需更细粒度，可补充 `get_fund_flow_daily(codes, days=120)`。

### Step 5：多周期数据加工
基于 Step 2 的K线数据，按交易日近似推算各周期表现：
- **T+1**：最新交易日涨跌幅。
- **T+3**：最新交易日与3个交易日前收盘价对比。
- **1W**：最新交易日与5个交易日前收盘价对比。
- **1M**：最新交易日与20/22个交易日前收盘价对比（按实际取数天数调整）。
同时计算：
- 量比 = 当日成交量 / 过去5日均量；
- 均线趋势：读取 `ma5_trend`、`ma10_trend`、`ma20_trend`、`ma60_trend` 判断多空排列；
- 估值分位：从 `get_capital_summary` 提取PE/PB历史分位数；
- 资金动向：汇总主力净流入方向与持续性。

### Step 6：交叉验证与结论合成
按以下维度整合结论，输出结构化报告：
1. **多周期技术面结论**：以表格形式呈现各周期涨跌幅与一句话判断（如"缩量回调，跌势趋缓"）。
2. **技术形态与指标**：均线状态、超买/超卖（RSI/KDJ）、MACD柱体变化、支撑/压力位（如MA120）。
3. **资金面**：主力净流入方向、是否逆势吸筹、筹码集中度。
4. **基本面支撑**：估值分位、机构目标价区间（若数据可用）。
5. **风险提示**：明确列出潜在利空与不确定性。
6. **免责声明**：必须包含"数据口径：日线级别，基于公开市场数据计算，不构成投资建议"。

## 注意事项

- **参数类型坑**：`agent_get_kline` 与 `get_realtime_quote` 等工具的 `codes` 参数在单标的场景下**必须传字符串**（如 `'600519.SH'`）。若传入 `list` 类型，可能触发 `AttributeError: 'list' object has no attribute 'split'`。
- **数据缺失兜底**：`get_realtime_quote` 可能返回 `{'error': '未获取到行情'}`，此时应立即以 `agent_get_kline` 或 `calculate_ma` 返回的最新收盘价 `latest_close` 作为当前价，并标注数据来源差异。
- **字段名保护**：部分字段可能缺失（如机构目标价、筹码集中度），读取前需做空值/键值检查，避免直接抛异常。
- **周期换算**：T+N 均按**交易日**近似计算，非自然日；若K线数据不足对应周期，需在报告中说明数据窗口限制。
- **估值分位依赖**：PE/PB 历史分位由 `get_capital_summary` 提供，若接口未返回分位字段，不得主观编造，可改为描述绝对估值水平。
- **输出纪律**：所有结论须基于返回数据，不得引入外部未经验证的消息；最终报告须附带数据口径与免责声明。
