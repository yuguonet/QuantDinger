# QuantDinger Backend 备忘录

**技术栈：** Flask 2.3 + PostgreSQL + Redis + Gunicorn  
**入口：** `run.py` → `create_app()` 工厂模式

---

## 目录结构

```
backend_api_python/
├── run.py                          # 入口，Flask app factory + 代理配置 + 安全检查
├── app/
│   ├── __init__.py                 # create_app() 工厂，启动所有后台 worker
│   ├── config/                     # 配置层
│   │   ├── settings.py             # 全局配置（端口、密钥等）
│   │   ├── database.py             # 数据库配置
│   │   ├── api_keys.py             # API 密钥管理
│   │   └── data_sources.py         # 数据源配置
│   │
│   ├── routes/                     # API 路由层（~20个蓝图模块）
│   │   ├── auth.py                 # 认证（JWT）
│   │   ├── market.py               # 行情数据
│   │   ├── kline.py                # K线数据
│   │   ├── strategy.py             # 策略 CRUD
│   │   ├── backtest.py             # 回测
│   │   ├── ai_chat.py              # AI 对话
│   │   ├── agent_analysis.py       # AI Agent 分析
│   │   ├── agent_blueprint.py      # Agent 蓝图
│   │   ├── portfolio.py            # 投资组合
│   │   ├── quick_trade.py          # 快速交易
│   │   ├── ibkr.py                 # 盈透证券接口
│   │   ├── mt5.py                  # MT5 外汇接口
│   │   ├── polymarket.py           # Polymarket 预测市场
│   │   ├── billing.py              # 计费系统
│   │   ├── community.py            # 社区
│   │   ├── credentials.py          # 凭据管理
│   │   ├── dashboard.py            # 仪表盘
│   │   ├── experiment.py           # 实验
│   │   ├── fast_analysis.py        # 快速分析
│   │   ├── health.py               # 健康检查
│   │   ├── indicator.py            # 指标
│   │   ├── settings.py             # 设置
│   │   ├── shichang.py             # 市场看板
│   │   ├── stock_screener_api.py   # 选股器 API
│   │   ├── user.py                 # 用户管理
│   │   └── xuangu.py               # 选股
│   │
│   ├── services/                   # 业务逻辑层（核心）
│   │   ├── llm.py                  # LLM 调用封装
│   │   ├── strategy.py             # 策略服务
│   │   ├── strategy_compiler.py    # 策略编译器
│   │   ├── strategy_loader.py      # 策略加载器
│   │   ├── strategy_snapshot.py    # 策略快照
│   │   ├── strategy_script_runtime.py # 脚本策略运行时
│   │   ├── backtest.py             # 回测引擎
│   │   ├── trading_executor.py     # 实盘执行器
│   │   ├── pending_order_worker.py # 挂单监控 worker
│   │   ├── exchange_execution.py   # 交易所执行
│   │   ├── kline.py                # K线数据服务
│   │   ├── kline_cache_manager.py  # K线缓存管理
│   │   ├── news_service.py         # 新闻服务
│   │   ├── news_analysis.py        # 新闻分析
│   │   ├── news_provider.py        # 新闻提供者
│   │   ├── news_search.py          # 新闻搜索
│   │   ├── billing_service.py      # 计费
│   │   ├── community_service.py    # 社区
│   │   ├── user_service.py         # 用户服务
│   │   ├── oauth_service.py        # OAuth
│   │   ├── email_service.py        # 邮件
│   │   ├── security_service.py     # 安全
│   │   ├── usdt_payment_service.py # USDT 支付
│   │   ├── portfolio_monitor.py    # 投资组合监控
│   │   ├── signal_notifier.py      # 信号通知
│   │   ├── fast_analysis.py        # 快速分析
│   │   ├── ai_calibration.py       # AI 校准
│   │   ├── reflection.py           # 反思系统
│   │   ├── analysis_memory.py      # 分析记忆
│   │   ├── indicator_code_quality.py # 指标代码质量
│   │   ├── indicator_params.py     # 指标参数
│   │   ├── indicator_review.py     # 指标审查
│   │   ├── builtin_indicators.py   # 内置指标
│   │   ├── polymarket_analyzer.py  # Polymarket 分析
│   │   ├── polymarket_batch_analyzer.py # Polymarket 批量分析
│   │   ├── polymarket_worker.py    # Polymarket 后台 worker
│   │   ├── market_data_collector.py # 市场数据采集
│   │   ├── symbol_name.py          # 品种名称
│   │   └── credential_crypto.py    # 凭据加密
│   │
│   ├── agent/                      # AI Agent 系统
│   │   ├── executor.py             # Agent 执行器
│   │   ├── factory.py              # Agent 工厂
│   │   ├── runner.py               # Agent 运行器
│   │   ├── session_store.py        # 会话存储
│   │   └── utils.py                # Agent 工具
│   │
│   ├── data_sources/               # 数据源抽象层（工厂模式）
│   │   ├── base.py                 # 基类
│   │   ├── factory.py              # 数据源工厂
│   │   ├── akshare.py              # AKShare（A股主要源）
│   │   ├── sina.py                 # 新浪行情
│   │   ├── sina_a_stock.py         # 新浪 A股
│   │   ├── tencent.py              # 腾讯行情
│   │   ├── eastmoney.py            # 东方财富
│   │   ├── cn_stock.py             # A股综合
│   │   ├── hk_stock.py             # 港股
│   │   ├── us_stock.py             # 美股
│   │   ├── crypto.py               # 加密货币（CCXT）
│   │   ├── forex.py                # 外汇
│   │   ├── futures.py              # 期货
│   │   ├── polymarket.py           # Polymarket
│   │   ├── asia_stock_kline.py     # 亚洲股市 K线
│   │   ├── cn_hk_fundamentals.py   # 中港基本面
│   │   ├── cache_manager.py        # 缓存管理
│   │   ├── circuit_breaker.py      # 熔断器
│   │   ├── rate_limiter.py         # 限流器
│   │   ├── market_detector.py      # 市场检测器
│   │   └── normalizer.py           # 数据标准化
│   │
│   ├── data_providers/             # 面向业务的数据提供者
│   │   ├── commodities.py          # 商品
│   │   ├── crypto.py               # 加密货币
│   │   ├── forex.py                # 外汇
│   │   ├── global_market.py        # 全球市场
│   │   ├── heatmap.py              # 热力图
│   │   ├── indices.py              # 指数
│   │   ├── news.py                 # 新闻
│   │   ├── opportunities.py        # 机会发现
│   │   └── sentiment.py            # 情绪分析
│   │
│   ├── market_cn/                  # A股特色模块
│   │   ├── china_market.py         # 中国市场
│   │   ├── china_stock.py          # 中国股票
│   │   ├── cli.py                  # CLI 工具
│   │   ├── fear_greed_index.py     # 恐贪指数
│   │   ├── hot_sectors.py          # 热门板块
│   │   ├── macro_analysis.py       # 宏观分析
│   │   ├── policy_analysis.py      # 政策分析
│   │   └── sector_history.py       # 板块历史
│   │
│   ├── interfaces/                 # 第三方接口（东方财富等）
│   │   ├── broken_board.py         # 炸板
│   │   ├── cn_stock_extent.py      # A股扩展
│   │   ├── dragon_tiger.py         # 龙虎榜
│   │   ├── fund_flow.py            # 资金流向
│   │   ├── hot_rank.py             # 热度排名
│   │   ├── limit_down.py           # 跌停
│   │   ├── market_snapshot.py      # 市场快照
│   │   ├── stock_fund_flow.py      # 个股资金流
│   │   ├── stock_info.py           # 股票信息
│   │   ├── trading_calendar.py     # 交易日历
│   │   ├── zt_pool.py              # 涨停池
│   │   ├── emotion_scheduler.py    # 情绪调度器
│   │   └── cache_file.py           # 文件缓存
│   │
│   ├── market_store/               # 市场数据存储 + 评分
│   │   ├── market_store.py         # 市场存储
│   │   ├── market_scorer.py        # 市场评分
│   │   ├── plugin_api.py           # 插件 API
│   │   └── plugin_register.py      # 插件注册
│   │
│   └── utils/                      # 工具库
│       ├── auth.py                 # JWT 认证
│       ├── cache.py                # 缓存工具
│       ├── config_loader.py        # 配置加载
│       ├── credential_crypto.py    # 凭据加密
│       ├── db.py                   # 数据库工具（strategy_db 连接池）
│       ├── db_postgres.py          # PostgreSQL 连接池
│       ├── db_market.py            # 多市场行情库管理（独立连接池）
│       ├── http.py                 # HTTP 工具
│       ├── language.py             # 语言检测
│       ├── logger.py               # 日志
│       ├── safe_exec.py            # 安全执行
│       └── strategy_runtime_logs.py # 策略运行日志
│
├── strategies/                     # 预置 YAML 策略模板（16个）
│   ├── adaptive_volatility.yaml    # 自适应波动率
│   ├── bottom_volume.yaml          # 底部放量
│   ├── box_oscillation.yaml        # 箱体震荡
│   ├── bull_trend.yaml             # 牛市趋势
│   ├── chan_theory.yaml             # 缠论
│   ├── dragon_head.yaml            # 龙头战法
│   ├── ema_rsi_pullback.yaml       # EMA+RSI 回踩
│   ├── emotion_cycle.yaml          # 情绪周期
│   ├── kdj_vwap_reversal.yaml      # KDJ+VWAP 反转
│   ├── ma_golden_cross.yaml        # 均线金叉
│   ├── one_yang_three_yin.yaml     # 一阳穿三阴
│   ├── rsi_bollinger_support.yaml  # RSI+布林支撑
│   ├── shrink_pullback.yaml        # 缩量回踩
│   ├── volume_breakout.yaml        # 放量突破
│   ├── vwap_macd_volume.yaml       # VWAP+MACD+量
│   ├── vwap_rsi_confirm.yaml       # VWAP+RSI 确认
│   └── wave_theory.yaml            # 波浪理论
│
├── yaml_indicator/                 # YAML ↔ 指标代码转换器
│   ├── indicator_to_yaml.py
│   └── yaml_to_indicator.py
│
├── migrations/                     # SQL 迁移
│   ├── init.sql                    # 初始化
│   ├── dragon_tiger.sql            # 龙虎榜
│   └── news_cache.sql              # 新闻缓存
│
├── scripts/                        # 运维脚本
│   ├── backfill_zero_trades.py     # 补零交易
│   ├── run_calibration.py          # 运行校准
│   ├── run_reflection_task.py      # 运行反思
│   └── simulate_trading_executor.py # 模拟交易执行器
│
├── tests/                          # 测试
│   ├── conftest.py
│   ├── test_data_providers.py
│   ├── test_health.py
│   ├── test_indicator_code_quality.py
│   ├── test_news_search.py
│   └── yaml_ind/                   # YAML 指标测试套件
│
├── Dockerfile                      # Docker 构建
├── docker-entrypoint.sh            # 容器入口
├── gunicorn_config.py              # Gunicorn 配置
├── requirements.txt                # Python 依赖
├── requirements-windows.txt        # Windows 依赖
├── env.example                     # 环境变量模板
└── start.sh                        # 启动脚本
```

---

## 启动后台 Worker 清单

`create_app()` 启动时依次初始化：

1. **PendingOrderWorker** — 挂单监控（默认开启）
2. **PortfolioMonitor** — 投资组合监控（默认开启）
3. **USDTOrderWorker** — USDT 支付确认（需 USDT_PAY_ENABLED=true）
4. **PolymarketWorker** — Polymarket 分析（已断开，代码保留）
5. **EmotionScheduler** — A股情绪采集（需 EMOTION_COLLECTOR_ENABLED=true）
6. **SectorHistoryScheduler** — 板块历史采集（需 SECTOR_HISTORY_ENABLED=true）
7. **AICalibrationWorker** — AI 阈值自校准
8. **ReflectionWorker** — 策略反思验证
9. **StrategyRestore** — 恢复上次运行中的策略

---

## 关键依赖

| 库 | 用途 |
|----|------|
| Flask 2.3 | Web 框架 |
| SQLAlchemy 2.0 | ORM |
| psycopg2-binary | PostgreSQL 驱动 |
| redis | 缓存 |
| gunicorn | 生产 WSGI |
| akshare | A股数据源 |
| yfinance | 美股/全球行情 |
| ccxt | 加密货币交易所 |
| finnhub | 美股新闻/数据 |
| PyJWT | JWT 认证 |
| bcrypt | 密码哈希 |
| ib_insync | 盈透证券 API |
| pydantic | 数据校验 |
| PyYAML | 策略配置 |
| pyarrow | 高性能数据存储 |
| bip-utils | USDT HD 钱包 |

---

## 架构特点

- **分层清晰**：routes → services → data_sources，依赖单向
- **数据源工厂**：统一抽象 A股/港股/美股/加密/外汇/期货
- **熔断+限流**：circuit_breaker + rate_limiter 保护数据源
- **安全意识强**：SECRET_KEY 自动检测/生成、JWT、SSE TCP_NODELAY
- **策略系统**：YAML 声明式 + Python 脚本式，支持回测和实盘
- **AI 深度集成**：Agent 系统、LLM 服务、AI 校准、反思机制
- **多市场行情库**：db_market.py 独立连接池，按市场隔离（CNStock_db, USStock_db ...），按年分区
