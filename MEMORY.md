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

## 2026-05-21 关键结论

### 测试结果（半导体，macd_kdj_resonance）
- Sharpe: -1.08 → -0.274 → 0.033 → 0.023（逐步改善，但仍在零附近）
- 胜率: ~43% → 48.65% → 47.91%
- 平均收益: 亏损 → +4.64% → +4.86%
- 交易数: 2 → 407 → 382

### 已修复
- 9个模板加 tradeDirection="long"
- strategy_compiler.py 新增 exit_rules + _rule_to_condition()
- macd_kdj_resonance 入场: diff_gt_dea + k_gt_d（状态持续）
- macd_kdj_resonance 出场: diff_lt_dea（去掉EMA出场）
- sector_aggregator.py 新增 --market-filter 大盘过滤（待验证）

### 待做
- 大盘过滤 debug（确认过滤逻辑是否生效）
- 参数优化（trailing_activation/callback 已在搜索空间）
- 其余8个策略加独立出场规则
- 测试其他板块

## 技术记录
- 新浪 HTTP API 零依赖可拉 5 大指数日线（sh000001/sz399001/sz399006/sh000688/bj899050）
- sector_aggregator.py 有 monkey-patch _direct_fetch，必须调用 df.set_index("time")
- runner.py L572: `_trade_dir = _tmpl_defaults.get('tradeDirection', 'both')` — 没有 strategy_defaults 就默认 both
- backtest.py 信号归一化：buy/sell 在 trade_direction='long' 时映射为 open_long/close_long
- strategy_compiler.py 出场条件用 OR 连接（多个出场理由独立触发）
- get_regime() 阈值：20日累计 < -3% 才算 down，可能太严
