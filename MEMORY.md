# MEMORY.md - 长期记忆

## 用户
- 有 Windows 机器，40 核 CPU
- 在做 A 股量化交易策略优化
- 项目：QuantDinger（GitHub: yuguonet/QuantDinger）
- 本地路径：D:\QuantDinger\
- 工作方式：本地跑命令，我分析结果出下一步指令

## 项目概况
- 自动策略优化器，支持 A 股 + 加密市场
- 核心流程：模板策略 → 参数优化(Optuna) → Walk-Forward 验证
- 数据库：PostgreSQL 存储 K 线数据（db_market）
- 优化器在 optimizer/ 目录

## 策略研究进展（截至 2026-05-20）

### 已测试组合
| 股票 | 模板 | 全量Sharpe | WF得分 | 结论 |
|------|------|-----------|--------|------|
| 300750.SZ | macd_crossover | 2.47 | 未测 | 早期结果 |
| 300054.SZ | kdj_crossover | 2.97 | -1.52 | ❌ 交易太少(13笔/3.5年) |
| 000636.SZ | kdj_crossover | 3.16 | -0.15 | ❌ WF负 |
| 002203.SZ | kdj_crossover | 3.17 | -0.29 | ❌ WF负 |

### 关键教训
1. **WF 验证是金标准** — 全量 Sharpe 高不代表可用，必须 WF>0
2. **交易笔数必须够** — <30 笔统计上不可靠，训练期可能 0 交易
3. **kdj_crossover 适合活跃股** — 低频触发的股票会失败
4. **原始模板 + sharpe 评分 + 2年半数据** 是正确组合（quick-resume.md 记录）

### 当前状态
- 正在批量扫描 200 只股票（kdj_crossover 模板）
- 等待结果，筛选 WF>0 的标的
- 本地共 5200 只股票待扫描
