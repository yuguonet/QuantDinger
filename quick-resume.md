继续量化。工作日志在 2026-05-22.md

环境：Windows + 40核, D:\QuantDinger\
目标：A股小资金快速复利

===== 当前状态 =====

连板猎手v3.1 IndicatorStrategy已完成,已注册到runner。
Walk-Forward验证通过(46/46月正收益)。

===== 已完成（2026-05-22）=====

1. 全市场连板扫描: 25,545段, 444,833行OHLCV
2. 横向分析: 买点收益链(次日/前1日/前2日)
3. 纵向分析: RSI/KDJ/MACD/布林/均线/ATR/量比
4. 量能分析: 量比对收益区分度<1%(不作独立筛选)
5. 干扰分析: 创科大跳空-13.9%(需过滤)
6. 共振分析: 龙头+32.7% vs 跟风+4.4%
7. 条件优化: 主板2板+高开<8% → 89%胜率+16.0%
8. Walk-Forward: 46/46月正收益,衰减1.27%
9. 压力测试: 8场景全部通过
10. IndicatorStrategy转换+注册

===== 回测结果（v3.1优化后）=====

主板2板+高开<8%: 2,565笔 89% +16.0% 回撤-3.2%
创科2-4板高开<12%: 304笔 92% +24.2% 回撤-4.3%
合计: 2,869笔 89% +16.9%

按连板数:
  2板: 1,535笔 80.5% +6.16%
  3板: 450笔 84.2% +13.52%
  4板: 232笔 87.5% +14.65%
  5板+: 各级81-100% +8~21%

===== 待做 =====

1. 【高优】Windows全市场回测:
   python -m optimizer.runner -t dragon_v3 --all -m CNStock -tf 1D --start 2024-01-01 --end 2026-05-21 -j 4

2. 【高优】导出概念/行业数据:
   python optimizer/export_sector_mapping.py

3. 【中优】根据回测结果调参

4. 【中优】样本外验证(2023数据)

5. 【低】合成数据压力测试(需更真实的合成)

===== 关键代码位置 =====

- IndicatorStrategy: optimizer/strategy_dragon_v3_indicator.py
- 独立回测: optimizer/strategy_dragon_v3.py
- 注册位置: optimizer/strategy_templates_ashare.py → dragon_v3
- 连板扫描: optimizer/export_dragon_runs.py
- 形态分析: optimizer/analyze_dragon_patterns.py
- 分析数据: analysis_output/step*.json

===== 运行命令 =====

# IndicatorStrategy回测
python -m optimizer.runner -t dragon_v3 --all -m CNStock -tf 1D --start 2024-01-01 --end 2026-05-21 -j 4

# 独立策略回测(CSV模式)
python optimizer/strategy_dragon_v3.py --csv analysis_output/dragon_ohlcv.csv

# 连板扫描+导出
python optimizer/export_dragon_runs.py --min-streak=1 --max-gap=2 --start=2024-01-01 --end=2026-12-31

# 形态分析
python optimizer/analyze_dragon_patterns.py --csv analysis_output/dragon_ohlcv.csv

# 概念/行业导出
python optimizer/export_sector_mapping.py
