继续量化。工作日志在 2026-05-21.md

环境：Windows + 40核, D:\QuantDinger\
目标：A股小资金快速复利

===== 当前状态 =====

根因已定位并部分修复。macd_kdj_resonance Sharpe 从 -1.08 改善到 -0.006。
正在打磨出场规则，下一步给其余8个策略加独立出场。

===== 核心发现 =====

Sharpe为负的两个根因：
1. 做空污染 — A股模板缺 tradeDirection="long"，回测偷偷做空 → 已修复
2. 出场逻辑错误 — 8/9策略用入场条件取反做出场，小赚大亏 → 部分修复

出场设计原则：
- 出场不能用入场条件取反！
- 出场比入场宽松，不要被正常回调洗出
- 出场条件用 OR（任一满足即出场）

===== 已修复文件 =====

1. optimizer/strategy_compiler.py
   - 新增 exit_rules 支持
   - 新增 _rule_to_condition() 方法
   - 出场条件用 OR 连接

2. optimizer/strategy_templates_ashare.py
   - 9个模板加 strategy_defaults: {"tradeDirection": "long"}
   - macd_kdj_resonance 加独立出场规则
   - 出场：EMA30跌破 OR MACD柱状线翻绿
   - 新增参数 exit_ema_period（可优化，15-40）

3. optimizer/sector_aggregator.py（前一轮修复）
   - _direct_fetch 加 df.set_index("time")

===== 测试结果（半导体，macd_kdj_resonance）=====

| 阶段 | Sharpe | 胜率 | 平均收益 |
|------|--------|------|----------|
| 初始 | -1.08 | ~43% | 亏损 |
| 做空修复后 | -0.274 | ~43% | 亏损 |
| 出场规则修复后 | -0.006 | 48.72% | +4.7% |

===== 待做 =====

1. 给其余8个策略加独立出场规则（最高优先级）
2. 出场参数优化（EMA周期15-40，追踪止损参数）
3. 测试其他板块
4. 大盘过滤
5. 如果趋势策略始终不行，考虑均值回归

===== 关键代码位置 =====

- 信号生成: optimizer/strategy_compiler.py → _get_entry_logic()
- 规则转条件: optimizer/strategy_compiler.py → _rule_to_condition()
- 策略模板: optimizer/strategy_templates_ashare.py → ASHARE_STRATEGY_TEMPLATES
- 板块聚合回测: optimizer/sector_aggregator.py
- 回测引擎: backend_api_python/app/services/backtest.py
- 信号归一化: backtest.py L793（trade_direction 映射）
- runner 读取方向: runner.py L572（默认 'both'）
