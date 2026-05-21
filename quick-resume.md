继续量化。工作日志在 2026-05-21.md

环境：Windows + 40核, D:\QuantDinger\
目标：A股小资金快速复利

===== 当前状态 =====

macd_kdj_resonance 策略优化中，已加入止盈+追踪止损机制。
待测试：用户需要复制文件到本地运行。

===== 已修改文件（需复制到本地）=====

1. optimizer/sector_aggregator.py — 修复 _direct_fetch 的 set_index 问题
2. optimizer/strategy_templates_ashare.py — 策略逻辑优化 + 出场机制优化

===== 待测试命令 =====

python -m optimizer.sector_aggregator -c "半导体" -t macd_kdj_resonance

===== 优化历史 =====

1. kdj_crossover → 信号太稀疏，放弃
2. 板块聚合验证成功 → 中位交易数从5提升到36
3. 多策略模板测试 → 中位交易数只有1-3条，条件太苛刻
4. 策略逻辑优化 → cross_up/gold_cross 改成 diff_gt_dea/k_gt_d，中位交易数提升到7
5. 参数优化 → 止损10%，仓位100%，Sharpe从-1.08改善到-0.274
6. 多板块验证 → Sharpe全负，出场机制有问题
7. 出场机制优化 → 加止盈20%+追踪止损（盈利10%后激活，回撤5.5%出场）

===== 待验证 =====

- 新出场机制能否改善Sharpe
- 如果仍为负，可能需要换策略思路（均值回归类）
