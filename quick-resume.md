继续量化。工作日志在 2026-05-22.md

环境：Windows + 40核, D:\QuantDinger\
目标：A股小资金快速复利

===== 当前状态 =====

连板猎手策略 v2 双分支架构完成，主板参数已优化。

===== 已完成（2026-05-22）=====

1. 板块分离：发现旧策略全来自创业板，改为双分支架构
2. 主板优化：量比是核心，`seal≤8%+波动≤3%+量比≤1` → 78.2%胜率
3. 横向分析：主板开板日跌>5%→胜率26%；创/科振幅小→收益高
4. 创/科参数不变（已验证81%胜率）

===== 最终结果 =====

主板: 174笔 78.2%胜率 +5.10%均值 盈亏比1.70
创/科: 34笔 73.5%胜率 +13.07%均值 盈亏比9.37（需runner确认81%）
合计: 208笔 77.4%胜率 +6.41%均值 盈亏比2.36

===== 待做 =====

1. 【最高优先】runner验证：`python -m optimizer.runner -t dragon_filter --all -m CNStock -tf 1D --start 2023-01-01 --end 2026-05-21 -j 4`
2. 横向分析特征（振幅/实体比）入runner多日策略
3. 大盘过滤debug + 8个策略加出场规则

===== 关键代码位置 =====

- 独立策略: optimizer/strategy_dragon_filter.py (双分支, BOARD_PARAMS)
- IS模板: optimizer/strategy_templates_ashare.py → dragon_filter
- 测试框架: optimizer/test_dragon_filter.py
- 主板优化: optimizer/optimize_mainboard.py
- 横向数据: analysis_output/dragon_pattern_features.csv

===== 运行命令 =====

# 独立策略
python optimizer/strategy_dragon_filter.py
python optimizer/strategy_dragon_filter.py --start 2024-01-01

# IndicatorStrategy (runner)
python -m optimizer.runner -t dragon_filter --all -m CNStock -tf 1D --start 2023-01-01 --end 2026-05-21 -j 4
python -m optimizer.runner -t dragon_filter -s "301179.SZ" -tf 1D --start 2026-01-01 --end 2026-05-21

# 测试
python optimizer/test_dragon_filter.py

# 主板优化
python optimizer/optimize_mainboard.py
