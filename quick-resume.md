继续量化。工作日志在 2026-05-22.md

环境：Windows + 40核, D:\QuantDinger\
目标：A股小资金快速复利

===== 当前状态 =====

连板猎手策略已完成 v1，可回测。IndicatorStrategy 模板已注册。

===== 已完成（2026-05-22）=====

1. 连板数据导出（905组，2-8板）+ 全市场数据（4730组，含单板3825）
2. 全市场验证：假阳性率 0.5-0.9%，过滤规则有效
3. 过滤规则优化搜索：涨幅≥20+封板≤2.8+上影2~8+波动≤10 最优
4. 独立策略脚本 strategy_dragon_filter.py（横向过滤+开板出场）
5. IndicatorStrategy 模板 dragon_filter（注册到 ASHARE_STRATEGY_TEMPLATES）
6. IS 与独立脚本 buy 信号 4710 组完全一致

===== 回测结果（2026-01~2026-05, db_market）=====

32笔交易, 81.2%胜率, 均值+14.90%, 盈亏比9.55
追踪止损27笔, 止盈15% 5笔（+36%~+205%）

===== 待做 =====

1. 【最高优先】扩大数据：`python export_dragon_runs.py --min-streak=1 --start=2023-01-01`
2. runner 全市场回测：`python -m optimizer.runner -t dragon_filter --all -m CNStock -tf 1D --start 2023-01-01 --end 2026-05-21 -j 4`
3. 参数优化：runner 搜索最优 min_return/max_seal/min_upper/max_volatility
4. 之前的待做：大盘过滤debug + 8个策略加出场规则

===== 关键代码位置 =====

- 独立策略: optimizer/strategy_dragon_filter.py
- IS 模板: optimizer/strategy_templates_ashare.py → dragon_filter
- 全市场验证: optimizer/validate_full_market.py
- 过滤优化: optimizer/optimize_filter.py
- 连板扫描: optimizer/export_dragon_runs.py → detect_limit_up_runs()
- 策略编译: optimizer/strategy_compiler.py → StrategyCompiler
- 指标策略生成: optimizer/indicator_strategy_builder.py → render_indicator_strategy()
- runner: optimizer/runner.py → ALL_TEMPLATES, BacktestObjective

===== 运行命令 =====

# 独立策略
python optimizer/strategy_dragon_filter.py
python optimizer/strategy_dragon_filter.py --min-return 25 --max-seal 2.0

# IndicatorStrategy (runner)
python -m optimizer.runner -t dragon_filter --all -m CNStock -tf 1D --start 2023-01-01 --end 2026-05-21 -j 4
python -m optimizer.runner -t dragon_filter -s "301179.SZ" -tf 1D --start 2026-01-01 --end 2026-05-21

# 验证
python optimizer/validate_full_market.py --csv analysis_output/dragon_ohlcv.csv
python optimizer/optimize_filter.py --csv analysis_output/dragon_ohlcv.csv
