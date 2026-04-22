# A股数据源增强 — 完整方案

## 解决的问题

原有 `cn_stock.py` fallback 链路:
```
Twelve Data → 腾讯(日/周) → yfinance → AkShare
```

| 问题 | 影响 |
|------|------|
| Twelve Data 需付费 Key | 大多数用户直接跳过 |
| yfinance 对A股海外常限流 | .SS/.SZ 经常超时 |
| AkShare 依赖东财接口 | 海外访问不稳定 |
| 无超时机制 | 某源挂起阻塞整个流程 |
| 无熔断保护 | 连续失败源反复请求 |
| 无缓存 | 重复请求浪费资源 |

## 新的 Fallback 链

**K线**:
```
Twelve Data → 腾讯(日/周) → 东方财富(全周期) → 新浪(日线) → yfinance → AkShare
```

**实时行情**:
```
腾讯 qt.gtimg.cn → 东方财富 push2 → 新浪 hq.sinajs.cn
```

## 文件清单

### 新增

| 文件 | 行数 | 功能 |
|------|------|------|
| `app/data_sources/sina.py` | ~300 | 新浪财经: 日K线(JSON+hisdata双端点) + 实时行情 |
| `app/data_sources/eastmoney.py` | ~300 | 东方财富直接API: 全周期K线 + 单股实时行情 |
| `test_cn_datasources.py` | ~120 | 集成测试脚本 |

### 重写

| 文件 | 原行 | 新行 | 改动 |
|------|------|------|------|
| `app/data_sources/cn_stock.py` | ~80 | ~380 | 全量数据源 + 超时 + 熔断 + 缓存 + 校验 |

## 核心机制

### 1. 超时溢出

```python
_TIMEOUT_EXECUTOR = ThreadPoolExecutor(max_workers=4)

def _fetch_with_timeout(func, *, timeout=10, source_name=""):
    future = _TIMEOUT_EXECUTOR.submit(func)
    try:
        return future.result(timeout=timeout), None
    except TimeoutError:
        future.cancel()          # 不等了，立刻走
        return None, "timeout"
```

- 共享线程池（4 workers），不每次创建
- 超时立即切下一个数据源
- 默认 10s，可通过 `DATA_SOURCE_TIMEOUT` 配置

### 2. 熔断保护

复用 `circuit_breaker.py` 的全局熔断器:
- 连续 2 次失败 → 熔断 3 分钟
- 熔断期间自动跳过
- 冷却后半开探测
- 成功恢复，失败继续

### 3. 数据缓存

复用 `cache_manager.py`:
- 实时行情: 10分钟 TTL
- 日K线: 5分钟 TTL
- 分钟线: 2分钟 TTL
- LRU 淘汰，线程安全

### 4. 数据校验 (`_validate_kline_result`)

缓存前校验，避免脏数据污染:
- bars 非空且 >= 请求量
- `time` 为正整数
- `close > 0`（排除停牌）
- `high >= low`

### 5. Lambda 闭包安全

用 default-arg 捕获循环变量，避免引用陷阱:
```python
# ✅ 正确
lambda _c=code, _t=tf: fetch(_c, _t)

# ❌ 错误（闭包捕获最终值）
lambda: fetch(code, tf)
```

### 6. 限流防封禁

| 数据源 | 最小间隔 | Jitter |
|--------|---------|--------|
| 东方财富 | 2.0s | 1.0-3.0s |
| 腾讯 | 1.0s | 0.5-1.5s |
| 新浪(行情) | 0.8s | 0.3-1.2s |
| 新浪(K线) | 1.5s | 0.8-2.5s |

## 数据源对比

| | 腾讯 | 东方财富 | 新浪 | Twelve Data | yfinance |
|---|---|---|---|---|---|
| API Key | ❌ | ❌ | ❌ | ✅ | ❌ |
| 日K线 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 分钟K线 | ❌ | ✅ | ❌ | ✅ | ✅ |
| 周K线 | ✅ | ✅ | ❌ | ✅ | ✅ |
| 实时行情 | ✅ | ✅ | ✅ | ✅ | ⚠️ |
| 国内速度 | ⚡ | ⚡ | ⚡ | 🐌 | 🐌 |
| 海外可用 | ⚠️ | ⚠️ | ⚠️ | ✅ | ✅ |

## 使用

### 测试

```bash
cd backend_api_python
python3 test_cn_datasources.py 600519
python3 test_cn_datasources.py 000001
```

### 代码调用（接口不变）

```python
from app.data_sources.factory import DataSourceFactory

source = DataSourceFactory.get_source("CNStock")
ticker = source.get_ticker("600519")
kline = source.get_kline("600519", "1D", 100)
```

### 环境变量

```bash
DATA_SOURCE_TIMEOUT=10      # 单源超时秒数
DATA_SOURCE_RETRY=3         # 重试次数
TWELVE_DATA_API_KEY=xxx     # 可选
```

## 架构

```
┌──────────────────────────────────────────────┐
│             CNStockDataSource                │
│                                              │
│  ┌────────┐  ┌────────┐  ┌────────────┐     │
│  │ Cache  │  │Circuit │  │ Rate Limit │     │
│  │  TTL   │  │Breaker │  │  + Jitter  │     │
│  └───┬────┘  └───┬────┘  └─────┬──────┘     │
│      │           │              │            │
│  ┌───▼───────────▼──────────────▼──────────┐ │
│  │    _fetch_with_timeout()                │ │
│  │    (ThreadPool 4 workers, 10s cap)      │ │
│  └─────────────────┬───────────────────────┘ │
│                    │                         │
│  ┌─────────────────▼───────────────────────┐ │
│  │  Twelve → 腾讯 → 东方财富 → 新浪        │ │
│  │                      → yfinance → AkShare│ │
│  └─────────────────────────────────────────┘ │
└──────────────────────────────────────────────┘
```

## 本次修复的问题

1. **`lstrip` 字符集误伤** → 改用 `s[2:]` 安全截断
2. **每次创建 ThreadPoolExecutor** → 共享 4-worker 池
3. **Lambda 闭包变量引用陷阱** → default-arg 捕获
4. **无数据校验** → `_validate_kline_result()` 缓存前校验
5. **超时不一致** → 统一 `_get_timeout()` 配置
6. **缺少 retry 异常类型限定** → 指定 `requests.exceptions.*`
7. **东财 K线 high/low 顺序注释错误** → 修正为 f54=high, f55=low
8. **新浪未用函数** → 删除 `_sina_symbol_for_kline`
9. **东财行情停牌检测** → last=0 && prev=0 时返回 None
