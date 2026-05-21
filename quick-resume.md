继续量化。工作日志在 2026-05-22.md

环境：Windows + 40核, D:\QuantDinger\
目标：A股小资金快速复利

===== 当前状态 =====

连板策略研究中。已完成数据分析和初步回测，待全市场验证。

===== 已完成（2026-05-22）=====

1. 连板数据导出（905组，2-8板）
2. 形态分析：起涨窗口 + 见顶窗口
3. 买点分析：涨停前10天逐天模拟
4. 卖点分析：止盈/回撤多组对比

===== 关键发现 =====

买点：
- 涨停日买入有缺陷（封板买不到/高位接盘）
- 涨停前越早买收益越高（+71%→+42%），但回撤越大
- 阴线比阳线好（+62% vs +56%）→ 蓄势越安静越好
- 需要全市场数据验证假阳性率

卖点：
- 止盈15%最优：胜率93.4%，大亏3%
- 97%的最高点不是涨停日 → 涨停就拿着，开板就卖

===== 待做 =====

1. 【最高优先】本地运行 export_dragon_runs.py --min-streak=1
   导出全市场所有涨停日（含单板），用于验证买点信号假阳性率
2. 用全市场数据对比"涨停前信号"在连板 vs 单板的差异
3. 完善策略代码（买点+卖点+完整回测）
4. 之前的待做：大盘过滤debug + 8个策略加出场规则

===== 新增文件 =====

optimizer/export_dragon_runs.py — 连板股扫描+OHLCV导出（已修复peak后5天数据）
optimizer/analyze_dragon_patterns.py — 连板形态横向分析
optimizer/strategy_dragon_board.py — 连板策略全市场回测

===== 关键代码位置 =====

- 连板扫描: optimizer/export_dragon_runs.py → detect_limit_up_runs()
- 形态分析: optimizer/analyze_dragon_patterns.py → extract_run_features()
- 策略回测: optimizer/strategy_dragon_board.py → run_strategy()
- 策略模板: optimizer/strategy_templates_ashare.py → ASHARE_STRATEGY_TEMPLATES
- 策略编译: optimizer/strategy_compiler.py → StrategyCompiler
- 板块聚合: optimizer/sector_aggregator.py → SectorAggregator
- 大盘情绪: optimizer/market_sentiment.py → MarketBenchmark
