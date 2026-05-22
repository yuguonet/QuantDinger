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
- optimizer/ 目录是策略优化器，sector_aggregator.py 做板块聚合回测
- runner.py 串联所有模块，支持 IndicatorStrategy 和旧版 JSON config

## 核心方法论

### 三层关系
1. **个股策略信号** — 被验证的对象，产生候选交易
2. **行业/概念** — 横向扩展样本量。同概念 20 只股票跑同一策略 → 5笔×20只=100笔
3. **大盘** — 去噪工具，判断个股信号是真强还是搭大盘便车

### 出场机制设计原则（重要！）
- 出场条件必须独立于入场条件，不能用入场条件取反！
- 出场比入场宽松：入场要求多条件AND，出场只需趋势明显破坏
- 让利润奔跑：追踪止损保护浮盈，不要一有回调就跑
- 好的出场 > 好的入场（出场决定盈亏比）
- 出场条件用 OR：多个出场理由独立触发

### Sharpe 为负的根因（2026-05-21 发现）
1. **做空污染**：A 股模板缺 tradeDirection="long"，回测偷偷做空 → 已修复
2. **出场逻辑错误**：8/9 策略用入场条件取反做出场 → 小赚大亏 → 部分修复

## 2026-05-22 连板策略研究（完整）

### 新增文件
- `optimizer/strategy_dragon_filter.py` — 连板猎手 v2 独立策略
- `optimizer/validate_full_market.py` — 全市场假阳性验证
- `optimizer/optimize_filter.py` — 过滤规则优化搜索
- `optimizer/strategy_templates_ashare.py` — 新增 dragon_filter IndicatorStrategy 模板

### 全市场数据（--min-streak=1）
- dragon_ohlcv.csv: 4730组（单板3825 + 连板905），94737行
- 日期范围：2026-01-06 ~ 2026-05-21（88个交易日）
- 一字板：单板61 + 连板96（不可买入，需排除）

### 过滤规则（非一字板中验证）
- 基准浓度：17.7%（807 multi / 4565 total）
- **最优组合**：涨幅≥20% + 封板≤2.8% + 上影2~8% + 波动≤10%
  - 39通过, 2FP, 精确率94.9%, 召回率4.6%
- **严格组合**：涨幅≥20% + 封板≤2.8% + 振幅≥5% + 波动≤5%
  - 32通过, 0FP, 精确率100%, 召回率4.0%

### 回测结果（db_market, 2026-01~2026-05）
- 32笔交易, 81.2%胜率, 均值+14.90%, 盈亏比9.55
- 追踪止损27笔, 止盈15% 5笔（超级大肉+36%~+205%）
- 信号集中：04-28一天23笔, 05-07 4笔

### IndicatorStrategy 转换
- 模板 key: `dragon_filter`
- Buy 信号与独立脚本 4710 组完全一致 ✅
- Sell 信号差异：IS用固定8%阈值 vs 独立脚本用 threshold*0.95（实际影响极小）
- 运行：`python -m optimizer.runner -t dragon_filter --all -m CNStock -tf 1D`

### 策略框架
```
买: 第一板涨停 + 涨幅≥20% + 封板≤2.8% + 上影2~8% + 波动≤10%（非一字板）
持: 涨停就拿着
卖: 开板（当天涨幅<8%）/ 止损10% / 追踪止损 / 止盈15%
```

## 技术记录
- 新浪 HTTP API 零依赖可拉 5 大指数日线（sh000001/sz399001/sz399006/sh000688/bj899050）
- sector_aggregator.py 有 monkey-patch _direct_fetch，必须调用 df.set_index("time")
- runner.py L572: `_trade_dir = _tmpl_defaults.get('tradeDirection', 'both')` — 没有 strategy_defaults 就默认 both
- backtest.py 信号归一化：buy/sell 在 trade_direction='long' 时映射为 open_long/close_long
- strategy_compiler.py 出场条件用 OR 连接（多个出场理由独立触发）
- get_regime() 阈值：20日累计 < -3% 才算 down，可能太严
- IndicatorStrategy 模板用 `render_indicator_strategy` 生成代码，注册到 ASHARE_STRATEGY_TEMPLATES
- runner 的 ALL_TEMPLATES = STRATEGY_TEMPLATES + ASHARE_STRATEGY_TEMPLATES + LLM + MY + GENERATED
