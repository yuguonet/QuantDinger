# MEMORY.md - 长期记忆

## 用户
- 做 A 股量化交易，目标小资金快速复利
- 本地环境：Windows + 40核，D:\QuantDinger\
- 有 2 年 15 分钟数据库
- 对 A 股板块轮动有经验认知
- 倾向于先打磨一个策略，再推广到其他

## QuantDinger 项目
- GitHub: https://github.com/yuguonet/QuantDinger
- 后端 Python (Flask)，前端 Vue，PostgreSQL 数据库
- optimizer/ 目录是策略优化器
- runner.py 串联所有模块，支持 IndicatorStrategy

## 核心方法论

### 三层关系
1. **个股策略信号** — 产生候选交易
2. **行业/概念** — 横向扩展样本量
3. **大盘** — 去噪工具，判断信号是真强还是搭便车

### 出场机制设计原则
- 出场条件必须独立于入场条件
- 出场比入场宽松
- 让利润奔跑：追踪止损保护浮盈
- 好的出场 > 好的入场

## 2026-05-22 连板猎手v3.1 (完整)

### 策略架构
```
双分支: 沪深主板(10%) / 创科板(20%)
买: ≥2板涨停 + 高开<阈值 + 封板强度≤0.5%
卖: 追踪止损 / 止盈 / 峰值信号(RSI+上影线)
```

### 最优参数 (22977连板段验证)
- **主板**: 2板+ 高开<8% → 2,565笔 89%胜率 +16.0%
- **创科**: 2-4板 高开<12% → 304笔 92%胜率 +24.2%
- **合计**: 2,869笔 89%胜率 +16.9%

### Walk-Forward验证
- 月度OOS: 46/46月全部正收益(100%)
- IS→OOS衰减: 仅1.27%
- 三年胜率: 89.2% / 88.2% / 90.5%

### 关键发现
1. **连板数是最强筛选维度**: 1板55%→2板73%→3板92%→4板100%
2. **高开幅度是第二维度**: 平开65%→超大高开47%
3. **龙头vs跟风**: 龙头+32.7% vs 跟风+4.4%,84%跑赢
4. **创科5板+是陷阱**: 20%胜率,-43%回撤
5. **board_break开板信号有害**: 胜率仅4-7%,已移除
6. **量比/RSI/KDJ区分度弱**: 不作为独立筛选条件
7. **峰值后D+1**: 79%概率跌3.3%,必须当天出

### 文件清单
- `optimizer/strategy_dragon_v3_indicator.py` — IndicatorStrategy版
- `optimizer/strategy_dragon_v3.py` — 独立回测版
- `optimizer/strategy_templates_ashare.py` — dragon_v3已注册
- `analysis_output/step*.json` — 7步结构化分析数据
- `analysis_output/strategy_v3.1_optimized.json` — 最优参数
- `analysis_output/AI_review.md` — AI复核报告

### 运行命令
```bash
# IndicatorStrategy回测 (runner)
python -m optimizer.runner -t dragon_v3 --all -m CNStock -tf 1D --start 2024-01-01 --end 2026-05-21 -j 4

# 独立策略回测
python optimizer/strategy_dragon_v3.py --csv analysis_output/dragon_ohlcv.csv

# 连板扫描+导出
python optimizer/export_dragon_runs.py --min-streak=1 --max-gap=2 --start=2024-01-01 --end=2026-12-31
```

## 2026-05-23 连板猎手V1 (最终版)

### 核心发现: V3.1买入不可执行
- V3.1在涨停日close买入, 实盘中涨停封死买不到
- 修正为D+1 open后, 胜率从90%→50%, 溢价吃掉全部利润
- **涨停日买入策略在实盘中根本不可行**

### V1策略: 第一板识别→D+1开盘买入
- 不追涨停板, 在涨停次日开盘买入
- 核心筛选: 量比>2x + 上影线<0.5% + 排除一字板 + D+1涨幅<2%

### 回测结果 (513笔)
- 全市场: 95.1%胜率 +12.28%均收益 盈亏比1.65
- 主板: 472笔 95.1% +10.91%
- 创科: 41笔 95.1% +28.06%
- 3板+: 100%胜率 +18~43%

### 筛选因子重要性排序
1. **D+1开盘涨幅** — 最核心, <2%最优(主板)
2. **D0量比** — >2x从80%→92%胜率
3. **D0上影线** — >0.5%直接排除(胜率<30%)
4. **一字板** — 排除(买不到)
5. 前5天特征 — 锦上添花, 区分度不大

### 板块差异化
- 主板: D+1涨幅<2% (85-92%胜率)
- 创科: D+1涨幅<5% + 量比>2x (92-98%胜率)

### 文件清单
- `optimizer/strategy_dragon_v1_indicator.py` — V1 IndicatorStrategy
- `optimizer/strategy_dragon_v1.py` — V1 独立回测
- `optimizer/strategy_templates_ashare.py` — dragon_v1已注册
- `analysis_backtest_next_open.py` — 买入时机修正分析
- `analysis_entry_modes.py` — 入场方案对比
- `analysis_v1_executable.py` — V1可执行分析
- `analysis_v1_buyable.py` — 可买性分析
- `analysis_v1_filter.py` — 第一板筛选因子
- `analysis_v1_d0_factors.py` — D0深度筛选
- `analysis_v1_pre5.py` — 前5天特征分析

## 技术记录
- 新浪 HTTP API 零依赖可拉 5 大指数日线
- runner.py ALL_TEMPLATES = STRATEGY_TEMPLATES + ASHARE_STRATEGY_TEMPLATES + LLM + MY + GENERATED
- IndicatorStrategy 用 render_indicator_strategy 生成代码
- 涨停阈值: 主板9.8%, 创科19.8%
- 涨停检测 change_pct >= threshold，注意浮点精度(合成数据用9.7%容差)
