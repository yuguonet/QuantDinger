# QuantDinger 后端 app/ 模块扫描报告

扫描文件数: 264

### `__init__.py`
**路径:** `__init__.py`
**说明:** QuantDinger Python API - Flask application factory.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `SafeJSONProvider` |  | — | JSON provider that converts NaN / Infinity to null. [methods: default() — Handle non-serializable objects (same as super)., dumps()] |
| func | `get_trading_executor` |  | — | Get the trading executor singleton. |
| func | `get_pending_order_worker` |  | — | Get the pending order worker singleton. |
| func | `start_polymarket_worker` |  | — | [DISCONNECTED] Polymarket后台任务 — 已断开，不启动 |
| func | `start_portfolio_monitor` |  | — | Start the portfolio monitor service if enabled. |
| func | `start_pending_order_worker` |  | — | Start the pending order worker (disabled by default in paper mode). |
| func | `start_usdt_order_worker` |  | — | Start the USDT order background worker. |
| func | `start_emotion_scheduler` |  | — | 启动情绪采集调度器（仅在 EMOTION_COLLECTOR_ENABLED=true 时） |
| func | `start_sector_history_scheduler` |  | — | 启动板块历史采集调度器（仅在 SECTOR_HISTORY_ENABLED=true 时） |
| func | `restore_running_strategies` |  | — | Restore running strategies on startup. |
| func | `create_app` | config_name | — | Flask application factory. |
| func | `default` | o | staticmethod | Handle non-serializable objects (same as super). |
| func | `dumps` | obj | — | — |
| func | `__init__` | wsgi_app | — | — |


## 📁 routes

### `__init__.py`
**路径:** `routes/__init__.py`
**说明:** API Routes Module

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `register_routes` | app | — | Register all API route blueprints |

### `agent_analysis.py`
**路径:** `routes/agent_analysis.py`
**说明:** /api/agent-analysis/* — 股票分析 + 异步任务管理

**外部 HTTP 接口:**

| 方法+路径 | 函数名 | 说明 |
|-----------|--------|------|
| `ROUTE /analyze` | `trigger_analysis()` | 触发股票分析（同步/异步）。 |
| `ROUTE /tasks` | `get_task_list()` | 获取任务列表。 |
| `ROUTE /tasks/stream` | `task_stream()` | SSE 任务状态流。 |
| `ROUTE /status/<task_id>` | `get_analysis_status()` | 查询单个任务状态。 |

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `canonical_stock_code` | code | — | 统一股票代码格式（转大写、去空格）。 |
| class | `DuplicateTaskError` |  | — | — [methods: __init__()] |
| class | `TaskQueueSimulator` |  | — | 内存任务队列，用于跟踪异步分析任务。 [methods: __init__(), submit_task(), get_task(), list_all(), list_pending()] |
| func | `__init__` | stock_code, existing_task_id | — | — |
| func | `__init__` |  | — | — |
| func | `submit_task` | stock_code, stock_name, report_type, force_refresh | — | — |
| func | `get_task` | task_id | — | — |
| func | `list_all` | limit | — | — |
| func | `list_pending` |  | — | — |
| func | `stats` |  | — | — |
| func | `subscribe` | q | — | — |
| func | `unsubscribe` | q | — | — |
| func | `gen` |  | — | — |

### `agent_blueprint.py`
**路径:** `routes/agent_blueprint.py`
**说明:** /api/agent/* — AI Agent 聊天 & 流式接口

**外部 HTTP 接口:**

| 方法+路径 | 函数名 | 说明 |
|-----------|--------|------|
| `ROUTE /strategies` | `get_strategies()` | — |
| `ROUTE /chat` | `agent_chat()` | — |
| `ROUTE /chat/stream` | `agent_chat_stream()` | — |
| `ROUTE /chat/sessions` | `list_chat_sessions()` | — |
| `ROUTE /chat/sessions/<session_id>` | `get_chat_session_messages()` | — |
| `ROUTE /chat/sessions/<session_id>` | `delete_chat_session()` | — |
| `ROUTE /visualize` | `visualize_agent()` | 返回 Agent 结构树（工具列表、managed agents、配置）。 |
| `ROUTE /save` | `save_agent()` | 保存当前 Agent 配置到磁盘（可复现部署）。 |
| `ROUTE /saved` | `list_saved_agents()` | 列出已保存的 Agent。 |
| `ROUTE /replay/<session_id>` | `replay_session()` | 回放指定会话的 Agent 执行过程。 |
| `ROUTE /interrupt/<session_id>` | `interrupt_agent()` | 中断正在运行的 Agent。 |
| `ROUTE /tools` | `list_tools()` | 列出所有可用工具（含来源分类）。 |

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `register_interrupt` | session_id, agent | — | — |
| func | `unregister_interrupt` | session_id | — | — |

### `auth.py`
**路径:** `routes/auth.py`
**说明:** Authentication API Routes

**外部 HTTP 接口:**

| 方法+路径 | 函数名 | 说明 |
|-----------|--------|------|
| `ROUTE /security-config` | `get_security_config()` | Get public security configuration for frontend. |
| `ROUTE /login` | `login()` | User login endpoint. |
| `ROUTE /login-code` | `login_with_code()` | Login with email verification code (quick login / register). |
| `ROUTE /send-code` | `send_verification_code()` | Send verification code to email. |
| `ROUTE /register` | `register()` | Register new user with email verification. |
| `ROUTE /reset-password` | `reset_password()` | Reset password with email verification. |
| `ROUTE /change-password` | `change_password()` | Change password with email verification (for logged-in users). |
| `ROUTE /oauth/google` | `oauth_google()` | Redirect to Google OAuth authorization page |
| `ROUTE /oauth/google/callback` | `oauth_google_callback()` | Handle Google OAuth callback |
| `ROUTE /oauth/github` | `oauth_github()` | Redirect to GitHub OAuth authorization page |
| `ROUTE /oauth/github/callback` | `oauth_github_callback()` | Handle GitHub OAuth callback |
| `ROUTE /logout` | `logout()` | Logout (client removes token; server is stateless). |
| `ROUTE /info` | `get_user_info()` | Get current user info. |

### `backtest.py`
**路径:** `routes/backtest.py`
**说明:** Backtest API routes

**外部 HTTP 接口:**

| 方法+路径 | 函数名 | 说明 |
|-----------|--------|------|
| `ROUTE /backtest/precision-info` | `get_precision_info()` | 获取回测精度信息（用于前端提示） |
| `ROUTE /backtest` | `run_backtest()` | Run indicator backtest for the current user. |
| `ROUTE /backtest/history` | `get_backtest_history()` | Get backtest run history for the current user. |
| `ROUTE /backtest/get` | `get_backtest_run()` | Get a backtest run detail by run id for the current user. |
| `ROUTE /backtest/aiAnalyze` | `ai_analyze_backtest_runs()` | AI analyze selected backtest runs and provide strategy_config tuning suggestions |

### `billing.py`
**路径:** `routes/billing.py`
**说明:** Billing APIs - 会员购买/套餐配置（Mock支付）

**外部 HTTP 接口:**

| 方法+路径 | 函数名 | 说明 |
|-----------|--------|------|
| `ROUTE /plans` | `get_membership_plans()` | Get membership plan configuration + current user's billing snapshot. |
| `ROUTE /purchase` | `purchase_membership()` | Purchase membership (mock: immediate activation). |
| `ROUTE /usdt/create` | `usdt_create_order()` | Create USDT order for membership plan (per-order address). |
| `ROUTE /usdt/order/<int:order_id>` | `usdt_get_order()` | Get my USDT order; refresh chain status by default. |

### `community.py`
**路径:** `routes/community.py`
**说明:** Community APIs - 指标社区接口

**外部 HTTP 接口:**

| 方法+路径 | 函数名 | 说明 |
|-----------|--------|------|
| `ROUTE /indicators` | `get_market_indicators()` | 获取市场指标列表 |
| `ROUTE /indicators/<int:indicator_id>` | `get_indicator_detail()` | 获取指标详情 |
| `ROUTE /indicators/<int:indicator_id>/purchase` | `purchase_indicator()` | 购买指标 |
| `ROUTE /indicators/<int:indicator_id>/sync` | `sync_purchased_indicator()` | 同步已购买指标的最新代码 |
| `ROUTE /my-purchases` | `get_my_purchases()` | 获取我购买的指标列表 |
| `ROUTE /indicators/<int:indicator_id>/comments` | `get_comments()` | 获取指标评论列表 |
| `ROUTE /indicators/<int:indicator_id>/comments` | `add_comment()` | 添加评论 |
| `ROUTE /indicators/<int:indicator_id>/comments/<int:comment_id>` | `update_comment()` | 更新评论（只能修改自己的评论） |
| `ROUTE /indicators/<int:indicator_id>/my-comment` | `get_my_comment()` | 获取当前用户对指定指标的评论（用于编辑） |
| `ROUTE /indicators/<int:indicator_id>/performance` | `get_indicator_performance()` | 获取指标的实盘表现统计 |
| `ROUTE /admin/pending-indicators` | `get_pending_indicators()` | 获取待审核的指标列表（管理员专用） |
| `ROUTE /admin/review-stats` | `get_review_stats()` | 获取审核统计数据（管理员专用） |
| `ROUTE /admin/indicators/<int:indicator_id>/review` | `review_indicator()` | 审核指标（管理员专用） |
| `ROUTE /admin/indicators/<int:indicator_id>/unpublish` | `unpublish_indicator()` | 下架指标（管理员专用） |
| `ROUTE /admin/indicators/<int:indicator_id>` | `admin_delete_indicator()` | 删除指标（管理员专用） |

### `credentials.py`
**路径:** `routes/credentials.py`
**说明:** Exchange credentials vault.

**外部 HTTP 接口:**

| 方法+路径 | 函数名 | 说明 |
|-----------|--------|------|
| `ROUTE /list` | `list_credentials()` | List all credentials for the current user. |
| `ROUTE /egress-ip` | `get_egress_ip()` | Public egress IPv4/IPv6 of this API server (for exchange API key IP whitelist). |
| `ROUTE /create` | `create_credential()` | Create a new credential for the current user. |
| `ROUTE /delete` | `delete_credential()` | Delete a credential for the current user. |
| `ROUTE /get` | `get_credential()` | Return decrypted credential for form auto-fill. |

### `dashboard.py`
**路径:** `routes/dashboard.py`
**说明:** Dashboard APIs (local-first).

**外部 HTTP 接口:**

| 方法+路径 | 函数名 | 说明 |
|-----------|--------|------|
| `ROUTE /summary` | `summary()` | Return dashboard summary used by the frontend dashboard view (private Vue repo). |
| `ROUTE /pendingOrders` | `pending_orders()` | Return pending orders list for dashboard page. |
| `ROUTE /pendingOrders/<int:order_id>` | `delete_pending_order()` | Delete a pending order record (dashboard operation). |

### `experiment.py`
**路径:** `routes/experiment.py`
**说明:** Experiment orchestration API routes.

**外部 HTTP 接口:**

| 方法+路径 | 函数名 | 说明 |
|-----------|--------|------|
| `ROUTE /regime/detect` | `detect_market_regime()` | Detect the current market regime for a symbol/timeframe/date range. |
| `ROUTE /pipeline/run` | `run_experiment_pipeline()` | Legacy grid-search pipeline (kept for backward compat). |
| `ROUTE /ai-optimize` | `ai_optimize()` | LLM-driven multi-round optimization pipeline with SSE progress streaming. |
| `ROUTE /ai-optimize-sync` | `ai_optimize_sync()` | Non-streaming version (simpler client integration). |
| `ROUTE /structured-tune` | `structured_tune()` | Grid or random search over explicit parameterSpace (no LLM). |
| `ROUTE /save-strategy` | `save_experiment_strategy()` | Save the best experiment candidate as a strategy record. |

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `on_progress` | data | — | — |
| func | `run` |  | — | — |
| func | `generate` |  | — | — |

### `fast_analysis.py`
**路径:** `routes/fast_analysis.py`
**说明:** Fast Analysis API Routes

**外部 HTTP 接口:**

| 方法+路径 | 函数名 | 说明 |
|-----------|--------|------|
| `ROUTE /analyze` | `analyze()` | Fast AI analysis for any symbol. |
| `ROUTE /analyze-legacy` | `analyze_legacy()` | Fast analysis with legacy format output. |
| `ROUTE /history` | `get_history()` | Get analysis history for a symbol. |
| `ROUTE /history/all` | `get_all_history()` | Get all analysis history with pagination. |
| `ROUTE /history/<int:memory_id>` | `delete_history()` | Delete a history record. |
| `ROUTE /feedback` | `submit_feedback()` | Submit user feedback on an analysis. |
| `ROUTE /performance` | `get_performance()` | Get AI analysis performance statistics. |
| `ROUTE /similar-patterns` | `get_similar_patterns()` | Get similar historical patterns for current market conditions. |

### `health.py`
**路径:** `routes/health.py`
**说明:** 健康检查路由

**外部 HTTP 接口:**

| 方法+路径 | 函数名 | 说明 |
|-----------|--------|------|
| `ROUTE /` | `index()` | API 首页 |
| `ROUTE /health` | `health_check()` | 健康检查 |
| `ROUTE /api/health` | `api_health_check()` | 兼容路径：用于容器健康检查/反代探针等场景。 |

### `ibkr.py`
**路径:** `routes/ibkr.py`
**说明:** Interactive Brokers API Routes

**外部 HTTP 接口:**

| 方法+路径 | 函数名 | 说明 |
|-----------|--------|------|
| `ROUTE /status` | `get_status()` | Get connection status. |
| `ROUTE /connect` | `connect()` | Connect to TWS / IB Gateway. |
| `ROUTE /disconnect` | `disconnect()` | Disconnect from IBKR. |
| `ROUTE /account` | `get_account()` | Get account information. |
| `ROUTE /positions` | `get_positions()` | Get positions. |
| `ROUTE /orders` | `get_orders()` | Get open orders. |
| `ROUTE /order` | `place_order()` | Place an order. |
| `ROUTE /order/<int:order_id>` | `cancel_order()` | Cancel an order. |
| `ROUTE /quote` | `get_quote()` | Get real-time quote. |

### `indicator.py`
**路径:** `routes/indicator.py`
**说明:** Indicator APIs (local-first).

**外部 HTTP 接口:**

| 方法+路径 | 函数名 | 说明 |
|-----------|--------|------|
| `ROUTE /getIndicators` | `get_indicators()` | Get indicator list for the current user. |
| `ROUTE /saveIndicator` | `save_indicator()` | Create or update an indicator for the current user. |
| `ROUTE /deleteIndicator` | `delete_indicator()` | Delete an indicator by id for the current user. |
| `ROUTE /getIndicatorParams` | `get_indicator_params()` | 获取指标的参数声明 |
| `ROUTE /verifyCode` | `verify_code()` | Verify/Dry-run indicator code with mock data. |
| `ROUTE /aiGenerate` | `ai_generate()` | SSE endpoint to generate indicator code. |
| `ROUTE /codeQualityHints` | `code_quality_hints()` | Heuristic hints for indicator code (structure, @strategy risk/position). |
| `ROUTE /parseStrategyConfig` | `parse_strategy_config()` | Parse @strategy annotations from indicator code and return strategy config. |
| `ROUTE /callIndicator` | `call_indicator()` | 调用另一个指标（供前端 Pyodide 环境使用） |

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `stream` |  | — | — |

### `kline.py`
**路径:** `routes/kline.py`
**说明:** K线数据 API 路由

**外部 HTTP 接口:**

| 方法+路径 | 函数名 | 说明 |
|-----------|--------|------|
| `ROUTE /kline` | `get_kline()` | 获取K线数据 |
| `ROUTE /price` | `get_price()` | 获取最新价格 |

### `market.py`
**路径:** `routes/market.py`
**说明:** Market API routes (local-only).

**外部 HTTP 接口:**

| 方法+路径 | 函数名 | 说明 |
|-----------|--------|------|
| `ROUTE /config` | `get_public_config()` | Public config for frontend (local mode). |
| `ROUTE /types` | `get_market_types()` | Return supported market types for the add-watchlist modal. |
| `ROUTE /menuFooterConfig` | `get_menu_footer_config()` | Compatibility stub for old PHP `getMenuFooterConfig`. |
| `ROUTE /symbols/search` | `search_symbols()` | Lightweight symbol search. |
| `ROUTE /symbols/hot` | `get_hot_symbols()` | Return a small curated hot list per market (local-only). |
| `ROUTE /watchlist/get` | `get_watchlist()` | Get watchlist for the current user. |
| `ROUTE /watchlist/add` | `add_watchlist()` | Add a symbol to watchlist for the current user, with validation for CNStock. |
| `ROUTE /watchlist/remove` | `remove_watchlist()` | Remove a symbol from watchlist for the current user. |
| `ROUTE /watchlist/prices` | `get_watchlist_prices()` | 批量获取自选股价格 — 按市场分组，使用批量 API 一次拉取。 |
| `ROUTE /price` | `get_price()` | 获取单个标的价格 |
| `ROUTE /stock/name` | `get_stock_name()` | 获取股票名称 |

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `get_single_price` | market, symbol | — | 获取单个标的的价格数据 |

### `mt5.py`
**路径:** `routes/mt5.py`
**说明:** MetaTrader 5 Trading API Routes

**外部 HTTP 接口:**

| 方法+路径 | 函数名 | 说明 |
|-----------|--------|------|
| `ROUTE /status` | `get_status()` | Get MT5 connection status. |
| `ROUTE /connect` | `connect()` | Connect to MT5 terminal. |
| `ROUTE /disconnect` | `disconnect()` | Disconnect from MT5 terminal. |
| `ROUTE /account` | `get_account()` | Get account information. |
| `ROUTE /positions` | `get_positions()` | Get open positions. |
| `ROUTE /orders` | `get_orders()` | Get pending orders. |
| `ROUTE /symbols` | `get_symbols()` | Get available symbols. |
| `ROUTE /order` | `place_order()` | Place an order. |
| `ROUTE /close` | `close_position()` | Close a position. |
| `ROUTE /order/<int:ticket>` | `cancel_order()` | Cancel a pending order. |
| `ROUTE /quote` | `get_quote()` | Get real-time quote. |

### `polymarket.py`
**路径:** `routes/polymarket.py`
**说明:** Polymarket预测市场API路由

**外部 HTTP 接口:**

| 方法+路径 | 函数名 | 说明 |
|-----------|--------|------|
| `ROUTE /analyze` | `analyze_polymarket()` | 分析Polymarket预测市场（用户输入链接或标题） |
| `ROUTE /history` | `get_polymarket_history()` | Get user's Polymarket analysis history. |

### `portfolio.py`
**路径:** `routes/portfolio.py`
**说明:** Portfolio API routes (local-only).

**外部 HTTP 接口:**

| 方法+路径 | 函数名 | 说明 |
|-----------|--------|------|
| `ROUTE /positions` | `get_positions()` | Get all manual positions with current prices for the current user. |
| `ROUTE /positions` | `add_position()` | Add a new manual position for the current user. |
| `ROUTE /positions/<int:position_id>` | `update_position()` | Update an existing position for the current user. |
| `ROUTE /positions/<int:position_id>` | `delete_position()` | Delete a position for the current user. |
| `ROUTE /summary` | `get_portfolio_summary()` | Get portfolio summary with total value, PnL, and market distribution for the current user. |
| `ROUTE /monitors` | `get_monitors()` | Get all position monitors for the current user. |
| `ROUTE /monitors` | `add_monitor()` | Add a new position monitor for the current user. |
| `ROUTE /monitors/<int:monitor_id>` | `update_monitor()` | Update an existing monitor for the current user. |
| `ROUTE /monitors/<int:monitor_id>` | `delete_monitor()` | Delete a monitor for the current user. |
| `ROUTE /monitors/<int:monitor_id>/run` | `run_monitor_now()` | Manually trigger a monitor to run immediately. |
| `ROUTE /alerts` | `get_alerts()` | Get all position alerts for the current user. |
| `ROUTE /alerts` | `add_alert()` | Add a new position alert for the current user. |
| `ROUTE /alerts/<int:alert_id>` | `update_alert()` | Update an existing alert for the current user. |
| `ROUTE /alerts/<int:alert_id>` | `delete_alert()` | Delete an alert for the current user. |
| `ROUTE /groups` | `get_groups()` | Get list of all groups with position counts for the current user. |
| `ROUTE /groups/rename` | `rename_group()` | Rename a group for the current user. |

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `run_in_background` | mid, lang, uid | — | — |

### `quick_trade.py`
**路径:** `routes/quick_trade.py`
**说明:** Quick Trade API - manual / discretionary order placement.

**外部 HTTP 接口:**

| 方法+路径 | 函数名 | 说明 |
|-----------|--------|------|
| `ROUTE /place-order` | `place_order()` | Place a quick market or limit order. |
| `ROUTE /balance` | `get_balance()` | Get available balance from exchange. |
| `ROUTE /position` | `get_position()` | Get current position for a symbol from exchange. |
| `ROUTE /close-position` | `close_position()` | Close an existing position. |
| `ROUTE /history` | `get_history()` | Get quick trade history for the current user. |

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `norm` | x | — | — |

### `settings.py`
**路径:** `routes/settings.py`
**说明:** Settings API - 读取和保存 .env 配置

**外部 HTTP 接口:**

| 方法+路径 | 函数名 | 说明 |
|-----------|--------|------|
| `ROUTE /schema` | `get_settings_schema()` | 获取配置项定义 (admin only) |
| `ROUTE /public-config` | `get_public_config()` | Return non-sensitive config values needed by frontend widgets. |
| `ROUTE /values` | `get_settings_values()` | 获取当前配置值 - 包括敏感信息（真实值）(admin only) |
| `ROUTE /save` | `save_settings()` | 保存配置 (admin only) |
| `ROUTE /openrouter-balance` | `get_openrouter_balance()` | 查询 OpenRouter 账户余额 (admin only) |
| `ROUTE /test-connection` | `test_connection()` | 测试API连接 (admin only) |

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `read_env_file` |  | — | 读取 .env 文件 |
| func | `write_env_file` | env_values | — | 写入 .env 文件，保留注释和格式 |

### `shichang.py`
**路径:** `routes/shichang.py`
**说明:** 市场看板后端 API — 薄壳路由层

**外部 HTTP 接口:**

| 方法+路径 | 函数名 | 说明 |
|-----------|--------|------|
| `ROUTE /overview` | `overview()` | — |
| `ROUTE /streak` | `streak()` | — |
| `ROUTE /dragon` | `dragon()` | — |
| `ROUTE /hot` | `hot()` | — |
| `ROUTE /strong` | `strong()` | — |
| `ROUTE /` | `market_data()` | 兼容旧接口 — 聚合所有卡片数据 |
| `ROUTE /china-macro` | `china_macro()` | — |
| `ROUTE /china-fear-greed` | `china_fear_greed()` | — |
| `ROUTE /china-policy` | `china_policy()` | — |
| `ROUTE /hot-sectors` | `hot_sectors()` | — |
| `ROUTE /sector-detail/<board_code>` | `sector_detail()` | — |
| `ROUTE /sector-trend` | `sector_trend()` | — |
| `ROUTE /sector-prediction` | `sector_prediction()` | — |
| `ROUTE /sector-history` | `sector_history()` | — |
| `ROUTE /sector-cycle` | `sector_cycle()` | — |
| `ROUTE /emotion/history` | `emotion_history()` | — |
| `ROUTE /refresh` | `refresh_data()` | — |
| `ROUTE /sentiment` | `market_sentiment()` | — |
| `ROUTE /indices` | `market_indices()` | — |
| `ROUTE /heatmap` | `market_heatmap()` | — |
| `ROUTE /news` | `market_news()` | — |
| `ROUTE /refresh` | `global_refresh_data()` | — |
| `ROUTE /cards` | `list_cards()` | 返回所有可用卡片的元数据，前端可用来动态渲染 |

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `handler` |  | — | — |

### `stock_screener_api.py`
**路径:** `routes/stock_screener_api.py`
**说明:** /api/stock-screener/* — 选股器独立 API 路由

**外部 HTTP 接口:**

| 方法+路径 | 函数名 | 说明 |
|-----------|--------|------|
| `ROUTE /search` | `search_stocks()` | 智能选股搜索。支持 keyword 模式和 filters 模式。 |
| `ROUTE /presets` | `get_presets()` | 获取选股器支持的所有筛选条件分类和示例。 |
| `ROUTE /filters` | `get_filters()` | 获取筛选条件的完整结构（130+ 字段的默认值）。 |
| `ROUTE /parse` | `parse_text()` | 将自然语言选股文本解析为结构化筛选条件。 |
| `ROUTE /build` | `build_text()` | 将结构化筛选条件转换为自然语言关键词。 |
| `ROUTE /batch` | `batch_screen()` | 批量筛选：一次请求多个条件。 |

### `strategy.py`
**路径:** `routes/strategy.py`
**说明:** Trading Strategy API Routes

**外部 HTTP 接口:**

| 方法+路径 | 函数名 | 说明 |
|-----------|--------|------|
| `ROUTE /templates` | `list_strategy_templates()` | Return pre-built strategy templates for one-click import. |
| `ROUTE /templates/<key>` | `get_strategy_template()` | Return a single strategy template by key. |
| `ROUTE /strategies` | `list_strategies()` | List strategies for the current user. |
| `ROUTE /strategies/detail` | `get_strategy_detail()` | — |
| `ROUTE /strategies/backtest` | `run_strategy_backtest()` | — |
| `ROUTE /strategies/backtest/history` | `get_strategy_backtest_history()` | — |
| `ROUTE /strategies/backtest/get` | `get_strategy_backtest_run()` | — |
| `ROUTE /strategies/create` | `create_strategy()` | — |
| `ROUTE /strategies/batch-create` | `batch_create_strategies()` | Batch create strategies (multiple symbols) |
| `ROUTE /strategies/batch-start` | `batch_start_strategies()` | Batch start strategies |
| `ROUTE /strategies/batch-stop` | `batch_stop_strategies()` | Batch stop strategies |
| `ROUTE /strategies/batch-delete` | `batch_delete_strategies()` | Batch delete strategies |
| `ROUTE /strategies/update` | `update_strategy()` | — |
| `ROUTE /strategies/delete` | `delete_strategy()` | — |
| `ROUTE /strategies/trades` | `get_trades()` | Get trade records for the current user's strategy. |
| `ROUTE /strategies/positions` | `get_positions()` | Get position records for the current user's strategy. |
| `ROUTE /strategies/equityCurve` | `get_equity_curve()` | Get equity curve for the current user's strategy. |
| `ROUTE /strategies/stop` | `stop_strategy()` | Stop a strategy for the current user. |
| `ROUTE /strategies/start` | `start_strategy()` | Start a strategy for the current user. |
| `ROUTE /strategies/test-connection` | `test_connection()` | Test exchange connection. |
| `ROUTE /strategies/get-symbols` | `get_symbols()` | Get exchange trading pairs list. |
| `ROUTE /strategies/preview-compile` | `preview_compile()` | Preview compiled strategy result. |
| `ROUTE /strategies/notifications` | `get_strategy_notifications()` | Strategy signal notifications for the current user. |
| `ROUTE /strategies/notifications/unread-count` | `get_unread_notification_count()` | Get unread notification count for the current user. |
| `ROUTE /strategies/notifications/read` | `mark_notification_read()` | Mark a single notification as read for the current user. |
| `ROUTE /strategies/notifications/read-all` | `mark_all_notifications_read()` | Mark all notifications as read for the current user. |
| `ROUTE /strategies/notifications/clear` | `clear_notifications()` | Clear all notifications for the current user. |
| `ROUTE /strategies/verify-code` | `verify_strategy_code()` | Verify script strategy code syntax and safety. |
| `ROUTE /strategies/ai-generate` | `ai_generate_strategy()` | Generate strategy code or suggest template parameter updates using AI. |
| `ROUTE /strategies/performance` | `get_strategy_performance()` | Get strategy performance metrics (aggregated from equity curve and trades). |
| `ROUTE /strategies/logs` | `get_strategy_logs()` | Get strategy running logs. |

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `get_strategy_service` |  | — | — |
| func | `get_backtest_service` |  | — | — |

### `user.py`
**路径:** `routes/user.py`
**说明:** User Management API Routes

**外部 HTTP 接口:**

| 方法+路径 | 函数名 | 说明 |
|-----------|--------|------|
| `ROUTE /list` | `list_users()` | List all users (admin only). |
| `ROUTE /export` | `export_users()` | Export all users as an Excel-friendly CSV file (admin only). |
| `ROUTE /detail` | `get_user_detail()` | Get user detail by ID (admin only) |
| `ROUTE /create` | `create_user()` | Create a new user (admin only). |
| `ROUTE /update` | `update_user()` | Update user information (admin only). |
| `ROUTE /delete` | `delete_user()` | Delete a user (admin only) |
| `ROUTE /reset-password` | `reset_user_password()` | Reset a user's password (admin only). |
| `ROUTE /roles` | `get_roles()` | Get available roles and their permissions |
| `ROUTE /set-credits` | `set_user_credits()` | Set user credits (admin only). |
| `ROUTE /set-vip` | `set_user_vip()` | Set user VIP status (admin only). |
| `ROUTE /credits-log` | `get_user_credits_log()` | Get user credits log (admin only). |
| `ROUTE /profile` | `get_profile()` | Get current user's profile with billing info and notification settings |
| `ROUTE /profile/update` | `update_profile()` | Update current user's profile (limited fields). |
| `ROUTE /my-credits-log` | `get_my_credits_log()` | Get current user's credits log. |
| `ROUTE /my-referrals` | `get_my_referrals()` | Get list of users referred by current user. |
| `ROUTE /notification-settings` | `get_notification_settings()` | Get current user's notification settings. |
| `ROUTE /notification-settings` | `update_notification_settings()` | Update current user's notification settings. |
| `ROUTE /chart-templates` | `get_chart_templates()` | Get current user's indicator chart templates. |
| `ROUTE /chart-templates` | `save_chart_template()` | Create or update a user's indicator chart template. |
| `ROUTE /chart-templates` | `delete_chart_template()` | Delete a user's chart template by id. |
| `ROUTE /notification-settings/test` | `test_notification_settings()` | Send a test notification using the current user's saved notification_settings |
| `ROUTE /change-password` | `change_password()` | Change current user's password. |
| `ROUTE /system-strategies` | `get_system_strategies()` | Get all strategies across the entire system (admin only). |
| `ROUTE /admin-orders` | `get_admin_orders()` | Get all orders across the system (admin only). |
| `ROUTE /admin-ai-stats` | `get_admin_ai_stats()` | Get AI analysis usage statistics across the system (admin only). |

### `xuangu.py`
**路径:** `routes/xuangu.py`
**说明:** 选股器路由模块

**外部 HTTP 接口:**

| 方法+路径 | 函数名 | 说明 |
|-----------|--------|------|
| `ROUTE /` | `index()` | 选股器概览：最新数据日期和记录数 |
| `ROUTE /stats` | `table_stats()` | 返回 cnstock_selection 表的统计信息 |
| `ROUTE /favorites` | `get_favorites()` | 获取当前用户的收藏策略列表 |
| `ROUTE /favorites` | `save_favorite()` | 保存或更新收藏策略 |
| `ROUTE /favorites/<int:strategy_id>` | `delete_favorite()` | 删除收藏策略 |
| `ROUTE /watchlist` | `add_to_watchlist()` | 添加股票到自选股表 |
| `ROUTE /watchlist` | `get_watchlist()` | 获取当前用户的自选股列表 |
| `ROUTE /watchlist/<int:item_id>` | `remove_from_watchlist()` | 从自选股中删除 |
| `ROUTE /review` | `review_by_indicator()` | 指标策略自动审核 — SSE 流式返回逐只股票审核进度。 |
| `ROUTE /review/cancel` | `cancel_review()` | 显式取消当前用户正在进行的指标审核。 |

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `generate` |  | — | — |
| func | `heartbeat` |  | — | — |
| func | `producer` |  | — | — |


## 📁 routes/schemas

### `__init__.py`
**路径:** `routes/schemas/__init__.py`

_无公开接口/类定义_

### `analysis.py`
**路径:** `routes/schemas/analysis.py`
**说明:** ===================================

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `TaskStatusEnum` |  | — | 任务状态枚举 |
| class | `AnalyzeRequest` |  | — | 分析请求模型 |
| class | `AnalysisResultResponse` |  | — | 分析结果响应模型 |
| class | `TaskAccepted` |  | — | 异步任务接受响应 |
| class | `TaskStatus` |  | — | 任务状态模型 |
| class | `TaskInfo` |  | — | 任务详情模型 |
| class | `TaskListResponse` |  | — | 任务列表响应模型 |
| class | `DuplicateTaskErrorResponse` |  | — | 重复任务错误响应模型 |
| class | `Config` |  | — | — |
| class | `Config` |  | — | — |
| class | `Config` |  | — | — |
| class | `Config` |  | — | — |
| class | `Config` |  | — | — |
| class | `Config` |  | — | — |
| class | `Config` |  | — | — |


## 📁 config

### `__init__.py`
**路径:** `config/__init__.py`
**说明:** 配置模块

_无公开接口/类定义_

### `api_keys.py`
**路径:** `config/api_keys.py`
**说明:** API key configuration.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `MetaAPIKeys` |  | — | API Keys 元类，用于支持类属性的动态获取 [methods: FINNHUB_API_KEY(), COINGLASS_API_KEY(), CRYPTOQUANT_API_KEY(), TIINGO_API_KEY(), TWELVE_DATA_API_KEY()] |
| class | `APIKeys` |  | — | API 密钥配置类 [methods: get() — 获取 API 密钥, is_configured() — 检查 API 密钥是否已配置] |
| func | `FINNHUB_API_KEY` | cls | property | — |
| func | `COINGLASS_API_KEY` | cls | property | — |
| func | `CRYPTOQUANT_API_KEY` | cls | property | — |
| func | `TIINGO_API_KEY` | cls | property | — |
| func | `TWELVE_DATA_API_KEY` | cls | property | — |
| func | `OPENROUTER_API_KEY` | cls | property | — |
| func | `OPENAI_API_KEY` | cls | property | OpenAI direct API key |
| func | `GOOGLE_API_KEY` | cls | property | Google Gemini API key |
| func | `DEEPSEEK_API_KEY` | cls | property | DeepSeek API key |
| func | `GROK_API_KEY` | cls | property | xAI Grok API key |
| func | `TAVILY_API_KEYS` | cls | property | Tavily Search API keys (comma-separated for rotation) |
| func | `SERPAPI_KEYS` | cls | property | SerpAPI keys (comma-separated for rotation) |
| func | `BOCHA_AI_API_KEY` | cls | property | BochaAI (博查) search API key |
| func | `BAIDU_SEARCH_API_KEY` | cls | property | Baidu search (千帆 AppBuilder) API key |
| func | `SOGOU_SEARCH_API_KEY` | cls | property | Sogou search (搜狗搜索) API key |
| func | `get` | cls, key_name, default | classmethod | 获取 API 密钥 |
| func | `is_configured` | cls, key_name | classmethod | 检查 API 密钥是否已配置 |

### `data_sources.py`
**路径:** `config/data_sources.py`
**说明:** 数据源配置

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `MetaDataSourceConfig` |  | — | — [methods: DEFAULT_TIMEOUT(), RETRY_COUNT(), RETRY_BACKOFF()] |
| class | `DataSourceConfig` |  | — | 数据源通用配置 |
| class | `MetaFinnhubConfig` |  | — | — [methods: BASE_URL(), TIMEOUT(), RATE_LIMIT(), RATE_LIMIT_PERIOD()] |
| class | `FinnhubConfig` |  | — | Finnhub 数据源配置 |
| class | `MetaTiingoConfig` |  | — | — [methods: BASE_URL(), TIMEOUT()] |
| class | `TiingoConfig` |  | — | Tiingo 数据源配置 |
| class | `MetaYFinanceConfig` |  | — | — [methods: TIMEOUT(), INTERVAL_MAP()] |
| class | `YFinanceConfig` |  | — | Yahoo Finance 数据源配置 |
| class | `MetaCCXTConfig` |  | — | — [methods: DEFAULT_EXCHANGE(), TIMEOUT(), ENABLE_RATE_LIMIT(), TIMEFRAME_MAP(), PROXY()] |
| class | `CCXTConfig` |  | — | CCXT 加密货币数据源配置 |
| class | `MetaAkshareConfig` |  | — | — [methods: TIMEOUT(), PERIOD_MAP()] |
| class | `AkshareConfig` |  | — | Akshare 数据源配置 |
| func | `DEFAULT_TIMEOUT` | cls | property | — |
| func | `RETRY_COUNT` | cls | property | — |
| func | `RETRY_BACKOFF` | cls | property | — |
| func | `BASE_URL` | cls | property | — |
| func | `TIMEOUT` | cls | property | — |
| func | `RATE_LIMIT` | cls | property | — |
| func | `RATE_LIMIT_PERIOD` | cls | property | — |
| func | `BASE_URL` | cls | property | — |
| func | `TIMEOUT` | cls | property | — |
| func | `TIMEOUT` | cls | property | — |
| func | `INTERVAL_MAP` | cls | property | — |
| func | `DEFAULT_EXCHANGE` | cls | property | — |
| func | `TIMEOUT` | cls | property | — |
| func | `ENABLE_RATE_LIMIT` | cls | property | — |
| func | `TIMEFRAME_MAP` | cls | property | — |
| func | `PROXY` | cls | property | — |
| func | `TIMEOUT` | cls | property | — |
| func | `PERIOD_MAP` | cls | property | — |

### `database.py`
**路径:** `config/database.py`
**说明:** 数据库和缓存配置

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `MetaRedisConfig` |  | — | Redis 配置 [methods: HOST(), PORT(), PASSWORD(), DB(), CONNECT_TIMEOUT()] |
| class | `RedisConfig` |  | — | Redis 缓存配置 [methods: get_url() — 获取 Redis 连接 URL] |
| class | `MetaCacheConfig` |  | — | 缓存业务配置 [methods: ENABLED(), DEFAULT_EXPIRE(), KLINE_CACHE_TTL(), ANALYSIS_CACHE_TTL(), PRICE_CACHE_TTL()] |
| class | `CacheConfig` |  | — | 缓存配置 |
| func | `HOST` | cls | property | — |
| func | `PORT` | cls | property | — |
| func | `PASSWORD` | cls | property | — |
| func | `DB` | cls | property | — |
| func | `CONNECT_TIMEOUT` | cls | property | — |
| func | `SOCKET_TIMEOUT` | cls | property | — |
| func | `MAX_CONNECTIONS` | cls | property | — |
| func | `get_url` | cls | classmethod | 获取 Redis 连接 URL |
| func | `ENABLED` | cls | property | — |
| func | `DEFAULT_EXPIRE` | cls | property | — |
| func | `KLINE_CACHE_TTL` | cls | property | — |
| func | `ANALYSIS_CACHE_TTL` | cls | property | — |
| func | `PRICE_CACHE_TTL` | cls | property | — |

### `settings.py`
**路径:** `config/settings.py`
**说明:** 应用主配置

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `MetaConfig` |  | — | — [methods: HOST(), PORT(), DEBUG(), APP_NAME(), VERSION()] |
| class | `Config` |  | — | 应用配置类 [methods: get_log_path() — 获取日志文件完整路径] |
| func | `HOST` | cls | property | — |
| func | `PORT` | cls | property | — |
| func | `DEBUG` | cls | property | — |
| func | `APP_NAME` | cls | property | — |
| func | `VERSION` | cls | property | — |
| func | `SECRET_KEY` | cls | property | — |
| func | `ADMIN_USER` | cls | property | — |
| func | `ADMIN_PASSWORD` | cls | property | — |
| func | `LOG_LEVEL` | cls | property | — |
| func | `LOG_DIR` | cls | property | — |
| func | `LOG_FILE` | cls | property | — |
| func | `LOG_MAX_BYTES` | cls | property | — |
| func | `LOG_BACKUP_COUNT` | cls | property | — |
| func | `RATE_LIMIT` | cls | property | — |
| func | `ENABLE_CACHE` | cls | property | — |
| func | `ENABLE_REQUEST_LOG` | cls | property | — |
| func | `get_log_path` | cls | classmethod | 获取日志文件完整路径 |


## 📁 data

### `__init__.py`
**路径:** `data/__init__.py`

_无公开接口/类定义_

### `market_symbols_seed.py`
**路径:** `data/market_symbols_seed.py`
**说明:** Market symbols seed data and lookup functions.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `get_hot_symbols` | market, limit | — | Get hot symbols for a market. |
| func | `search_symbols` | market, keyword, limit | — | Search symbols by keyword. |
| func | `get_symbol_name` | market, symbol | — | Get display name for a symbol. |
| func | `get_all_symbols` | market | — | Get all active symbols, optionally filtered by market. |


## 📁 interfaces

### `__init__.py`
**路径:** `interfaces/__init__.py`
**说明:** A股接口层 (interfaces)

_无公开接口/类定义_

### `cache_file.py`
**路径:** `interfaces/cache_file.py`
**说明:** Feather 数据存储管理模块 - 统一管理所有历史数据表

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `cache_db` |  | — | Feather 数据管理器（对外 API 与原 SQLite 版完全兼容） [methods: __init__(), insert_batch() — 批量插入数据，支持按主键去重（等价于 INSERT OR REPLACE）, query() — 查询数据, query_between_dates() — 按日期范围查询, query_dates_exist() — 查询已存在的日期] |
| func | `__init__` | data_dir | — | — |
| func | `insert_batch` | table, data, conflict_keys | — | 批量插入数据，支持按主键去重（等价于 INSERT OR REPLACE） |
| func | `query` | table, conditions, order_by, limit | — | 查询数据 |
| func | `query_between_dates` | table, date_column, start_date, end_date, order_by | — | 按日期范围查询 |
| func | `query_dates_exist` | table, date_column, start_date, end_date | — | 查询已存在的日期 |
| func | `table_info` | table | — | 获取表信息（行数、文件大小等） |
| func | `compact` | table | — | 压缩表：清理碎片并重新写入 |
| func | `backup_all` |  | — | 手动备份所有表 |
| func | `replace_rows` | table, rows | — | 原子替换整表数据（用于裁剪/过滤后的重写） |
| func | `upsert_and_prune` | table, rows, prune_column, keep_after, conflict_keys | — | 批量写入并裁剪过期数据（EmotionScheduler 等后台任务使用） |

### `emotion_scheduler.py`
**路径:** `interfaces/emotion_scheduler.py`
**说明:** 情绪指数定时采集 + 存储 + 查询

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `EmotionScheduler` |  | — | 后台情绪采集调度器（单线程 Timer，轻量无外部依赖） [methods: __init__() — Args:, start(), stop()] |
| func | `query_emotion_history` | db, date, hours | — | 查询情绪历史（通过 cache_db 公开 API） |
| func | `__init__` | hub, db | — | Args: |
| func | `start` |  | — | — |
| func | `stop` |  | — | — |


## 📁 data_sources

### `__init__.py`
**路径:** `data_sources/__init__.py`
**说明:** 数据源模块

_无公开接口/类定义_

### `asia_stock_kline.py`
**路径:** `data_sources/asia_stock_kline.py`
**说明:** A-share / H-share chart K-lines — multi-tier fallback.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `normalize_chart_timeframe` | timeframe | — | — |
| func | `ak_a_code_from_tencent` | tencent_code | — | — |
| func | `ak_hk_code_from_tencent` | tencent_code | — | — |
| func | `fetch_twelvedata_klines` |  | — | Fetch K-lines from Twelve Data REST API. Requires TWELVE_DATA_API_KEY. |
| func | `yf_symbol_from_tencent` | tencent_code, is_hk | — | Convert Tencent-style code (SH600519 / SZ000001 / BJ830799 / HK00700) to yfinance ticker. |
| func | `fetch_yfinance_klines` |  | — | Fetch K-lines via yfinance for CN/HK stocks. Globally accessible, no API key needed. |
| func | `fetch_akshare_minute_klines` |  | — | — |
| func | `fetch_akshare_weekly_klines` |  | — | — |

### `backfill_db.py`
**路径:** `data_sources/backfill_db.py`
**说明:** backfill_db.py — A 股 K 线增量同步 + 后台同步

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `BackfillSource` |  | — | 数据源配置。 [methods: __init__()] |
| class | `BackfillDB` |  | — | 全盘批量同步工具。 [methods: __init__(), run_once() — 执行一次同步。tf 默认取 source.timeframe。] |
| func | `start_scheduler` |  | — | 启动统一同步器（幂等，重复调用安全）。 |
| func | `stop_scheduler` |  | — | 停止同步器，取消所有待执行 timer。 |
| func | `__init__` | name, market, timeframe, db_pool | — | — |
| func | `__init__` | source | — | — |
| func | `run_once` | tf, symbols, skip_repair, force_refetch | — | 执行一次同步。tf 默认取 source.timeframe。 |

### `base.py`
**路径:** `data_sources/base.py`
**说明:** 数据源基类

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `BaseDataSource` |  | — | 数据源基类 [methods: get_kline() — 获取K线数据, get_ticker() — Get latest ticker for a symbol (best-effort)., format_kline() — 格式化单条K线数据, calculate_time_range() — 计算获取指定数量K线所需的时间范围（秒）, filter_and_limit() — 过滤和限制K线数据] |
| func | `get_kline` | symbol, timeframe, limit, before_time, after_time | abstractmethod | 获取K线数据 |
| func | `get_ticker` | symbol | — | Get latest ticker for a symbol (best-effort). |
| func | `format_kline` | timestamp, open_price, high, low, close, volume | — | 格式化单条K线数据 |
| func | `calculate_time_range` | timeframe, limit, buffer_ratio | — | 计算获取指定数量K线所需的时间范围（秒） |
| func | `filter_and_limit` | klines, limit, before_time, after_time, truncate | — | 过滤和限制K线数据 |
| func | `log_result` | symbol, klines, timeframe | — | 记录获取结果日志。 |

### `circuit_breaker.py`
**路径:** `data_sources/circuit_breaker.py`
**说明:** 熔断器模块 — 数据源故障保护

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `CircuitBreaker` |  | — | 熔断器 — 连续失败超阈值则熔断，冷却后恢复。 [methods: __init__() — 初始化熔断器。, is_available() — 检查指定数据源是否可用（未处于熔断状态）。, record_success() — 记录请求成功 — 重置失败计数。, record_failure() — 记录请求失败 — 累加失败计数，超阈值则触发熔断。, reset() — 手动重置熔断器。] |
| func | `get_realtime_circuit_breaker` |  | — | 获取实时行情熔断器实例 |
| func | `get_overseas_circuit_breaker` |  | — | 获取海外行情熔断器实例 |
| func | `__init__` | failure_threshold, cooldown_seconds, name | — | 初始化熔断器。 |
| func | `is_available` | source | — | 检查指定数据源是否可用（未处于熔断状态）。 |
| func | `record_success` | source | — | 记录请求成功 — 重置失败计数。 |
| func | `record_failure` | source, reason | — | 记录请求失败 — 累加失败计数，超阈值则触发熔断。 |
| func | `reset` | source | — | 手动重置熔断器。 |

### `cn_hk_fundamentals.py`
**路径:** `data_sources/cn_hk_fundamentals.py`
**说明:** A-share / HK share fundamentals — multi-tier fallback.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `fetch_twelvedata_fundamental` | tencent_code, is_hk | — | Fetch PE/PB/PS/PEG/ROE/margin/market_cap/52w from Twelve Data /statistics. |
| func | `fetch_twelvedata_statements` | tencent_code, is_hk | — | Fetch structured financial statements from Twelve Data |
| func | `fetch_twelvedata_profile` | tencent_code, is_hk | — | Fetch company info from Twelve Data /profile. |
| func | `fetch_twelvedata_earnings` | tencent_code, is_hk | — | Fetch quarterly earnings history from Twelve Data /earnings endpoint. |
| func | `fetch_cn_fundamental_akshare` | tencent_code | — | PE/PB/PS, market cap, ROE proxy, EPS for A-share (best-effort). |
| func | `fetch_hk_fundamental_akshare` | tencent_code | — | — |
| func | `fetch_cn_company_extras` | tencent_code | — | — |
| func | `fetch_hk_company_extras` | tencent_code | — | — |
| func | `fetch_cn_financial_indicators` | tencent_code | — | Fetch revenue growth, debt/equity, current ratio, FCF, margins from |
| func | `fetch_cn_financial_statements` | tencent_code | — | Build structured financial_statements dict for A-shares (latest report). |
| func | `fetch_hk_financial_indicators` | tencent_code | — | Fetch growth & debt metrics for HK stocks from Eastmoney financial indicators. |
| func | `fetch_hk_financial_statements` | tencent_code | — | Build structured financial_statements dict for H-shares using Eastmoney financial indicators. |

### `cn_stock.py`
**路径:** `data_sources/cn_stock.py`
**说明:** 中国A股数据源

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `CNStockDataSource` |  | — | A股数据源: 1D/1W 走 DB + TTL, 15m/30m/1h/2h/4h 盘后走 DB, 其余走远端。 [methods: get_tickers() — 批量获取实时行情，写入 TTL 缓存。, get_ticker() — 获取单股实时行情。, get_kline() — 获取 K 线数据。] |
| func | `__init__` |  | — | — |
| func | `symbols` |  | — | 返回 TTL 中所有去重后的 symbol（去市场前缀的纯代码）。 |
| func | `refresh` | symbols | — | 单股场景下的快捷拉取：调 coordinator_tickers 并写入 TTL。 |
| func | `write` | quotes | — | 将行情数据写入 TTL 内存，超 500 条按最旧时间丢弃。 |
| func | `get` | symbol | — | 从 TTL 内存中查找指定 symbol（pure 和原始 key 都尝试）。 |
| func | `get_tickers` | symbols | — | 批量获取实时行情，写入 TTL 缓存。 |
| func | `get_ticker` | symbol | — | 获取单股实时行情。 |
| func | `get_kline` | symbol, timeframe, limit, before_time, after_time | — | 获取 K 线数据。 |

### `coordinator.py`
**路径:** `data_sources/coordinator.py`
**说明:** 协助层 (Coordinator) — 数据源并发调度的核心引擎

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `CircuitBreaker` |  | — | 熔断器 — 两态状态机: Closed → Open → Closed。 [methods: __init__(), is_available(), remaining_cooldown() — 返回源的剩余冷却时间（秒），未熔断返回 0, record_success(), record_failure()] |
| func | `get_realtime_circuit_breaker` |  | — | 获取实时行情熔断器实例 |
| class | `Coordinator` |  | — | 协助层 — 并发调度引擎。 [methods: __init__(), prepare() — 提前初始化数据源前置依赖（cookie、服务器探测等）。, coordinate_kline() — 单股K线获取 — 多源顺序尝试，第一个成功即返回。, coordinate_ticker() — 单股实时行情 — Race 多源并发抢答，第一个返回有效价格的直接用。, coordinate_tickers() — 批量实时行情 — 直接委托 coordinate_batch_quotes。] |
| func | `get_coordinator` |  | — | 获取全局 Coordinator 单例。 |
| func | `Coordinator_direct_call` | fn | — | Coordinator 的直接调用入口 — 不走动态队列/Race/熔断，直接执行 fn。 |
| func | `__init__` | failure_threshold, cooldown_seconds, name | — | — |
| func | `is_available` | source | — | — |
| func | `remaining_cooldown` | source | — | 返回源的剩余冷却时间（秒），未熔断返回 0 |
| func | `record_success` | source | — | — |
| func | `record_failure` | source, reason | — | — |
| func | `reset` | source | — | — |
| func | `fetch_fn` | symbol, timeframe, limit | — | — |
| func | `fetch_fn` | symbol | — | — |
| func | `__init__` |  | — | — |
| func | `prepare` | market, providers | — | 提前初始化数据源前置依赖（cookie、服务器探测等）。 |
| func | `coordinate_kline` | symbol, timeframe, limit, market, timeout, preferred_source, ... | — | 单股K线获取 — 多源顺序尝试，第一个成功即返回。 |
| func | `coordinate_ticker` | symbol, sources, timeout, preferred_source, market, max_race_sources | — | 单股实时行情 — Race 多源并发抢答，第一个返回有效价格的直接用。 |
| func | `coordinate_tickers` | symbols, market, timeout, preferred_source | — | 批量实时行情 — 直接委托 coordinate_batch_quotes。 |
| func | `coordinate_batch_quotes` | symbols, market, timeout, preferred_source | — | 批量行情获取 — 长效线程 + 主池/重试池 + 硬超时 + 逐 symbol 失败追踪。 |
| func | `coordinate_market_kline` | market, timeframe, count, timeout, preferred_source, start_date, ... | — | 全市场批量K线 — 长效线程 + 主池/重试池 + 立即退出。 |
| func | `direct_call` | fn | staticmethod | 直接调用 — 不加任何并发/重试/熔断逻辑。 |

### `crypto.py`
**路径:** `data_sources/crypto.py`
**说明:** =============================================

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `CryptoDataSource` |  | — | 加密货币数据源 [methods: __init__(), get_ticker() — Get latest ticker for a crypto symbol via CCXT., get_kline() — 获取加密货币K线数据] |
| func | `__init__` |  | — | — |
| func | `get_ticker` | symbol | — | Get latest ticker for a crypto symbol via CCXT. |
| func | `get_kline` | symbol, timeframe, limit, before_time, after_time | — | 获取加密货币K线数据 |

### `factory.py`
**路径:** `data_sources/factory.py`
**说明:** 数据源工厂

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `DataSourceFactory` |  | — | 数据源工厂。 [methods: normalize_market() — 统一市场枚举大小写与别名，供路由与数据源入口使用。, get_source() — 获取指定市场的数据源, get_data_source() — Backward compatible alias used by older code paths., get_kline() — 获取K线数据的便捷方法, get_ticker() — 获取实时报价的便捷方法] |
| func | `normalize_market` | cls, market | classmethod | 统一市场枚举大小写与别名，供路由与数据源入口使用。 |
| func | `get_source` | cls, market | classmethod | 获取指定市场的数据源 |
| func | `get_data_source` | cls, name | classmethod | Backward compatible alias used by older code paths. |
| func | `get_kline` | cls, market, symbol, timeframe, limit, before_time, ... | classmethod | 获取K线数据的便捷方法 |
| func | `get_ticker` | cls, market, symbol | classmethod | 获取实时报价的便捷方法 |

### `forex.py`
**路径:** `data_sources/forex.py`
**说明:** =============================================

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `ForexDataSource` |  | — | 外汇数据源 — Twelve Data (primary) + Tiingo (fallback) [methods: __init__(), get_ticker() — 获取外汇实时报价, get_kline() — 获取外汇K线数据] |
| func | `__init__` |  | — | — |
| func | `get_ticker` | symbol | — | 获取外汇实时报价 |
| func | `get_kline` | symbol, timeframe, limit, before_time, after_time | — | 获取外汇K线数据 |

### `futures.py`
**路径:** `data_sources/futures.py`
**说明:** =============================================

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `FuturesDataSource` |  | — | 期货数据源 [methods: __init__(), get_ticker() — Get latest ticker for futures symbol., get_kline() — 获取期货K线数据] |
| func | `__init__` |  | — | — |
| func | `get_ticker` | symbol | — | Get latest ticker for futures symbol. |
| func | `get_kline` | symbol, timeframe, limit, before_time, after_time | — | 获取期货K线数据 |

### `hk_stock.py`
**路径:** `data_sources/hk_stock.py`
**说明:** 港股/H股数据源 — 直接调用 Provider 层

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `HKStockDataSource` |  | — | 港股/H股数据源 — 直接调用 Provider，不经过 Coordinator [methods: __init__(), get_ticker() — 获取最新报价 — 逐源尝试，第一个成功的直接返回, get_kline() — 获取 K 线 — 逐源尝试，第一个成功的直接返回] |
| func | `__init__` |  | — | — |
| func | `get_ticker` | symbol | — | 获取最新报价 — 逐源尝试，第一个成功的直接返回 |
| func | `get_kline` | symbol, timeframe, limit, before_time, after_time | — | 获取 K 线 — 逐源尝试，第一个成功的直接返回 |

### `kline_clean.py`
**路径:** `data_sources/kline_clean.py`
**说明:** kline_clean.py — K 线数据连贯性补齐（纯数据处理)

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `clean_klines` | bars, timeframe | — | 补齐 K 线中间缺失部分（前向填充） |

### `market_detector.py`
**路径:** `data_sources/market_detector.py`
**说明:** 市场类型自动推断

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `detect_market` | symbol | — | 根据符号格式推断市场类型 |
| func | `validate_market` | declared_market, symbol | — | 验证声明的市场类型是否与符号匹配 |
| func | `safe_market` | declared_market, symbol | — | 返回安全的市场类型： |

### `normalizer.py`
**路径:** `data_sources/normalizer.py`
**说明:** A股数据标准化层 — 工具函数 + safe 类型转换

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `normalize_cn_code` | symbol | — | Normalize A-share symbol to Tencent code: sh600519 / sz000001 / bj830799. |
| func | `normalize_hk_code` | symbol | — | Normalize HK stock symbol to Tencent code: hk00700 (5 digits). |
| func | `to_raw_digits` | symbol | — | 从各种格式的股票代码中提取纯 6 位数字。 |
| func | `detect_market` | symbol | — | 识别股票代码所属市场。 |
| func | `add_market_prefix` | symbol, market | — | 给股票代码添加市场前缀，防止重复添加。 |
| func | `strip_market_prefix` | symbol | — | 去掉股票代码的市场前缀，返回纯数字代码。防止错误除去。 |

### `polymarket.py`
**路径:** `data_sources/polymarket.py`
**说明:** Polymarket预测市场数据源

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `PolymarketDataSource` |  | — | Polymarket预测市场数据源 [methods: __init__(), get_trending_markets() — 获取热门预测市场, get_market_details() — 获取单个市场详情, get_market_history() — 获取市场历史价格数据, search_markets() — 搜索相关预测市场] |
| func | `__init__` |  | — | — |
| func | `get_trending_markets` | category, limit | — | 获取热门预测市场 |
| func | `get_market_details` | market_id | — | 获取单个市场详情 |
| func | `get_market_history` | market_id, days | — | 获取市场历史价格数据 |
| func | `search_markets` | keyword, limit, use_cache | — | 搜索相关预测市场 |

### `rate_limiter.py`
**路径:** `data_sources/rate_limiter.py`
**说明:** ===================================

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `get_random_user_agent` |  | — | 获取随机 User-Agent |
| func | `get_request_headers` | referer | — | 获取带有随机 User-Agent 的请求头 |
| func | `random_sleep` | min_seconds, max_seconds, log | — | 随机休眠（Jitter） |
| class | `RateLimiter` |  | — | 请求频率限制器 [methods: __init__() — 初始化频率限制器, wait() — 等待直到可以发起下一次请求, reset() — 重置限制器] |
| func | `retry_with_backoff` | max_attempts, base_delay, max_delay, exponential_base, exceptions, on_retry | — | 指数退避重试装饰器 |
| func | `get_eastmoney_limiter` |  | — | 获取东方财富限流器 |
| func | `get_tencent_limiter` |  | — | 获取腾讯财经限流器 |
| func | `get_akshare_limiter` |  | — | 获取 Akshare 限流器 |
| func | `get_shared_session` |  | — | 获取共享的 requests.Session（禁用 SSL 验证）。 |
| func | `throttled_get` | url, headers, params, timeout, limiter | — | 带限流的 HTTP GET 请求 — 使用共享 Session（连接复用 + 禁用 SSL 验证）。 |
| func | `__init__` | min_interval, jitter_min, jitter_max | — | 初始化频率限制器 |
| func | `wait` |  | — | 等待直到可以发起下一次请求 |
| func | `reset` |  | — | 重置限制器 |
| func | `decorator` | func | — | — |
| func | `init_poolmanager` |  | — | — |
| func | `wrapper` |  | wraps | — |

### `source_config.py`
**路径:** `data_sources/source_config.py`
**说明:** 数据源配置模块 — 并发控制 + 市场分配 + 吞吐跟踪

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `SourceConfig` |  | dataclass | 单个数据源的并发/市场/吞吐配置。 [methods: record() — 记录一次请求的结果（由 Coordinator 调用）。, throughput() — 最近窗口的实际 QPS（请求/秒）。, success_rate() — 最近窗口的成功率 (0.0 ~ 1.0), avg_latency() — 最近窗口的平均延迟（秒），仅统计成功的请求, effective_weight() — 有效权重 — 用于 Coordinator 动态分配任务。] |
| func | `get_source_config` | name | — | 按名称获取源配置。 |
| func | `get_sources_for_market` | market | — | 获取支持指定市场的所有启用源，按 effective_weight 降序排列。 |
| func | `get_all_enabled_sources` |  | — | 获取所有启用的源配置 |
| func | `record` | success, elapsed | — | 记录一次请求的结果（由 Coordinator 调用）。 |
| func | `throughput` |  | property | 最近窗口的实际 QPS（请求/秒）。 |
| func | `success_rate` |  | property | 最近窗口的成功率 (0.0 ~ 1.0) |
| func | `avg_latency` |  | property | 最近窗口的平均延迟（秒），仅统计成功的请求 |
| func | `effective_weight` |  | — | 有效权重 — 用于 Coordinator 动态分配任务。 |
| func | `stats_summary` |  | — | 返回简短的统计摘要，用于日志和监控。 |

### `us_stock.py`
**路径:** `data_sources/us_stock.py`
**说明:** =============================================

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `USStockDataSource` |  | — | 美股数据源 [methods: __init__(), get_ticker() — 获取美股实时报价, get_kline() — 获取美股K线数据] |
| func | `__init__` |  | — | — |
| func | `get_ticker` | symbol | — | 获取美股实时报价 |
| func | `get_kline` | symbol, timeframe, limit, before_time, after_time | — | 获取美股K线数据 |


## 📁 data_sources/provider

### `10jqka.py`
**路径:** `data_sources/provider/10jqka.py`
**说明:** 同花顺(10jqka)数据源 Provider

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `ThsDataSource` |  | register | 同花顺(10jqka)数据源 — HTTP接口，无需额外依赖。 [methods: fetch_kline(), fetch_ticker(), fetch_batch_quotes()] |
| func | `fetch_kline` | code, timeframe, count, timeout, start_date, end_date | — | — |
| func | `fetch_ticker` | code, timeout | — | — |
| func | `fetch_batch_quotes` | codes, timeout | — | — |

### `__init__.py`
**路径:** `data_sources/provider/__init__.py`
**说明:** A股数据源 Provider 框架 — 自注册 + 能力声明 + 统一接口

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `NotSupportedResult` |  | — | 标准化的"不支持"响应包装。 [methods: __init__()] |
| func | `is_not_supported` | result | — | 判断结果是否为"不支持"响应。 |
| func | `calc_kline_count` | timeframe, start_date, end_date | — | 根据交易日历推算需要拉取的 K 线条数。 |
| func | `filter_bars_by_date` | bars, start_date, end_date | — | 按日期范围过滤 K 线数据。 |
| class | `BaseDataSource` |  | runtime_checkable | A股数据源统一接口（Protocol 类型协议）。 [methods: prepare() — 下载前准备 — 由 Coordinator 在派发任务前统一调用。, fetch_kline() — 获取单只股票K线数据 — 日/周/分钟共用同一接口。, fetch_ticker() — 获取单只股票实时行情。, fetch_batch_quotes() — 批量获取实时行情（单次HTTP请求）。] |
| func | `register` | cls | — | Provider 注册装饰器 — 支持两种用法。 |
| func | `get_providers` | capability, timeframe, market | — | 获取可用 Provider 列表 — 按 priority 排序 + 多维过滤。 |
| func | `get_provider` | name | — | 按名称获取单个 Provider。 |
| func | `autodiscover` |  | — | 扫描 app.data_sources.provider 包下所有模块，触发 @register。 |
| func | `__init__` | source, interface, reason | — | — |
| func | `prepare` |  | — | 下载前准备 — 由 Coordinator 在派发任务前统一调用。 |
| func | `fetch_kline` | code, timeframe, count, timeout, start_date, end_date | — | 获取单只股票K线数据 — 日/周/分钟共用同一接口。 |
| func | `fetch_ticker` | code, timeout | — | 获取单只股票实时行情。 |
| func | `fetch_batch_quotes` | codes, timeout | — | 批量获取实时行情（单次HTTP请求）。 |

### `adjustment.py`
**路径:** `data_sources/provider/adjustment.py`
**说明:** 除权除息因子模块 — 独立模块，不依赖项目其他文件

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `fetch_qfq_factors` | code | — | 获取前复权因子。 |
| func | `reverse_fwd_adjust` | klines, code | — | 将前复权K线还原为不复权。公式: unadj_price = fwd_price * qfq_factor |
| func | `unadj_to_qfq` | klines, code | — | 不复权 → 前复权。公式: fwd_price = unadj_price / qfq_factor |
| func | `unadj_to_hfq` | klines, code | — | 不复权 → 后复权。公式: hfq_price = unadj_price * hfq_factor |
| func | `update_all_factors` | max_workers | — | 拉取所有活跃股票的因子。已有缓存的跳过，只拉缺失的，16 并发。 |

### `baidu.py`
**路径:** `data_sources/provider/baidu.py`
**说明:** 百度股市通数据源 Provider

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `BaiduDataSource` |  | register | 百度股市通数据源 — A股数据源（priority=50）。 [methods: fetch_kline() — 获取单只股票K线。支持 1D/1W/1M。, fetch_ticker() — 获取单只股票实时行情, fetch_batch_quotes()] |
| func | `fetch_kline` | code, timeframe, count, timeout, start_date, end_date | — | 获取单只股票K线。支持 1D/1W/1M。 |
| func | `fetch_ticker` | code, timeout | — | 获取单只股票实时行情 |
| func | `fetch_batch_quotes` | codes, timeout | — | — |

### `eastmoney.py`
**路径:** `data_sources/provider/eastmoney.py`
**说明:** 东方财富数据源 Provider

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `EastMoneyDataSource` |  | register | 东方财富数据源 — 国内最稳定的免费数据源之一（priority=70）。 [methods: fetch_kline(), fetch_ticker(), fetch_batch_quotes()] |
| func | `__init__` | referers | — | — |
| func | `next` |  | — | — |
| func | `fetch_kline` | code, timeframe, count, timeout, start_date, end_date | — | — |
| func | `fetch_ticker` | code, timeout | — | — |
| func | `fetch_batch_quotes` | codes, timeout | — | — |

### `sina.py`
**路径:** `data_sources/provider/sina.py`
**说明:** 新浪财经数据源 Provider

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `SinaDataSource` |  | register | 新浪财经数据源 — A股第二选择（priority=20）。 [methods: fetch_kline(), fetch_ticker(), fetch_batch_quotes()] |
| func | `__init__` | referers | — | — |
| func | `next` |  | — | — |
| func | `fetch_kline` | code, timeframe, count, timeout, start_date, end_date | — | — |
| func | `fetch_ticker` | code, timeout | — | — |
| func | `fetch_batch_quotes` | codes, timeout | — | — |

### `sohu.py`
**路径:** `data_sources/provider/sohu.py`
**说明:** 搜狐财经数据源 Provider

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `SohuDataSource` |  | register | 搜狐财经数据源 — A股数据源（priority=45）。 [methods: __init__(), fetch_kline() — 获取单只股票K线。支持日/周/月线 + 5m/15m/30m/60m历史分钟线 + 当日1m分时。, fetch_ticker() — 获取单只股票实时行情快照。, fetch_batch_quotes() — 批量获取实时行情快照。] |
| func | `__init__` |  | — | — |
| func | `fetch_kline` | code, timeframe, count, timeout, start_date, end_date | — | 获取单只股票K线。支持日/周/月线 + 5m/15m/30m/60m历史分钟线 + 当日1m分时。 |
| func | `fetch_ticker` | code, timeout | — | 获取单只股票实时行情快照。 |
| func | `fetch_batch_quotes` | codes, timeout | — | 批量获取实时行情快照。 |

### `tdx_ex.py`
**路径:** `data_sources/provider/tdx_ex.py`
**说明:** 通达信数据源 Provider (pytdx 二进制协议)

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `TdxExDataSource` |  | register | 通达信数据源 — pytdx 二进制协议（priority=22）。 [methods: __init__() — 启动时探测服务器, prepare() — 下载前准备: 确保有可用服务器, fetch_kline() — 获取单只股票K线，支持 1m/5m/15m/30m/1H/1D/1W, fetch_ticker() — 获取单只股票实时行情, fetch_batch_quotes() — 批量实时行情] |
| func | `__init__` |  | — | 启动时探测服务器 |
| func | `prepare` |  | — | 下载前准备: 确保有可用服务器 |
| func | `fetch_kline` | code, timeframe, count, timeout, start_date, end_date | — | 获取单只股票K线，支持 1m/5m/15m/30m/1H/1D/1W |
| func | `fetch_ticker` | code, timeout | — | 获取单只股票实时行情 |
| func | `fetch_batch_quotes` | codes, timeout | — | 批量实时行情 |

### `tencent.py`
**路径:** `data_sources/provider/tencent.py`
**说明:** 腾讯财经数据源 Provider

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `TencentDataSource` |  | register | 腾讯财经数据源 — A股首选数据源（priority=10）。 [methods: fetch_kline(), fetch_ticker(), fetch_batch_quotes()] |
| func | `__init__` | referers | — | — |
| func | `next` |  | — | — |
| func | `fetch_kline` | code, timeframe, count, timeout, start_date, end_date | — | — |
| func | `fetch_ticker` | code, timeout | — | — |
| func | `fetch_batch_quotes` | codes, timeout | — | — |

### `twelve_data.py`
**路径:** `data_sources/provider/twelve_data.py`
**说明:** Twelve Data 数据源 Provider — 海外付费兜底源

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `TwelveDataSource` |  | register | Twelve Data 数据源 — 海外付费兜底源（priority=100）。 [methods: fetch_kline(), fetch_ticker(), fetch_batch_quotes()] |
| func | `fetch_kline` | code, timeframe, count, timeout, start_date, end_date | — | — |
| func | `fetch_ticker` | code, timeout | — | — |
| func | `fetch_batch_quotes` | codes, timeout | — | — |

### `xueqiu.py`
**路径:** `data_sources/provider/xueqiu.py`
**说明:** 雪球数据源 Provider

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `XueqiuDataSource` |  | register | 雪球数据源 — A股数据源（priority=40）。 [methods: __init__() — 初始化: 预热 cookie, prepare() — 下载前准备: 刷新 cookie，失败则不可用, fetch_kline() — 获取单只股票K线，支持 1m/5m/15m/30m/1H/1D/1W, fetch_ticker() — 获取单只股票实时行情, fetch_batch_quotes()] |
| func | `__init__` |  | — | 初始化: 预热 cookie |
| func | `prepare` |  | — | 下载前准备: 刷新 cookie，失败则不可用 |
| func | `fetch_kline` | code, timeframe, count, timeout, start_date, end_date | — | 获取单只股票K线，支持 1m/5m/15m/30m/1H/1D/1W |
| func | `fetch_ticker` | code, timeout | — | 获取单只股票实时行情 |
| func | `fetch_batch_quotes` | codes, timeout | — | — |


## 📁 agent

### `agent.py`
**路径:** `agent/agent.py`
**说明:** Agent — smolagents Agent for QuantDinger.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `get_smolagent` | skills, user_id, model, provider, max_steps, user_message, ... | — | Build a fresh agent instance per call. |
| class | `AgentResult` |  | — | — [methods: __init__()] |
| func | `build_agent_executor` | skills, user_id, max_steps, timeout_seconds, model, provider | — | — |
| func | `__init__` | success, content, tool_calls_log, total_steps, total_tokens, model, ... | — | — |
| func | `__init__` | skills, user_id, max_steps, timeout_seconds, model, provider | — | — |
| func | `chat` | message, session_id, context, progress_callback, user_id | — | Blocking chat — waits for full result. |
| func | `chat_stream` | message, session_id, context, progress_callback, user_id | — | Streaming chat — yields SSE event dicts as smolagents produces steps. |
| func | `run_agent_fn` | agent_name, msg, ctx | — | — |

### `context_compressor.py`
**路径:** `agent/context_compressor.py`
**说明:** Context Compressor — 跨轮上下文压缩。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `extract_structured_info` | output, tool_calls, domain | — | 从 agent 输出中提取结构化股票信息。 |
| func | `format_structured_info` | info | — | 将结构化信息格式化为 markdown 区块，注入到压缩结果前部。 |
| func | `compress_context_rule` | output, tool_calls, max_len | — | 规则引擎压缩 agent 输出。 |
| func | `compress_context` | output, tool_calls, model, domain, age_turns | — | 压缩 agent 本轮输出为结构化 markdown 摘要。 |

### `domain_registry.py`
**路径:** `agent/domain_registry.py`
**说明:** Domain Registry — 领域注册与管理。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `DomainConfig` |  | dataclass | 单个领域的配置。 |
| func | `register_domain` | config | — | 注册一个领域配置。 |
| func | `get_domain` | name | — | 按名称获取领域配置，不存在时返回 None。 |
| func | `all_domains` |  | — | 返回所有已注册的领域。 |
| func | `init_builtin_domains` |  | — | 注册内置领域（幂等，多次调用无副作用）。 |

### `evaluator.py`
**路径:** `agent/evaluator.py`
**说明:** Agent Evaluator — 执行后自动评估 + 工具链学习闭环。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `EvalResult` |  | dataclass | 评估结果。 |
| func | `evaluate` | agent_result, tool_chain, verb, noun, domain | — | 评估 agent 执行结果。 |
| func | `learn_from_execution` | eval_result, verb, noun | — | 根据评估结果执行闭环动作。 |
| func | `get_failure_record` | verb, noun | — | 查询某场景的失败记录（供路由决策参考）。 |

### `intent_analyzer.py`
**路径:** `agent/intent_analyzer.py`
**说明:** Intent Analyzer — 基于语义路由的意图分类（v2）。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `IntentResult` |  | dataclass | 意图分析结果。 [methods: domain_config(), tool_filter(), domain_instructions()] |
| func | `analyze_intent` | message, model, provider, history, session_id | — | 分析用户消息的意图。 |
| func | `format_intent_for_agent` | intent, original_message | — | 将意图分析结果格式化为 agent 可用的上下文。 |
| func | `domain_config` |  | property | — |
| func | `tool_filter` |  | property | — |
| func | `domain_instructions` |  | property | — |

### `model.py`
**路径:** `agent/model.py`
**说明:** Model adapter — bridges QuantDinger's LLMService config to smolagents OpenAIModel.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `build_model` | model, provider, temperature | — | Build a smolagents OpenAIModel using QuantDinger's LLM config. |

### `project_scanner.py`
**路径:** `agent/project_scanner.py`
**说明:** Project Scanner — 允许 Agent 只读扫描项目源码。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `get_scan_paths` |  | — | 从配置读取可扫描路径列表。 |
| func | `is_scan_enabled` |  | — | — |
| func | `list_project_files` | max_depth | — | 列出可扫描目录下的文件结构。 |
| func | `read_project_file` | path | — | 只读读取项目源码文件。 |
| func | `grep_project` | pattern, max_results | — | 在可扫描范围内搜索代码。 |

### `run.py`
**路径:** `agent/run.py`
**说明:** Agent 独立调试入口

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `main` |  | — | — |

### `session_store.py`
**路径:** `agent/session_store.py`
**说明:** Session Store — Redis-backed session storage with in-memory fallback.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `get_session_store` |  | — | Get or initialize the session store. |
| func | `__init__` | max_sessions, session_ttl | — | — |
| func | `session_lock` | session_id | — | Return a context manager that locks a specific session. |
| func | `get_session` | session_id | — | — |
| func | `create_session` | session_id, data | — | — |
| func | `update_session` | session_id | — | — |
| func | `delete_session` | session_id | — | — |
| func | `list_sessions` | limit | — | — |
| func | `get_history` | session_id | — | — |
| func | `add_message` | session_id, role, content, max_turns | — | — |
| func | `clear_history` | session_id | — | — |
| func | `save_tool_results` | session_id, results | — | Persist tool call results for reuse in subsequent turns. |
| func | `get_tool_results` | session_id | — | — |
| func | `clear_tool_results` | session_id | — | — |
| func | `save_context_summary` | session_id, summary, domain | — | 保存压缩上下文摘要。按领域分别存储，同时记录年龄（轮次计数）。 |
| func | `get_context_summary` | session_id, current_domain, with_age | — | 获取指定领域的压缩上下文摘要。 |
| func | `cleanup_expired` |  | — | — |
| func | `__init__` | redis_client, session_ttl | — | — |
| func | `session_lock` | session_id | — | Return a context manager that locks a specific session. |
| func | `get_session` | session_id | — | — |
| func | `create_session` | session_id, data | — | — |
| func | `update_session` | session_id | — | — |
| func | `delete_session` | session_id | — | — |
| func | `list_sessions` | limit | — | — |
| func | `get_history` | session_id | — | — |
| func | `add_message` | session_id, role, content, max_turns | — | Atomically append a message using Lua script (no TOCTOU race). |
| func | `clear_history` | session_id | — | — |
| func | `save_tool_results` | session_id, results | — | — |
| func | `get_tool_results` | session_id | — | — |
| func | `clear_tool_results` | session_id | — | — |
| func | `save_context_summary` | session_id, summary, domain | — | — |
| func | `get_context_summary` | session_id, current_domain, with_age | — | — |
| func | `cleanup_expired` |  | — | — |
| func | `__init__` | session_dir, session_ttl, max_sessions | — | — |
| func | `session_lock` | session_id | — | Return a context manager that locks a specific session. |
| func | `get_session` | session_id | — | — |
| func | `create_session` | session_id, data | — | — |
| func | `update_session` | session_id | — | — |
| func | `delete_session` | session_id | — | — |
| func | `list_sessions` | limit | — | — |
| func | `get_history` | session_id | — | — |
| func | `add_message` | session_id, role, content, max_turns | — | — |
| func | `clear_history` | session_id | — | — |
| func | `save_tool_results` | session_id, results | — | — |
| func | `get_tool_results` | session_id | — | — |
| func | `clear_tool_results` | session_id | — | — |
| func | `save_context_summary` | session_id, summary, domain | — | — |
| func | `get_context_summary` | session_id, current_domain, with_age | — | — |
| func | `cleanup_expired` |  | — | — |

### `tool_adapter.py`
**路径:** `agent/tool_adapter.py`
**说明:** Tool Adapter — converts QuantDinger's dict-based tool definitions

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `load_tools_from_module` | tool_list | — | Convert a list of dict-based tool specs into smolagents Tool instances. |
| func | `build_all_tools` | config | — | Load all tools: QuantDinger built-in + smolagents built-in + Hub + MCP. |
| func | `forward` |  | — | — |

### `tool_context.py`
**路径:** `agent/tool_context.py`
**说明:** Tool Context — inject runtime context (session_id, user_id, progress_callback, etc.)

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `set_tool_context` | ctx | — | Set the current tool context (called before agent loop). |
| func | `get_tool_context` |  | — | Get the current tool context. |
| func | `get_session_id` |  | — | Get current session_id from context. |
| func | `get_user_id` |  | — | Get current user_id from context. |
| func | `get_domain` |  | — | Get current domain from context. |
| func | `get_progress_callback` |  | — | Get current progress_callback from context (for real-time streaming). |
| func | `emit_progress` | event | — | Emit a progress event via the current callback (if any). |

### `utils.py`
**路径:** `agent/utils.py`
**说明:** Shared agent utilities — market detection, code parsing, etc.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `detect_market` | stock_code | — | Detect market type from stock code. |

### `workspace.py`
**路径:** `agent/workspace.py`
**说明:** Code Workspace — persistent per-session file storage for iterative code analysis.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `CodeWorkspace` |  | — | Manages a single session's code workspace. [methods: __init__(), save_script() — Save a Python script with automatic versioning., load_script() — Load a script from the workspace., list_scripts() — List all scripts in the workspace (latest versions only)., list_script_versions() — List all versions of a specific script.] |
| func | `apply_template` | session_id, template_name | — | Apply a project template to a workspace. |
| func | `list_templates` |  | — | List available project templates. |
| func | `cleanup_expired_workspaces` | root, max_age_hours | — | Remove workspaces older than max_age_hours. |
| func | `start_cleanup_scheduler` |  | — | Start periodic cleanup of expired workspaces. |
| func | `stop_cleanup_scheduler` |  | — | Stop the cleanup scheduler. |
| func | `get_workspace` | session_id, user_id, domain | — | Get or create a workspace with per-user, per-domain isolation. |
| func | `__init__` | session_id, root | — | — |
| func | `save_script` | name, code, description | — | Save a Python script with automatic versioning. |
| func | `load_script` | name, version | — | Load a script from the workspace. |
| func | `list_scripts` |  | — | List all scripts in the workspace (latest versions only). |
| func | `list_script_versions` | name | — | List all versions of a specific script. |
| func | `diff_versions` | name, v1, v2 | — | Get a diff between two script versions. |
| func | `delete_script` | name | — | Delete a script and all its versions. |
| func | `save_data` | name, content, fmt | — | Save a data file (JSON, CSV, text). |
| func | `load_data` | name | — | Load a data file. |
| func | `list_data` |  | — | List all data files. |
| func | `save_output` | name, content | — | Save an output/result file. |
| func | `list_outputs` |  | — | List all output files. |
| func | `info` |  | — | Get workspace summary. |
| func | `get_context_summary` |  | — | Get a text summary of workspace state for injection into agent prompts. |


## 📁 agent/chain

### `__init__.py`
**路径:** `agent/chain/__init__.py`
**说明:** Chain Orchestration — 链路编排层。

_无公开接口/类定义_

### `chains.py`
**路径:** `agent/chain/chains.py`
**说明:** Chain Definitions — 链路定义。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `ChainStep` |  | dataclass | 链路中的一个步骤。 |
| class | `ChainDef` |  | dataclass | 链路定义。 |
| func | `register_chain` | chain_def | — | 注册一条链路。 |
| func | `get_chain` | chain_id | — | 获取链路定义。 |
| func | `get_chain_for_intent` | verb, noun | — | 根据动词+对象查找匹配的链路。 |
| func | `list_chains` |  | — | 列出所有已注册链路。 |

### `evaluator.py`
**路径:** `agent/chain/evaluator.py`
**说明:** Chain Evaluator — 链路评估器。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `evaluate_pending` | days_old, market | — | 评估所有待评估的链路执行记录。 |
| func | `get_chain_accuracy` | chain_id, days | — | 获取某条链路的准确率统计。 |
| func | `get_step_ranking` | days | — | 获取所有步骤的准确率排名。 |
| func | `update_eval_summary` | eval_date | — | 聚合评估结果，写入 qd_chain_eval_summary。 |
| func | `get_step_weights` | chain_id, days | — | 获取链路各步骤的历史准确率权重，供 executor 加权投票用。 |
| func | `get_eval_report` | chain_id, days | — | 获取评估报告：整体评估 + 分项评估。 |
| func | `generate_optimization` | chain_id, days | — | 基于历史评估数据，生成链路优化建议。 |

### `executor.py`
**路径:** `agent/chain/executor.py`
**说明:** Chain Executor — 链路执行器。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `StepResult` |  | — | 单步执行结果。 [methods: __init__(), to_dict()] |
| class | `ChainResult` |  | — | 整条链路执行结果。 [methods: __init__(), to_dict()] |
| class | `ChainExecutor` |  | — | 链路执行器。 [methods: __init__(), execute() — 执行链路。] |
| func | `__init__` | step | — | — |
| func | `to_dict` |  | — | — |
| func | `__init__` | chain_def, stock_code, stock_name | — | — |
| func | `to_dict` |  | — | — |
| func | `__init__` | chain_id, stock_code, stock_name, user_id | — | — |
| func | `execute` | run_agent_fn, context | — | 执行链路。 |


## 📁 agent/tools

### `__init__.py`
**路径:** `agent/tools/__init__.py`
**说明:** Agent tools subpackage.

_无公开接口/类定义_

### `analysis_tools.py`
**路径:** `agent/tools/analysis_tools.py`
**说明:** Analysis tools — comprehensive technical analysis for agent.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `analyze_trend` | stock_code | tool | 综合技术趋势分析：均线排列 + MACD + RSI + BOLL + KDJ，给出多维度趋势评分和买卖信号。 |
| func | `calculate_ma` | stock_code, periods | tool | 计算指定周期的均线数值，同时返回均线斜率（趋势方向）。 |
| func | `get_volume_analysis` | stock_code | tool | 分析量能变化：量比、成交量趋势、放量/缩量判断、量价关系。 |
| func | `analyze_pattern` | stock_code | tool | 识别K线形态（增强版）：锤子线、十字星、吞没、早晨/晚星、三连阳/阴、长上影/下影、缺口等。 |
| func | `get_chip_distribution` | stock_code | tool | 分析筹码分布：获利比例、平均成本、集中度。 |
| func | `get_indicator_snapshot` | stock_code | tool | 一次性返回所有主要技术指标的最新值，供 Agent 全局研判。 |

### `backtest_tools.py`
**路径:** `agent/tools/backtest_tools.py`
**说明:** Backtest tools — run backtests and query history.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `run_backtest` | strategy_id, stock_code, start_date, end_date, timeframe, user_id | tool | 对指定策略在指定股票上跑历史回测，返回绩效指标。 |
| func | `get_backtest_history` | strategy_id, user_id, limit | tool | 查询策略的历史回测记录。 |

### `chart_tools.py`
**路径:** `agent/tools/chart_tools.py`
**说明:** Chart tools — 蜡烛图展示工具。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `render_candlestick` | stock_code, timeframe, days, stock_name, ma_periods, show_volume, ... | tool | 生成蜡烛图 SVG，可直接嵌入对话展示。 |
| func | `render_candlestick_mini` | stock_code, timeframe, days, stock_name, market | tool | 生成迷你蜡烛图（快速预览版），60天日线+MA5/10/20+成交量。 |
| func | `x_of` | i | — | — |
| func | `y_of` | price | — | — |
| func | `vol_y` | v | — | — |

### `code_workspace_tools.py`
**路径:** `agent/tools/code_workspace_tools.py`
**说明:** Code Workspace Tools — file system + command execution, sandboxed to workspace.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `shell_exec` | command, timeout | tool | Execute a shell command in the session's workspace directory. |
| func | `workspace_save_script` | name, code, description | tool | Save a Python script with automatic versioning. |
| func | `workspace_load_script` | name, version | tool | Load a script from the workspace. |
| func | `workspace_list` |  | tool | List all files in the workspace (scripts, data, outputs). |
| func | `workspace_write_file` | path, content | tool | Write content to a file in the workspace. |
| func | `workspace_read_file` | path, max_chars | tool | Read a file from the workspace. |
| func | `workspace_edit_file` | path, find, replace, regex, count, line_range | tool | Edit a file in the workspace with find/replace or regex replace. |
| func | `workspace_code_review` | path, code | tool | Static analysis of Python code — syntax check, AST analysis, common pitfalls. |
| func | `workspace_exec_script` | name, code, timeout, save_as | tool | Execute a Python script in the workspace with full filesystem access + data source. |
| func | `run_background` | code, name, timeout | tool | Execute a script in the background. Returns immediately with a task_id. |
| func | `poll_task` | task_id | tool | Poll the status of a background task. |
| func | `__init__` | original, capture, q | — | — |
| func | `write` | text | — | — |
| func | `flush` |  | — | — |

### `data_tools.py`
**路径:** `agent/tools/data_tools.py`
**说明:** Data tools — real-time quotes, K-lines, stock info.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `search_stock_by_name` | keyword, market, limit | tool | 根据中文名称或关键词搜索股票代码,支持模糊搜索。 |
| func | `get_realtime_quote` | stock_code | tool | 获取股票/交易对的实时行情数据，包括最新价、涨跌幅、成交量、换手率等。 |
| func | `agent_get_kline` | stock_code, timeframe, days, market | tool | 获取股票/交易对的K线数据（OHLCV）。 |
| func | `generate_kline_chart` | stock_code, timeframe, days, stock_name, indicators | tool | 生成K线图（HTML 交互式图表），返回文件路径。 |
| func | `get_stock_info` | stock_code | tool | 获取股票基本面信息（行业、概念、市值、PE、PB 等）。 |

### `eastmoney_extra_tools.py`
**路径:** `agent/tools/eastmoney_extra_tools.py`
**说明:** EastMoney Extra Tools — 补充 QuantDinger 缺失的 A 股数据端点。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `get_stock_reports` | stock_code, max_pages | tool | 获取个股研报列表（东财 reportapi）。 |
| func | `get_consensus_eps` | stock_code | tool | 获取同花顺机构一致预期EPS。 |
| func | `get_hot_stocks_with_reason` | date | tool | 获取同花顺当日强势股+题材归因。 |
| func | `get_northbound_flow` |  | tool | 获取北向资金实时分钟流向（同花顺 hsgtApi）。 |
| func | `get_stock_concept_blocks` | stock_code | tool | 获取个股所属板块/概念归属（东财 slist）。 |
| func | `get_lockup_expiry` | stock_code, forward_days | tool | 获取限售解禁日历。 |
| func | `get_industry_ranking` | top_n | tool | 获取行业板块涨跌幅排名（东财行业板块）。 |
| func | `get_dragon_tiger_detail` | stock_code, look_back_days | tool | 获取个股龙虎榜详情（席位+机构）。 |
| func | `get_margin_trading` | stock_code, days | tool | 获取融资融券明细。 |
| func | `get_block_trades` | stock_code, page_size | tool | 获取大宗交易记录。 |
| func | `get_holder_count` | stock_code | tool | 获取股东户数变化。 |
| func | `get_dividend_history` | stock_code | tool | 获取分红送转历史。 |
| func | `get_fund_flow_120d` | stock_code | tool | 获取个股资金流120日日级数据（东财 push2his）。 |
| func | `get_fund_flow_minute` | stock_code | tool | 获取个股资金流向分钟级（东财 push2）。 |
| func | `get_eastmoney_stock_news` | stock_code, page_size | tool | 获取东财个股新闻。 |
| func | `get_global_finance_news` | page_size | tool | 获取东财全球财经资讯。 |
| func | `get_financial_statements` | stock_code | tool | 获取新浪财报三表。 |
| func | `get_stock_filings` | stock_code, page_size | tool | 获取个股公告列表（巨潮 cninfo）。 |
| func | `get_order_book` | stock_code | tool | 获取五档盘口+实时行情（腾讯财经）。 |
| func | `get_valuation_metrics` | stock_code | tool | 获取估值指标（腾讯财经）。 |
| func | `get_index_etf_quote` | codes | tool | 获取指数/ETF实时行情（腾讯财经）。 |
| func | `batch_valuation_compare` | stock_codes | tool | 批量估值对比（腾讯财经）。 |

### `enhanced_coding_tools.py`
**路径:** `agent/tools/enhanced_coding_tools.py`
**说明:** Enhanced Coding Tools — OpenCode-inspired tools for QuantDinger's agent.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `apply_patch` | patch_text, dry_run | tool | Apply a unified diff patch to workspace files. |
| func | `glob_files` | pattern, max_results | tool | Search workspace files by glob pattern. |
| func | `grep_code` | pattern, file_glob, max_results, context_lines | tool | Search code in workspace with regex pattern. |
| func | `git_snapshot` | message, action, ref | tool | Git snapshot management for workspace. |
| func | `code_lint` | path, fix | tool | Run ruff linter on workspace Python files. |
| func | `lsp_diagnostics` | path | tool | Run pyright type checker on workspace Python files. |
| func | `read_lines` | path, start_line, end_line | tool | Read specific line range from a workspace file. |
| func | `test_generator` | path, function_name | tool | Generate pytest test template for a function. |

### `indicator_tools.py`
**路径:** `agent/tools/indicator_tools.py`
**说明:** Indicator tools — run indicator strategies, list indicators, get parameters.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `list_indicators` | user_id | tool | 列出用户的所有指标策略（自建 + 购买的）。 |
| func | `get_indicator_params` | indicator_id, user_id | tool | 获取指标策略声明的可配置参数。 |
| func | `run_indicator_signal` | indicator_id, stock_code, timeframe, days, user_id, params | tool | 对单只股票执行指标策略，返回最新的 buy/sell 信号和指标数据。 |

### `iteration_tools.py`
**路径:** `agent/tools/iteration_tools.py`
**说明:** Iteration Tools — OpenCode-inspired task management, user interaction, and auto-snapshot.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `todowrite` | todos | tool | Create or update a structured task list. |
| func | `question` | question_text, options, context | tool | Ask the user a question with structured options. |
| func | `auto_snapshot_before_edit` | reason | — | Auto-snapshot before file edits. Call from edit/patch tools. |

### `market_data_tools.py`
**路径:** `agent/tools/market_data_tools.py`
**说明:** Market Data Tools — 龙虎榜、热榜、涨跌停池、资金流向。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `get_dragon_tiger` | stock_code, date, days | tool | 获取龙虎榜数据。 |
| func | `get_hot_rank` | top_n | tool | 获取实时股票热榜/人气榜。 |
| func | `get_zt_pool` | date, min_continuous_days | tool | 获取涨停股票池。 |
| func | `get_limit_down` | date | tool | 获取跌停股票池。 |
| func | `get_broken_board` | date | tool | 获取炸板(开板)股票池。炸板=曾封涨停但被打开，是资金分歧信号。 |
| func | `get_market_overview` |  | tool | 获取全市场涨跌统计快照：上涨/下跌家数、情绪指标。 |
| func | `get_fund_flow` | stock_codes | tool | 获取个股资金流向。支持单只或批量（逗号分隔），单次最多20只。 |
| func | `get_sector_fund_flow` | date | tool | 获取行业板块资金流向排名。 |
| func | `get_concept_fund_flow` | date | tool | 获取概念板块资金流向排名。 |

### `market_tools.py`
**路径:** `agent/tools/market_tools.py`
**说明:** Market tools — index quotes, sector rankings.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `get_market_indices` |  | tool | 获取大盘指数行情（上证指数、深证成指、创业板指）。 |
| func | `get_sector_rankings` |  | tool | 获取行业板块涨跌排名和资金流向。 |

### `news_search_tools.py`
**路径:** `agent/tools/news_search_tools.py`
**说明:** news Search tools — news search, comprehensive intelligence.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `fetch_financial_news` | lang, market, symbol, name, keywords | — | 转接适配函数 — 调用 news_search.fetch_financial_news()（带缓存） |
| func | `search_stock_news` | stock_code, keyword | tool | 搜索股票相关新闻、公告、研报。 |
| func | `search_comprehensive_intel` | stock_code | tool | 综合情报搜索：最新消息 + 风险排查 + 业绩预期。 |

### `pagination.py`
**路径:** `agent/tools/pagination.py`
**说明:** Pagination & Cache — 对大数据量工具返回值做分页处理。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `paginate_result` | cache_key, data, page, page_size, data_key | — | 对数据做分页处理，返回第 page 页 + 分页元信息。 |
| func | `get_page` | cache_key, page, page_size | — | 从缓存中取指定页。自动识别列表模式/文本模式。 |
| func | `get_cache_summary` | cache_key | — | 查看缓存摘要（不返回数据，只返回元信息）。 |
| func | `paginate_text` | cache_key, text, chunk_size, data_key | — | 对大文本做截断，缓存全文，返回首段 + 分页信息。 |
| func | `get_text_page` | cache_key, page, chunk_size | — | 从缓存中取文本的指定块。供 page_tool 调用。 |
| func | `paginated` | page_size, data_key, auto_key | — | 装饰器：自动为工具函数添加分页支持。 |
| func | `register_page_tool` |  | — | 注册翻页工具到全局 registry。 |
| func | `__init__` | ttl, max_entries | — | — |
| func | `put` | key, data, data_key | — | 存入完整数据。 |
| func | `get` | key | — | 取出缓存。返回 (data, data_key) 或 None。 |
| func | `remove` | key | — | — |
| func | `decorator` | fn | — | — |
| func | `page_tool` | cache_key, page, page_size | reg_tool | 翻页查看缓存数据（自动识别列表/文本模式）。 |
| func | `wrapper` |  | wraps | — |

### `registry.py`
**路径:** `agent/tools/registry.py`
**说明:** Tool Registry — decorator-based self-registration for QuantDinger tools.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `ToolSpec` |  | dataclass | Registered tool metadata, convertible to smolagents Tool. [methods: to_smolagents_tool() — Convert to a smolagents Tool subclass instance.] |
| class | `ToolRegistry` |  | — | Central registry for @tool-decorated functions. [methods: __init__(), register() — Register a tool function. Called by the @tool decorator., discover() — Import all modules in the package to trigger @tool registrations., build() — Build smolagents Tool list with optional policy filtering., categories() — Return {category: [tool_names]} mapping.] |
| func | `tool` | description, name, category, layer, domain, output_type | — | Decorator to register a function as a QuantDinger tool. |
| func | `to_smolagents_tool` |  | — | Convert to a smolagents Tool subclass instance. |
| func | `__init__` |  | — | — |
| func | `register` | fn, name, description, category, layer, domain, ... | — | Register a tool function. Called by the @tool decorator. |
| func | `discover` | package | — | Import all modules in the package to trigger @tool registrations. |
| func | `build` | config | — | Build smolagents Tool list with optional policy filtering. |
| func | `categories` |  | property | Return {category: [tool_names]} mapping. |
| func | `layered_categories` |  | property | Return {layer: {category: [tool_names]}} mapping. |
| func | `all_names` |  | property | — |
| func | `get` | name | — | — |
| func | `decorator` | fn | — | — |
| func | `forward` |  | — | — |

### `scan_tools.py`
**路径:** `agent/tools/scan_tools.py`
**说明:** Project Scan Tools — Agent 只读扫描项目源码的工具集。

_无公开接口/类定义_

### `screener_config.py`
**路径:** `agent/tools/screener_config.py`
**说明:** Screener Config — 选股器常量配置。

_无公开接口/类定义_

### `screener_filters.py`
**路径:** `agent/tools/screener_filters.py`
**说明:** Screener Filters — 筛选条件转换与分类说明。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `build_keyword_from_filters` | filters | — | 将结构化筛选条件转换为自然语言关键词字符串。 |
| func | `get_screener_presets` |  | tool | 获取选股器支持的所有筛选条件分类和示例。 |

### `screener_tools.py`
**路径:** `agent/tools/screener_tools.py`
**说明:** Screener Tools — Agent 选股工具。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `search_stocks` | query, source, filters, market, top_n | tool | 统一选股工具：根据条件从全市场筛选股票。 |

### `screening_tools.py`
**路径:** `agent/tools/screening_tools.py`
**说明:** Screening tools — stock screening (选股) and indicator-based review.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `review_stocks_with_indicator` | stock_codes, indicator_id, user_id, params | tool | 用指标策略批量审核股票，检查是否出现买入信号。 |
| func | `list_user_selection_strategies` | user_id | tool | 列出用户收藏的选股策略。 |

### `sector_analysis_tools.py`
**路径:** `agent/tools/sector_analysis_tools.py`
**说明:** Sector Analysis Tools — 桥接 market_cn.china_market 到 agent 工具系统。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `get_hot_sectors` | industry_limit, concept_limit | tool | 获取实时热门板块（行业+概念），含涨停数/领涨股/强度标签/情绪判断。 |
| func | `get_sector_trend_analysis` | board_type | tool | 获取板块趋势分析（1月趋势+6月周期+今日预测）。 |
| func | `get_sector_history_data` | board_type, days | tool | 获取板块历史排名数据。 |
| func | `get_sector_prediction` |  | tool | 获取今日热门板块预测。 |
| func | `get_sector_cycle` | board_type | tool | 获取板块6个月周期分析。 |
| func | `get_stock_sector_info` | stock_code | tool | 从本地数据库查询股票所属行业和概念。 |
| func | `get_sector_stocks` | board_code, limit | tool | 获取板块内强势个股。 |

### `self_modify_tools.py`
**路径:** `agent/tools/self_modify_tools.py`
**说明:** Self-Modify Tools — Agent 对指定目录的自修改、自升级、自扩充能力。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `is_self_modify_enabled` |  | — | — |
| func | `get_modify_paths` |  | — | 从配置读取可修改目录列表。所有路径基于 BACKEND_ROOT 解析。 |
| func | `self_modify_list_dirs` |  | — | 列出允许修改的目录及其文件。 |
| func | `self_modify_read` | filepath | — | 读取指定文件的源码。 |
| func | `self_modify_write` | filepath, content, reason | — | 写入/修改文件（自动备份原文件）。 |
| func | `self_modify_create` | filepath, content, description | — | 创建新文件。 |
| func | `self_modify_diff` | filepath | — | 查看文件与最近备份的差异。 |
| func | `self_modify_rollback` | filepath | — | 回滚文件到最近的备份版本。 |
| func | `self_modify_log` | last_n | — | 查看修改历史日志。 |
| func | `self_modify_diff_head` | filepath, lines | — | 读取文件头部（快速预览结构，不读全文）。 |

### `tool_chain_tools.py`
**路径:** `agent/tools/tool_chain_tools.py`
**说明:** Tool Chain Tools — agent 自主维护工具链的工具。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `read_tool_chain` | verb, noun | tool | 读取指定场景的工具链。 |
| func | `write_tool_chain` | verb, noun, chain | tool | 保存工具链（执行验证后写回）。 |
| func | `list_tool_chains` |  | tool | 列出所有已配置的工具链。 |

### `trading_tools.py`
**路径:** `agent/tools/trading_tools.py`
**说明:** Trading tools — start/stop strategies, list strategies, get details.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `list_strategies` | user_id | tool | 列出用户的所有交易策略（含运行状态）。 |
| func | `get_strategy_detail` | strategy_id, user_id | tool | 获取策略的详细配置信息。 |
| func | `start_strategy` | strategy_id, user_id | tool | 启动一个交易策略（开始实盘运行）。 |
| func | `stop_strategy` | strategy_id, user_id | tool | 停止一个正在运行的交易策略。 |
| func | `get_strategy_trades` | strategy_id, user_id, limit | tool | 获取策略的最近交易记录。 |


## 📁 agent/skills

### `__init__.py`
**路径:** `agent/skills/__init__.py`
**说明:** Skills subpackage — domain-specific agent skills with self-registration.

_无公开接口/类定义_

### `backtest.py`
**路径:** `agent/skills/backtest.py`
**说明:** Backtest skill — 策略回测验证专家（A股规则特化）。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `BacktestSkill` |  | skill | 回测专家子 Agent。 |

### `bear.py`
**路径:** `agent/skills/bear.py`
**说明:** Bear Researcher skill — 空头研究员（A股中短线特化）。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `BearResearcherSkill` |  | skill | 空头研究员子 Agent。 |

### `bull.py`
**路径:** `agent/skills/bull.py`
**说明:** Bull Researcher skill — 多头研究员（A股中短线特化）。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `BullResearcherSkill` |  | skill | 多头研究员子 Agent。 |

### `concept.py`
**路径:** `agent/skills/concept.py`
**说明:** Concept Tracker skill — A股概念/题材追踪师。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `ConceptTrackerSkill` |  | skill | A股概念/题材追踪师子 Agent。 |

### `data_engineering.py`
**路径:** `agent/skills/data_engineering.py`
**说明:** Data engineering skill — 代码执行和数据处理专家。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `DataEngineeringSkill` |  | skill | 数据工程专家子 Agent。 |

### `guidance.py`
**路径:** `agent/skills/guidance.py`
**说明:** Guidance — 全局行为规则（A股中短线特化）。

_无公开接口/类定义_

### `hot_money.py`
**路径:** `agent/skills/hot_money.py`
**说明:** Hot Money Tracker skill — A股游资追踪师。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `HotMoneyTrackerSkill` |  | skill | A股游资追踪师子 Agent。 |

### `indicator_skills.py`
**路径:** `agent/skills/indicator_skills.py`
**说明:** Indicator skills — 用户自定义指标策略（indicator IDE 生成的交易信号代码）。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `get_indicator_skill_instructions` | skills, user_id | — | 加载用户自定义指标策略指令。 |

### `intelligence.py`
**路径:** `agent/skills/intelligence.py`
**说明:** Intelligence skill — 情报分析专家（A股事件驱动特化）。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `IntelligenceSkill` |  | skill | 情报分析专家子 Agent。 |

### `lockup.py`
**路径:** `agent/skills/lockup.py`
**说明:** Lockup Watcher skill — A股解禁监控师。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `LockupWatcherSkill` |  | skill | A股解禁监控师子 Agent。 |

### `market_data.py`
**路径:** `agent/skills/market_data.py`
**说明:** Market Data skill — 行情数据专家（A股板块轮动特化）。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `MarketDataSkill` |  | skill | 行情数据专家子 Agent。 |

### `momentum.py`
**路径:** `agent/skills/momentum.py`
**说明:** Momentum Tracker skill — A股动量追踪师。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `MomentumTrackerSkill` |  | skill | A股动量追踪师子 Agent。 |

### `policy.py`
**路径:** `agent/skills/policy.py`
**说明:** Policy Analyst skill — A股政策分析师。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `PolicyAnalystSkill` |  | skill | A股政策分析师子 Agent。 |

### `registry.py`
**路径:** `agent/skills/registry.py`
**说明:** Skill Registry — decorator-based self-registration for QuantDinger skills.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `SkillSpec` |  | dataclass | Registered skill metadata. |
| class | `SkillRegistry` |  | — | Central registry for @skill-decorated entries. [methods: __init__(), register() — Register a skill. Called by the @skill decorator., discover() — Import all modules in the package to trigger @skill registrations., build_managed_agents() — Build smolagents managed agents from registered skills., get()] |
| func | `skill` | name, description, instructions, tools, max_steps, priority | — | Decorator to register a managed agent skill. |
| func | `__init__` |  | — | — |
| func | `register` | spec | — | Register a skill. Called by the @skill decorator. |
| func | `discover` | package | — | Import all modules in the package to trigger @skill registrations. |
| func | `build_managed_agents` | smol_model, tool_map, agent_class, base_kwargs | — | Build smolagents managed agents from registered skills. |
| func | `get` | name | — | — |
| func | `all_names` |  | property | — |
| func | `decorator` | cls_or_fn | — | — |

### `screening.py`
**路径:** `agent/skills/screening.py`
**说明:** Screening skill — 选股专家（A股动量+概念筛选特化）。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `ScreeningSkill` |  | skill | 选股专家子 Agent。 |

### `technical.py`
**路径:** `agent/skills/technical.py`
**说明:** Technical Analysis skill — 技术分析专家（A股中短线特化）。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `TechnicalSkill` |  | skill | 技术分析专家子 Agent。 |

### `trading.py`
**路径:** `agent/skills/trading.py`
**说明:** Trading skill — 交易执行专家。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `TradingSkill` |  | skill | 交易执行专家子 Agent。 |


## 📁 agent/router

### `__init__.py`
**路径:** `agent/router/__init__.py`
**说明:** Semantic Intent Router — 基于向量相似度的意图路由引擎。

_无公开接口/类定义_

### `context.py`
**路径:** `agent/router/context.py`
**说明:** Context Manager — 多用户会话上下文管理。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `SessionState` |  | dataclass | 单个会话的路由状态。 [methods: add_route() — 记录一次路由结果。, detect_domain_switch() — 检测是否发生了领域切换。, get_context_domain() — 获取当前上下文 domain。, turn_count()] |
| class | `ContextManager` |  | — | 多用户会话上下文管理器。 [methods: __init__(), get_state() — 获取或创建会话状态。, get_context_domain() — 获取会话的当前上下文 domain。, record_route() — 记录一次路由结果到会话历史。, clear_session() — 清空指定会话。] |
| func | `add_route` | domain, intent, confidence, query | — | 记录一次路由结果。 |
| func | `detect_domain_switch` | new_domain, window | — | 检测是否发生了领域切换。 |
| func | `get_context_domain` |  | — | 获取当前上下文 domain。 |
| func | `turn_count` |  | property | — |
| func | `__init__` | session_ttl, max_sessions | — | — |
| func | `get_state` | session_id | — | 获取或创建会话状态。 |
| func | `get_context_domain` | session_id | — | 获取会话的当前上下文 domain。 |
| func | `record_route` | session_id, domain, intent, confidence, query | — | 记录一次路由结果到会话历史。 |
| func | `clear_session` | session_id | — | 清空指定会话。 |
| func | `get_session_stats` | session_id | — | 获取会话统计信息。 |
| func | `list_active_sessions` |  | — | 列出所有活跃会话 ID。 |
| func | `cleanup_all_expired` |  | — | 手动触发全量过期清理。 |

### `core.py`
**路径:** `agent/router/core.py`
**说明:** Core Router — 语义路由引擎核心。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `Route` |  | dataclass | 一条路由规则。 [methods: domain() — 从 name 提取 domain（如 'finance/stock_analysis' → 'finance'）, intent() — 从 name 提取 intent（如 'finance/stock_analysis' → 'stock_analysis'）] |
| class | `RouteResult` |  | dataclass | 路由结果。 [methods: matched()] |
| class | `LocalIndex` |  | — | 基于 numpy 的本地向量索引。 [methods: __init__(), add() — 添加向量到索引。, query() — 查询最相似的 top_k 条记录。, is_ready()] |
| class | `SemanticIntentRouter` |  | — | 语义意图路由器。 [methods: __init__(), add_routes() — 批量添加路由规则并构建索引。, route() — 对用户消息进行语义路由。, get_route() — 按名称获取路由定义。, list_routes() — 列出所有路由（调试用）。] |
| func | `domain` |  | property | 从 name 提取 domain（如 'finance/stock_analysis' → 'finance'） |
| func | `intent` |  | property | 从 name 提取 intent（如 'finance/stock_analysis' → 'stock_analysis'） |
| func | `matched` |  | property | — |
| func | `__init__` |  | — | — |
| func | `add` | embeddings, route_names, utterances | — | 添加向量到索引。 |
| func | `query` | vector, top_k | — | 查询最相似的 top_k 条记录。 |
| func | `is_ready` |  | — | — |
| func | `__init__` | encoder, routes, default_threshold, aggregation, top_k, context_boost, ... | — | — |
| func | `add_routes` | routes | — | 批量添加路由规则并构建索引。 |
| func | `route` | query, session_id, context_domain | — | 对用户消息进行语义路由。 |
| func | `get_route` | name | — | 按名称获取路由定义。 |
| func | `list_routes` |  | — | 列出所有路由（调试用）。 |

### `encoder.py`
**路径:** `agent/router/encoder.py`
**说明:** Encoder — embedding 编码器，零新增依赖。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `BaseEncoder` |  | — | 编码器基类。 [methods: encode() — 将文本列表编码为向量矩阵。shape: (len(texts), dimension)] |
| class | `RemoteEmbeddingEncoder` |  | — | 通过 OpenAI 兼容的 /v1/embeddings 接口获取向量。 [methods: __init__(), encode() — 调用 embedding 接口。Ollama 自动降级：/v1/embeddings → /api/embeddings。] |
| class | `HashEncoder` |  | — | 基于字符 n-gram 哈希的降级编码器。 [methods: __init__(), encode()] |
| func | `create_encoder` | backend, model_name, api_key, base_url | — | 创建编码器实例。 |
| func | `encode` | texts | — | 将文本列表编码为向量矩阵。shape: (len(texts), dimension) |
| func | `__init__` | api_key, base_url, model, dimension | — | — |
| func | `encode` | texts | — | 调用 embedding 接口。Ollama 自动降级：/v1/embeddings → /api/embeddings。 |
| func | `__init__` | dimension, ngram_range | — | — |
| func | `encode` | texts | — | — |

### `routes.py`
**路径:** `agent/router/routes.py`
**说明:** Routes — 语义路由的默认路由定义。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `build_default_routes` |  | — | 构建默认路由列表。 |

### `tool_chains.py`
**路径:** `agent/router/tool_chains.py`
**说明:** Tool Chains — 工具链读写接口。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `get_tool_chain` | verb, noun | — | 获取指定动作+对象的工具链。 |
| func | `save_tool_chain` | verb, noun, chain | — | 保存工具链（agent 自主学习后调用）。 |
| func | `list_all_chains` |  | — | 列出所有已配置的工具链。 |

### `verb_noun_router.py`
**路径:** `agent/router/verb_noun_router.py`
**说明:** VerbNoun Router — 动作-对象 两阶段意图路由。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `VerbNounResult` |  | dataclass | 路由结果。 [methods: matched()] |
| class | `VerbNounRouter` |  | — | 动作-对象两阶段路由器。 [methods: __init__(), route() — 路由用户消息。] |
| func | `matched` |  | property | — |
| func | `__init__` | semantic_router, context_boost | — | — |
| func | `route` | query, session_id, context_domain | — | 路由用户消息。 |


## 📁 services

### `__init__.py`
**路径:** `services/__init__.py`
**说明:** 业务服务层

_无公开接口/类定义_

### `ai_calibration.py`
**路径:** `services/ai_calibration.py`
**说明:** AI Calibration Service (offline).

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `CalibrationResult` |  | dataclass | — |
| class | `AICalibrationService` |  | — | — [methods: __init__(), get_latest() — Get latest calibration config for market., calibrate_market()] |
| func | `start_ai_calibration_worker` |  | — | Run offline calibration once on service startup (best-effort, non-blocking). |
| func | `__init__` |  | — | — |
| func | `get_latest` | market | — | Get latest calibration config for market. |
| func | `calibrate_market` | market | — | — |

### `analysis_memory.py`
**路径:** `services/analysis_memory.py`
**说明:** Analysis Memory System 2.0

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `AnalysisMemory` |  | — | Simple but effective memory system for AI analysis. [methods: __init__(), store() — Store an analysis result for future reference., get_recent() — Get recent analysis history for a symbol., get_all_history() — Get all analysis history with pagination., delete_history() — Delete a history record by ID.] |
| func | `get_analysis_memory` |  | — | Get singleton AnalysisMemory instance. |
| func | `__init__` |  | — | — |
| func | `store` | analysis_result, user_id | — | Store an analysis result for future reference. |
| func | `get_recent` | market, symbol, days, limit | — | Get recent analysis history for a symbol. |
| func | `get_all_history` | user_id, page, page_size | — | Get all analysis history with pagination. |
| func | `delete_history` | memory_id, user_id | — | Delete a history record by ID. |
| func | `create_pending_task` | market, symbol, language, model, timeframe, user_id | — | Create a processing record in history before long-running analysis starts. |
| func | `finalize_pending_task` | memory_id, result | — | Overwrite pending record with final analysis result. |
| func | `fail_pending_task` | memory_id, error_message | — | Mark pending task as failed. |
| func | `get_similar_patterns` | market, symbol, current_indicators, limit | — | Find historical analyses with similar technical patterns. |
| func | `record_feedback` | memory_id, feedback | — | Record user feedback on an analysis. |
| func | `validate_past_decisions` | days_ago | — | Validate historical decisions by comparing with actual price movements. |
| func | `validate_unvalidated_older_than` | min_age_days, limit | — | Best-effort backfill: |
| func | `get_confidence_accuracy_by_bucket` | market, symbol, days | — | Compute actual accuracy by confidence bucket for calibration. |
| func | `get_adjusted_confidence` | raw_confidence, market, symbol | — | Adjust confidence based on historical accuracy in that bucket. |
| func | `get_performance_stats` | market, symbol, days | — | Get AI performance statistics. |

### `backtest.py`
**路径:** `services/backtest.py`
**说明:** Backtest Service

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `BacktestService` |  | — | Backtest Service [methods: __init__(), ensure_storage_schema(), get_execution_timeframe() — Automatically select execution timeframe based on backtest date range., persist_run(), list_runs()] |
| func | `__init__` | max_size | — | — |
| func | `get` | key | — | — |
| func | `put` | key, df, timeframe | — | — |
| func | `__init__` |  | — | — |
| func | `ensure_storage_schema` |  | — | — |
| func | `get_execution_timeframe` | start_date, end_date, market | — | Automatically select execution timeframe based on backtest date range. |
| func | `persist_run` |  | — | — |
| func | `list_runs` |  | — | — |
| func | `get_run` |  | — | — |
| func | `run_multi_timeframe` | indicator_code, market, symbol, timeframe, start_date, end_date, ... | — | Multi-timeframe backtest. |
| func | `run_strategy_snapshot` | snapshot, start_date, end_date | — | — |
| func | `run_code_strategy` | code, symbol, timeframe, limit, market | — | Run strategy code and return the 'output' variable defined in code. |
| func | `run` | indicator_code, market, symbol, timeframe, start_date, end_date, ... | — | Run backtest. |
| class | `ScriptBar` |  | — | — |
| class | `ScriptPosition` |  | — | — [methods: __init__(), clear_position(), open_position(), add_position(), reduce_position() — Reduce position size by *amount*. Clears to flat when size reaches zero.] |
| class | `ScriptBacktestContext` |  | — | — [methods: __init__(), param(), bars(), log(), buy()] |
| func | `SMA` | series, period | — | — |
| func | `EMA` | series, period | — | — |
| func | `RSI` | series, period | — | — |
| func | `MACD` | series, fast, slow, signal | — | — |
| func | `BOLL` | series, period, std_dev | — | — |
| func | `ATR` | high, low, close, period | — | — |
| func | `CROSSOVER` | series1, series2 | — | — |
| func | `CROSSUNDER` | series1, series2 | — | — |
| func | `clean_value` | value | — | 清理数值，将NaN/Inf转换为0 |
| func | `__init__` |  | — | — |
| func | `clear_position` |  | — | — |
| func | `open_position` | side, entry_price, amount | — | — |
| func | `add_position` | entry_price, amount | — | — |
| func | `reduce_position` | amount | — | Reduce position size by *amount*. Clears to flat when size reaches zero. |
| func | `__init__` | bars_df, initial_balance | — | — |
| func | `param` | name, default | — | — |
| func | `bars` | n | — | — |
| func | `log` | message | — | — |
| func | `buy` | price, amount | — | — |
| func | `sell` | price, amount | — | — |
| func | `close_position` |  | — | — |

### `billing_service.py`
**路径:** `services/billing_service.py`
**说明:** Billing Service - 统一计费服务

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `BillingService` |  | — | 计费服务类 [methods: __init__(), get_billing_config() — 获取计费配置, clear_config_cache() — 清除配置缓存, is_billing_enabled() — 检查是否启用计费, get_feature_cost() — 获取指定功能的积分消耗，0 表示免费] |
| func | `get_billing_service` |  | — | 获取计费服务单例 |
| func | `__init__` |  | — | — |
| func | `get_billing_config` |  | — | 获取计费配置 |
| func | `clear_config_cache` |  | — | 清除配置缓存 |
| func | `is_billing_enabled` |  | — | 检查是否启用计费 |
| func | `get_feature_cost` | feature | — | 获取指定功能的积分消耗，0 表示免费 |
| func | `get_user_credits` | user_id | — | 获取用户积分余额 |
| func | `get_user_vip_status` | user_id | — | 获取用户VIP状态 |
| func | `get_membership_plans` |  | — | Get membership plans from .env (configured via Settings UI). |
| func | `purchase_membership` | user_id, plan | — | Purchase membership plan (mock payment: immediately activates). |
| func | `check_and_consume` | user_id, feature, reference_id | — | 检查并消耗积分 |
| func | `add_credits` | user_id, amount, action, remark, operator_id, reference_id | — | 增加用户积分 |
| func | `set_credits` | user_id, amount, remark, operator_id | — | 设置用户积分（管理员直接设置） |
| func | `set_vip` | user_id, expires_at, remark, operator_id | — | 设置用户VIP状态 |
| func | `get_credits_log` | user_id, page, page_size | — | 获取用户积分变动日志 |
| func | `get_user_billing_info` | user_id | — | 获取用户计费与会员信息快照（供前端显示） |

### `builtin_indicators.py`
**路径:** `services/builtin_indicators.py`
**说明:** 新用户注册时写入内置示例指标（可自由修改、删除）。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `seed_builtin_indicators_for_new_user` | db, user_id | — | 注册成功后写入示例指标包。若该用户已有锚点名称指标则跳过（幂等）。 |

### `community_service.py`
**路径:** `services/community_service.py`
**说明:** Community Service - 指标社区服务

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `CommunityService` |  | — | 指标社区服务类 [methods: __init__(), get_market_indicators() — 获取市场上已发布的指标列表, get_indicator_detail() — 获取指标详情, purchase_indicator() — 购买指标, sync_purchased_indicator() — Refresh a buyer's local copy with the publisher's latest code/description.] |
| func | `get_community_service` |  | — | 获取社区服务单例 |
| func | `__init__` |  | — | — |
| func | `get_market_indicators` | page, page_size, keyword, pricing_type, sort_by, user_id | — | 获取市场上已发布的指标列表 |
| func | `get_indicator_detail` | indicator_id, user_id | — | 获取指标详情 |
| func | `purchase_indicator` | buyer_id, indicator_id | — | 购买指标 |
| func | `sync_purchased_indicator` | buyer_id, indicator_id | — | Refresh a buyer's local copy with the publisher's latest code/description. |
| func | `get_my_purchases` | user_id, page, page_size | — | 获取用户购买的指标列表 |
| func | `get_comments` | indicator_id, page, page_size | — | 获取指标评论列表 |
| func | `add_comment` | user_id, indicator_id, rating, content | — | 添加评论（只有购买过的用户可以评论，且只能评论一次） |
| func | `update_comment` | user_id, comment_id, indicator_id, rating, content | — | 更新评论（只能修改自己的评论） |
| func | `get_user_comment` | user_id, indicator_id | — | 获取用户对某个指标的评论 |
| func | `get_pending_indicators` | page, page_size, review_status | — | 获取待审核的指标列表（管理员用） |
| func | `review_indicator` | admin_id, indicator_id, action, note | — | 审核指标 |
| func | `unpublish_indicator` | admin_id, indicator_id, note | — | 下架指标（取消发布） |
| func | `admin_delete_indicator` | admin_id, indicator_id | — | 管理员删除指标 |
| func | `get_review_stats` |  | — | 获取审核统计 |
| func | `get_indicator_performance` | indicator_id | — | 获取指标的实盘表现统计 |

### `email_service.py`
**路径:** `services/email_service.py`
**说明:** Email Service - Handles email verification codes and notifications.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `get_email_service` |  | — | Get singleton EmailService instance |
| class | `EmailService` |  | — | Email service for verification codes and notifications [methods: __init__(), is_configured() — Check if email service is properly configured, generate_code() — Generate a random numeric verification code, create_verification_code() — Create and store a new verification code., verify_code() — Verify a submitted code with brute-force protection.] |
| func | `__init__` |  | — | — |
| func | `is_configured` |  | — | Check if email service is properly configured |
| func | `generate_code` |  | — | Generate a random numeric verification code |
| func | `create_verification_code` | email, code_type, ip_address | — | Create and store a new verification code. |
| func | `verify_code` | email, code, code_type | — | Verify a submitted code with brute-force protection. |
| func | `send_email` | to_email, subject, html_body | — | Send an email. |
| func | `send_verification_code` | email, code_type, ip_address | — | Generate and send a verification code email. |
| func | `is_valid_email` | email | staticmethod | Basic email format validation |

### `exchange_execution.py`
**路径:** `services/exchange_execution.py`
**说明:** Exchange execution helpers (local deployment).

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `mask_secret` | s, keep | — | Return a masked representation of a secret for safe logs. |
| func | `safe_exchange_config_for_log` | cfg | — | — |
| func | `load_strategy_configs` | strategy_id | — | Load strategy config fields needed for live execution. |
| func | `resolve_exchange_config` | exchange_config, user_id | — | Resolve exchange config. |

### `fast_analysis.py`
**路径:** `services/fast_analysis.py`
**说明:** Fast Analysis Service 3.0

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `FastAnalysisService` |  | — | 快速分析服务 3.0 [methods: __init__(), analyze() — Run fast single-call analysis., analyze_legacy_format() — Returns analysis in legacy multi-agent format for backward compatibility.] |
| func | `get_fast_analysis_service` |  | — | Get singleton FastAnalysisService instance. |
| func | `fast_analyze` | market, symbol, language, model, timeframe | — | Convenience function for fast analysis. |
| func | `__init__` |  | — | — |
| func | `analyze` | market, symbol, language, model, timeframe, user_id | — | Run fast single-call analysis. |
| func | `analyze_legacy_format` | market, symbol, language, model, timeframe | — | Returns analysis in legacy multi-agent format for backward compatibility. |
| func | `add` | name, value, reason | — | — |
| func | `add` | name, value, reason | — | — |

### `indicator_analyzer.py`
**路径:** `services/indicator_analyzer.py`
**说明:** app/services/indicator_analyzer.py — 指标行为分析器

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `analyze_indicator` | indicator_id, user_id, symbol, market, timeframe, bars, ... | — | 对单个指标进行完整的沙箱行为分析。 |
| func | `analyze_user_indicators` | user_id, symbol, market, timeframe, bars | — | 分析用户的所有指标，返回摘要列表。 |
| func | `build_agent_skill_instructions` | user_id, indicator_ids, symbol, market, timeframe | — | 为 Agent 生成策略指令字符串，替代原 YAML 策略加载器。 |

### `indicator_code_quality.py`
**路径:** `services/indicator_code_quality.py`
**说明:** Heuristic quality hints for QuantDinger indicator Python code.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `analyze_indicator_code_quality` | code | — | Returns a list of hints: |

### `indicator_params.py`
**路径:** `services/indicator_params.py`
**说明:** Indicator Parameters Parser and Helper Functions

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `StrategyConfigParser` |  | — | 解析指标代码中的 @strategy 注解，提取策略配置（止盈止损、仓位等）。 [methods: parse() — 解析代码中的 @strategy 注解，返回策略配置字典。, generate_annotations() — 从策略配置字典生成 @strategy 注解行。] |
| class | `IndicatorParamsParser` |  | — | 解析指标代码中的参数声明，支持搜索范围（用于参数优化）。 [methods: parse_params() — 解析指标代码中的参数声明。, merge_params() — 合并声明的参数和用户提供的参数。, get_searchable_params() — 返回所有声明了搜索范围的参数（searchable=True）。, generate_param_grid() — 从参数声明生成笛卡尔积搜索网格。, generate_random_params() — 随机采样 n 组参数（适合参数空间太大时用随机搜索代替网格搜索）。] |
| class | `IndicatorCaller` |  | — | 指标调用器 - 允许一个指标调用另一个指标 [methods: __init__(), call_indicator() — 调用另一个指标并返回结果] |
| func | `get_indicator_params` | indicator_id | — | 获取指标的参数声明（供API调用） |
| func | `parse` | cls, code | classmethod | 解析代码中的 @strategy 注解，返回策略配置字典。 |
| func | `generate_annotations` | cls, config | classmethod | 从策略配置字典生成 @strategy 注解行。 |
| func | `parse_params` | cls, indicator_code | classmethod | 解析指标代码中的参数声明。 |
| func | `merge_params` | cls, declared_params, user_params | classmethod | 合并声明的参数和用户提供的参数。 |
| func | `get_searchable_params` | cls, declared_params | classmethod | 返回所有声明了搜索范围的参数（searchable=True）。 |
| func | `generate_param_grid` | cls, declared_params, max_combinations | classmethod | 从参数声明生成笛卡尔积搜索网格。 |
| func | `generate_random_params` | cls, declared_params, n_samples, seed | classmethod | 随机采样 n 组参数（适合参数空间太大时用随机搜索代替网格搜索）。 |
| func | `__init__` | user_id, current_indicator_id | — | — |
| func | `call_indicator` | indicator_ref, df, params, _depth | — | 调用另一个指标并返回结果 |

### `indicator_review.py`
**路径:** `services/indicator_review.py`
**说明:** ╔══════════════════════════════════════════════════════════════════╗

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `review_stocks` | user_id, indicator_id, stocks, user_params, review_mode, _cancelled | — | 逐个审核股票，通过 SSE 流式返回进度。 |

### `kline.py`
**路径:** `services/kline.py`
**说明:** K线数据服务

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `KlineService` |  | — | K线数据服务 [methods: __init__(), get_kline() — 获取K线数据, get_latest_price() — 获取最新价格（使用1分钟K线，已弃用，建议使用 get_realtime_price）, get_realtime_price() — 获取实时价格（优先使用 ticker API，降级使用分钟 K 线）] |
| func | `__init__` |  | — | — |
| func | `get_kline` | market, symbol, timeframe, limit, before_time | — | 获取K线数据 |
| func | `get_latest_price` | market, symbol | — | 获取最新价格（使用1分钟K线，已弃用，建议使用 get_realtime_price） |
| func | `get_realtime_price` | market, symbol, force_refresh | — | 获取实时价格（优先使用 ticker API，降级使用分钟 K 线） |

### `llm.py`
**路径:** `services/llm.py`
**说明:** LLM service.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `LLMProvider` |  | — | Supported LLM providers |
| class | `LLMService` |  | — | LLM provider wrapper with multi-provider support. [methods: __init__() — Initialize LLM service., provider() — Get the active LLM provider., get_api_key() — Get API key for the specified provider., get_base_url() — Get base URL for the specified provider., get_default_model() — Get default model for the specified provider.] |
| func | `__init__` | provider | — | Initialize LLM service. |
| func | `provider` |  | property | Get the active LLM provider. |
| func | `get_api_key` | provider | — | Get API key for the specified provider. |
| func | `get_base_url` | provider | — | Get base URL for the specified provider. |
| func | `get_default_model` | provider | — | Get default model for the specified provider. |
| func | `get_code_generation_model` | provider | — | Get model for AI code generation; fallback to provider default when unset. |
| func | `api_key` |  | property | — |
| func | `base_url` |  | property | — |
| func | `call_llm_api` | messages, model, temperature, use_fallback, provider, use_json_mode, ... | — | Call LLM API with the specified or default provider. |
| func | `call_openrouter_api` | messages, model, temperature, use_fallback | — | Call LLM API (legacy method name for backward compatibility). |
| func | `safe_call_llm` | system_prompt, user_prompt, default_structure, model, provider | — | Safe LLM call with robust JSON parsing and fallback structure. |
| func | `call_with_tools` | messages, tools, temperature, model, provider | — | Call LLM with OpenAI function-calling tools. |
| func | `shutdown_async_pool` | wait | — | Shut down the async thread pool. Call on app teardown. |
| func | `call_with_tools_async` | messages, tools | — | Non-blocking wrapper: runs call_with_tools in a thread pool. |
| func | `call_llm_async` | messages | — | Non-blocking wrapper for call_llm_api. |
| func | `get_available_providers` | cls | classmethod | Get list of available (configured) providers. |

### `market_data_collector.py`
**路径:** `services/market_data_collector.py`
**说明:** 市场数据采集服务 - AI分析专用

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `MarketDataCollector` |  | — | 市场数据采集器 [methods: __init__(), collect_all() — 采集所有市场数据] |
| func | `get_market_data_collector` |  | — | 获取市场数据采集器单例 |
| func | `__init__` |  | — | — |
| func | `collect_all` | market, symbol, timeframe, include_macro, include_news, include_polymarket, ... | — | 采集所有市场数据 |

### `news_analysis.py`
**路径:** `services/news_analysis.py`
**说明:** 新闻分析评分引擎 — news_analysis.py

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `keyword_score_article` | title, snippet, news_type | — | 单篇规则引擎评分 (纯算法, -10 ~ +10) |
| func | `composite_score` | articles, now | — | 多篇新闻综合评分 (RMS 聚合 + 非对称时间衰减) |

### `news_compressor.py`
**路径:** `services/news_compressor.py`
**说明:** 新闻摘要压缩器 — news_compressor.py

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `compress_news` | text, min_len, max_len, title | — | 压缩新闻 snippet 到 [min_len, max_len] 范围。 |
| func | `compress_news_batch` | items, snippet_key, title_key, max_len | — | 批量压缩新闻列表 (原地修改 snippet 字段) |
| func | `extract_key_sentences` | text, max_chars | — | 兼容 news_analysis._extract_key_sentences() 的接口 |

### `news_provider.py`
**路径:** `services/news_provider.py`
**说明:** 财经新闻直接抓取 — 不依赖 AKShare (v2.2)

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `fetch_eastmoney_news` | category, max_items | — | 东方财富财经新闻 |
| func | `fetch_sina_finance_news` | max_items | — | 新浪财经新闻 (v2.3 修复) |
| func | `fetch_cls_news` | max_items | — | 财联社电报 |
| func | `fetch_wallstreetcn_news` | max_items | — | 华尔街见闻快讯 (公开 API) — 无需修改, 原样工作 |
| func | `fetch_akshare_news` | max_items | — | AKShare 财经新闻 (降级备选) — 兼容新旧列结构 |
| func | `fetch_all_news` | max_per_source | — | 聚合所有新闻源 (供 news.py 调用) |
| func | `fetch_cls_market` | max_items, days | — | 财联社电报 (市场) |
| func | `fetch_wallstreetcn_market` | max_items, days | — | 华尔街见闻快讯 (市场) |
| func | `fetch_eastmoney_market` | max_items, days | — | 东方财富财经新闻 (市场) |
| func | `fetch_sina_market` | max_items, days | — | 新浪财经新闻 (市场) |
| func | `fetch_akshare_market` | max_items, days | — | AKShare 财经新闻 (市场) |
| func | `fetch_eastmoney_stock` | code, days, name | — | 东方财富 公告+新闻 (个股) |
| func | `fetch_sina_stock` | code, days, name | — | 新浪财经个股页 |
| func | `fetch_sina7x24_stock` | code, days, name | — | 新浪7x24 快讯 (按个股关键词过滤) |
| func | `fetch_tencent_stock` | code, days, name | — | 腾讯财经个股新闻 |
| func | `fetch_ifeng_stock` | code, days, name | — | 凤凰财经个股新闻 |
| func | `fetch_all_market_news` | max_per_source, days | — | 并行抓取所有市场新闻源 |
| func | `fetch_all_stock_news` | code, days, name | — | 并行抓取所有个股新闻源 |

### `news_search.py`
**路径:** `services/news_search.py`
**说明:** Search service v2.3 - 搜索引擎调度器

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `SearchResult` |  | dataclass | 搜索结果数据类 [methods: to_text() — 转换为文本格式, to_dict() — 转换为字典] |
| class | `SearchResponse` |  | dataclass | 搜索响应 [methods: to_context() — 将搜索结果转换为可用于 AI 分析的上下文, to_list() — 转换为列表格式（兼容旧接口）] |
| class | `BaseSearchProvider` |  | — | 搜索引擎基类 [methods: __init__(), name(), is_available() — 检查是否有可用的 API Key, search()] |
| class | `TavilySearchProvider` |  | — | Tavily 搜索引擎 [methods: __init__()] |
| class | `SerpAPISearchProvider` |  | — | SerpAPI 搜索引擎 [methods: __init__()] |
| class | `GoogleSearchProvider` |  | — | Google Custom Search (CSE) 搜索引擎 [methods: __init__()] |
| class | `BingSearchProvider` |  | — | Bing Search API 搜索引擎 [methods: __init__()] |
| class | `BaiduSearchProvider` |  | — | 百度搜索 (千帆 AppBuilder API) [methods: __init__()] |
| class | `BochaAISearchProvider` |  | — | Bocha AI (博查) — 国内 AI 搜索引擎 [methods: __init__()] |
| class | `DuckDuckGoSearchProvider` |  | — | DuckDuckGo 搜索引擎（免费，无需 API Key） [methods: __init__()] |
| class | `CLSNewsProvider` |  | — | 财联社电报 [methods: __init__()] |
| class | `WallStreetCNNewsProvider` |  | — | 华尔街见闻快讯 [methods: __init__()] |
| class | `EastMoneyNewsProvider` |  | — | 东方财富财经新闻 [methods: __init__()] |
| class | `SinaFinanceNewsProvider` |  | — | 新浪财经新闻 [methods: __init__()] |
| class | `AKShareNewsProvider` |  | — | AKShare 财经新闻 (降级备选) [methods: __init__()] |
| class | `SearchService` |  | — | 搜索服务 v2.2 [methods: __init__(), is_available(), search() — 执行搜索（兼容旧接口，默认走并行）, search_with_fallback() — 执行搜索（带自动故障转移）, search_parallel() — 多源并行搜索 — 同时请求所有可用引擎, 聚合去重] |
| func | `get_search_service` |  | — | 获取搜索服务单例 |
| func | `reset_search_service` |  | — | 重置搜索服务（用于测试或配置更新后） |
| func | `get_news_type` | symbol, market | — | 通过 symbol 判断新闻类型 (用于选择评分策略) |
| class | `NewsCacheManager` |  | — | 新闻缓存管理器 (纯 DB 比对, 无内存状态) [methods: __init__(), get_items() — 从 DB 查询缓存新闻, 同时清理过期记录, should_search() — 判断是否需要搜索, calc_dynamic_days() — 根据最后搜索时间动态缩减搜索天数, save_items() — 将搜索结果写入明细表, ON CONFLICT 更新。中性评分(score=0)也入库。] |
| func | `get_news_cache_manager` |  | — | 获取新闻缓存管理器单例 |
| func | `fetch_financial_news` | lang, market, symbol, name, keywords | — | 财经新闻统一入口 — 唯一对外接口 |
| func | `to_text` |  | — | 转换为文本格式 |
| func | `to_dict` |  | — | 转换为字典 |
| func | `to_context` | max_results | — | 将搜索结果转换为可用于 AI 分析的上下文 |
| func | `to_list` |  | — | 转换为列表格式（兼容旧接口） |
| func | `__init__` | api_keys, name | — | — |
| func | `name` |  | property | — |
| func | `is_available` |  | property | 检查是否有可用的 API Key |
| func | `search` | query, max_results, days | — | — |
| func | `__init__` | api_keys | — | — |
| func | `__init__` | api_keys | — | — |
| func | `__init__` | api_key, cx | — | — |
| func | `__init__` | api_key | — | — |
| func | `__init__` | api_key | — | — |
| func | `__init__` | api_key | — | — |
| func | `__init__` |  | — | — |
| func | `__init__` | name | — | — |
| func | `__init__` |  | — | — |
| func | `__init__` |  | — | — |
| func | `__init__` |  | — | — |
| func | `__init__` |  | — | — |
| func | `__init__` |  | — | — |
| func | `__init__` |  | — | — |
| func | `is_available` |  | property | — |
| func | `search` | query, num_results, date_restrict, days | — | 执行搜索（兼容旧接口，默认走并行） |
| func | `search_with_fallback` | query, max_results, days | — | 执行搜索（带自动故障转移） |
| func | `search_parallel` | query, max_results, days, max_workers, dedup, timeout | — | 多源并行搜索 — 同时请求所有可用引擎, 聚合去重 |
| func | `search_news_dispatch` | symbol, market, lang, days, max_web_results, name, ... | — | 新闻统一调度 — 唯一对外接口, 所有路由内部消化 |
| func | `search_stock_news` | stock_code, stock_name, market, max_results | — | 搜索股票相关新闻 |
| func | `search_stock_events` | stock_code, stock_name, event_types | — | 搜索股票特定事件（年报预告、减持等） |
| func | `__init__` |  | — | — |
| func | `get_items` | symbol, market | — | 从 DB 查询缓存新闻, 同时清理过期记录 |
| func | `should_search` | symbol, market, is_watchlist | — | 判断是否需要搜索 |
| func | `calc_dynamic_days` | symbol, market, default_days | — | 根据最后搜索时间动态缩减搜索天数 |
| func | `save_items` | symbol, market, results, name | — | 将搜索结果写入明细表, ON CONFLICT 更新。中性评分(score=0)也入库。 |
| func | `calc_score` | symbol, market | staticmethod | 综合评分 — 委托 news_analysis.composite_score() |

### `oauth_service.py`
**路径:** `services/oauth_service.py`
**说明:** OAuth Service - Handles Google and GitHub OAuth authentication.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `get_oauth_service` |  | — | Get singleton OAuthService instance |
| class | `OAuthService` |  | — | OAuth service for Google and GitHub authentication [methods: __init__(), get_google_auth_url() — Generate Google OAuth authorization URL., handle_google_callback() — Handle Google OAuth callback., get_github_auth_url() — Generate GitHub OAuth authorization URL., handle_github_callback() — Handle GitHub OAuth callback.] |
| func | `__init__` |  | — | — |
| func | `get_google_auth_url` | state | — | Generate Google OAuth authorization URL. |
| func | `handle_google_callback` | code, state | — | Handle Google OAuth callback. |
| func | `get_github_auth_url` | state | — | Generate GitHub OAuth authorization URL. |
| func | `handle_github_callback` | code, state | — | Handle GitHub OAuth callback. |
| func | `get_or_create_user_from_oauth` | oauth_info | — | Get existing user or create new user from OAuth info. |
| func | `get_user_oauth_links` | user_id | — | Get all OAuth links for a user |
| func | `unlink_oauth` | user_id, provider | — | Unlink an OAuth provider from user account |
| func | `cleanup_expired_states` | max_age_minutes | — | Clean up expired OAuth states |

### `pending_order_worker.py`
**路径:** `services/pending_order_worker.py`
**说明:** Pending order worker.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `PendingOrderWorker` |  | — | — [methods: __init__(), start(), stop()] |
| func | `__init__` | poll_interval_sec, batch_size | — | — |
| func | `start` |  | — | — |
| func | `stop` | timeout_sec | — | — |

### `polymarket_analyzer.py`
**路径:** `services/polymarket_analyzer.py`
**说明:** Polymarket预测市场分析器

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `PolymarketAnalyzer` |  | — | 预测市场AI分析器 [methods: __init__(), analyze_market() — 分析单个预测市场, generate_asset_trading_opportunities() — 基于预测市场生成相关资产的交易机会] |
| func | `__init__` |  | — | — |
| func | `analyze_market` | market_id, user_id, use_cache, language, model | — | 分析单个预测市场 |
| func | `generate_asset_trading_opportunities` | market_id | — | 基于预测市场生成相关资产的交易机会 |

### `polymarket_batch_analyzer.py`
**路径:** `services/polymarket_batch_analyzer.py`
**说明:** Polymarket批量分析器

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `PolymarketBatchAnalyzer` |  | — | 批量分析预测市场，由AI筛选交易机会 [methods: __init__(), batch_analyze_markets() — 批量分析市场，由AI筛选出有交易机会的市场, save_batch_analysis() — 保存批量分析结果到数据库] |
| func | `__init__` |  | — | — |
| func | `batch_analyze_markets` | markets, max_opportunities | — | 批量分析市场，由AI筛选出有交易机会的市场 |
| func | `save_batch_analysis` | markets | — | 保存批量分析结果到数据库 |

### `polymarket_worker.py`
**路径:** `services/polymarket_worker.py`
**说明:** Polymarket后台任务

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `PolymarketWorker` |  | — | Polymarket数据更新和分析后台任务 [methods: __init__() — 初始化后台任务, start() — 启动后台任务, stop() — 停止后台任务, force_update() — 强制立即更新（用于手动触发）] |
| func | `get_polymarket_worker` |  | — | 获取PolymarketWorker单例 |
| func | `__init__` | update_interval_minutes, analysis_cache_minutes | — | 初始化后台任务 |
| func | `start` |  | — | 启动后台任务 |
| func | `stop` | timeout_sec | — | 停止后台任务 |
| func | `force_update` |  | — | 强制立即更新（用于手动触发） |

### `portfolio_monitor.py`
**路径:** `services/portfolio_monitor.py`
**说明:** Portfolio Monitor Service.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `run_single_monitor` | monitor_id, override_language, user_id, skip_notification | — | Run a single monitor and return the result. |
| func | `notify_strategy_signal_for_positions` | market, symbol, signal_type, signal_detail, user_id | — | Called when a strategy signal is triggered.  |
| func | `start_monitor_service` |  | — | Start the background monitor service. |
| func | `stop_monitor_service` |  | — | Stop the background monitor service. |

### `reflection.py`
**路径:** `services/reflection.py`
**说明:** Reflection Service - Post-trade validation and learning.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `ReflectionService` |  | — | Runs verification cycle: validate unvalidated decisions, optionally run calibration. [methods: run_verification_cycle() — Run one verification cycle:] |
| func | `start_reflection_worker` |  | — | Start background reflection worker (validates + calibrates periodically). |
| func | `run_verification_cycle` |  | — | Run one verification cycle: |

### `rule_engine.py`
**路径:** `services/rule_engine.py`
**说明:** 规则引擎评分模块 — rule_engine.py

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `Rule` |  | dataclass | 单条评分规则 |
| func | `get_ruleset` | news_type | — | 根据新闻类型返回对应的规则集 |
| class | `RuleMatch` |  | dataclass | 单次规则命中记录 |
| func | `rule_engine_score` | title, snippet, news_type | — | 规则引擎评分 — 替代原 keyword_score_article() |

### `security_service.py`
**路径:** `services/security_service.py`
**说明:** Security Service - Handles Turnstile verification, rate limiting, and brute-force protection.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `get_security_service` |  | — | Get singleton SecurityService instance |
| class | `SecurityService` |  | — | Security service for authentication protection [methods: __init__(), get_security_config() — Get public security config for frontend, verify_turnstile() — Verify Cloudflare Turnstile token., record_login_attempt() — Record a login attempt for rate limiting., is_blocked() — Check if an identifier (IP or account) is blocked due to too many failed attempts.] |
| func | `__init__` |  | — | — |
| func | `get_security_config` |  | — | Get public security config for frontend |
| func | `verify_turnstile` | token, ip_address | — | Verify Cloudflare Turnstile token. |
| func | `record_login_attempt` | identifier, identifier_type, success, ip_address, user_agent | — | Record a login attempt for rate limiting. |
| func | `is_blocked` | identifier, identifier_type | — | Check if an identifier (IP or account) is blocked due to too many failed attempts. |
| func | `check_login_allowed` | username, ip_address | — | Check if login is allowed for the given username and IP. |
| func | `clear_login_attempts` | identifier, identifier_type | — | Clear login attempts for an identifier (called after successful login). |
| func | `log_security_event` | action, user_id, ip_address, user_agent, details | — | Log a security-related event. |
| func | `can_send_verification_code` | email, ip_address | — | Check if we can send a verification code to this email from this IP. |
| func | `validate_password_strength` | password | — | Validate password meets minimum security requirements. |
| func | `cleanup_old_records` | days | — | Clean up old login attempts and expired verification codes. |

### `signal_notifier.py`
**路径:** `services/signal_notifier.py`
**说明:** Strategy signal notification service.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `SignalNotifier` |  | — | Notify signal events across channels. [methods: __init__(), notify_signal(), send_profile_test_notifications() — Send a short test message to each selected channel (profile / notification settings).] |
| func | `__init__` |  | — | — |
| func | `notify_signal` |  | — | — |
| func | `send_profile_test_notifications` |  | — | Send a short test message to each selected channel (profile / notification settings). |
| func | `esc` | s | — | — |

### `strategy.py`
**路径:** `services/strategy.py`

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `StrategyService` |  | — | Strategy service. [methods: __init__(), get_running_strategies() — Get all running strategies (ID only), get_running_strategies_with_type() — Get all running strategies (with type info), get_exchange_symbols() — Get exchange trading pairs (no API Key required), test_exchange_connection() — Test exchange connection via direct REST clients (no ccxt).] |
| func | `__init__` |  | — | — |
| func | `get_running_strategies` |  | — | Get all running strategies (ID only) |
| func | `get_running_strategies_with_type` |  | — | Get all running strategies (with type info) |
| func | `get_exchange_symbols` | exchange_config | — | Get exchange trading pairs (no API Key required) |
| func | `test_exchange_connection` | exchange_config, user_id | — | Test exchange connection via direct REST clients (no ccxt). |
| func | `get_strategy_type` | strategy_id | — | Get strategy type from DB. |
| func | `update_strategy_status` | strategy_id, status, user_id | — | Update strategy status. If user_id is provided, verify ownership. |
| func | `list_strategies` | user_id | — | List strategies for the specified user. |
| func | `get_strategy` | strategy_id, user_id | — | Get strategy by ID. If user_id is provided, verify ownership. |
| func | `create_strategy` | payload | — | — |
| func | `batch_create_strategies` | payload | — | Batch create strategies (multi-symbol) |
| func | `batch_start_strategies` | strategy_ids, user_id | — | Batch start strategies. If user_id is provided, verify ownership. |
| func | `batch_stop_strategies` | strategy_ids, user_id | — | Batch stop strategies. If user_id is provided, verify ownership. |
| func | `batch_delete_strategies` | strategy_ids, user_id | — | Batch delete strategies. If user_id is provided, verify ownership. |
| func | `get_strategies_by_group` | strategy_group_id, user_id | — | Get all strategies in a group. If user_id is provided, filter by user. |
| func | `update_strategy` | strategy_id, payload, user_id | — | — |
| func | `delete_strategy` | strategy_id, user_id | — | Delete strategy. If user_id is provided, verify ownership. |

### `strategy_compiler.py`
**路径:** `services/strategy_compiler.py`

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `StrategyCompiler` |  | — | — [methods: compile() — Compiles the strategy configuration JSON into executable Python code.] |
| func | `compile` | config | — | Compiles the strategy configuration JSON into executable Python code. |

### `strategy_script_runtime.py`
**路径:** `services/strategy_script_runtime.py`
**说明:** Python 策略脚本（on_init / on_bar + ctx.buy/sell/close_position）运行时。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `ScriptBar` |  | — | — |
| class | `ScriptPosition` |  | — | — [methods: __init__(), clear_position(), open_position(), add_position(), reduce_position() — Reduce position size by *amount*. Clears to flat when size reaches zero.] |
| class | `StrategyScriptContext` |  | — | 与回测 ScriptBacktestContext 行为一致，供实盘按根推进。 [methods: __init__(), param(), bars(), log(), buy()] |
| func | `compile_strategy_script_handlers` | code | — | 校验并编译策略脚本，返回 (on_init, on_bar)。 |
| func | `__init__` |  | — | — |
| func | `clear_position` |  | — | — |
| func | `open_position` | side, entry_price, amount | — | — |
| func | `add_position` | entry_price, amount | — | — |
| func | `reduce_position` | amount | — | Reduce position size by *amount*. Clears to flat when size reaches zero. |
| func | `__init__` | bars_df, initial_balance | — | — |
| func | `param` | name, default | — | — |
| func | `bars` | n | — | — |
| func | `log` | message | — | — |
| func | `buy` | price, amount | — | — |
| func | `sell` | price, amount | — | — |
| func | `close_position` |  | — | — |

### `strategy_snapshot.py`
**路径:** `services/strategy_snapshot.py`

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `StrategySnapshotResolver` |  | — | Resolve stored strategy rows into backtest-ready snapshots. [methods: __init__(), resolve()] |
| func | `__init__` | user_id | — | — |
| func | `resolve` | strategy, override_config | — | — |

### `symbol_name.py`
**路径:** `services/symbol_name.py`
**说明:** Symbol/company name resolver for local-only mode.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `fetch_quote` | code, timeout | — | 通过腾讯 qt.gtimg.cn 接口获取单只股票/指数实时行情。 |
| func | `resolve_symbol_name` | market, symbol | — | Resolve a display name for a symbol. |

### `trading_executor.py`
**路径:** `services/trading_executor.py`
**说明:** 实时交易执行服务。

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `TradingExecutor` |  | — | 实时交易执行器 (Signal Provider Mode) [methods: __init__(), start_strategy() — 启动策略, stop_strategy() — 停止策略] |
| func | `__init__` |  | — | — |
| func | `start_strategy` | strategy_id | — | 启动策略 |
| func | `stop_strategy` | strategy_id | — | 停止策略 |

### `usdt_payment_service.py`
**路径:** `services/usdt_payment_service.py`
**说明:** USDT Payment Service (方案B：每单独立地址 + 自动对账)

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `UsdtPaymentService` |  | — | — [methods: __init__(), create_order(), get_order(), refresh_all_active_orders() — Scan all pending/paid USDT orders and refresh their chain status.] |
| class | `UsdtOrderWorker` |  | — | Background thread that periodically scans pending/paid USDT orders [methods: __init__(), start(), stop()] |
| func | `get_usdt_payment_service` |  | — | — |
| func | `get_usdt_order_worker` |  | — | — |
| func | `__init__` |  | — | — |
| func | `create_order` | user_id, plan | — | — |
| func | `get_order` | user_id, order_id, refresh | — | — |
| func | `refresh_all_active_orders` |  | — | Scan all pending/paid USDT orders and refresh their chain status. |
| func | `__init__` | poll_interval_sec | — | — |
| func | `start` |  | — | — |
| func | `stop` |  | — | — |

### `user_service.py`
**路径:** `services/user_service.py`
**说明:** User Service - Multi-user management

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `UserService` |  | — | User management service [methods: hash_password() — Hash password using bcrypt (preferred) or SHA256 (fallback), verify_password() — Verify password against hash, get_user_by_id() — Get user by ID, get_user_by_username() — Get user by username (includes password_hash for auth), get_user_by_email() — Get user by email (includes password_hash for auth)] |
| func | `get_user_service` |  | — | Get UserService singleton |
| func | `hash_password` | password | — | Hash password using bcrypt (preferred) or SHA256 (fallback) |
| func | `verify_password` | password, password_hash | — | Verify password against hash |
| func | `get_user_by_id` | user_id | — | Get user by ID |
| func | `get_user_by_username` | username | — | Get user by username (includes password_hash for auth) |
| func | `get_user_by_email` | email | — | Get user by email (includes password_hash for auth) |
| func | `authenticate` | username, password | — | Authenticate user with username/email and password. |
| func | `get_token_version` | user_id | — | 获取用户当前的 token 版本号。 |
| func | `increment_token_version` | user_id | — | 递增用户的 token 版本号，使旧的 token 失效。 |
| func | `create_user` | data | — | Create a new user. |
| func | `update_user` | user_id, data | — | Update user information. |
| func | `change_password` | user_id, old_password, new_password | — | Change user password (requires old password verification, except for users with no password) |
| func | `reset_password` | user_id, new_password | — | Reset user password (admin operation, no old password required) |
| func | `update_password` | user_id, new_password | — | Alias for reset_password - update user password without old password verification |
| func | `delete_user` | user_id | — | Delete a user |
| func | `list_users` | page, page_size, search | — | List all users with pagination and optional search |
| func | `list_all_users_for_export` | search | — | List all users for export with the same fields as the admin user table. |
| func | `get_user_permissions` | role | — | Get permissions for a role |
| func | `ensure_admin_exists` |  | — | Ensure at least one admin user exists. |


## 📁 services/ibkr_trading

### `__init__.py`
**路径:** `services/ibkr_trading/__init__.py`
**说明:** Interactive Brokers (IBKR) Trading Module

_无公开接口/类定义_

### `client.py`
**路径:** `services/ibkr_trading/client.py`
**说明:** Interactive Brokers Trading Client

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `IBKRConfig` |  | dataclass | IBKR connection configuration. |
| class | `OrderResult` |  | dataclass | Order execution result. |
| class | `IBKRClient` |  | — | Interactive Brokers Trading Client [methods: __init__(), connected() — Check if connected., connect() — Connect to TWS or IB Gateway., disconnect() — Disconnect from IBKR., place_market_order() — Place a market order.] |
| func | `get_ibkr_client` | config | — | Get global IBKR client singleton. |
| func | `reset_ibkr_client` |  | — | Reset global client (disconnect and clear instance). |
| func | `__init__` | config | — | — |
| func | `connected` |  | property | Check if connected. |
| func | `connect` |  | — | Connect to TWS or IB Gateway. |
| func | `disconnect` |  | — | Disconnect from IBKR. |
| func | `place_market_order` | symbol, side, quantity, market_type | — | Place a market order. |
| func | `place_limit_order` | symbol, side, quantity, price, market_type | — | Place a limit order. |
| func | `cancel_order` | order_id | — | Cancel an order. |
| func | `get_account_summary` |  | — | Get account summary. |
| func | `get_positions` |  | — | Get current positions. |
| func | `get_open_orders` |  | — | Get open orders. |
| func | `get_quote` | symbol, market_type | — | Get real-time quote. |
| func | `get_connection_status` |  | — | Get connection status. |

### `symbols.py`
**路径:** `services/ibkr_trading/symbols.py`
**说明:** Symbol Mapping and Conversion

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `normalize_symbol` | symbol, market_type | — | Convert system symbol to IB contract parameters. |
| func | `parse_symbol` | symbol | — | Parse symbol and auto-detect market type. |
| func | `format_display_symbol` | ib_symbol, exchange | — | Convert IB contract format back to display format. |


## 📁 services/ext_params

### `__init__.py`
**路径:** `services/ext_params/__init__.py`
**说明:** ext_params — IndicatorStrategy 扩展参数插件系统

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `provider` | func | — | 装饰器：标记一个函数为扩展参数提供者。 |
| func | `collect_extras` | ctx | — | 收集所有插件提供的扩展变量。 |

### `concept_heat.py`
**路径:** `services/ext_params/concept_heat.py`
**说明:** concept_heat — 股票概念热度 + 概念板块资金流向

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `register` | ctx | provider | — |

### `kline_derived.py`
**路径:** `services/ext_params/kline_derived.py`
**说明:** kline_derived — K线衍生分析指标

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `register` | ctx | provider | — |

### `money_flow.py`
**路径:** `services/ext_params/money_flow.py`
**说明:** money_flow — 个股资金流向扩展参数

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `register` | ctx | provider | — |

### `northbound.py`
**路径:** `services/ext_params/northbound.py`
**说明:** northbound — 北向资金 (沪深港通) 持股扩展参数

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `register` | ctx | provider | — |

### `sector_flow.py`
**路径:** `services/ext_params/sector_flow.py`
**说明:** sector_flow — 个股所属板块/概念的资金流向

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `register` | ctx | provider | — |

### `stock_basic.py`
**路径:** `services/ext_params/stock_basic.py`
**说明:** stock_basic — 股票基本面扩展参数

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `register` | ctx | provider | — |

### `stock_concept_industry.py`
**路径:** `services/ext_params/stock_concept_industry.py`
**说明:** stock_concept_industry — 股票所属概念 & 行业扩展参数

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `register` | ctx | provider | — |

### `tsohlcv.py`
**路径:** `services/ext_params/tsohlcv.py`
**说明:** tsohlcv — TSOHLCV 模式匹配扩展参数

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `match_tsohlcvs` | tsohlcv_data, symbol, df | — | 在 OHLCV 数据上扫描 TSOHLCV 模式。 |
| func | `register` | ctx | provider | 注册 TSOHLCV 扩展参数。 |


## 📁 services/mt5_trading

### `__init__.py`
**路径:** `services/mt5_trading/__init__.py`
**说明:** MetaTrader 5 Trading Module

_无公开接口/类定义_

### `client.py`
**路径:** `services/mt5_trading/client.py`
**说明:** MetaTrader 5 Trading Client

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `MT5Config` |  | dataclass | MT5 connection configuration. |
| class | `OrderResult` |  | dataclass | Order execution result. |
| class | `MT5Client` |  | — | MetaTrader 5 Trading Client [methods: __init__(), connected() — Check if connected to MT5 terminal., connect() — Connect to MT5 terminal., disconnect() — Disconnect from MT5 terminal., place_market_order() — Place a market order.] |
| func | `get_mt5_client` | config | — | Get global MT5 client singleton. |
| func | `reset_mt5_client` |  | — | Reset global client (disconnect and clear instance). |
| func | `__init__` | config | — | — |
| func | `connected` |  | property | Check if connected to MT5 terminal. |
| func | `connect` |  | — | Connect to MT5 terminal. |
| func | `disconnect` |  | — | Disconnect from MT5 terminal. |
| func | `place_market_order` | symbol, side, volume, deviation, comment | — | Place a market order. |
| func | `place_limit_order` | symbol, side, volume, price, comment | — | Place a pending limit order. |
| func | `close_position` | ticket, volume, deviation, comment | — | Close an open position. |
| func | `cancel_order` | ticket | — | Cancel a pending order. |
| func | `get_account_info` |  | — | Get account information. |
| func | `get_positions` | symbol | — | Get open positions. |
| func | `get_orders` | symbol | — | Get pending orders. |
| func | `get_quote` | symbol | — | Get real-time quote. |
| func | `get_symbols` | group | — | Get available symbols. |
| func | `get_connection_status` |  | — | Get connection status. |

### `symbols.py`
**路径:** `services/mt5_trading/symbols.py`
**说明:** Symbol Mapping and Conversion for MT5

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `normalize_symbol` | symbol, broker_suffix | — | Normalize symbol to MT5 format. |
| func | `parse_symbol` | symbol | — | Parse symbol and extract base/quote currencies. |
| func | `get_lot_size_info` | symbol | — | Get lot size information for a symbol. |


## 📁 services/live_trading

### `__init__.py`
**路径:** `services/live_trading/__init__.py`
**说明:** Live trading (direct exchange REST) clients.

_无公开接口/类定义_

### `base.py`
**路径:** `services/live_trading/base.py`
**说明:** Base REST client helpers for direct exchange connections.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `LiveOrderResult` |  | dataclass | — |
| class | `LiveTradingError` |  | — | — |
| class | `BaseRestClient` |  | — | — [methods: __init__(), get_fee_rate() — Query account fee rate from exchange. Returns {"maker": 0.0002, "taker": 0.0005} or None.] |
| func | `__init__` | base_url, timeout_sec | — | — |
| func | `get_fee_rate` | symbol, market_type | — | Query account fee rate from exchange. Returns {"maker": 0.0002, "taker": 0.0005} or None. |

### `binance.py`
**路径:** `services/live_trading/binance.py`
**说明:** Binance USDT-M Futures (direct REST) client.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `BinanceFuturesClient` |  | — | — [methods: __init__(), get_mark_price() — Best-effort mark price for MIN_NOTIONAL validation., get_symbol_filters() — Get futures symbol filters from exchangeInfo (best-effort)., ping(), get_account() — Private endpoint to validate credentials.] |
| func | `__init__` |  | — | — |
| func | `get_mark_price` |  | — | Best-effort mark price for MIN_NOTIONAL validation. |
| func | `get_symbol_filters` |  | — | Get futures symbol filters from exchangeInfo (best-effort). |
| func | `ping` |  | — | — |
| func | `get_account` |  | — | Private endpoint to validate credentials. |
| func | `get_user_trades` |  | — | Fetch user trades (fills). |
| func | `get_fee_for_order` |  | — | Best-effort: sum commissions from fills for a specific order. |
| func | `get_fee_rate` | symbol, market_type | — | — |
| func | `set_leverage` |  | — | Set futures leverage for a symbol (USDT-M). |
| func | `get_dual_side_position` |  | — | Best-effort read of position mode: |
| func | `get_order` |  | — | Query order status/details. |
| func | `wait_for_fill` |  | — | Poll order detail to obtain (best-effort) executed quantity, average price, |
| func | `place_market_order` |  | — | — |
| func | `place_limit_order` |  | — | — |
| func | `cancel_order` |  | — | — |
| func | `set_margin_type` |  | — | Set symbol margin mode on USDT-M futures. |
| func | `get_positions` |  | — | Futures positions (position risk). Optional ``symbol`` filters to one contract. |

### `binance_spot.py`
**路径:** `services/live_trading/binance_spot.py`
**说明:** Binance Spot (direct REST) client.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `BinanceSpotClient` |  | — | — [methods: __init__(), ping() — Public connectivity check., get_symbol_filters() — Get spot symbol filters from exchangeInfo (best-effort)., place_limit_order(), place_market_order()] |
| func | `__init__` |  | — | — |
| func | `ping` |  | — | Public connectivity check. |
| func | `get_symbol_filters` |  | — | Get spot symbol filters from exchangeInfo (best-effort). |
| func | `place_limit_order` |  | — | — |
| func | `place_market_order` |  | — | — |
| func | `get_account` |  | — | Get spot account balances. |
| func | `get_my_trades` |  | — | Fetch spot trade fills. |
| func | `get_fee_for_order` |  | — | Best-effort: sum commissions from fills for a specific spot order. |
| func | `get_fee_rate` | symbol, market_type | — | — |
| func | `cancel_order` |  | — | — |
| func | `get_order` |  | — | — |
| func | `wait_for_fill` |  | — | — |

### `bitget.py`
**路径:** `services/live_trading/bitget.py`
**说明:** Bitget (direct REST) client for USDT-margined perpetual orders.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `BitgetMixClient` |  | — | — [methods: __init__(), get_ticker() — Public mix ticker (for USDT-notional -> base size conversion in quick trade)., get_account_pos_mode() — Returns Bitget posMode for the contract account: 'hedge_mode', 'one_way_mode', or '' if unknown., get_contract() — Fetch contract metadata (best-effort) from public endpoint., ping()] |
| func | `__init__` |  | — | — |
| func | `get_ticker` |  | — | Public mix ticker (for USDT-notional -> base size conversion in quick trade). |
| func | `get_account_pos_mode` |  | — | Returns Bitget posMode for the contract account: 'hedge_mode', 'one_way_mode', or '' if unknown. |
| func | `get_contract` |  | — | Fetch contract metadata (best-effort) from public endpoint. |
| func | `ping` |  | — | — |
| func | `get_accounts` |  | — | Private endpoint to validate credentials (best-effort). |
| func | `get_positions` |  | — | Get positions (best-effort). |
| func | `get_fee_rate` | symbol, market_type | — | — |
| func | `set_leverage` |  | — | Best-effort set leverage for Bitget mix. |
| func | `place_market_order` |  | — | — |
| func | `place_limit_order` |  | — | — |
| func | `cancel_order` |  | — | — |
| func | `get_order_detail` |  | — | — |
| func | `get_order_fills` |  | — | — |
| func | `wait_for_fill` |  | — | Poll order fills/detail to obtain (best-effort) executed size and average price. |

### `bitget_spot.py`
**路径:** `services/live_trading/bitget_spot.py`
**说明:** Bitget Spot (direct REST) client.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `BitgetSpotClient` |  | — | — [methods: __init__(), get_symbol_meta() — Fetch spot symbol metadata (best-effort)., place_limit_order(), place_market_order() — NOTE: Bitget spot market BUY may expect quote amount. We accept `size` as base size,, cancel_order()] |
| func | `__init__` |  | — | — |
| func | `get_symbol_meta` |  | — | Fetch spot symbol metadata (best-effort). |
| func | `place_limit_order` |  | — | — |
| func | `place_market_order` |  | — | NOTE: Bitget spot market BUY may expect quote amount. We accept `size` as base size, |
| func | `cancel_order` |  | — | — |
| func | `get_order` |  | — | — |
| func | `get_fills` |  | — | — |
| func | `wait_for_fill` |  | — | — |
| func | `get_assets` |  | — | Spot assets/balances. |
| func | `get_ticker` |  | — | — |

### `bybit.py`
**路径:** `services/live_trading/bybit.py`
**说明:** Bybit (direct REST) client for spot / linear perpetual orders (v5).

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `BybitClient` |  | — | — [methods: __init__(), sync_server_time_offset() — Align signing timestamp with Bybit server (public /v5/market/time)., ping(), get_ticker() — Public market price for USDT notional -> base qty (quick_trade / execution)., get_wallet_balance()] |
| func | `__init__` |  | — | — |
| func | `sync_server_time_offset` |  | — | Align signing timestamp with Bybit server (public /v5/market/time). |
| func | `ping` |  | — | — |
| func | `get_ticker` |  | — | Public market price for USDT notional -> base qty (quick_trade / execution). |
| func | `get_wallet_balance` |  | — | — |
| func | `get_instrument_info` |  | — | — |
| func | `place_market_order` |  | — | — |
| func | `place_limit_order` |  | — | — |
| func | `cancel_order` |  | — | — |
| func | `get_order` |  | — | — |
| func | `wait_for_fill` |  | — | — |
| func | `get_positions` |  | — | GET /v5/position/list — Bybit v5 requires ``symbol`` OR ``settleCoin`` with ``category``. |
| func | `get_fee_rate` | symbol, market_type | — | — |
| func | `set_leverage` |  | — | — |

### `coinbase_exchange.py`
**路径:** `services/live_trading/coinbase_exchange.py`
**说明:** Coinbase Exchange (legacy, direct REST) client.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `CoinbaseExchangeClient` |  | — | — [methods: __init__(), ping(), get_accounts(), place_market_order(), place_limit_order()] |
| func | `__init__` |  | — | — |
| func | `ping` |  | — | — |
| func | `get_accounts` |  | — | — |
| func | `place_market_order` |  | — | — |
| func | `place_limit_order` |  | — | — |
| func | `cancel_order` |  | — | — |
| func | `get_order` |  | — | — |
| func | `wait_for_fill` |  | — | — |

### `deepcoin.py`
**路径:** `services/live_trading/deepcoin.py`
**说明:** Deepcoin (direct REST) client for spot / perpetual swap orders.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `DeepcoinClient` |  | — | Deepcoin REST client for spot and perpetual swap trading. [methods: __init__(), ping() — Test API connectivity using public endpoint., get_balance() — Get account balance., get_positions() — Get open positions., set_leverage() — Set leverage for a trading pair.] |
| func | `__init__` |  | — | — |
| func | `ping` |  | — | Test API connectivity using public endpoint. |
| func | `get_balance` |  | — | Get account balance. |
| func | `get_positions` |  | — | Get open positions. |
| func | `set_leverage` |  | — | Set leverage for a trading pair. |
| func | `get_instrument_info` |  | — | Get instrument metadata (min qty, qty step, etc.). |
| func | `place_market_order` |  | — | Place a market order. |
| func | `place_limit_order` |  | — | Place a limit order. |
| func | `cancel_order` |  | — | Cancel an order. |
| func | `get_order` |  | — | Get order details. |
| func | `get_open_orders` |  | — | Get open orders. |
| func | `get_order_history` |  | — | Get order history. |
| func | `wait_for_fill` |  | — | Poll order status until filled or timeout. |

### `execution.py`
**路径:** `services/live_trading/execution.py`
**说明:** Translate a strategy signal into a direct-exchange order call.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `place_order_from_signal` | client | — | — |

### `factory.py`
**路径:** `services/live_trading/factory.py`
**说明:** Factory for direct exchange clients.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `create_client` | exchange_config | — | — |
| func | `create_ibkr_client` | exchange_config | — | Create IBKR client for US stock trading. |
| func | `create_mt5_client` | exchange_config | — | Create MT5 client for forex trading. |
| func | `query_fee_rate` | exchange_config, symbol, market_type | — | Best-effort: create a temporary client and query the account's fee tier |

### `gate.py`
**路径:** `services/live_trading/gate.py`
**说明:** Gate.io (direct REST) clients:

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `GateSpotClient` |  | — | — [methods: ping(), get_ticker(), get_accounts(), place_limit_order(), place_market_order()] |
| class | `GateUsdtFuturesClient` |  | — | — [methods: __init__(), ping(), get_ticker(), get_contract() — Fetch contract metadata with ``X-Gate-Size-Decimal: 1`` to get accurate string-typed size fields., contracts_signed_to_base_qty() — Convert signed position size (contracts) from Gate positions API to base-asset quantity.] |
| func | `__init__` |  | — | — |
| func | `get_fee_rate` | symbol, market_type | — | — |
| func | `ping` |  | — | — |
| func | `get_ticker` |  | — | — |
| func | `get_accounts` |  | — | — |
| func | `place_limit_order` |  | — | — |
| func | `place_market_order` |  | — | — |
| func | `cancel_order` |  | — | — |
| func | `get_order` |  | — | — |
| func | `wait_for_fill` |  | — | — |
| func | `__init__` |  | — | — |
| func | `ping` |  | — | — |
| func | `get_ticker` |  | — | — |
| func | `get_contract` |  | — | Fetch contract metadata with ``X-Gate-Size-Decimal: 1`` to get accurate string-typed size fields. |
| func | `contracts_signed_to_base_qty` |  | — | Convert signed position size (contracts) from Gate positions API to base-asset quantity. |
| func | `get_accounts` |  | — | — |
| func | `get_positions` |  | — | — |
| func | `set_leverage` |  | — | — |
| func | `place_market_order` |  | — | — |
| func | `place_limit_order` |  | — | — |
| func | `cancel_order` |  | — | — |
| func | `get_order` |  | — | — |
| func | `wait_for_fill` |  | — | — |

### `htx.py`
**路径:** `services/live_trading/htx.py`
**说明:** HTX (Huobi) direct REST client for spot and USDT-margined perpetual swap.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `HtxClient` |  | — | — [methods: __init__(), ping(), get_accounts(), get_balance(), get_positions()] |
| func | `__init__` |  | — | — |
| func | `ping` |  | — | — |
| func | `get_accounts` |  | — | — |
| func | `get_balance` |  | — | — |
| func | `get_positions` |  | — | — |
| func | `get_ticker` |  | — | — |
| func | `get_contract_info` |  | — | — |
| func | `set_leverage` |  | — | — |
| func | `place_market_order` |  | — | — |
| func | `place_limit_order` |  | — | — |
| func | `cancel_order` |  | — | — |
| func | `get_order` |  | — | — |
| func | `wait_for_fill` |  | — | — |

### `kraken.py`
**路径:** `services/live_trading/kraken.py`
**说明:** Kraken (direct REST) client (spot).

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `KrakenClient` |  | — | — [methods: __init__(), ping(), get_balance() — Private balance endpoint (best-effort credential validation)., add_order(), place_market_order()] |
| func | `__init__` |  | — | — |
| func | `ping` |  | — | — |
| func | `get_balance` |  | — | Private balance endpoint (best-effort credential validation). |
| func | `add_order` |  | — | — |
| func | `place_market_order` |  | — | — |
| func | `place_limit_order` |  | — | — |
| func | `cancel_order` |  | — | — |
| func | `get_order` |  | — | — |
| func | `wait_for_fill` |  | — | — |

### `kraken_futures.py`
**路径:** `services/live_trading/kraken_futures.py`
**说明:** Kraken Futures (direct REST) client.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `KrakenFuturesClient` |  | — | — [methods: __init__(), ping(), get_accounts(), get_open_positions(), place_market_order()] |
| func | `__init__` |  | — | — |
| func | `ping` |  | — | — |
| func | `get_accounts` |  | — | — |
| func | `get_open_positions` |  | — | — |
| func | `place_market_order` |  | — | — |
| func | `place_limit_order` |  | — | — |
| func | `cancel_order` |  | — | — |
| func | `get_order` |  | — | — |
| func | `wait_for_fill` |  | — | — |

### `kucoin.py`
**路径:** `services/live_trading/kucoin.py`
**说明:** KuCoin (direct REST) client (spot).

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `KucoinSpotClient` |  | — | — [methods: __init__(), ping(), get_accounts(), get_ticker(), place_limit_order()] |
| class | `KucoinFuturesClient` |  | — | KuCoin Futures (USDT perpetual) direct REST client. [methods: __init__(), ping(), get_contract(), get_accounts(), get_positions()] |
| func | `__init__` |  | — | — |
| func | `ping` |  | — | — |
| func | `get_accounts` |  | — | — |
| func | `get_ticker` |  | — | — |
| func | `place_limit_order` |  | — | — |
| func | `place_market_order` |  | — | KuCoin market order: |
| func | `cancel_order` |  | — | — |
| func | `get_order` |  | — | — |
| func | `get_fills` |  | — | — |
| func | `wait_for_fill` |  | — | — |
| func | `__init__` |  | — | — |
| func | `ping` |  | — | — |
| func | `get_contract` |  | — | — |
| func | `get_accounts` |  | — | — |
| func | `get_positions` |  | — | — |
| func | `set_leverage` |  | — | — |
| func | `place_market_order` |  | — | — |
| func | `place_limit_order` |  | — | — |
| func | `cancel_order` |  | — | — |
| func | `get_order` |  | — | — |
| func | `wait_for_fill` |  | — | — |

### `okx.py`
**路径:** `services/live_trading/okx.py`
**说明:** OKX (direct REST) client for perpetual swap orders.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `OkxClient` |  | — | — [methods: __init__(), get_instrument() — Fetch OKX instrument metadata from public endpoint:, ping(), get_ticker() — Get ticker price for an instrument., get_balance() — Private endpoint to validate credentials (best-effort).] |
| func | `__init__` |  | — | — |
| func | `get_instrument` |  | — | Fetch OKX instrument metadata from public endpoint: |
| func | `ping` |  | — | — |
| func | `get_ticker` |  | — | Get ticker price for an instrument. |
| func | `get_balance` |  | — | Private endpoint to validate credentials (best-effort). |
| func | `get_fee_rate` | symbol, market_type | — | — |
| func | `get_positions` |  | — | Get positions (best-effort). |
| func | `set_leverage` |  | — | Set leverage for an instrument (best-effort). |
| func | `get_account_config` |  | — | Get account configuration (best-effort). |
| func | `place_market_order` |  | — | — |
| func | `place_limit_order` |  | — | — |
| func | `cancel_order` |  | — | — |
| func | `get_order` |  | — | — |
| func | `get_order_fills` |  | — | — |
| func | `wait_for_fill` |  | — | Poll order detail / fills to obtain (best-effort) executed size and average price. |

### `records.py`
**路径:** `services/live_trading/records.py`
**说明:** DB helpers for recording live trades and maintaining local position snapshots.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `normalize_strategy_symbol` | symbol | — | Canonical symbol for qd_strategy_positions / qd_strategy_trades (e.g. BTC/USDT). |
| func | `record_trade` |  | — | — |
| func | `upsert_position` |  | — | — |
| func | `apply_fill_to_local_position` |  | — | Apply a fill to the local position snapshot. |

### `symbols.py`
**路径:** `services/live_trading/symbols.py`
**说明:** Symbol normalization helpers.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `to_binance_futures_symbol` | symbol | — | — |
| func | `to_okx_swap_inst_id` | symbol | — | — |
| func | `to_okx_spot_inst_id` | symbol | — | — |
| func | `to_bitget_um_symbol` | symbol | — | — |
| func | `to_bybit_symbol` | symbol | — | Bybit symbol format (v5): typically concatenated, e.g. BTCUSDT. |
| func | `to_coinbase_product_id` | symbol | — | Coinbase Exchange product id format: BASE-QUOTE, e.g. BTC-USDT. |
| func | `to_kraken_pair` | symbol | — | Kraken spot pair format is exchange-specific (e.g. XBTUSDT). |
| func | `to_kucoin_symbol` | symbol | — | KuCoin spot symbol format: BASE-QUOTE, e.g. BTC-USDT. |
| func | `to_kucoin_futures_symbol` | symbol | — | KuCoin Futures (USDT perpetual) symbol is exchange-specific, common examples: |
| func | `to_kraken_futures_symbol` | symbol | — | Kraken Futures instruments are exchange-specific (e.g. PF_XBTUSD, PI_XBTUSD). |
| func | `to_gate_currency_pair` | symbol | — | Gate spot/futures currency_pair/contract format: BASE_QUOTE, e.g. BTC_USDT. |
| func | `to_deepcoin_symbol` | symbol | — | Deepcoin symbol format: typically BASE-QUOTE for spot, BASE-QUOTE-SWAP for perpetual. |
| func | `to_deepcoin_swap_symbol` | symbol | — | Deepcoin perpetual swap symbol format: BASE-QUOTE-SWAP, e.g. BTC-USDT-SWAP. |
| func | `to_htx_spot_symbol` | symbol | — | HTX spot symbol format: lowercase concatenated, e.g. btcusdt. |
| func | `to_htx_contract_code` | symbol | — | HTX USDT-margined swap contract code: BASE-QUOTE, e.g. BTC-USDT. |


## 📁 services/experiment

### `__init__.py`
**路径:** `services/experiment/__init__.py`
**说明:** Experiment orchestration services for AI trading system workflows.

_无公开接口/类定义_

### `evolution.py`
**路径:** `services/experiment/evolution.py`
**说明:** Strategy evolution helpers.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `StrategyEvolutionService` |  | — | Generate strategy variants from structured parameter spaces. [methods: build_variants()] |
| func | `build_variants` |  | — | — |

### `prompts.py`
**路径:** `services/experiment/prompts.py`
**说明:** LLM prompt construction and response parsing for AI experiment pipeline.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `extract_indicator_params` | code | — | Parse @param declarations from indicator code. |
| func | `build_round_prompt` |  | — | Build the user-message prompt for one optimization round. |
| func | `parse_llm_candidates` | raw_text | — | Parse LLM response into a list of candidate parameter dicts. |

### `regime.py`
**路径:** `services/experiment/regime.py`
**说明:** Market regime detection service.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `RegimeProfile` |  | dataclass | — |
| class | `MarketRegimeService` |  | — | Rule-based market regime detection for the first orchestration version. [methods: detect()] |
| func | `detect` | df | — | — |

### `runner.py`
**路径:** `services/experiment/runner.py`
**说明:** Experiment runner service.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `ExperimentRunnerService` |  | — | Orchestrate market regime detection, batch backtests, scoring and evolution. [methods: __init__(), run_ai_pipeline() — Multi-round LLM-driven optimization pipeline., save_as_strategy() — Persist the best experiment candidate as a strategy record., run_pipeline(), run_structured_tune() — Grid or random search over parameterSpace (strategy_config paths, leverage, etc.).] |
| func | `__init__` |  | — | — |
| func | `run_ai_pipeline` |  | — | Multi-round LLM-driven optimization pipeline. |
| func | `save_as_strategy` |  | — | Persist the best experiment candidate as a strategy record. |
| func | `run_pipeline` |  | — | — |
| func | `run_structured_tune` |  | — | Grid or random search over parameterSpace (strategy_config paths, leverage, etc.). |
| func | `detect_regime` | base | — | — |

### `scoring.py`
**路径:** `services/experiment/scoring.py`
**说明:** Strategy scoring service.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `StrategyScoringService` |  | — | Convert backtest results into comparable multi-factor scores. [methods: score_result(), rank_results()] |
| func | `score_result` | result | — | — |
| func | `rank_results` | items | — | — |


## 📁 market_store

### `__init__.py`
**路径:** `market_store/__init__.py`

_无公开接口/类定义_

### `plugin_api.py`
**路径:** `market_store/plugin_api.py`
**说明:** plugin_api.py — 本地行情存储 API（路由层）

**外部 HTTP 接口:**

| 方法+路径 | 函数名 | 说明 |
|-----------|--------|------|
| `ROUTE /overview` | `api_overview()` | GET /api/market-local/overview — 最新市场数据 + 综合评分。 |
| `ROUTE /query` | `api_query()` | GET /api/market-local/query — 条件查询历史数据。 |
| `ROUTE /score` | `api_score()` | GET /api/market-local/score — 市场评分。 |
| `ROUTE /sentiment` | `api_sentiment()` | GET /api/market-local/sentiment — 恐贪 + VIX + DXY。 |
| `ROUTE /symbol/<path:symbol>` | `api_symbol()` | GET /api/market-local/symbol/BTC — 单标的历史走势。 |
| `ROUTE /anomalies` | `api_anomalies()` | GET /api/market-local/anomalies — 急剧变化检测。 |
| `ROUTE /stats` | `api_stats()` | GET /api/market-local/stats — 存储统计。 |
| `ROUTE /fetch` | `api_fetch()` | POST /api/market-local/fetch |
| `ROUTE /prune` | `api_prune()` | POST /api/market-local/prune — 手动清理过期数据。 |

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `login_required` | f | — | — |


## 📁 backtest

### `backtest_all_cnstock.py`
**路径:** `backtest/backtest_all_cnstock.py`
**说明:** ╔══════════════════════════════════════════════════════════════════╗

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `backtest_all` | indicator_id, user_id, user_params, review_mode, strategies, save_to_db, ... | — | 全A股多策略回测筛选，结果写入 qd_backtest_runs 表。 |


## 📁 utils

### `__init__.py`
**路径:** `utils/__init__.py`
**说明:** 工具模块

_无公开接口/类定义_

### `auth.py`
**路径:** `utils/auth.py`
**说明:** Authentication Utilities

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `generate_token` | user_id, username, role, token_version | — | Generate JWT token with user information. |
| func | `verify_token` | token | — | Verify JWT token and return payload. |
| func | `get_current_user_id` |  | — | Get current user ID from flask.g context |
| func | `get_current_user_role` |  | — | Get current user role from flask.g context |
| func | `login_required` | f | — | Decorator that enforces Bearer token auth. |
| func | `admin_required` | f | — | Decorator that requires admin role. |
| func | `manager_required` | f | — | Decorator that requires manager or admin role. |
| func | `permission_required` | permission | — | Decorator factory that checks for a specific permission. |
| func | `authenticate_legacy` | username, password | — | Legacy single-user authentication (for backward compatibility). |
| func | `decorated` |  | wraps | — |
| func | `decorated` |  | wraps | — |
| func | `decorated` |  | wraps | — |
| func | `decorator` | f | — | — |
| func | `decorated` |  | wraps | — |

### `basicinfo_db.py`
**路径:** `utils/basicinfo_db.py`
**说明:** basicinfo_db.py — A股全市场股票基本信息读写

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `StockBasicDB` |  | — | A 股股票基本信息读写器。 [methods: __init__(), ensure_table() — 确保 stock_basic_info 表存在（幂等）。, upsert_stocks() — 批量写入/更新股票信息（UPSERT）。, get_stock() — 根据代码或名称精确查询单只股票，返回全部字段。, get_all_stocks() — 查询全部股票（可按交易所和状态过滤）。] |
| func | `get_stock_basic_db` |  | — | 获取全局 StockBasicDB 单例（线程安全）。 |
| func | `__init__` |  | — | — |
| func | `ensure_table` |  | — | 确保 stock_basic_info 表存在（幂等）。 |
| func | `upsert_stocks` | stocks | — | 批量写入/更新股票信息（UPSERT）。 |
| func | `get_stock` | code_or_name | — | 根据代码或名称精确查询单只股票，返回全部字段。 |
| func | `get_all_stocks` | market_cn, status | — | 查询全部股票（可按交易所和状态过滤）。 |
| func | `search_stocks` | keyword, limit | — | A股智能搜索：精确代码 → 代码/名称模糊匹配 → 拼音首字母匹配。 |
| func | `market_all_string` | status | — | 获取全市场所有股票代码，加上小写交易所前缀，以逗号拼接返回。 |
| func | `market_all_codes` | status | — | 获取全市场所有股票代码（纯 6 位数字，无交易所前缀）。 |
| func | `get_stock_count` | market_cn | — | 获取股票总数（只计 active 状态）。 |
| func | `get_industries` |  | — | 获取所有不重复的行业列表（去重、排序）。 |
| func | `get_all_concepts` |  | — | 获取所有不重复的概念标签（去重、排序）。 |
| func | `get_stocks_by_concept` | concept, status | — | 按概念标签查询股票。 |
| func | `get_concept_stock_map` | status | — | 获取 概念→股票列表 的映射。 |
| func | `get_stats` |  | — | 获取 stock_basic_info 表的统计信息。 |
| func | `sync_from_remote` |  | — | 从远程数据源同步全量 A 股股票列表到 CNStock_db。 |
| func | `enrich_stock_info` | code | — | 补充单只股票的详情字段（行业、市值、市盈率等）。 |
| func | `close` |  | — | 关闭连接（本模块不持有独立连接池，此方法为兼容接口）。 |

### `cache.py`
**路径:** `utils/cache.py`
**说明:** Cache utilities.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `MemoryCache` |  | — | 内存缓存（Redis 不可用时的备选方案） [methods: __init__(), get(), setex(), delete(), clear()] |
| class | `CacheManager` |  | — | 缓存管理器 [methods: __init__(), get() — 获取缓存, set() — 设置缓存, delete() — 删除缓存, is_redis()] |
| func | `__init__` |  | — | — |
| func | `get` | key | — | — |
| func | `setex` | key, ttl, value | — | — |
| func | `delete` | key | — | — |
| func | `clear` |  | — | — |
| func | `__init__` |  | — | — |
| func | `get` | key | — | 获取缓存 |
| func | `set` | key, value, ttl | — | 设置缓存 |
| func | `delete` | key | — | 删除缓存 |
| func | `is_redis` |  | property | — |

### `cn_stock_info.py`
**路径:** `utils/cn_stock_info.py`
**说明:** cn_stock_info.py — A股个股基本面数据（纯 HTTP，双源互补）

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `fetch_sina` | code | — | 新浪财经 — 聚合。 |
| func | `fetch_tencent` | code | — | 腾讯 — 聚合。 |
| func | `get_cn_stock_info` | code | — | 获取 A 股个股全面基本面数据（双源互补）。 |

### `config_loader.py`
**路径:** `utils/config_loader.py`
**说明:** Config loader (local-only).

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `load_addon_config` |  | — | Build config from environment variables (.env / OS env). |
| func | `get_internal_api_key` |  | — | 获取内部API密钥（优先从环境变量读取） |
| func | `clear_config_cache` |  | — | 清除配置缓存（配置更新后调用） |
| func | `set_nested` | cfg, dotted_key, value | — | — |
| func | `env_get` | name | — | — |

### `credential_crypto.py`
**路径:** `utils/credential_crypto.py`
**说明:** Fernet encryption for qd_exchange_credentials.encrypted_config.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `encrypt_credential_blob` | plaintext_json | — | Encrypt JSON text for storage in encrypted_config. |
| func | `decrypt_credential_blob` | stored | — | Decrypt DB value to JSON text. Empty / None yields empty string. |

### `db.py`
**路径:** `utils/db.py`
**说明:** Database Connection Utility - PostgreSQL Only

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `get_db_type` |  | — | Get database type (always postgresql) |
| func | `is_postgres` |  | — | Check if using PostgreSQL (always True) |
| func | `init_database` |  | — | Initialize database connection. |
| func | `close_db_connection` |  | — | Legacy alias for close_db |

### `db_market.py`
**路径:** `utils/db_market.py`
**说明:** db_market.py — 多市场行情数据读写（上层）

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `MarketKlineWriter` |  | — | K线数据写入器。 [methods: __init__(), upsert() — 增量写入 K 线数据（UPSERT）。, bulk_write() — 大批量写入 K 线数据。, query() — 查询 K 线数据。, stats()] |
| func | `get_market_db_manager` |  | — | 获取全局 MarketDBManager 实例。 |
| func | `get_market_kline_writer` |  | — | — |
| func | `__init__` | manager | — | — |
| func | `upsert` | market, symbol, timeframe, records, atomic | — | 增量写入 K 线数据（UPSERT）。 |
| func | `bulk_write` | market, records, on_conflict, batch_size | — | 大批量写入 K 线数据。 |
| func | `query` | market, symbol, timeframe, start_time, end_time, limit | — | 查询 K 线数据。 |
| func | `stats` | market | — | — |

### `db_multi.py`
**路径:** `utils/db_multi.py`
**说明:** db_multi.py — 多市场数据库连接池与 DDL 管理（下层）

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `MarketPool` |  | — | 单个市场数据库的线程安全连接池 [methods: __init__(), connection() — 获取连接（上下文管理器，自动归还）, cursor() — 获取游标（上下文管理器，自动 commit/rollback）, close() — 关闭连接池] |
| class | `MarketDBManager` |  | — | 多市场数据库管理器。 [methods: __init__(), close_pool() — 关闭指定市场库的连接池, close_all_pools() — 关闭所有市场库连接池 + 管理员连接, close_admin_conn() — 关闭管理员连接, market_db_exists()] |
| func | `__init__` | db_name, params, minconn, maxconn | — | — |
| func | `connection` |  | contextmanager | 获取连接（上下文管理器，自动归还） |
| func | `cursor` |  | contextmanager | 获取游标（上下文管理器，自动 commit/rollback） |
| func | `close` |  | — | 关闭连接池 |
| func | `__init__` | base_conn_url, strategy_db_name | — | — |
| func | `close_pool` | market | — | 关闭指定市场库的连接池 |
| func | `close_all_pools` |  | — | 关闭所有市场库连接池 + 管理员连接 |
| func | `close_admin_conn` |  | — | 关闭管理员连接 |
| func | `market_db_exists` | market | — | — |
| func | `create_market_db` | market | — | — |
| func | `ensure_market_db` | market | — | — |
| func | `drop_market_db` | market | — | — |
| func | `list_market_dbs` |  | — | — |
| func | `ensure_year_table` | market, timeframe, year | — | — |
| func | `setup_fdw_from_strategy_db` | market | — | 从 strategy_db 建立 postgres_fdw 映射。 |

### `db_postgres.py`
**路径:** `utils/db_postgres.py`
**说明:** PostgreSQL Database Connection Utility

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `PostgresCursor` |  | — | PostgreSQL cursor wrapper with placeholder conversion for backward compatibility. [methods: __init__(), execute() — Execute SQL statement., insert_returning() — Execute an INSERT and return the new row's ``id``., fetchone() — Fetch single row., fetchall() — Fetch all rows] |
| class | `PostgresConnection` |  | — | PostgreSQL connection wrapper [methods: __init__(), cursor() — Create cursor, commit() — Commit transaction, rollback() — Rollback transaction, close() — Return connection to pool.  Broken connections are discarded so] |
| func | `get_pg_connection` |  | contextmanager | Get PostgreSQL database connection (Context Manager). |
| func | `get_pg_connection_sync` |  | — | Get connection synchronously (caller must close). |
| func | `execute_sql` | sql, params | — | Execute SQL and return results (convenience function) |
| func | `is_postgres_available` |  | — | Check if PostgreSQL is available |
| func | `close_pool` |  | — | Close connection pool (call on app shutdown) |
| func | `__init__` | cursor | — | — |
| func | `execute` | query, args | — | Execute SQL statement. |
| func | `insert_returning` | query, args | — | Execute an INSERT and return the new row's ``id``. |
| func | `fetchone` |  | — | Fetch single row. |
| func | `fetchall` |  | — | Fetch all rows |
| func | `executemany` | query, args_list | — | Execute SQL statement for multiple rows |
| func | `close` |  | — | Close cursor |
| func | `lastrowid` |  | property | Get last inserted row ID. |
| func | `returning_row` |  | property | The full row returned by ``RETURNING`` (if any). |
| func | `rowcount` |  | property | Get affected row count |
| func | `__init__` | conn | — | — |
| func | `cursor` |  | — | Create cursor |
| func | `commit` |  | — | Commit transaction |
| func | `rollback` |  | — | Rollback transaction |
| func | `close` |  | — | Return connection to pool.  Broken connections are discarded so |

### `http.py`
**路径:** `utils/http.py`
**说明:** HTTP 工具模块

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `get_retry_session` | retries, backoff_factor, status_forcelist | — | 获取带重试机制的 HTTP Session |

### `language.py`
**路径:** `utils/language.py`
**说明:** Language helpers (local-only).

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `detect_request_language` | flask_request, body, default | — | Detect language for the current request. |

### `logger.py`
**路径:** `utils/logger.py`
**说明:** Logging utilities (local-only friendly).

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `setup_logger` |  | — | 配置全局日志 |
| func | `get_logger` | name | — | 获取指定名称的日志记录器 |

### `pinyin_initials.py`
**路径:** `utils/pinyin_initials.py`
**说明:** Chinese character → pinyin initial mapping for A-share stock name search.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `pinyin_initials` | text | — | Convert Chinese text to pinyin initials. |

### `safe_exec.py`
**路径:** `utils/safe_exec.py`
**说明:** 安全的代码执行工具

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `TimeoutError` |  | — | 代码执行超时异常 |
| func | `build_safe_builtins` | extra_allowed | — | Build a restricted __builtins__ dict for sandboxed exec(). |
| func | `timeout_context` | seconds | contextmanager | 代码执行超时上下文管理器 |
| func | `safe_exec_code` | code, exec_globals, exec_locals, timeout, max_memory_mb | — | 安全执行Python代码（当前进程内，带超时） |
| func | `safe_exec_with_validation` | code, exec_globals, exec_locals, timeout, max_memory_mb, pre_import | — | Validate + execute user code in one call. |
| func | `safe_exec_isolated` | code, input_data, timeout, max_memory_mb | — | Execute user code in an isolated subprocess. |
| func | `validate_code_safety` | code | — | 验证代码安全性（正则 + AST 双重检查） |
| func | `safe_import` | name | — | — |
| func | `timeout_handler` | signum, frame | — | — |

### `strategy_runtime_logs.py`
**路径:** `utils/strategy_runtime_logs.py`
**说明:** Persist strategy runtime lines for the strategy management UI (`qd_strategy_logs`).

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `append_strategy_log` | strategy_id, level, message | — | Best-effort insert; never raises to caller. |

### `trading_calendar.py`
**路径:** `utils/trading_calendar.py`
**说明:** 交易日历模块

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `is_trading_day` | date | — | 判断是否为交易日 (YYYY-MM-DD 或 YYYYMMDD) |
| func | `is_trading_day_today` |  | — | — |
| func | `prev_trading_day` | date, n | — | 前 n 个交易日 |
| func | `next_trading_day` | date, n | — | 后 n 个交易日 |
| func | `last_finish_trading_day` | ref_dt | — | 返回已经结束的最近一个交易日。 |
| func | `trade_date_range` | start_date, end_date | — | 范围内的交易日列表 |
| func | `trading_days_count` | start_date, end_date | — | — |
| func | `is_business_day` | date | — | — |


## 📁 market_cn

### `__init__.py`
**路径:** `market_cn/__init__.py`
**说明:** 🇨🇳 china-market-tools — 中国金融市场分析工具箱

_无公开接口/类定义_

### `china_market.py`
**路径:** `market_cn/china_market.py`
**说明:** 国内市场宏观数据 — 统一数据入口

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `cache_get` | endpoint | — | 读缓存: 从内存字典取数据。 |
| func | `cache_is_stale` | endpoint | — | 检查缓存是否过期。 |
| func | `cache_put` | endpoint, data | — | 写缓存: 更新内存 + 触发文件保存。 |
| func | `get_china_macro` |  | — | 国内宏观经济: GDP, CPI, PPI, PMI, M2, 社融, 进出口, LPR |
| func | `get_fear_greed` |  | — | A股市场贪婪恐惧指数 (7维度综合) |
| func | `get_hot_sectors` | industry_limit, concept_limit | — | 热门板块 & 概念板块实时分析 |
| func | `get_sector_trend` | board_type | — | 板块1个月趋势 + 6个月周期 + 预测 |
| func | `get_sector_prediction` |  | — | 今日热门板块预测 |
| func | `get_sector_cycle` | board_type | — | 板块6个月周期分析 |
| func | `get_sector_stocks` | board_code, limit | — | 板块内个股详情（无缓存，实时查） |
| func | `get_sector_history` | board_type, days | — | 板块历史排名数据（无缓存，每次直接读 feather） |
| func | `get_emotion_history` | hours, date | — | 情绪指数历史数据（无缓存，每次直接读 feather） |
| func | `get_policy` |  | — | AI政策解读 — 直接调用，由 news.py PostgreSQL 缓存管理 |
| func | `refresh` | target | — | 强制刷新: 立即从远端拉取并更新缓存。 |

### `china_stock.py`
**路径:** `market_cn/china_stock.py`
**说明:** 中国金融数据统一获取层 — 多源降级

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `retry` | max_retries, delay | — | 重试装饰器 |
| func | `fallback` |  | — | 降级链: 按顺序尝试数据源，第一个成功即返回。 |
| func | `ts_gdp` |  | retry | Tushare: GDP |
| func | `ts_cpi` |  | retry | Tushare: CPI |
| func | `ts_ppi` |  | retry | Tushare: PPI |
| func | `ts_pmi` |  | retry | Tushare: PMI |
| func | `ts_m2` |  | retry | Tushare: M2 |
| func | `ts_money_supply` |  | retry | Tushare: 货币供应 |
| func | `ts_lpr` |  | retry | Tushare: LPR 利率 |
| func | `ts_index_daily` | symbol | retry | Tushare: 指数日线 |
| func | `ts_stock_daily` | ts_code | retry | Tushare: 个股日线 |
| func | `ts_stock_basic` |  | retry | Tushare: 全部A股列表 |
| func | `ts_northbound` |  | retry | Tushare: 北向资金 |
| func | `ak_gdp` |  | retry | AKShare: GDP |
| func | `ak_cpi` |  | retry | AKShare: CPI |
| func | `ak_ppi` |  | retry | AKShare: PPI |
| func | `ak_pmi` |  | retry | AKShare: PMI |
| func | `ak_m2` |  | retry | AKShare: M2 |
| func | `ak_index_daily` | code | retry | AKShare: 指数日线 |
| func | `ak_stock_daily` | code | retry | AKShare: 个股日线 |
| func | `ak_stock_basic` |  | retry | AKShare: A股列表 |
| func | `ak_northbound` |  | retry | AKShare: 北向资金 |
| func | `ak_lpr` |  | retry | AKShare: LPR |
| func | `ak_social_financing` |  | retry | AKShare: 社融 |
| func | `ak_trade` |  | retry | AKShare: 进出口 |
| func | `ak_news` |  | retry | AKShare: 财经新闻 |
| func | `bs_index_daily` | code | retry | BaoStock: 指数日线 |
| func | `bs_stock_daily` | code | retry | BaoStock: 个股日线 |
| func | `bs_stock_basic` |  | retry | BaoStock: A股列表 |
| func | `official_lpr` |  | retry | 中国人民银行: LPR 利率 (直接爬取) |
| func | `official_stats_gdp` |  | retry | 国家统计局: GDP 数据 (通过 API) |
| func | `official_stats_cpi` |  | retry | 国家统计局: CPI |
| func | `official_stats_pmi` |  | retry | 国家统计局: PMI |
| class | `ChinaData` |  | — | 中国金融数据统一入口，多源自动降级 [methods: __init__(), gdp() — GDP 季度数据, cpi() — CPI 月度, ppi() — PPI 月度, pmi() — PMI] |
| func | `decorator` | func | — | — |
| func | `wrapper` |  | — | — |
| func | `__init__` |  | — | — |
| func | `gdp` |  | — | GDP 季度数据 |
| func | `cpi` |  | — | CPI 月度 |
| func | `ppi` |  | — | PPI 月度 |
| func | `pmi` |  | — | PMI |
| func | `m2` |  | — | M2 货币供应 |
| func | `lpr` |  | — | LPR 利率 |
| func | `social_financing` |  | — | 社会融资规模 |
| func | `trade` |  | — | 进出口贸易 |
| func | `index_daily` | code | — | 指数日线 (沪深300) |
| func | `stock_daily` | code | — | 个股日线 |
| func | `stock_list` |  | — | 全A股列表 |
| func | `northbound` |  | — | 北向资金 |
| func | `news` |  | — | 财经新闻 |
| func | `wrapper` |  | wraps | — |

### `data_bridge.py`
**路径:** `market_cn/data_bridge.py`
**说明:** market_cn 数据桥接层 — 对外输出格式与 global_market 完全一致

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `get_macro_data` |  | — | A股宏观数据 → 与 global_market.get_sentiment() 同格式输出 |
| func | `fetch_sentiment` |  | — | A股情绪数据 → 与 global_market.get_sentiment() 同格式输出 |
| func | `fetch_overview` |  | — | A股概览数据 → 与 global_market.get_indices() 同格式输出 |

### `dragon_limit.py`
**路径:** `market_cn/dragon_limit.py`
**说明:** 龙虎榜 / 涨跌停池 / 炸板池 — 统一数据层

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `get_dragon_tiger` | start_date, end_date | — | 获取龙虎榜数据。HTTP 东财搜索优先，AkShare 兜底。 |
| func | `get_zt_pool` | trade_date | — | 获取涨停池。HTTP 东财搜索优先，AkShare 兜底。 |
| func | `get_dt_pool` | trade_date | — | 获取跌停池。HTTP 东财搜索优先，AkShare 兜底。 |
| func | `get_broken_board` | trade_date | — | 获取炸板池。HTTP 东财搜索优先，AkShare 兜底。 |
| func | `get_hot_rank` |  | — | 获取热榜。HTTP 东财搜索优先，AkShare 兜底。 |

### `eastmoney_search.py`
**路径:** `market_cn/eastmoney_search.py`
**说明:** 东财智能选股搜索 — 正本 (single source of truth)

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `search_stocks` | keyword, page_size, page_no, timeout | — | 调东财智能选股接口，返回标准化结果。 |

### `fear_greed_index.py`
**路径:** `market_cn/fear_greed_index.py`
**说明:** A股市场贪婪恐惧指数 — 简化版

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `fear_greed_index` |  | — | 计算综合贪恐指数，返回结构化结果 |

### `hot_sectors.py`
**路径:** `market_cn/hot_sectors.py`
**说明:** 热门板块 & 概念板块实时分析

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `get_hot_industry_boards` | limit | — | 获取热门行业板块（按涨幅排序） |
| func | `get_hot_concept_boards` | limit | — | 获取热门概念板块（按涨幅排序） |
| func | `get_sector_detail` | board_code, limit | — | 获取板块内强势个股 |
| func | `get_all_hot_sectors` | industry_limit, concept_limit | — | 获取全部热门板块数据（供 API 使用） |
| func | `decorator` | func | — | — |
| func | `wrapper` |  | wraps | — |

### `policy_analysis.py`
**路径:** `market_cn/policy_analysis.py`
**说明:** 最新政策解读抓取与分析

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `get_financial_news` |  | — | 获取财经要闻 |
| func | `get_macro_news` |  | — | 获取宏观要闻 (兼容新旧 AKShare 版本) |
| func | `get_policy_keywords` |  | — | 政策关键词扫描 — 从新闻标题中筛出政策相关 |
| func | `analyze_policy_impact` | titles | — | 简单的政策影响预判 (基于关键词) |
| func | `policy_dashboard` |  | — | 政策解读看板 |

### `sector_history.py`
**路径:** `market_cn/sector_history.py`
**说明:** 板块历史存储 + 趋势/周期/预测分析

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `SectorHistoryScheduler` |  | — | 每日收盘后采集板块排名数据 [methods: __init__(), start(), stop()] |
| class | `SectorAnalyzer` |  | — | 板块历史分析引擎 [methods: __init__(), full_analysis() — 完整分析报告] |
| func | `get_sector_trend` | db, board_type | — | 获取板块趋势分析 |
| func | `get_sector_history` | db, board_type, days | — | 获取板块历史排名（供前端图表使用） |
| func | `__init__` | db | — | — |
| func | `start` |  | — | — |
| func | `stop` |  | — | — |
| func | `__init__` | db | — | — |
| func | `full_analysis` | board_type | — | 完整分析报告 |


## 📁 market_cn/cards

### `__init__.py`
**路径:** `market_cn/cards/__init__.py`
**说明:** A股看板卡片自注册包

_无公开接口/类定义_

### `_base.py`
**路径:** `market_cn/cards/_base.py`
**说明:** 卡片自注册协议

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| class | `CardMeta` |  | dataclass | 卡片元数据 |
| func | `register` | meta, fetch_fn | — | 卡片模块 import 时调用，注册到全局表 |
| func | `get_all` |  | — | 返回所有已注册卡片 |
| func | `get_enabled` |  | — | 返回 enabled=True 的卡片，按 order 排序 |
| func | `get_meta_list` |  | — | 返回所有启用卡片的元数据（给前端 /cards 接口用） |

### `_hub_helper.py`
**路径:** `market_cn/cards/_hub_helper.py`
**说明:** Hub 辅助模块 — 统一获取 AShareDataHub，缺失时返回 None

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `get_hub` |  | — | 获取 AShareDataHub 实例，cn_stock_hub 未就绪时返回 None |

### `ai_analysis.py`
**路径:** `market_cn/cards/ai_analysis.py`
**说明:** AI市场分析卡片 — 综合市场数据生成分析结论（规则引擎，无需 LLM）

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `fetch` |  | — | — |

### `daily_scan_card.py`
**路径:** `market_cn/cards/daily_scan_card.py`
**说明:** 板块每日扫描卡片 — 脉冲检测 + 热点/衰减/异常分析

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `fetch` | quantile, limit | — | — |

### `dragon_tiger.py`
**路径:** `market_cn/cards/dragon_tiger.py`
**说明:** 龙虎榜卡片 — 数据来源: dragon_limit (HTTP 东财搜索 + AkShare 兜底)

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `fetch` |  | — | — |

### `emotion_cycle.py`
**路径:** `market_cn/cards/emotion_cycle.py`
**说明:** 情绪周期卡片 — 情绪指数历史折线图数据

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `fetch` |  | — | — |

### `hot_list.py`
**路径:** `market_cn/cards/hot_list.py`
**说明:** 同花顺热榜卡片 — 数据来源: 东财智能选股搜索

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `fetch` |  | — | — |

### `macro.py`
**路径:** `market_cn/cards/macro.py`
**说明:** 国内宏观数据卡片 — GDP/CPI/PPI/PMI/M2 + 贪婪恐惧 + 政策解读

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `fetch` |  | — | — |

### `overview.py`
**路径:** `market_cn/cards/overview.py`
**说明:** 市场总览卡片 — 顶部 8 小格（指数/涨跌停/北向/情绪等）

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `fetch` |  | — | — |

### `peripheral.py`
**路径:** `market_cn/cards/peripheral.py`
**说明:** 外围市场卡片 — 国际情绪指标 + 大宗商品

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `fetch` |  | — | — |

### `streak.py`
**路径:** `market_cn/cards/streak.py`
**说明:** 连板天梯卡片 — 数据来源: 东财智能选股搜索

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `fetch` |  | — | — |

### `strong_stocks.py`
**路径:** `market_cn/cards/strong_stocks.py`
**说明:** 强势股卡片 — 数据来源: 东财智能选股搜索

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `fetch` |  | — | — |


## 📁 data_providers

### `__init__.py`
**路径:** `data_providers/__init__.py`
**说明:** Unified data provider layer for global market data.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `get_cached` | key, ttl | — | Return cached data if not expired. |
| func | `set_cached` | key, data, ttl | — | Write a cache entry with the appropriate TTL. |
| func | `clear_cache` |  | — | Clear all cached data (used by /refresh endpoint). |

### `commodities.py`
**路径:** `data_providers/commodities.py`
**说明:** Commodity price data fetchers — 多源降级版.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `fetch_commodities` |  | — | Fetch commodity prices. 新浪 → 东财 → TwelveData → akshare → yfinance → Tiingo. |

### `crypto.py`
**路径:** `data_providers/crypto.py`
**说明:** Crypto price data fetchers with multi-source fallback.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `fetch_crypto_prices_ccxt` |  | — | Fetch crypto prices using CCXT (system's existing data source). |
| func | `fetch_crypto_prices_yfinance` |  | — | Fetch crypto prices using yfinance as fallback. |
| func | `fetch_crypto_prices` |  | — | Fetch top crypto prices — try CCXT → yfinance → CoinGecko. |
| func | `fetch_crypto_heatmap_coingecko` |  | — | Fetch crypto heatmap from CoinGecko with retry. |
| func | `fetch_crypto_heatmap_coincap` |  | — | Fetch crypto heatmap from CoinCap API (free, no key needed). |

### `forex.py`
**路径:** `data_providers/forex.py`
**说明:** Forex pair data fetchers — 多源降级版.

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `fetch_forex_pairs` |  | — | Fetch major forex pairs. 新浪 → 东财 → 腾讯 → TwelveData → akshare → yfinance → Tiingo. |

### `global_market.py`
**路径:** `data_providers/global_market.py`
**说明:** 国际市场宏观数据 — 统一数据入口

**公开类/函数:**

| 类型 | 名称 | 参数 | 装饰器 | 说明 |
|------|------|------|--------|------|
| func | `get_sentiment` |  | — | 7 个宏观情绪指标 + 大宗商品，自动缓存。 |
| func | `get_indices` |  | — | 全球股指 + 外汇 + 加密货币 |
| func | `get_heatmap` |  | — | — |
| func | `get_news` | lang | — | — |
| func | `refresh` | target | — | 手动刷新：强制从远端拉取并写入缓存，不走 _cached_fetch。 |


---
_扫描完成_
