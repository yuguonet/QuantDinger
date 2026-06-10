# market_cn 缓存改造设计

> 日期: 2026-06-10
> 范围: market_cn 模块（不含 K 线 / data_sources / data_providers / tape.py）
> 原则: 读写分离 — get_xxx() 只读缓存，refresh_xxx() 负责拉取写缓存

## 一、改造思路

### 极简方案

每个文件内部拆分两个函数：

```python
# get_xxx() — 读缓存，无缓存返回空
def get_xxx():
    return _xxx_cache or {"code": 0, "msg": "缓存未就绪", "data": {}}

# refresh_xxx() — 拉远端，写缓存（就是原来 get_xxx 里的拉取逻辑）
def refresh_xxx():
    global _xxx_cache
    _xxx_cache = 原来的拉取逻辑
```

- **不新建模块**（scheduler.py 是唯一新增文件，约 50 行）
- **不改接口签名**（get_xxx 签名和返回格式完全不变）
- **后台只负责定时调 refresh_xxx()，不管成功失败**

## 二、数据分类

### 日级档 — 盘后启动加载 1 次

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

### 盘中慢档 — 30 分钟

| 函数 | 模块 |
|------|------|
| `get_fear_greed` | china_market |
| `fetch_emotion_cycle` | emotion |
| `get_policy` | china_market |
| `get_financial_news` | policy_analysis |
| `get_macro_news` | policy_analysis |

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

**改动 1：get_xxx() 去掉同步兜底**

```python
# ═══ 改前 ═══
def get_china_macro() -> dict:
    data = cache_get("china_macro")
    if data is not None:
        return data
    _refresh("china_macro", force=True)          # ← 同步阻塞，去掉
    return cache_get("china_macro") or {"code": 0, "msg": "获取失败", "data": {}}

def get_fear_greed() -> dict:
    data = cache_get("china_fg")
    if data is not None:
        return data
    _refresh("china_fg", force=True)             # ← 同步阻塞，去掉
    data = cache_get("china_fg")
    return {"code": 1 if data else 0, "msg": "success", "data": data or {}}

def get_hot_sectors(industry_limit=15, concept_limit=15) -> dict:
    data = cache_get("hot_sectors")
    if data is not None:
        return data
    _refresh("hot_sectors", force=True)          # ← 同步阻塞，去掉
    data = cache_get("hot_sectors")
    return {"code": 1 if data else 0, "msg": "success", "data": data or {}}

def get_sector_trend(board_type="industry") -> dict:
    key = f"sector_trend_{board_type}"
    data = cache_get(key)
    if data is not None:
        return data
    _refresh(key, force=True)                    # ← 同步阻塞，去掉
    data = cache_get(key)
    return {"code": 1 if data else 0, "msg": "success", "data": data or {}}

def get_sector_prediction() -> dict:
    data = cache_get("sector_prediction")
    if data is not None:
        return data
    _refresh("sector_prediction", force=True)    # ← 同步阻塞，去掉
    return data or {"code": 0, "msg": "获取失败", "data": {}}

def get_sector_cycle(board_type="industry") -> dict:
    key = f"sector_cycle_{board_type}"
    data = cache_get(key)
    if data is not None:
        return data
    _refresh(key, force=True)                    # ← 同步阻塞，去掉
    return data or {"code": 0, "msg": "获取失败", "data": {}}

# ═══ 改后 ═══
def get_china_macro() -> dict:
    data = cache_get("china_macro")
    return data or {"code": 0, "msg": "缓存未就绪", "data": {}}

def get_fear_greed() -> dict:
    data = cache_get("china_fg")
    return {"code": 1 if data else 0, "msg": "success", "data": data or {}}

def get_hot_sectors(industry_limit=15, concept_limit=15) -> dict:
    data = cache_get("hot_sectors")
    return {"code": 1 if data else 0, "msg": "success", "data": data or {}}

def get_sector_trend(board_type="industry") -> dict:
    data = cache_get(f"sector_trend_{board_type}")
    return {"code": 1 if data else 0, "msg": "success", "data": data or {}}

def get_sector_prediction() -> dict:
    data = cache_get("sector_prediction")
    return data or {"code": 0, "msg": "缓存未就绪", "data": {}}

def get_sector_cycle(board_type="industry") -> dict:
    data = cache_get(f"sector_cycle_{board_type}")
    return data or {"code": 0, "msg": "缓存未就绪", "data": {}}
```

**改动 2：去掉 `_bg_watchdog` 定时线程**

```python
# 删除这段（约第 120-130 行）
def _bg_watchdog():
    """每 60 秒扫描一次，过期自动刷新。"""
    while True:
        time.sleep(_BG_INTERVAL)
        for endpoint in _CACHE_CONFIG:
            if cache_is_stale(endpoint):
                _refresh_async(endpoint)

# 删除模块末尾的启动代码（已被注释，确认删除）
# threading.Thread(target=_warmup, daemon=True).start()
# threading.Thread(target=_bg_watchdog, daemon=True).start()
```

刷新统一由 scheduler.py 管理，china_market.py 不再自启后台线程。

**改动 3：保留 `_warmup()` 但去掉自动刷新**

`_warmup()` 保留，它只负责从文件加载缓存到内存（冷启动用）。去掉里面 `_refresh_async(endpoint, force=True)` 那行。

```python
# ═══ 改前 ═══
def _warmup():
    _cache_load()
    for endpoint in _CACHE_CONFIG:
        if cache_is_stale(endpoint):
            _refresh_async(endpoint, force=True)   # ← 去掉

# ═══ 改后 ═══
def _warmup():
    _cache_load()   # 只从文件加载，不刷新
```

**改动 4：新增 refresh_xxx() 函数**

这些函数就是原来 `_do_fetch()` + `cache_put()` 的封装，供 scheduler.py 调用。

```python
def refresh_china_macro():
    data = _fetch_china_macro()
    if data is not None:
        cache_put("china_macro", data)

def refresh_fear_greed():
    data = _fetch_fear_greed()
    if data is not None:
        cache_put("china_fg", data)

def refresh_hot_sectors():
    data = _fetch_hot_sectors()
    if data is not None:
        cache_put("hot_sectors", data)

def refresh_sector_trend(board_type="industry"):
    data = _fetch_sector_trend(board_type)
    if data is not None:
        cache_put(f"sector_trend_{board_type}", data)

def refresh_sector_prediction():
    data = _fetch_sector_prediction()
    if data is not None:
        cache_put("sector_prediction", data)

def refresh_sector_cycle(board_type="industry"):
    data = _fetch_sector_cycle(board_type)
    if data is not None:
        cache_put(f"sector_cycle_{board_type}", data)

def refresh_policy():
    data = _fetch_policy()
    if data is not None:
        cache_put("policy", data)
```

**注意**：`_fetch_xxx()` 函数已存在（文件底部），无需修改。`refresh_xxx()` 只是包一层。

---

### 3.2 index.py

当前状态：**完全没有缓存**，所有函数直接 requests.get。

**改动 1：文件顶部新增缓存变量**

```python
# ═══ 缓存变量 ═══
_idx_rt_cache = None          # get_index_realtime
_idx_daily_cache = {}         # {code: [kline_data]}
_nb_rt_cache = None           # get_northbound_realtime
_nb_daily_cache = None        # get_northbound_daily
_nb_holdings_cache = None     # get_northbound_holdings
_mf_rt_cache = None           # get_market_fund_flow_realtime
_mf_daily_cache = None        # get_market_fund_flow_daily
_sector_flow_cache = None     # get_sector_fund_flow
_fund_flow_cache_ts = 0       # 资金流实时缓存时间戳
```

**改动 2：原有函数改名为 `_fetch_xxx()`（加下划线）**

把原来的实现函数全部加下划线前缀：

```python
# ═══ 原来的 ═══
def get_index_realtime(codes=None):
    ...（300 行拉取逻辑）

# ═══ 改为 ═══
def _fetch_index_realtime(codes=None):
    ...（300 行拉取逻辑，一字不改）
```

同理：
- `get_index_daily_kline` → `_fetch_index_daily_kline`
- `get_index_kline` → `_fetch_index_kline`
- `get_northbound_realtime` → `_fetch_northbound_realtime`
- `get_northbound_daily` → `_fetch_northbound_daily`
- `get_northbound_holdings` → `_fetch_northbound_holdings`
- `get_market_fund_flow_realtime` → `_fetch_market_fund_flow_realtime`
- `get_market_fund_flow_daily` → `_fetch_market_fund_flow_daily`
- `get_sector_fund_flow` → `_fetch_sector_fund_flow`

**改动 3：新增读接口（对外，只读缓存）**

```python
# ═══ 读接口 ═══

def get_index_realtime(codes=None):
    """指数实时行情（只读缓存）"""
    return _idx_rt_cache or []

def get_index_daily_kline(code="000001", days=200):
    """指数日K线（只读缓存）"""
    return _idx_daily_cache.get(code, [])

def get_index_kline(code="000001", frequency="1D", days=200):
    """指数多周期K线（只读缓存，日线走缓存，分钟线走实时）"""
    if frequency == "1D":
        return _idx_daily_cache.get(code, [])
    # 非日线周期不缓存，直接调 _fetch
    return _fetch_index_kline(code, frequency, days)

def get_northbound_realtime():
    """北向实时（只读缓存）"""
    return _nb_rt_cache or {"error": "缓存未就绪", "points": 0, "data": []}

def get_northbound_daily(days=120):
    """北向日级（只读缓存）"""
    return _nb_daily_cache or []

def get_northbound_holdings(top=50):
    """北向持股（只读缓存）"""
    return _nb_holdings_cache or []

def get_market_fund_flow_realtime():
    """大盘资金流实时（只读缓存）"""
    return _mf_rt_cache or {"source": "none", "error": "缓存未就绪"}

def get_market_fund_flow_daily(days=120):
    """大盘资金流日级（只读缓存）"""
    return _mf_daily_cache or []

def get_sector_fund_flow(indicator="今日"):
    """行业板块资金流（只读缓存）"""
    return _sector_flow_cache or []
```

**改动 4：新增写接口（后台调用）**

```python
# ═══ 写接口（scheduler.py 调用） ═══

def refresh_index_realtime():
    global _idx_rt_cache
    try:
        _idx_rt_cache = _fetch_index_realtime()
    except Exception:
        pass

def refresh_index_daily_kline():
    global _idx_daily_cache
    try:
        for code in INDEX_CODES:
            data = _fetch_index_daily_kline(code, 200)
            if data:
                _idx_daily_cache[code] = data
    except Exception:
        pass

def refresh_northbound_realtime():
    global _nb_rt_cache
    try:
        _nb_rt_cache = _fetch_northbound_realtime()
    except Exception:
        pass

def refresh_northbound_daily():
    global _nb_daily_cache
    try:
        _nb_daily_cache = _fetch_northbound_daily(120)
    except Exception:
        pass

def refresh_northbound_holdings():
    global _nb_holdings_cache
    try:
        _nb_holdings_cache = _fetch_northbound_holdings(50)
    except Exception:
        pass

def refresh_market_fund_flow_realtime():
    global _mf_rt_cache, _fund_flow_cache_ts
    try:
        data = _fetch_market_fund_flow_realtime()
        if data:
            _mf_rt_cache = data
            _fund_flow_cache_ts = time.time()
    except Exception:
        pass

def refresh_market_fund_flow_daily():
    global _mf_daily_cache
    try:
        _mf_daily_cache = _fetch_market_fund_flow_daily(120)
    except Exception:
        pass

def refresh_sector_fund_flow():
    global _sector_flow_cache
    try:
        _sector_flow_cache = _fetch_sector_fund_flow("今日")
    except Exception:
        pass
```

---

### 3.3 emotion.py

当前状态：有自己的 JSON 文件缓存，但 `fetch_emotion_cycle()` 每次仍会 requests.get。

**改动 1：fetch_emotion_cycle() 拆分**

```python
# ═══ 原来的 fetch_emotion_cycle 改名为 _fetch_emotion_cycle ═══
def _fetch_emotion_cycle():
    """拉取最新情绪数据 + 追加快照到当天缓存"""
    ...（原逻辑一字不改）

# ═══ 新增：读接口 ═══
_emotion_cache = None

def fetch_emotion_cycle():
    """读缓存"""
    return _emotion_cache or {"code": 0, "msg": "缓存未就绪", "data": {}}

# ═══ 新增：写接口 ═══
def refresh_emotion_cycle():
    global _emotion_cache
    try:
        _emotion_cache = _fetch_emotion_cycle()
    except Exception:
        pass
```

**改动 2：get_emotion_history() 和 get_emotion_latest() 不变**

它们已经是从文件缓存读取，不需要改。

---

### 3.4 dragon_limit.py

当前状态：每次调用都直接 HTTP 请求或 AkShare 拉取。

**改动 1：原有函数加下划线**

```python
def get_dragon_tiger(...) → def _fetch_dragon_tiger(...)
def get_zt_pool(...)      → def _fetch_zt_pool(...)
def get_dt_pool(...)      → def _fetch_dt_pool(...)
def get_broken_board(...) → def _fetch_broken_board(...)
def get_hot_rank()        → def _fetch_hot_rank()
```

**改动 2：新增缓存 + 读接口 + 写接口**

```python
# ═══ 缓存 ═══
_dragon_tiger_cache = None
_zt_pool_cache = None
_dt_pool_cache = None
_broken_board_cache = None
_hot_rank_cache = None

# ═══ 读接口 ═══
def get_dragon_tiger(start_date="", end_date=""):
    return _dragon_tiger_cache or []

def get_zt_pool(trade_date=""):
    return _zt_pool_cache or []

def get_dt_pool(trade_date=""):
    return _dt_pool_cache or []

def get_broken_board(trade_date=""):
    return _broken_board_cache or []

def get_hot_rank():
    return _hot_rank_cache or []

# ═══ 写接口 ═══
def refresh_dragon_tiger():
    global _dragon_tiger_cache
    try:
        _dragon_tiger_cache = _fetch_dragon_tiger()
    except Exception:
        pass

def refresh_zt_pool():
    global _zt_pool_cache
    try:
        _zt_pool_cache = _fetch_zt_pool()
    except Exception:
        pass

def refresh_dt_pool():
    global _dt_pool_cache
    try:
        _dt_pool_cache = _fetch_dt_pool()
    except Exception:
        pass

def refresh_broken_board():
    global _broken_board_cache
    try:
        _broken_board_cache = _fetch_broken_board()
    except Exception:
        pass

def refresh_hot_rank():
    global _hot_rank_cache
    try:
        _hot_rank_cache = _fetch_hot_rank()
    except Exception:
        pass
```

---

### 3.5 hot_sectors.py

当前状态：`get_all_hot_sectors()` 每次都拉东财/新浪 API。

**改动 1：原有函数加下划线**

```python
def get_all_hot_sectors(...) → def _fetch_all_hot_sectors(...)
def get_hot_industry_boards(...) → def _fetch_hot_industry_boards(...)
def get_hot_concept_boards(...) → def _fetch_hot_concept_boards(...)
```

**改动 2：新增缓存 + 读接口 + 写接口**

```python
# ═══ 缓存 ═══
_hot_sectors_cache = None

# ═══ 读接口 ═══
def get_all_hot_sectors(industry_limit=15, concept_limit=15):
    return _hot_sectors_cache or {
        "timestamp": "", "industry": [], "concept": [],
        "analysis": {"summary": "缓存未就绪", "sentiment": "未知"}
    }

def get_hot_industry_boards(limit=20):
    result = _hot_sectors_cache or {}
    return result.get("industry", [])[:limit]

def get_hot_concept_boards(limit=20):
    result = _hot_sectors_cache or {}
    return result.get("concept", [])[:limit]

# ═══ 写接口 ═══
def refresh_hot_sectors():
    global _hot_sectors_cache
    try:
        _hot_sectors_cache = _fetch_all_hot_sectors()
    except Exception:
        pass
```

**注意**：`get_sector_detail(board_code, limit)` 是实时查个股，不缓存，保持不变。

---

### 3.6 policy_analysis.py

当前状态：每次调用都拉 AkShare/东财，无缓存，且函数内有 print 语句（不适合 API 调用）。

**改动 1：原有函数加下划线**

```python
def get_financial_news() → def _fetch_financial_news()
def get_macro_news()     → def _fetch_macro_news()
```

**改动 2：新增缓存 + 读接口 + 写接口**

```python
# ═══ 缓存 ═══
_financial_news_cache = None
_macro_news_cache = None

# ═══ 读接口 ═══
def get_financial_news():
    return _financial_news_cache

def get_macro_news():
    return _macro_news_cache

# ═══ 写接口 ═══
def refresh_financial_news():
    global _financial_news_cache
    try:
        _financial_news_cache = _fetch_financial_news()
    except Exception:
        pass

def refresh_macro_news():
    global _macro_news_cache
    try:
        _macro_news_cache = _fetch_macro_news()
    except Exception:
        pass
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
    """日级数据: 宏观/板块趋势/北向日级/龙虎榜/情绪历史"""
    from app.market_cn.china_market import (
        refresh_china_macro, refresh_sector_trend,
        refresh_sector_prediction, refresh_sector_cycle,
    )
    from app.market_cn.index import (
        refresh_index_daily_kline, refresh_northbound_daily,
        refresh_northbound_holdings, refresh_market_fund_flow_daily,
    )
    from app.market_cn.dragon_limit import (
        refresh_dragon_tiger, refresh_zt_pool,
        refresh_dt_pool, refresh_broken_board,
    )
    from app.market_cn.emotion import refresh_emotion_history  # 已有文件缓存

    fns = [
        refresh_china_macro, refresh_sector_trend,
        refresh_sector_prediction, refresh_sector_cycle,
        refresh_index_daily_kline, refresh_northbound_daily,
        refresh_northbound_holdings, refresh_market_fund_flow_daily,
        refresh_dragon_tiger, refresh_zt_pool,
        refresh_dt_pool, refresh_broken_board,
    ]
    for fn in fns:
        try:
            fn()
        except Exception as e:
            logger.warning("[daily] %s 失败: %s", fn.__name__, e)


# ═══ 盘中慢档 ═══

def refresh_slow_all():
    """盘中慢档: 贪恐/情绪/政策/新闻"""
    from app.market_cn.china_market import refresh_fear_greed, refresh_policy
    from app.market_cn.emotion import refresh_emotion_cycle
    from app.market_cn.policy_analysis import refresh_financial_news, refresh_macro_news

    fns = [refresh_fear_greed, refresh_policy, refresh_emotion_cycle,
           refresh_financial_news, refresh_macro_news]
    for fn in fns:
        try:
            fn()
        except Exception as e:
            logger.warning("[slow] %s 失败: %s", fn.__name__, e)


# ═══ 盘中快档 ═══

def refresh_fast_all():
    """盘中快档: 指数实时/北向实时/资金流/热门板块/人气"""
    from app.market_cn.index import (
        refresh_index_realtime, refresh_northbound_realtime,
        refresh_market_fund_flow_realtime, refresh_sector_fund_flow,
    )
    from app.market_cn.china_market import refresh_hot_sectors
    from app.market_cn.dragon_limit import refresh_hot_rank

    fns = [
        refresh_index_realtime, refresh_northbound_realtime,
        refresh_market_fund_flow_realtime, refresh_sector_fund_flow,
        refresh_hot_sectors, refresh_hot_rank,
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


def start():
    """应用启动时调用（在 Flask app.run 之前或 after_fork）"""
    logger.info("[scheduler] market_cn 数据刷新启动")

    # 1. 冷启动预热
    try:
        from app.market_cn.china_market import _warmup
        _warmup()
    except Exception:
        pass

    # 2. 加载全部数据到缓存
    refresh_daily_all()
    refresh_slow_all()
    refresh_fast_all()

    # 3. 启动定时刷新（仅交易时段有效）
    _schedule("fast", lambda: refresh_fast_all() if _is_trading_time() else None, 300)   # 5min
    _schedule("slow", lambda: refresh_slow_all() if _is_trading_time() else None, 1800)  # 30min
    # 日级不定时，启动时跑一次就够了

    logger.info("[scheduler] 定时刷新已启动: fast=5min, slow=30min")
```

---

## 五、冷启动流程

```
应用启动
  │
  ▼
china_market._warmup()     ← 从文件加载缓存到内存
  │
  ▼
scheduler.start()           ← 新增
  │
  ├── refresh_daily_all()   ← 加载日级数据到缓存
  ├── refresh_slow_all()    ← 加载盘中慢档
  ├── refresh_fast_all()    ← 加载盘中快档
  │
  ├── _schedule("fast", ..., 300)   ← 启动 5min 定时
  └── _schedule("slow", ..., 1800)  ← 启动 30min 定时
```

盘后场景：`_is_trading_time()` 返回 False，定时任务空转不拉数据。

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
| `market_cn/china_market.py` | 修改 | get_xxx() 去掉同步兜底；删 _bg_watchdog；_warmup() 去掉刷新；新增 7 个 refresh_xxx() |
| `market_cn/index.py` | 修改 | 原函数改名 _fetch_xxx()；新增 8 个缓存变量 + 8 个读接口 + 8 个写接口 |
| `market_cn/emotion.py` | 修改 | fetch_emotion_cycle 改名 _fetch；新增读接口 + refresh |
| `market_cn/dragon_limit.py` | 修改 | 原函数改名 _fetch_xxx()；新增 5 个缓存 + 5 个读 + 5 个写 |
| `market_cn/hot_sectors.py` | 修改 | 原函数改名 _fetch_xxx()；新增 1 个缓存 + 3 个读 + 1 个写 |
| `market_cn/policy_analysis.py` | 修改 | 原函数改名 _fetch_xxx()；新增 2 个缓存 + 2 个读 + 2 个写 |
| `market_cn/scheduler.py` | **新增** | 定时调度器，约 80 行 |
| `run.py` 或 `app/__init__.py` | 修改 | 加一行 `start_market_cn_scheduler()` |

---

## 八、不改动

| 文件 | 原因 |
|------|------|
| market_cn/tape.py | 实时盘口 |
| market_cn/finance.py | 个股按需查 |
| market_cn/data_bridge.py | 调用方，接口不变 |
| market_cn/cards/*.py | 调用方，接口不变 |
| data_sources/backfill_db.py | K 线不动 |
| data_sources/coordinator.py | K 线不动 |
| data_providers/*.py | 国际市场单独处理 |

---

## 九、注意事项

1. **refresh_xxx() 里的 try/except 必须兜住** — 定时任务调用，任何异常不能传播
2. **index.py 的 get_index_kline() 非日线周期不缓存** — 分钟线实时性要求高，直接调 _fetch
3. **dragon_limit 的 refresh 不带参数** — 龙虎榜/涨跌停池默认拉当天
4. **emotion.py 的文件缓存保留** — 它已有 emotion.json 持久化，refresh 只更新内存
5. **data_bridge.py 不改** — 它调的是 china_market 的 get_xxx()，接口不变自然无感

---

## 十、工期估算

| 文件 | 工时 |
|------|------|
| china_market.py | 0.5 天 |
| index.py | 1 天（函数最多） |
| emotion.py | 0.5 天 |
| dragon_limit.py | 0.5 天 |
| hot_sectors.py | 0.5 天 |
| policy_analysis.py | 0.5 天 |
| scheduler.py | 0.5 天 |
| 联调测试 | 0.5 天 |
| **合计** | **约 4.5 天** |
