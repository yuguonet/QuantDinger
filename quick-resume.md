继续量化。工作日志在 2026-05-23.md

环境：Windows + 40核, D:\QuantDinger\
目标：A股小资金快速复利

===== 当前状态 =====

连板猎手V1策略已完成 (最终版)
筛选逻辑: D0盘后量比>2x+上影<0.5%+排除一字板 → D+1开盘买入

===== V1最终回测结果 =====

513笔 95.1%胜率 +12.28%均收益 盈亏比1.65

主板: 472笔 95.1% +10.91%
创科: 41笔 95.1% +28.06%

按连板数:
  2板: 333笔 92.5% +7.54%
  3板: 89笔 100% +18.83%
  4板: 49笔 100% +20.93%
  5板+: 36笔 100% +20~43%

===== 筛选逻辑 =====

D0盘后:
  1. 第一板涨停 (非连板中间)
  2. 量比>2x (放量涨停)
  3. 上影线<0.5% (封死无分歧)
  4. 排除一字板 (振幅<0.2%)
D1早盘:
  5. 主板: 开盘涨幅<2%
  6. 创科: 开盘涨幅<5%
  → 买入

===== 待做 =====

1. 【高优】Windows全市场回测:
   python -m optimizer.runner -t dragon_v1 --all -m CNStock -tf 1D --start 2024-01-01 --end 2026-05-22 -j 4

2. 【高优】Walk-Forward验证

3. 【中优】与V3.1对比回测

4. 【低】前5天特征作为加分项(非核心)

===== 关键代码位置 =====

- V1 IndicatorStrategy: optimizer/strategy_dragon_v1_indicator.py
- V1 独立回测: optimizer/strategy_dragon_v1.py
- V1 注册: optimizer/strategy_templates_ashare.py → dragon_v1

===== 运行命令 =====

# V1 IndicatorStrategy回测 (runner)
python -m optimizer.runner -t dragon_v1 --all -m CNStock -tf 1D --start 2024-01-01 --end 2026-05-22 -j 4

# V1 独立回测 (CSV模式, 默认参数)
python optimizer/strategy_dragon_v1.py --source csv --csv analysis_output/dragon_ohlcv.csv

# V1 独立回测 (DB模式, 全市场)
python optimizer/strategy_dragon_v1.py --source db
python optimizer/strategy_dragon_v1.py --source db --start 2024-01-01 --end 2026-05-21
python optimizer/strategy_dragon_v1.py --source db --quick

# V1 调参
python optimizer/strategy_dragon_v1.py --source csv --csv analysis_output/dragon_ohlcv.csv --min-vol-ratio 3
python optimizer/strategy_dragon_v1.py --source db --max-d1-gap 3
