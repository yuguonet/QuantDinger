# MEMORY.md

## 项目: QuantDinger
- GitHub: https://github.com/yuguonet/QuantDinger
- 量化交易系统，Python Flask 后端 + Vue 前端
- 架构：routes → services → data_sources (三层)
- KlineService (services/kline.py) 是缓存层分水岭

## 关键发现 (2026-05-03)
- data_sources 层的 provider 从独立函数重构为类实现（tencent/sina/eastmoney）
- 旧的 `app.data_sources.tencent/sina/eastmoney` 模块路径已不存在
- `cn_stock.py` 是 A 股数据链基座，依赖旧 provider，是最大的待修复瓶颈
- `cn_stock_extent.py` 继承 cn_stock，被上层 6 个文件广泛使用
