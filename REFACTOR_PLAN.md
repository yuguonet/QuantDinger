# QuantDinger 后端重构计划

> 启动日期: 2026-05-04
> 策略: **从下往上（Bottom-Up）**

---

## Phase 1: 数据层统一（地基） 🔨

**目标**: 将 5 套数据抽象合并为 1 套统一接口

### Step 1.1: 盘点现有数据层依赖关系
- [ ] 绘制 data_sources / data_providers / interfaces / market_cn / market_store 的调用关系图
- [ ] 标记每个模块的核心职责和对外暴露的接口
- [ ] 识别重复代码（同一数据在多个模块中获取）

### Step 1.2: 定义统一 DataSource 协议
- [ ] 扩展 `data_sources/base.py` 的 BaseDataSource，统一 K线/报价/基本面接口
- [ ] 让 data_providers 中的模块也实现同一协议
- [ ] interfaces 中的功能逐步迁移到 data_sources 或作为 data_sources 的上层封装

### Step 1.3: 消除 interfaces 层
- [ ] `interfaces/zt_pool.py`（涨停池）→ 并入 data_sources/cn_stock.py
- [ ] `interfaces/broken_board.py`（炸板）→ 并入 data_sources/cn_stock.py
- [ ] `interfaces/dragon_tiger.py`（龙虎榜）→ 并入 data_sources/cn_stock.py
- [ ] `interfaces/fund_flow.py`（资金流）→ 并入 data_sources/cn_stock.py
- [ ] `interfaces/hot_rank.py`（热门排名）→ 并入 data_sources/cn_stock.py
- [ ] `interfaces/limit_down.py`（跌停）→ 并入 data_sources/cn_stock.py
- [ ] `interfaces/market_snapshot.py` → 并入 data_sources/cn_stock.py
- [ ] `interfaces/stock_info.py` → 并入 data_sources/cn_stock.py
- [ ] `interfaces/trading_calendar.py` → 提升为 utils/trading_calendar.py
- [ ] `interfaces/cache_file.py` → 保留，作为通用缓存工具
- [ ] `interfaces/emotion_scheduler.py` → 移入 market_cn/
- [ ] `interfaces/cn_stock_extent.py`（AShareDataHub）→ 重构为 market_cn/data_hub.py

### Step 1.4: 合并 market_cn / market_store
- [ ] market_cn/ 保留为 A股特色数据模块（板块、情绪、选股）
- [ ] market_store/ 保留为本地存储层（feather 文件读写）
- [ ] 删除 interfaces/ 目录

### 预期成果
- 数据层从 5 层 → 3 层：`data_sources`（通用）+ `market_cn`（A股特色）+ `market_store`（存储）
- 每个数据获取只走一条路径

---

## Phase 2: 服务层拆分（骨架） 🏗️

**目标**: 巨型服务文件拆分为职责单一的小模块

### Step 2.1: 拆分 backtest.py (4937行)
- [ ] `services/backtest/data_fetcher.py` — 回测数据获取
- [ ] `services/backtest/signal_generator.py` — 信号生成
- [ ] `services/backtest/portfolio_sim.py` — 模拟组合
- [ ] `services/backtest/report_builder.py` — 报告生成
- [ ] `services/backtest/engine.py` — 主引擎，组合以上模块

### Step 2.2: 拆分 trading_executor.py (3862行)
- [ ] `services/trading/order_manager.py` — 订单管理
- [ ] `services/trading/position_tracker.py` — 持仓跟踪
- [ ] `services/trading/risk_checker.py` — 风控检查
- [ ] `services/trading/executor.py` — 主执行器

### Step 2.3: 拆分 fast_analysis.py (3204行)
- [ ] 按分析类型拆分：技术面 / 基本面 / 情绪面 / 资金面

### Step 2.4: 拆分 pending_order_worker.py (2451行)
- [ ] 分离订单扫描 / 状态机 / 通知逻辑

### Step 2.5: 拆分 market_data_collector.py (2356行)
- [ ] 按数据源类型拆分采集器

### Step 2.6: 引入依赖注入
- [ ] 创建 `services/container.py` — 简易 DI 容器
- [ ] 服务通过构造函数接收依赖，不再自行创建
- [ ] 消除 `__init__.py` 中的全局单例函数

### 预期成果
- 最大文件不超过 500 行
- 每个服务模块职责单一、可独立测试

---

## Phase 3: 路由层瘦身（皮肤） ✂️

**目标**: 路由只做 HTTP 层工作

### Step 3.1: 瘦身路由
- [ ] `routes/strategy.py` (2028行) → 路由只做校验+调用+响应，业务逻辑移入 service
- [ ] `routes/user.py` (1948行) → 同上
- [ ] `routes/quick_trade.py` (1551行) → 同上
- [ ] 其他 > 500 行的路由文件逐一处理

### Step 3.2: 统一命名
- [ ] `xuangu.py` → `stock_picker.py`
- [ ] `shichang.py` → `market_dashboard.py`
- [ ] 所有中文注释保留，但文件名/函数名统一英文

### Step 3.3: 统一错误处理
- [ ] 创建 `routes/error_handler.py` — 统一异常 → JSON 响应
- [ ] 移除各路由中的重复 try/except 模式

### 预期成果
- 路由文件平均 < 200 行
- 所有路由都是薄层：校验 → 服务调用 → 响应

---

## Phase 4: 启动治理 🚀

### Step 4.1: 重构 create_app()
- [ ] Worker/Scheduler 通过配置文件驱动注册
- [ ] create_app() 只做 blueprint 注册和中间件挂载
- [ ] 后台任务统一管理（启停/健康检查）

### Step 4.2: 配置治理
- [ ] 合并 `config/settings.py` / `config/api_keys.py` / `config/data_sources.py`
- [ ] 统一环境变量读取方式

---

## 执行原则

1. **每一步都保证可运行** — 不做破坏性大重构，每改一个模块就跑通测试
2. **先写测试再改代码** — 对要改动的模块先补测试
3. **小步提交** — 每个 Step 一个 commit，方便回滚
4. **保持 API 兼容** — 路由的 URL 和响应格式尽量不变，前端不用改

---

## 当前进度

- [x] 项目下载 & 解压
- [x] 架构分析 & 重构计划制定
- [x] Phase 1 Step 1.1: 盘点数据层依赖关系 ✅ (2026-05-04)
- [ ] Phase 1 Step 1.2: 定义统一 DataSource 协议 ← **下一步**
