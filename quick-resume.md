继续量化。工作日志在 2026-05-22.md

环境：Windows + 40核, D:\QuantDinger\
目标：A股小资金快速复利

===== 当前状态 =====

连板猎手 v2 CSV验证中。修复了停牌复牌假信号和导出bug。
2026年表现好(63%胜率)，2024-2025弱市失效(37-40%)。

===== 已完成（2026-05-22 下午）=====

1. CSV数据排查：发现停牌复牌导致-60%假亏损，已加open_gap过滤
2. export_dragon_runs.py修复：min-streak默认改1、元数据bug、重复append
3. optimize_mainboard.py同步修复
4. 全量回测：249笔 42.6%胜率，2024:37% 2025:40% 2026:63%
5. 新股过滤发现：信号日前数据<=20天的59笔胜率仅29%

===== 回测结果（修复后）=====

2026-01~2026-05: 43笔 62.8% +3.55% 盈亏比1.60
全量2024-01~2026-05: 249笔 42.6% -0.11% 盈亏比1.30
  沪主板: 130笔 42% +0.70%
  深主板: 110笔 44% -0.87%
  创业板: 9笔 33% -2.52%

===== 待做 =====

1. 【高优】新股过滤：信号日前数据<20天的跳过
2. 【高优】止损改进：当前-10%触发实际-14%滑点，考虑降低阈值
3. 【中优】市场过滤：大盘下跌趋势不开仓
4. 【中优】创/科参数独立优化（当前样本太少）
5. runner验证（db模式）

===== 关键代码位置 =====

- 独立策略: optimizer/strategy_dragon_filter.py (双分支, BOARD_PARAMS, 停牌复牌过滤)
- 导出脚本: optimizer/export_dragon_runs.py (已修复: min-streak=1, 元数据bug)
- 主板优化: optimizer/optimize_mainboard.py (已加停牌复牌过滤)
- IS模板: optimizer/strategy_templates_ashare.py → dragon_filter
- 测试框架: optimizer/test_dragon_filter.py

===== 运行命令 =====

# 独立策略（CSV模式）
python optimizer/strategy_dragon_filter.py --source csv --csv analysis_output/dragon_ohlcv.csv --start 2024-01-01 --end 2026-05-21

# 独立策略（DB模式，Windows本地）
python optimizer/strategy_dragon_filter.py --source db --start 2024-01-01 --end 2026-05-21

# 导出CSV（Windows本地）
python optimizer/export_dragon_runs.py --min-streak=1 --full-history --start 2024-01-01 --end 2026-05-21

# 主板参数优化
python optimizer/optimize_mainboard.py

# IndicatorStrategy (runner)
python -m optimizer.runner -t dragon_filter --all -m CNStock -tf 1D --start 2023-01-01 --end 2026-05-21 -j 4
