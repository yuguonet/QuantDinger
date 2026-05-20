# 快速恢复指令 - 下次直接发这段

```
继续量化。工作日志在 D:\QuantDinger\工作日志_20260520.md

环境：Windows + PowerShell, 40核, D:\QuantDinger\
目标：A股小资金快速复利

已完成7轮测试，重大突破：
1. 日线+原始模板+5股 → 300750.SZ+macd_crossover Sharpe 2.47 ✅
2. 日线+mine模板+5股 → 零交易
3. 15m+mine/原始模板 → 全部负收益，15m不可行
4. 日线+ashare模板+50股 → WF全负（交易太少）
5. 日线+原始模板+100股+2023-2026+300trials+sharpe → 🎯 300054.SZ+kdj_crossover WF=2.24

🏆 最优组合：300054.SZ + kdj_crossover
Sharpe=2.97, 胜率84.6%, 回撤-3.8%, 收益25.7%, 13笔交易, WF测试2.24 ✅

其他候选：000636.SZ+kdj(Sharpe 3.16, WF -0.15), 002203.SZ+kdj(Sharpe 3.17, WF -0.29)

关键发现：kdj_crossover最有效，原始模板+sharpe评分+2年半数据是正确组合

下一步：对300054.SZ+kdj_crossover做详细WF验证，扫描更多股票找更多WF>0的组合

项目路径：D:\QuantDinger\, 优化器在 optimizer/ 目录
CLI速查：python -m optimizer.runner -t kdj_crossover -s "300054.SZ" -tf 1D --start 2023-01-01 --trials 300 --score sharpe
```
