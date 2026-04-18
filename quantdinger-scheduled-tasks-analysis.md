# QuantDinger `backend_api_python` 定时任务/后台线程分析

> 项目地址: https://github.com/yuguonet/QuantDinger/tree/main/backend_api_python
> 分析时间: 2026-04-19

---

## 结论

项目**有多个定时调用/后台任务**，但**没有使用第三方定时任务框架**（如 APScheduler、`schedule`、Celery 等），全部基于 Python 原生的 `threading.Thread` + `threading.Event` + `time.sleep` 实现。

---

## 发现的定时任务/后台线程

### 1. TradingExecutor（策略交易执行线程）
- **文件**: `app/services/trading_executor.py`
- **模式**: 每个策略启动一个独立的 `threading.Thread`，在 `_run_strategy_loop` 中以固定间隔（tick cadence）循环拉取 K 线数据、计算信号、写入 pending_orders
- **上限**: 可配置 `STRATEGY_MAX_THREADS`（默认 64）
- **启动**: 应用启动时 `restore_running_strategies()` 恢复所有运行中的策略线程；也通过 API `batch-start`/`batch-stop` 动态管理

### 2. PendingOrderWorker（挂单调度工作线程）
- **文件**: `app/services/pending_order_worker.py`
- **模式**: 后台 daemon 线程，以 `poll_interval_sec=1.0`（默认每秒）轮询 `pending_orders` 表，分发订单到交易所执行；同时定期同步持仓状态
- **控制**: `threading.Event` 停止信号
- **启动**: `__init__.py` 中 `start_pending_order_worker()`

### 3. PortfolioMonitor（投资组合监控）
- **文件**: `app/services/portfolio_monitor.py`
- **模式**: 后台 daemon 线程，定期对用户持仓运行 AI 分析并推送预警通知
- **环境变量控制**: `ENABLE_PORTFOLIO_MONITOR=true`（默认开启）
- **启动**: `__init__.py` 中 `start_portfolio_monitor()`

### 4. Reflection Worker（复盘校验工作线程）
- **文件**: `app/services/reflection.py`
- **模式**: 后台 daemon 线程，间隔 `REFLECTION_WORKER_INTERVAL_SEC=86400`（默认 24 小时）执行一次验证周期：检查历史 AI 分析决策与实际价格走势的偏差，触发校准
- **核心**: `_stop_event.wait(timeout=interval_sec)` 实现定时等待
- **环境变量**: `ENABLE_REFLECTION_WORKER=true`（默认开启）

### 5. Polymarket Worker（Polymarket 后台任务）
- **文件**: `app/services/polymarket_worker.py`
- **模式**: 后台线程（daemon thread 模式）
- **启动**: `__init__.py` 中 `start_polymarket_worker()`

### 6. USDT Order Worker（USDT 支付订单监控）
- **文件**: `app/services/usdt_payment_service.py`
- **模式**: 后台线程定期扫描待支付/已支付 USDT 订单并检查链上状态
- **环境变量**: `USDT_PAY_ENABLED=true` 时才启动
- **启动**: `__init__.py` 中 `start_usdt_order_worker()`

### 7. AI Calibration Worker（AI 校准工作线程）
- **文件**: `app/services/ai_calibration.py`
- **模式**: 离线校准，使 AI 阈值自适应
- **启动**: `__init__.py` 中 `start_ai_calibration_worker()`

---

## 总体架构

```
Flask App 启动 (create_app)
  │
  ├─ start_pending_order_worker()      → daemon thread, 每秒轮询
  ├─ start_portfolio_monitor()         → daemon thread, 定期 AI 分析
  ├─ start_usdt_order_worker()         → daemon thread, 定期扫描
  ├─ start_polymarket_worker()         → daemon thread
  ├─ start_ai_calibration_worker()     → daemon thread
  ├─ start_reflection_worker()         → daemon thread, 24h 周期
  └─ restore_running_strategies()      → 为每个运行中的策略启动独立线程
```

## 统一的定时实现模式

```python
def _run_loop(self):
    while not self._stop_event.is_set():
        try:
            self._tick()          # 执行任务
        except Exception as e:
            logger.warning(...)
        self._stop_event.wait(timeout=self.interval_sec)  # 定时等待（可被 stop() 中断）
```

## 关键环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `STRATEGY_MAX_THREADS` | 64 | 策略线程上限 |
| `PRICE_CACHE_TTL_SEC` | 10 | 价格缓存 TTL |
| `ENABLE_PORTFOLIO_MONITOR` | true | 投资组合监控开关 |
| `ENABLE_REFLECTION_WORKER` | true | 复盘工作线程开关 |
| `REFLECTION_WORKER_INTERVAL_SEC` | 86400 | 复盘间隔（秒） |
| `ENABLE_PENDING_ORDER_WORKER` | true | 挂单工作线程开关 |
| `PENDING_ORDER_STALE_SEC` | 90 | 挂单过期时间 |
| `POSITION_SYNC_ENABLED` | true | 持仓同步开关 |
| `POSITION_SYNC_INTERVAL_SEC` | 10 | 持仓同步间隔 |
| `USDT_PAY_ENABLED` | false | USDT 支付开关 |
| `DISABLE_RESTORE_RUNNING_STRATEGIES` | false | 禁止启动时恢复策略 |
