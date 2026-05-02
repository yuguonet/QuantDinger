# MEMORY.md

## 项目：A股量化交易系统

### 数据存储方案（2026-05-02 确定）
- **PG 双库架构**：market_db（市场数据）+ strategy_db（策略数据），postgres_fdw 桥接
- **15分钟K线为唯一数据源**，按年分区，日线用 VIEW 聚合
- 5年全量约 6-7GB，CSV 原始约 2.5GB
- 策略：收盘买、开盘卖的超短策略
- 用户已有通达信数据源（前复权CSV）

### 技术栈
- PostgreSQL + postgres_fdw
- 通达信下载数据（前复权）
