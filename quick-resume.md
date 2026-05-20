继续量化。工作日志在 memory/2026-05-20.md，长期记忆在 MEMORY.md

环境：Windows + 40核, D:\QuantDinger\
目标：A股小资金快速复利

当前状态：批量扫描 200 只股票（kdj_crossover 模板）已跑约 10 分钟
命令：python -m optimizer.runner --all -t kdj_crossover -m CNStock -tf 1D --all-local --random-sample 200 --seed 42 --start 2023-01-01 --end 2026-05-19 --trials 300 --score sharpe -j 20 --resume

已完成分析：
1. 300054.SZ + kdj_crossover 深度 WF 验证 → ❌ 失败（WF=-1.52，交易太少只有13笔/3.5年）
2. 关键教训：交易笔数必须>30才统计可靠，WF>0才是真可用

下一步：
1. 等 200 只扫描结果，把 optimizer_output/ 下的 _summary.json 传给我分析
2. 筛选 WF>0 且交易笔数>30 的标的
3. 如果命中率可观，考虑跑全量 5200 只（预计 4~7 小时）
4. 建立候选池，准备实盘验证

本地共 5200 只股票，当前跑了 3.8% 样本
