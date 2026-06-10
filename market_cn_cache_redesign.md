# market_cn 缓存改造设计

> 日期: 2026-06-10
> 范围: market_cn 模块（不含 K 线 / data_sources / data_providers / tape.py）
> 原则: 读写分离 — get_xxx() 只读缓存，refresh_xxx() 负责拉取写缓存

## 一、改造思路

### 极简方案

每个文件内部**新增** `refresh_xxx()` 函数，原有 `get_xxx()` **一字不改**：

```python
# get_xxx() — 原有逻辑完全不动，含同步兜底、文件缓存等全部保留
def get_xxx():
    ...  # 原代码一字不改

# refresh_xxx() — 新增，拉远端写内存缓存（scheduler 自调度调用）
_xxx_cache = None  # 新增内存缓存变量

def refresh_xxx():
    global _xxx_cache
    _xxx_cache = 原来的拉取逻辑（从 get_xxx 中提取出来，或直接复用内部 _fetch 函数）
```

- **不改任何对外函数**（get_xxx / fetch_xxx 签名、返回格式不变，外部调用无感）
- **内部函数可按需调整**（如拆分拉取逻辑、加缓存写入等，不影响外部调用方）
- **不新建模块**（scheduler.py 是唯一新增文件）
- **只新增**：内存缓存变量 + `refresh_xxx()` 写接口 + `scheduler.py` 定时调度
- **后台只负责定时调 refresh_xxx()，不管成功失败**

### ⚠️ 读接口不动

**现有 get_xxx() 一字不改** — 原文件中读取数据的接口是什么样就是什么样，不动。
它们继续走原有逻辑（同步拉取 / 文件缓存 / 降级等），不受 scheduler 影响。

### ⚠️ 远端拉取最大化策略

**refresh 不知道上游要多少 bar，所以需要峰值跟踪。**

原则：
- get_xxx() 记录上游实际请求的 bar 数（峰值跟踪）
- refresh 取 `max(默认值, 峰值 * 1.5)` 从远端拉取，默认值尽量大，尽量不触发峰值
- get_xxx() 优先读缓存，缓存够直接返回，不够才走远端 fallback
- 原有远端逻辑一字不改，只是从"默认路径"降级为"缓存不够时的 fallback"

**自适应缓存闭环：**

```
冷启动（缓存空）: get_index_daily_kline(code, days=200)
  → 缓存空，走远端 fallback 拿 200 条
  → 峰值记录: _rt_max_idx_daily_days = 200

第 1 次 refresh（定时触发）:
  → fetch_days = max(500, 200) = 500
  → 从远端拉 500 条写入缓存

后续调用: get_index_daily_kline(code, days=200)
  → 缓存有数据，直接返回，0 网络请求

如果上游要更大: get_index_daily_kline(code, days=800)
  → 缓存没有（或不够），走远端 fallback 拿 800 条
  → 峰值更新: _rt_max_idx_daily_days = 800

下次 refresh:
  → fetch_days = max(500, 800) = 800
  → 缓存补满
```

get_xxx() 改动示例：

```python
_rt_max_idx_daily_days = 0   # 峰值记录（文件顶部）
_rt_idx_daily = {}           # 内存缓存（文件顶部）

def get_index_daily_kline(code="000001", days=200):
    global _rt_max_idx_daily_days
    _rt_max_idx_daily_days = max(_rt_max_idx_daily_days, days)  # ① 峰值记录

    cached = _rt_idx_daily.get(code)                             # ② 读缓存
    if cached and len(cached) >= days:
        return cached[:days]

    ... 原有 requests.get 逻辑一字不改 ...                        # ③ fallback
```

refresh 侧：
```python
def refresh_index_daily_kline():
    fetch_days = max(500, int(_rt_max_idx_daily_days * 1.5))  # 默认值尽量大，峰值兜底
    for code in INDEX_CODES:
        data = get_index_daily_kline(code, fetch_days)
        if data:
            _rt_idx_daily[code] = data
```

**关键：原有远端逻辑一字不改，只是从"默认路径"降级为"缓存不够时的 fallback"。**

## 二、数据分类

### 日级档 — 启动加载 + 盘后 15:30 自动刷新

| 函数 | 模块 | 说明 |
|------|------|------|
| `get_china_macro` | china_market | GDP/CPI/PMI 等宏观 |
| `get_sector_trend` | china_market | 板块趋势+周期 |
| `get_sector_prediction` | china_market | 板块预测 |
| `get_sector_cycle` | china_market | 板块周期分析 |
| `get_index_daily_kline` | index | 指数日K |
| `get_northbound_daily` | index | 北向日级历史 |
| `get_northbound_holdings` | index | 北向持股明细 |
| `get_market_fund_flow_daily` | index | 资金流日级 |
| `get_dragon_tiger` | dragon_limit | 龙虎榜 |
| `get_zt_pool` | dragon_limit | 涨停池 |
| `get_dt_pool` | dragon_limit | 跌停池 |
| `get_broken_board` | dragon_limit | 炸板池 |
| `get_emotion_history` | emotion | 情绪历史 |
| `get_sector_history` | china_market | 板块历史趋势 |
| `fetch_vix` | data_providers/sentiment | VIX 恐慌指数 |
| `fetch_dollar_index` | data_providers/sentiment | 美元指数 |
| `fetch_yield_curve` | data_providers/sentiment | 美债收益率曲线 |
| `fetch_put_call_ratio` | data_providers/sentiment | Put/Call 比率 |
| `fetch_commodities` | data_providers/commodities | 大宗商品（黄金/原油/铜） |
| `fetch_forex_pairs` | data_providers/forex | 主要外汇对 |

### 盘中慢档 — 30 分钟

| 函数 | 模块 |
|------|------|
| `get_fear_greed` | china_market |
| `fetch_emotion_cycle` | emotion |
| `get_policy` | china_market |
| `get_financial_news` | policy_analysis |
| `get_macro_news` | policy_analysis |
| `fetch_fear_greed_index` | data_providers/sentiment | CNN 恐慌贪婪指数 |
| `get_sentiment_data` | data_providers/sentiment | 综合情绪数据 |
| `get_sentiment` | data_providers/global_market | 全球市场情绪 |
| `get_news` | data_providers/global_market | 全球财经新闻 |
| `fetch_crypto_prices` | data_providers/crypto | 加密货币行情 |
| `fetch_crypto_heatmap` | data_providers/crypto | 加密热力图 |

### 盘中快档 — 5~10 分钟

| 函数 | 模块 |
|------|------|
| `get_index_realtime` | index |
| `get_northbound_realtime` | index |
| `get_hot_sectors` | china_market |
| `get_market_fund_flow_realtime` | index |
| `get_sector_fund_flow` | index |
| `get_hot_rank` | dragon_limit |
| `get_emotion_latest` | emotion |
| `get_indices` | data_providers/global_market | 全球主要股指 |
| `get_heatmap` | data_providers/global_market | 全球市场热力图 |

### 不动

| 模块 | 原因 |
|------|------|
| tape.py | 实时盘口，不缓存 |
| finance.py | 个股财务，按需查 |
| cards/*.py | 调用方，接口不变 |
| data_sources/coordinator.py | K 线已完善，不动 |
| data_bridge.py | 调用方，接口不变 |

---

## 三、各文件具体改动

### 3.1 china_market.py

已有 `_cache_store` + `cache_get/cache_put` 机制，改动最小。

**改动：get_xxx() 加缓存优先读取 + 峰值记录，原有逻辑降级为 fallback**

```python
# ═══ 新增变量（文件顶部）═══
_rt_max_sector_trend_days = {}    # 峰值记录 {board_type: days}
_rt_max_sector_cycle_days = {}    # 峰值记录 {board_type: days}
_rt_max_sector_history_days = 0   # 峰值记录
_rt_max_emotion_history_hours = 0 # 峰值记录

# ═══ get_xxx() — 加缓存优先 + 峰值记录，原有逻辑 fallback ═══

def get_sector_trend(board_type="industry") -> dict:
    _rt_max_sector_trend_days[board_type] = max(_rt_max_sector_trend_days.get(board_type, 0), 30)  # ① 峰值
    cached = _rt_sector_trend.get(board_type)                              # ② 缓存
    if cached:
        return cached
    ... 原有远端拉取逻辑一字不改 ...                                                                 # ③ fallback

def get_sector_cycle(board_type="industry") -> dict:
    _rt_max_sector_cycle_days[board_type] = max(_rt_max_sector_cycle_days.get(board_type, 0), 180)
    cached = _rt_sector_cycle.get(board_type)
    if cached:
        return cached
    ... 原有远端拉取逻辑一字不改 ...

def get_sector_history(board_type="industry", days=30) -> dict:
    global _rt_max_sector_history_days
    _rt_max_sector_history_days = max(_rt_max_sector_history_days, days)
    cached = _rt_sector_history.get(board_type)
    if cached:
        return cached
    ... 原有远端拉取逻辑一字不改 ...

def get_emotion_history(hours=None, date=None) -> dict:
    if hours:
        global _rt_max_emotion_history_hours
        _rt_max_emotion_history_hours = max(_rt_max_emotion_history_hours, hours)
    if _rt_emotion_history:
        return _rt_emotion_history
    ... 原有远端拉取逻辑一字不改 ...

# get_china_macro / get_fear_greed / get_hot_sectors / get_sector_prediction
# get_sector_stocks / get_policy — 无动态参数，只加缓存优先读取，不加峰值记录
def get_china_macro() -> dict:
    if _rt_china_macro:                    # ① 缓存
        return _rt_china_macro
    ... 原有远端拉取逻辑一字不改 ...       # ② fallback

def get_fear_greed() -> dict:
    if _rt_fear_greed:
        return _rt_fear_greed
    ... 原有远端拉取逻辑一字不改 ...

def get_hot_sectors(industry_limit=15, concept_limit=15) -> dict:
    if _rt_hot_sectors:
        return _rt_hot_sectors
    ... 原有远端拉取逻辑一字不改 ...

def get_sector_prediction() -> dict:
    if _rt_sector_prediction:
        return _rt_sector_prediction
    ... 原有远端拉取逻辑一字不改 ...

def get_policy() -> dict:
    if _rt_policy:
        return _rt_policy
    ... 原有远端拉取逻辑一字不改 ...
```

**不动：`_bg_watchdog` 和 `_warmup()` 保留原样**

**新增：内存缓存变量 + refresh_xxx() 函数**

```python
# ═══ 新增内存缓存变量（独立于原有 _cache_store）═══
_rt_china_macro = None
_rt_fear_greed = None
_rt_hot_sectors = None
_rt_sector_trend = {}       # {board_type: data}
_rt_sector_prediction = None
_rt_sector_cycle = {}       # {board_type: data}
_rt_sector_stocks = {}      # {board_code: data}
_rt_sector_history = {}     # {board_type: data}
_rt_emotion_history = None
_rt_policy = None

# ═══ 新增 refresh_xxx()（scheduler 调用）═══

def refresh_china_macro():
    global _rt_china_macro
    try:
        _rt_china_macro = get_china_macro()
    except Exception as e:
        logger.warning("[refresh] china_macro 失败: %s", e)

def refresh_fear_greed():
    global _rt_fear_greed
    try:
        _rt_fear_greed = get_fear_greed()
    except Exception as e:
        logger.warning("[refresh] fear_greed 失败: %s", e)

def refresh_hot_sectors():
    global _rt_hot_sectors
    try:
        _rt_hot_sectors = get_hot_sectors()
    except Exception as e:
        logger.warning("[refresh] hot_sectors 失败: %s", e)

def refresh_sector_trend(board_type="industry"):
    global _rt_sector_trend
    try:
        _rt_sector_trend[board_type] = get_sector_trend(board_type)
    except Exception as e:
        logger.warning("[refresh] sector_trend(%s) 失败: %s", board_type, e)

def refresh_sector_prediction():
    global _rt_sector_prediction
    try:
        _rt_sector_prediction = get_sector_prediction()
    except Exception as e:
        logger.warning("[refresh] sector_prediction 失败: %s", e)

def refresh_sector_cycle(board_type="industry"):
    global _rt_sector_cycle
    try:
        _rt_sector_cycle[board_type] = get_sector_cycle(board_type)
    except Exception as e:
        logger.warning("[refresh] sector_cycle(%s) 失败: %s", board_type, e)

def refresh_sector_stocks(board_code="BK0475", limit=30):
    global _rt_sector_stocks
    try:
        _rt_sector_stocks[board_code] = get_sector_stocks(board_code, limit)
    except Exception as e:
        logger.warning("[refresh] sector_stocks(%s) 失败: %s", board_code, e)

def refresh_sector_history(board_type="industry"):
    global _rt_sector_history
    try:
        fetch_days = max(250, int(_rt_max_sector_history_days * 1.5))
        _rt_sector_history[board_type] = get_sector_history(board_type, fetch_days)
    except Exception as e:
        logger.warning("[refresh] sector_history(%s) 失败: %s", board_type, e)

def refresh_emotion_history():
    global _rt_emotion_history
    try:
        fetch_hours = max(48, int(_rt_max_emotion_history_hours * 1.5)) if _rt_max_emotion_history_hours > 0 else 48
        _rt_emotion_history = get_emotion_history(hours=fetch_hours)
    except Exception as e:
        logger.warning("[refresh] emotion_history 失败: %s", e)

def refresh_policy():
    global _rt_policy
    try:
        _rt_policy = get_policy()
    except Exception as e:
        logger.warning("[refresh] policy 失败: %s", e)
```

def refresh_policy():
    global _rt_policy
    try:
        _rt_policy = get_policy()
    except Exception as e:
        logger.warning("[refresh] policy 失败: %s", e)
```

**说明**：`refresh_xxx()` 内部直接调用原有 `get_xxx()`，拿到结果写入 `_rt_xxx` 内存缓存。原有函数完全不动。

---

### 3.2 index.py

当前状态：**完全没有缓存**，所有函数直接 requests.get。

**改动：get_xxx() 加缓存优先读取 + 峰值记录，原有逻辑降级为 fallback**

```python
# ═══ 新增变量（文件顶部）═══
_rt_max_idx_daily_days = 0
_rt_max_nb_daily_days = 0
_rt_max_nb_holdings_top = 0
_rt_max_mf_daily_days = 0

# ═══ get_xxx() — 加缓存优先 + 峰值记录，原有逻辑 fallback ═══

def get_index_daily_kline(code="000001", days=200):
    global _rt_max_idx_daily_days
    _rt_max_idx_daily_days = max(_rt_max_idx_daily_days, days)       # ① 峰值
    cached = _rt_idx_daily.get(code)                 # ② 缓存
    if cached and len(cached) >= days:
        return cached[:days]
    ... 原有 requests.get 逻辑一字不改 ...                             # ③ fallback

def get_northbound_daily(days=120):
    global _rt_max_nb_daily_days
    _rt_max_nb_daily_days = max(_rt_max_nb_daily_days, days)
    if _rt_nb_daily and len(_rt_nb_daily) >= days:
        return _rt_nb_daily[:days]
    ... 原有远端拉取逻辑一字不改 ...

def get_northbound_holdings(top=50):
    global _rt_max_nb_holdings_top
    _rt_max_nb_holdings_top = max(_rt_max_nb_holdings_top, top)
    if _rt_nb_holdings and len(_rt_nb_holdings) >= top:
        return _rt_nb_holdings[:top]
    ... 原有远端拉取逻辑一字不改 ...

def get_market_fund_flow_daily(days=120):
    global _rt_max_mf_daily_days
    _rt_max_mf_daily_days = max(_rt_max_mf_daily_days, days)
    if _rt_mf_daily and len(_rt_mf_daily) >= days:
        return _rt_mf_daily[:days]
    ... 原有远端拉取逻辑一字不改 ...

# 无动态参数的函数 — 只加缓存优先，不加峰值记录
def get_index_realtime(codes=None):
    if _rt_idx_realtime:                             # 缓存
        return _rt_idx_realtime
    ... 原有远端拉取逻辑一字不改 ...                                    # fallback

def get_northbound_realtime():
    if _rt_nb_realtime:
        return _rt_nb_realtime
    ... 原有远端拉取逻辑一字不改 ...

def get_market_fund_flow_realtime():
    if _rt_mf_realtime:
        return _rt_mf_realtime
    ... 原有远端拉取逻辑一字不改 ...

def get_sector_fund_flow(indicator="今日"):
    if _rt_sector_flow:
        return _rt_sector_flow
    ... 原有远端拉取逻辑一字不改 ...
```

**新增：内存缓存变量 + refresh_xxx() 写接口**

```python
# ═══ 新增内存缓存变量（文件顶部）═══
_rt_idx_realtime = None
_rt_idx_daily = {}            # {code: [kline_data]}
_rt_nb_realtime = None
_rt_nb_daily = None
_rt_nb_holdings = None
_rt_mf_realtime = None
_rt_mf_daily = None
_rt_sector_flow = None

# ═══ 新增 refresh_xxx()（scheduler 调用）═══

def refresh_index_realtime():
    """拉取指数实时行情写入内存缓存"""
    global _rt_idx_realtime
    try:
        _rt_idx_realtime = get_index_realtime()
    except Exception as e:
        logger.warning("[refresh] index_realtime 失败: %s", e)

def refresh_index_daily_kline():
    """拉取主要指数日K写入内存缓存（拉取量 >= 消费峰值）"""
    global _rt_idx_daily
    try:
        INDEX_CODES = ["000001", "399001", "399006", "000300"]
        fetch_days = max(500, int(_rt_max_idx_daily_days * 1.5))
        for code in INDEX_CODES:
            data = get_index_daily_kline(code, fetch_days)
            if data:
                _rt_idx_daily[code] = data
    except Exception as e:
        logger.warning("[refresh] index_daily_kline 失败: %s", e)

def refresh_northbound_realtime():
    global _rt_nb_realtime
    try:
        _rt_nb_realtime = get_northbound_realtime()
    except Exception as e:
        logger.warning("[refresh] northbound_realtime 失败: %s", e)

def refresh_northbound_daily():
    """拉取北向日级数据（拉取量 >= 消费峰值）"""
    global _rt_nb_daily
    try:
        fetch_days = max(250, int(_rt_max_nb_daily_days * 1.5))
        _rt_nb_daily = get_northbound_daily(fetch_days)
    except Exception as e:
        logger.warning("[refresh] northbound_daily 失败: %s", e)

def refresh_northbound_holdings():
    """拉取北向持股明细（拉取量 >= 消费峰值）"""
    global _rt_nb_holdings
    try:
        fetch_top = max(100, int(_rt_max_nb_holdings_top * 1.5))
        _rt_nb_holdings = get_northbound_holdings(fetch_top)
    except Exception as e:
        logger.warning("[refresh] northbound_holdings 失败: %s", e)

def refresh_market_fund_flow_realtime():
    global _rt_mf_realtime
    try:
        _rt_mf_realtime = get_market_fund_flow_realtime()
    except Exception as e:
        logger.warning("[refresh] market_fund_flow_realtime 失败: %s", e)

def refresh_market_fund_flow_daily():
    """拉取资金流日级数据（拉取量 >= 消费峰值）"""
    global _rt_mf_daily
    try:
        fetch_days = max(250, int(_rt_max_mf_daily_days * 1.5))
        _rt_mf_daily = get_market_fund_flow_daily(fetch_days)
    except Exception as e:
        logger.warning("[refresh] market_fund_flow_daily 失败: %s", e)

def refresh_sector_fund_flow():
    global _rt_sector_flow
    try:
        _rt_sector_flow = get_sector_fund_flow("今日")
    except Exception as e:
        logger.warning("[refresh] sector_fund_flow 失败: %s", e)
```

**说明**：
- `refresh_xxx()` 内部直接调用原有 `get_xxx()`，拿到结果写入 `_rt_xxx` 内存缓存
- **拉取量用保守默认值**（300 天 / 250 天 / top 100），远超 get_xxx() 的消费量（200 / 120 / 50）
- 原有 get_xxx() 函数完全不动

---

### 3.3 emotion.py

当前状态：有自己的 JSON 文件缓存，但 `fetch_emotion_cycle()` 每次仍会 requests.get。

**改动：get_xxx() / fetch_xxx() 加缓存优先读取，原有逻辑降级为 fallback**

```python
# ═══ 新增内存缓存变量 ═══
_rt_emotion_cycle = None

# ═══ get_xxx() / fetch_xxx() — 加缓存优先，原有逻辑 fallback ═══

def fetch_emotion_cycle():
    if _rt_emotion_cycle:                    # ① 缓存
        return _rt_emotion_cycle
    ... 原有 requests.get 逻辑一字不改 ...                # ② fallback

def get_emotion_latest():
    if _rt_emotion_cycle is not None:                    # 缓存优先
        return _rt_emotion_cycle
    ... 原有逻辑一字不改 ...

# get_emotion_history — 已有文件缓存，不动
```

---

### 3.4 dragon_limit.py

当前状态：每次调用都直接 HTTP 请求或 AkShare 拉取。

**改动：get_xxx() 加缓存优先读取，原有逻辑降级为 fallback**

```python
# ═══ 新增内存缓存变量 ═══
_rt_dragon_tiger = None
_rt_zt_pool = None
_rt_dt_pool = None
_rt_broken_board = None
_rt_hot_rank = None

# ═══ get_xxx() — 加缓存优先，原有逻辑 fallback ═══

def get_dragon_tiger(start_date="", end_date=""):
    if _rt_dragon_tiger:                          # ① 缓存
        return _rt_dragon_tiger
    ... 原有 AkShare/HTTP 拉取逻辑一字不改 ...                  # ② fallback

def get_zt_pool(trade_date=""):
    if _rt_zt_pool:
        return _rt_zt_pool
    ... 原有远端拉取逻辑一字不改 ...

def get_dt_pool(trade_date=""):
    if _rt_dt_pool:
        return _rt_dt_pool
    ... 原有远端拉取逻辑一字不改 ...

def get_broken_board(trade_date=""):
    if _rt_broken_board:
        return _rt_broken_board
    ... 原有远端拉取逻辑一字不改 ...

def get_hot_rank():
    if _rt_hot_rank:
        return _rt_hot_rank
    ... 原有远端拉取逻辑一字不改 ...
```

---

### 3.5 hot_sectors.py

当前状态：`get_all_hot_sectors()` 每次都拉东财/新浪 API。

**不动：所有 get_xxx() 函数一字不改**

```python
# get_all_hot_sectors / get_hot_industry_boards / get_hot_concept_boards
# get_sector_detail — 全部原样保留
```

**新增：内存缓存变量 + refresh_xxx() 写接口**

```python
# ═══ 新增内存缓存变量 ═══
_rt_hot_sectors = None

# ═══ 新增写接口（scheduler 调用）═══

def refresh_hot_sectors():
    global _rt_hot_sectors
    try:
        _rt_hot_sectors = get_all_hot_sectors()
    except Exception as e:
        logger.warning("[refresh] hot_sectors 失败: %s", e)
```

**说明**：`get_sector_detail(board_code, limit)` 是实时查个股，不缓存，保持不变。

---

### 3.6 policy_analysis.py

当前状态：每次调用都拉 AkShare/东财，无缓存，且函数内有 print 语句（不适合 API 调用）。

**不动：所有 get_xxx() 函数一字不改**

```python
# get_financial_news / get_macro_news — 全部原样保留
```

**新增：内存缓存变量 + refresh_xxx() 写接口**

```python
# ═══ 新增内存缓存变量 ═══
_rt_financial_news = None
_rt_macro_news = None

# ═══ 新增写接口（scheduler 调用）═══

def refresh_financial_news():
    global _rt_financial_news
    try:
        _rt_financial_news = get_financial_news()
    except Exception as e:
        logger.warning("[refresh] financial_news 失败: %s", e)

def refresh_macro_news():
    global _rt_macro_news
    try:
        _rt_macro_news = get_macro_news()
    except Exception as e:
        logger.warning("[refresh] macro_news 失败: %s", e)
```

---

### 3.7 data_providers/sentiment.py（国际恐慌/宏观指标）

当前状态：每次调用都直接 HTTP 拉取，无缓存。

**不动：所有 fetch_xxx() 函数一字不改**

**新增：内存缓存变量 + refresh_xxx() 写接口**

```python
# ═══ 新增内存缓存变量 ═══
_rt_vix = None
_rt_dollar_index = None
_rt_yield_curve = None
_rt_fear_greed_index = None
_rt_put_call_ratio = None
_rt_sentiment_data = None

# ═══ 新增 refresh_xxx()（scheduler 调用）═══

def refresh_vix():
    global _rt_vix
    try:
        _rt_vix = fetch_vix()
    except Exception as e:
        logger.warning("[refresh] vix 失败: %s", e)

def refresh_dollar_index():
    global _rt_dollar_index
    try:
        _rt_dollar_index = fetch_dollar_index()
    except Exception as e:
        logger.warning("[refresh] dollar_index 失败: %s", e)

def refresh_yield_curve():
    global _rt_yield_curve
    try:
        _rt_yield_curve = fetch_yield_curve()
    except Exception as e:
        logger.warning("[refresh] yield_curve 失败: %s", e)

def refresh_fear_greed_index():
    global _rt_fear_greed_index
    try:
        _rt_fear_greed_index = fetch_fear_greed_index()
    except Exception as e:
        logger.warning("[refresh] fear_greed_index 失败: %s", e)

def refresh_put_call_ratio():
    global _rt_put_call_ratio
    try:
        _rt_put_call_ratio = fetch_put_call_ratio()
    except Exception as e:
        logger.warning("[refresh] put_call_ratio 失败: %s", e)

def refresh_sentiment_data():
    global _rt_sentiment_data
    try:
        _rt_sentiment_data = get_sentiment_data()
    except Exception as e:
        logger.warning("[refresh] sentiment_data 失败: %s", e)
```

---

### 3.8 data_providers/global_market.py（全球指数/情绪/新闻）

**不动：所有 get_xxx() 函数一字不改**

**新增：内存缓存变量 + refresh_xxx() 写接口**

```python
# ═══ 新增内存缓存变量 ═══
_rt_global_sentiment = None
_rt_global_indices = None
_rt_global_heatmap = None
_rt_global_news = None

# ═══ 新增 refresh_xxx()（scheduler 调用）═══

def refresh_global_sentiment():
    global _rt_global_sentiment
    try:
        _rt_global_sentiment = get_sentiment()
    except Exception as e:
        logger.warning("[refresh] global_sentiment 失败: %s", e)

def refresh_global_indices():
    global _rt_global_indices
    try:
        _rt_global_indices = get_indices()
    except Exception as e:
        logger.warning("[refresh] global_indices 失败: %s", e)

def refresh_global_heatmap():
    global _rt_global_heatmap
    try:
        _rt_global_heatmap = get_heatmap()
    except Exception as e:
        logger.warning("[refresh] global_heatmap 失败: %s", e)

def refresh_global_news():
    global _rt_global_news
    try:
        _rt_global_news = get_news()
    except Exception as e:
        logger.warning("[refresh] global_news 失败: %s", e)
```

---

### 3.9 data_providers/commodities.py（大宗商品）

**不动：fetch_commodities() 一字不改**

**新增：**

```python
_rt_commodities = None

def refresh_commodities():
    global _rt_commodities
    try:
        _rt_commodities = fetch_commodities()
    except Exception as e:
        logger.warning("[refresh] commodities 失败: %s", e)
```

---

### 3.10 data_providers/crypto.py（加密货币）

**不动：所有 fetch_xxx() 函数一字不改**

**新增：**

```python
_rt_crypto_prices = None
_rt_crypto_heatmap = None

def refresh_crypto_prices():
    global _rt_crypto_prices
    try:
        _rt_crypto_prices = fetch_crypto_prices()
    except Exception as e:
        logger.warning("[refresh] crypto_prices 失败: %s", e)

def refresh_crypto_heatmap():
    global _rt_crypto_heatmap
    try:
        _rt_crypto_heatmap = fetch_crypto_heatmap_coingecko()
    except Exception as e:
        logger.warning("[refresh] crypto_heatmap 失败: %s", e)
```

---

### 3.11 data_providers/forex.py（外汇）

**不动：fetch_forex_pairs() 一字不改**

**新增：**

```python
_rt_forex_pairs = None

def refresh_forex_pairs():
    global _rt_forex_pairs
    try:
        _rt_forex_pairs = fetch_forex_pairs()
    except Exception as e:
        logger.warning("[refresh] forex_pairs 失败: %s", e)
```

---

## 四、定时任务 — market_cn/scheduler.py（新增）

```python
"""
market_cn 数据刷新调度器

三档刷新:
  - 日级: 盘后启动加载 1 次
  - 盘中慢: 30 分钟
  - 盘中快: 5 分钟

复用 backfill_db 的 Timer 自调度模式。
"""
import threading
import time
import logging

logger = logging.getLogger(__name__)


def _is_trading_time():
    """粗略判断是否在交易时段（9:00-15:30）"""
    from datetime import datetime
    now = datetime.now()
    if now.weekday() >= 5:  # 周末
        return False
    t = now.hour * 100 + now.minute
    return 900 <= t <= 1530


# ═══ 日级刷新 ═══

def refresh_daily_all():
    """日级数据: 宏观/板块趋势/北向日级/龙虎榜/情绪历史/国际宏观"""
    from app.market_cn.china_market import (
        refresh_china_macro, refresh_sector_trend,
        refresh_sector_prediction, refresh_sector_cycle,
        refresh_sector_history, refresh_emotion_history,
    )
    from app.market_cn.index import (
        refresh_index_daily_kline, refresh_northbound_daily,
        refresh_northbound_holdings, refresh_market_fund_flow_daily,
    )
    from app.market_cn.dragon_limit import (
        refresh_dragon_tiger, refresh_zt_pool,
        refresh_dt_pool, refresh_broken_board,
    )
    from app.data_providers.sentiment import (
        refresh_vix, refresh_dollar_index, refresh_yield_curve,
        refresh_put_call_ratio,
    )
    from app.data_providers.commodities import refresh_commodities
    from app.data_providers.forex import refresh_forex_pairs

    fns = [
        # A 股日级
        refresh_china_macro, refresh_sector_trend,
        refresh_sector_prediction, refresh_sector_cycle,
        refresh_sector_history, refresh_emotion_history,
        refresh_index_daily_kline, refresh_northbound_daily,
        refresh_northbound_holdings, refresh_market_fund_flow_daily,
        refresh_dragon_tiger, refresh_zt_pool,
        refresh_dt_pool, refresh_broken_board,
        # 国际宏观日级
        refresh_vix, refresh_dollar_index, refresh_yield_curve,
        refresh_put_call_ratio, refresh_commodities, refresh_forex_pairs,
    ]
    for fn in fns:
        try:
            fn()
        except Exception as e:
            logger.warning("[daily] %s 失败: %s", fn.__name__, e)


# ═══ 盘中慢档 ═══

def refresh_slow_all():
    """盘中慢档: 贪恐/情绪/政策/新闻/全球情绪/加密"""
    from app.market_cn.china_market import refresh_fear_greed, refresh_policy
    from app.market_cn.emotion import refresh_emotion_cycle
    from app.market_cn.policy_analysis import refresh_financial_news, refresh_macro_news
    from app.data_providers.sentiment import refresh_fear_greed_index, refresh_sentiment_data
    from app.data_providers.global_market import refresh_global_sentiment, refresh_global_news
    from app.data_providers.crypto import refresh_crypto_prices, refresh_crypto_heatmap

    fns = [refresh_fear_greed, refresh_policy, refresh_emotion_cycle,
           refresh_financial_news, refresh_macro_news,
           refresh_fear_greed_index, refresh_sentiment_data,
           refresh_global_sentiment, refresh_global_news,
           refresh_crypto_prices, refresh_crypto_heatmap]
    for fn in fns:
        try:
            fn()
        except Exception as e:
            logger.warning("[slow] %s 失败: %s", fn.__name__, e)


# ═══ 盘中快档 ═══

def refresh_fast_all():
    """盘中快档: 指数实时/北向实时/资金流/热门板块/人气/全球指数"""
    from app.market_cn.index import (
        refresh_index_realtime, refresh_northbound_realtime,
        refresh_market_fund_flow_realtime, refresh_sector_fund_flow,
    )
    from app.market_cn.china_market import refresh_hot_sectors
    from app.market_cn.dragon_limit import refresh_hot_rank
    from app.data_providers.global_market import refresh_global_indices, refresh_global_heatmap

    fns = [
        refresh_index_realtime, refresh_northbound_realtime,
        refresh_market_fund_flow_realtime, refresh_sector_fund_flow,
        refresh_hot_sectors, refresh_hot_rank,
        refresh_global_indices, refresh_global_heatmap,
    ]
    for fn in fns:
        try:
            fn()
        except Exception as e:
            logger.warning("[fast] %s 失败: %s", fn.__name__, e)


# ═══ Timer 自调度 ═══

_timers = {}


def _schedule(name, fn, interval):
    def _run():
        try:
            fn()
        except Exception as e:
            logger.error("[scheduler] %s 异常: %s", name, e)
        # 自调度下次
        t = threading.Timer(interval, _run)
        t.daemon = True
        t.start()
        _timers[name] = t

    t = threading.Timer(interval, _run)
    t.daemon = True
    t.start()
    _timers[name] = t


def _is_post_market():
    """判断是否在盘后时段（15:30-16:30）"""
    from datetime import datetime
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 100 + now.minute
    return 1530 <= t <= 1630


def start():
    """应用启动时调用（在 Flask app.run 之前或 after_fork）

    冷启动流程：全部从远端拉取到内存，不读本地缓存文件。
    """
    logger.info("[scheduler] market_cn 冷启动: 从远端拉取全部数据")

    # 1. 从远端拉取全部数据到内存（不读本地缓存文件）
    refresh_daily_all()
    refresh_slow_all()
    refresh_fast_all()

    logger.info("[scheduler] 冷启动完成，数据已加载到内存")

    # 2. 启动定时刷新
    # 盘中快档 5 分钟
    _schedule("fast", lambda: refresh_fast_all() if _is_trading_time() else None, 300)
    # 盘中慢档 30 分钟
    _schedule("slow", lambda: refresh_slow_all() if _is_trading_time() else None, 1800)
    # 盘后日级 20 分钟（15:30-16:30 检测一次，刷到就停）
    _schedule("post_market", _post_market_refresh, 1200)

    logger.info("[scheduler] 定时刷新已启动: fast=5min, slow=30min, post_market=10min")


_post_market_done_today = False


def _post_market_refresh():
    """盘后刷新：收盘后拉取日级数据（龙虎榜/涨跌停/北向日级等）"""
    global _post_market_done_today
    from datetime import datetime
    now = datetime.now()

    # 新的一天重置标记
    if now.hour < 8:
        _post_market_done_today = False
        return

    # 已完成或不在盘后时段，跳过
    if _post_market_done_today or not _is_post_market():
        return

    logger.info("[scheduler] 盘后刷新开始")
    try:
        refresh_daily_all()
        _post_market_done_today = True
        logger.info("[scheduler] 盘后刷新完成")
    except Exception as e:
        logger.error("[scheduler] 盘后刷新失败: %s", e)
```

---

## 五、冷启动流程

```
应用启动
  │
  ▼
scheduler.start()
  │
  ├── refresh_daily_all()   ← 全部从远端拉取到内存（不读本地缓存文件）
  ├── refresh_slow_all()    ← 全部从远端拉取到内存
  ├── refresh_fast_all()    ← 全部从远端拉取到内存
  │
  ├── _schedule("fast", ..., 300)        ← 盘中快档 5min
  ├── _schedule("slow", ..., 1800)       ← 盘中慢档 30min
  └── _schedule("post_market", ..., 600) ← 盘后日级 10min（15:30-16:30）
```

**关键原则：冷启动不从本地缓存文件读取，全部从远端拉取到内存。**

- 不调用 `china_market._warmup()`（它从文件加载缓存）
- 不读任何 `.json` 缓存文件
- 启动时直接从远端 API 拉取最新数据，保证内存中是最新的
- 盘中：快档 5min + 慢档 30min（`_is_trading_time()` 控制）
- 盘后：15:30-16:30 自动刷新日级数据（龙虎榜/涨跌停/北向日级等盘后才出的数据）
- 非交易时段：定时任务空转不拉数据

---

## 六、调用入口

在 `backend_api_python/run.py` 或 `app/__init__.py` 中启动 scheduler：

```python
# 在 Flask app 创建后调用
from app.market_cn.scheduler import start as start_market_cn_scheduler
start_market_cn_scheduler()
```

---

## 七、改动文件清单

| 文件 | 改动类型 | 改动内容 |
|------|----------|----------|
| `market_cn/china_market.py` | **仅新增** | 新增峰值记录变量 + 9 个内存缓存变量 + 10 个 refresh_xxx()，get_xxx() 逻辑不改 |
| `market_cn/index.py` | **仅新增** | 新增峰值记录变量 + 8 个内存缓存变量 + 8 个 refresh_xxx()，get_xxx() 逻辑不改 |
| `market_cn/emotion.py` | **仅新增** | 新增 1 个内存缓存变量 + 1 个 refresh_xxx() |
| `market_cn/dragon_limit.py` | **仅新增** | 新增 5 个内存缓存变量 + 5 个 refresh_xxx() |
| `market_cn/hot_sectors.py` | **仅新增** | 新增 1 个内存缓存变量 + 1 个 refresh_xxx() |
| `market_cn/policy_analysis.py` | **仅新增** | 新增 2 个内存缓存变量 + 2 个 refresh_xxx() |
| `data_providers/sentiment.py` | **仅新增** | 新增 6 个内存缓存变量 + 6 个 refresh_xxx() |
| `data_providers/global_market.py` | **仅新增** | 新增 4 个内存缓存变量 + 4 个 refresh_xxx() |
| `data_providers/commodities.py` | **仅新增** | 新增 1 个内存缓存变量 + 1 个 refresh_xxx() |
| `data_providers/crypto.py` | **仅新增** | 新增 2 个内存缓存变量 + 2 个 refresh_xxx() |
| `data_providers/forex.py` | **仅新增** | 新增 1 个内存缓存变量 + 1 个 refresh_xxx() |
| `market_cn/scheduler.py` | **新增** | 定时调度器，约 120 行 |
| `run.py` 或 `app/__init__.py` | **修改** | 加一行 `start_market_cn_scheduler()` |

**核心原则：对外函数（get_xxx / fetch_xxx）签名和返回格式不变，内部可按需调整。**

---

## 八、不改动

| 文件 | 原因 |
|------|------|
| market_cn/tape.py | 实时盘口，不缓存 |
| market_cn/finance.py | 个股财务，按需查 |
| market_cn/data_bridge.py | 调用方，接口不变 |
| market_cn/cards/*.py | 调用方，接口不变 |
| market_cn/sector_history.py | 被 china_market.py 的 get_sector_history 调用，不动 |
| data_sources/backfill_db.py | K 线不动 |
| data_sources/coordinator.py | K 线不动 |
| data_sources/cn_hk_fundamentals.py | 个股基本面，按需查 |
| data_sources/asia_stock_kline.py | 个股 K 线，按需查 |
| data_providers/sentiment.py 内部函数 | fetch_xxx() 逻辑不动，只加 refresh_xxx() |

---

## 九、注意事项

1. **refresh_xxx() 里的 try/except 必须兜住** — 定时任务调用，任何异常不能传播
2. **对外函数不改** — get_xxx / fetch_xxx 签名和返回格式不变，外部调用无感；内部函数可按需调整
3. **冷启动不读本地缓存文件** — 全部从远端拉取到内存，保证数据新鲜
4. **scheduler.py 是独立调度层** — 不替代 china_market.py 原有的 `_bg_watchdog` / `_warmup` 机制，两者并存
5. **远端拉取最大化** — refresh 用 `max(默认最大值, 峰值)` 拉取，峰值由 get_xxx() 记录
6. **emotion.py 的文件缓存保留** — 它已有 emotion.json 持久化，refresh 只更新内存
7. **data_bridge.py 不改** — 它调的是 china_market 的 get_xxx()，接口不变自然无感
8. **data_providers 同理** — sentiment/commodities/crypto/forex 的 fetch_xxx() 逻辑不改，只加 refresh

---

## 十、工期估算

| 文件 | 工时 | 说明 |
|------|------|------|
| china_market.py | 0.5 天 | 峰值记录 + 10 个 refresh_xxx() |
| index.py | 0.5 天 | 峰值记录 + 8 个 refresh_xxx() |
| emotion.py | 0.25 天 | 1 个 refresh_xxx() |
| dragon_limit.py | 0.25 天 | 5 个 refresh_xxx() |
| hot_sectors.py | 0.25 天 | 1 个 refresh_xxx() |
| policy_analysis.py | 0.25 天 | 2 个 refresh_xxx() |
| data_providers/sentiment.py | 0.5 天 | 6 个 refresh_xxx() |
| data_providers/global_market.py | 0.25 天 | 4 个 refresh_xxx() |
| data_providers/commodities.py | 0.1 天 | 1 个 refresh_xxx() |
| data_providers/crypto.py | 0.15 天 | 2 个 refresh_xxx() |
| data_providers/forex.py | 0.1 天 | 1 个 refresh_xxx() |
| scheduler.py | 0.5 天 | 新增定时调度器 |
| 联调测试 | 0.5 天 | |
| **合计** | **约 4 天** | |
