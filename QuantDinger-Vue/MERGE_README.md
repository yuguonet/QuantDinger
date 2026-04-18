# QuantDinger × equant 合并说明

## 合并日期
2026-04-18

## 合并内容
将 equant 项目的三个栏目合并到 QuantDinger 项目中：
1. **选股器** (xuangu)
2. **市场看板** (shichang)
3. **AI智能体** (ai-agent)

---

## 后端变更 (QuantDinger-Backend)

### 新增文件

| 文件 | 说明 |
|------|------|
| `app/routes/xuangu.py` | 选股器API（适配PostgreSQL） |
| `app/routes/shichang.py` | 市场看板API（总览/连板/龙虎榜/热榜/强势股） |
| `app/routes/agent_blueprint.py` | AI智能体聊天/SSE流式接口 |
| `app/routes/agent_analysis.py` | AI智能体分析任务接口 |
| `app/routes/schemas/analysis.py` | 分析任务Pydantic数据模型 |
| `app/interfaces/` | A股统一数据接口层（15个文件） |
| `app/interfaces/cn_stock_extent.py` | AShareDataHub入口 |
| `app/interfaces/zt_pool.py` | 涨停池接口 |
| `app/interfaces/limit_down.py` | 跌停池接口 |
| `app/interfaces/broken_board.py` | 炸板池接口 |
| `app/interfaces/dragon_tiger.py` | 龙虎榜接口 |
| `app/interfaces/hot_rank.py` | 热门排行接口 |
| `app/interfaces/market_snapshot.py` | 市场快照接口 |
| `app/interfaces/index.py` | 指数行情接口 |
| `app/interfaces/fund_flow.py` | 资金流向接口 |
| `app/interfaces/stock_info.py` | 股票信息接口 |
| `app/interfaces/stock_fund_flow.py` | 个股资金流向 |
| `app/interfaces/cache_file.py` | Feather数据存储管理 |
| `app/interfaces/realtime_cache.py` | 实时数据内存缓存 |
| `app/interfaces/helpers.py` | 工具函数 |
| `app/data_sources/eastmoney_source.py` | 东方财富数据源 |
| `app/data_sources/akshare_source.py` | AKShare数据源（市场看板专用） |
| `app/data_sources/tushare_source.py` | Tushare数据源 |
| `app/services/strategy_loader.py` | YAML策略加载器 |
| `strategies/*.yaml` | 11个AI分析策略文件 |

### 修改文件

| 文件 | 变更说明 |
|------|----------|
| `app/routes/__init__.py` | 注册4个新Blueprint |
| `app/data_sources/base.py` | 添加12个equant兼容方法 + log_result兼容 |
| `migrations/init.sql` | 新增cnstock_selection和user_strategies表 |
| `requirements.txt` | 新增pyarrow、PyYAML、beautifulsoup4 |

### 新增API端点

```
# 市场看板
GET  /api/shichang/          # 汇总数据
GET  /api/shichang/overview  # 市场总览
GET  /api/shichang/streak    # 连板数据
GET  /api/shichang/dragon    # 龙虎榜
GET  /api/shichang/hot       # 热榜
GET  /api/shichang/strong    # 强势股

# 选股器
POST /api/xuangu/search         # 选股搜索
GET  /api/xuangu/favorites      # 收藏策略
POST /api/xuangu/save_strategy  # 保存策略

# AI智能体
GET  /api/agent/strategies                # 获取策略列表
POST /api/agent/chat                      # 普通聊天
POST /api/agent/chat/stream               # 流式聊天(SSE)
GET  /api/agent/chat/sessions             # 会话列表
GET  /api/agent/chat/sessions/<id>        # 会话消息
DELETE /api/agent/chat/sessions/<id>      # 删除会话

# AI分析任务
POST /api/agent-analysis/analyze           # 触发分析
GET  /api/agent-analysis/tasks             # 任务列表
GET  /api/agent-analysis/tasks/stream      # 任务SSE流
GET  /api/agent-analysis/status/<task_id>  # 任务状态
```

### 合并规则遵循

1. ✅ 最小改动QuantDinger原有代码，只作功能扩展
2. ✅ eastmoney_source独立存在（无get_kline方法，不并入数据源工厂）
3. ✅ QuantDinger市场数据源保持不变，equant接口使用自己的数据源
4. ✅ xuangu使用QuantDinger的PostgreSQL数据库（cnstock_selection表）
5. ✅ AI分析调用QuantDinger的LLMService和DataSourceFactory

---

## 前端变更 (QuantDinger-Vue)

### 新增文件

| 文件 | 说明 |
|------|------|
| `src/views/xuangu/index.vue` | 选股器页面（2707行，含筛选器+结果表格） |
| `src/views/shichang/index.vue` | 市场看板主页面 |
| `src/views/shichang/StreakCard.vue` | 连板天梯组件 |
| `src/views/shichang/DragonTigerCard.vue` | 龙虎榜组件 |
| `src/views/shichang/HotListCard.vue` | 同花顺热榜组件 |
| `src/views/shichang/StrongStocksCard.vue` | 强势股组件 |
| `src/views/shichang/DetailModal.vue` | 详情弹窗组件 |
| `src/views/ai-agent/index.vue` | AI智能体聊天页面 |
| `src/views/ai-agent/components/ChatBubble.vue` | 聊天气泡组件 |
| `src/api/agent.js` | AI智能体API模块（含SSE工具函数） |

### 修改文件

| 文件 | 变更说明 |
|------|----------|
| `src/config/router.config.js` | 添加3个新路由（xuangu/shichang/ai-agent） |
| `src/locales/lang/zh-CN.js` | 添加中文翻译 |
| `src/locales/lang/en-US.js` | 添加英文翻译 |
| `package.json` | 新增element-ui ^2.15.14依赖 |

### 新增菜单项

- **选股器** (filter图标) → /xuangu
- **市场看板** (dashboard图标) → /shichang
- **AI智能体** (robot图标) → /ai-agent

### 注意事项

- xuangu页面使用Element UI（与QuantDinger-Vue的Ant Design Vue共存）
- shichang和ai-agent页面使用Ant Design Vue
- 前端代理已配置：`/api` → `http://localhost:5000`

---

## 运行方式

### 后端

```bash
cd QuantDinger-Backend/backend_api_python
pip install -r requirements.txt
# 确保PostgreSQL已启动且DATABASE_URL已配置
python run.py
```

### 前端

```bash
cd QuantDinger-Vue
npm install
npm run serve
```

### 数据库

首次启动会自动执行 `migrations/init.sql`，包含新增的选股器表：
- `cnstock_selection` — 股票筛选数据
- `user_strategies` — 用户收藏策略

---

## 目录结构

```
QuantDinger-Backend/
├── backend_api_python/
│   ├── app/
│   │   ├── routes/
│   │   │   ├── xuangu.py          ← 新增
│   │   │   ├── shichang.py        ← 新增
│   │   │   ├── agent_blueprint.py ← 新增
│   │   │   ├── agent_analysis.py  ← 新增
│   │   │   └── schemas/           ← 新增
│   │   ├── interfaces/            ← 新增 (15个文件)
│   │   ├── data_sources/
│   │   │   ├── eastmoney_source.py ← 新增
│   │   │   ├── akshare_source.py   ← 新增
│   │   │   ├── tushare_source.py   ← 新增
│   │   │   └── base.py            ← 修改
│   │   ├── services/
│   │   │   └── strategy_loader.py ← 新增
│   │   └── ...
│   ├── strategies/                ← 新增 (11个YAML)
│   └── migrations/
│       └── init.sql               ← 修改

QuantDinger-Vue/
├── src/
│   ├── views/
│   │   ├── xuangu/                ← 新增
│   │   ├── shichang/              ← 新增 (6个组件)
│   │   └── ai-agent/              ← 新增 (2个文件)
│   ├── api/
│   │   └── agent.js               ← 新增
│   ├── config/
│   │   └── router.config.js       ← 修改
│   └── locales/
│       └── lang/
│           ├── zh-CN.js           ← 修改
│           └── en-US.js           ← 修改
└── package.json                   ← 修改
```
