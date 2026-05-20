# MEMORY.md - 长期记忆

## 用户
- 做 A 股量化交易，目标小资金快速复利
- 本地环境：Windows + 40核，D:\QuantDinger\
- 有 2 年 15 分钟数据库
- 对 A 股板块轮动有经验认知

## QuantDinger 项目
- GitHub: https://github.com/yuguonet/QuantDinger
- 后端 Python (Flask)，前端 Vue，PostgreSQL 数据库
- 有重构计划（REFACTOR_PLAN.md），当前在 Phase 1

## 2026-05-20 关键结论
- kdj_crossover 日线级别信号太稀疏（中位 5 笔/3年），放弃
- 单只股票趋势策略 WF 检验基本无解（样本量不够）
- **三层过滤体系**是核心方向：大盘情绪 → 板块共振 → 个股择时
- stock_basic_info 表已加 concepts 字段，enrich_concepts.py 脚本已写好
- 15 分钟数据定位：执行层辅助，非策略层
- 三步走：①概念数据 ②大盘情绪 ③三层合并到策略
