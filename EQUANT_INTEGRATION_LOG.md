# eQuant Integration Work Log

## Date: 2026-04-18

## Task
Merge three features from [eQuant](https://github.com/yuguonet/equant) into [QuantDinger](https://github.com/brokermr810/QuantDinger):
1. **选股器 (Stock Screener)** - Stock screening/filtering tool
2. **市场看板 (Market Dashboard)** - A-share market overview with limit-up/down, dragon-tiger list, hot stocks
3. **AI智能体 (AI Agent)** - Conversational AI stock analysis agent

## Merge Rules Applied
1. ✅ Minimal changes to existing QuantDinger code - only extensions
2. ✅ QuantDinger's data sources preserved - equant features use QuantDinger's data source layer
3. ✅ Database reads/writes use QuantDinger's PostgreSQL; new tables added to QuantDinger's DB
4. ✅ AI analysis calls use QuantDinger's LLM service (LLMService)

## Changes Summary

### Backend Changes (backend_api_python/)

#### New Files Added:
- `app/interfaces/__init__.py` - A-share data interfaces module
- `app/interfaces/cn_stock_extent.py` - AShareDataHub (unified A-share data entry)
- `app/interfaces/cache_file.py` - Feather file storage for historical data
- `app/interfaces/realtime_cache.py` - In-memory TTL cache with singleflight
- `app/interfaces/index.py` - Market index interface
- `app/interfaces/dragon_tiger.py` - Dragon-tiger list interface
- `app/interfaces/hot_rank.py` - Hot/popularity ranking interface
- `app/interfaces/zt_pool.py` - Limit-up pool interface
- `app/interfaces/fund_flow.py` - Fund flow interface
- `app/interfaces/stock_info.py` - Stock info interface
- `app/interfaces/stock_fund_flow.py` - Stock fund flow interface
- `app/interfaces/market_snapshot.py` - Market snapshot interface
- `app/interfaces/limit_down.py` - Limit-down interface
- `app/interfaces/broken_board.py` - Broken board interface
- `app/interfaces/helpers.py` - Helper functions
- `app/data_sources/akshare_source.py` - Akshare data source
- `app/data_sources/eastmoney_source.py` - Eastmoney data source
- `app/data_sources/tushare_source.py` - Tushare data source
- `app/data_sources/baostock_source.py` - Baostock data source
- `app/utils/StockNLQ.py` - Natural language stock query engine
- `app/services/strategy_loader.py` - YAML strategy loader for AI agent
- `app/routes/xuangu.py` - Stock screener API endpoints
- `app/routes/shichang.py` - Market dashboard API endpoints
- `app/routes/agent_blueprint.py` - AI agent chat API endpoints
- `app/routes/agent_analysis.py` - AI analysis task management endpoints
- `app/routes/schemas/__init__.py` - Schemas module init
- `app/routes/schemas/analysis.py` - Pydantic models for analysis
- `strategies/*.yaml` - 11 YAML strategy files for AI agent
- `migrations/equant_features.sql` - Migration for new DB tables

#### Modified Files:
- `app/data_sources/base.py` - Added A-share specific methods to BaseDataSource (with default NotImplementedError)
- `app/routes/__init__.py` - Registered 4 new blueprints (xuangu, shichang, agent, agent_analysis)
- `requirements.txt` - Added: tushare, baostock, pyarrow, pydantic, PyYAML, beautifulsoup4

### Frontend Changes (frontend/)

#### New Files Added:
- `src/views/xuangu/index.vue` - Stock screener page (2707 lines)
- `src/views/shichang/index.vue` - Market dashboard page
- `src/views/shichang/DetailModal.vue` - Stock detail modal
- `src/views/shichang/DragonTigerCard.vue` - Dragon-tiger list card
- `src/views/shichang/HotListCard.vue` - Hot stocks card
- `src/views/shichang/StreakCard.vue` - Consecutive limit-up card
- `src/views/shichang/StrongStocksCard.vue` - Strong stocks card
- `src/views/ai-agent/index.vue` - AI agent chat page
- `src/views/ai-agent/components/ChatBubble.vue` - Chat message component
- `src/api/agent.js` - Agent API client functions

#### Modified Files:
- `src/config/router.config.js` - Added 3 new routes (/xuangu, /shichang, /ai-agent)
- `src/locales/lang/zh-CN.js` - Added Chinese menu translations
- `src/locales/lang/en-US.js` - Added English menu translations
- `package.json` - Added element-ui dependency (required by xuangu view)

## API Endpoints Added

### Stock Screener (选股器)
- `GET /api/xuangu/` - Proxy to eastmoney stock screener
- `POST /api/xuangu/search` - Search stocks with natural language / SQL filters
- `GET /api/xuangu/favorites` - Get saved screening strategies
- `POST /api/xuangu/save_strategy` - Save a screening strategy

### Market Dashboard (市场看板)
- `GET /api/shichang/` - Full market data (all sections combined)
- `GET /api/shichang/overview` - Market overview (indices, limit-up/down, emotion)
- `GET /api/shichang/streak` - Consecutive limit-up stocks
- `GET /api/shichang/dragon` - Dragon-tiger list
- `GET /api/shichang/hot` - Hot/popularity ranking
- `GET /api/shichang/strong` - Strong stocks (from limit-up pool)

### AI Agent (AI智能体)
- `GET /api/agent/strategies` - List available AI strategies
- `POST /api/agent/chat` - Synchronous AI chat
- `POST /api/agent/chat/stream` - Streaming AI chat (SSE)
- `GET /api/agent/chat/sessions` - List chat sessions
- `GET /api/agent/chat/sessions/<id>` - Get session messages
- `DELETE /api/agent/chat/sessions/<id>` - Delete session

### AI Analysis Tasks
- `POST /api/agent-analysis/analyze` - Trigger stock analysis (sync/async)
- `GET /api/agent-analysis/tasks` - List analysis tasks
- `GET /api/agent-analysis/tasks/stream` - SSE task status stream
- `GET /api/agent-analysis/status/<task_id>` - Get task status

## Database Migration

Run `migrations/equant_features.sql` to create new tables:
- `cnstock_selection` - Stock screening data
- `qd_user_strategies` - User saved screening strategies

## Build Instructions

### Backend
```bash
cd backend_api_python
pip install -r requirements.txt
# Run migration
psql $DATABASE_URL < migrations/equant_features.sql
python run.py
```

### Frontend
```bash
cd frontend
npm install
npm run serve    # Development
npm run build    # Production
```

## Notes
- The xuangu view uses ElementUI (added to package.json)
- A-share data sources (akshare, tushare, baostock) are conditionally imported
- The AI agent uses QuantDinger's existing LLMService for all LLM calls
- Historical data is stored in Feather format (pyarrow) in the data/ directory
