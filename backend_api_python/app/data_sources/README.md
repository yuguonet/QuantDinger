# kline_service — 多源股票K线数据服务

多数据源统一接入的股票K线/行情服务，支持A股、港股，内置缓存、熔断、限流、复权。

## 架构概览

```
┌─────────────────────────────────────────────────────────────────┐
│                      KlineService (缓存层)                       │
│              缓存读写 / 批量分离 / 预热 / 便捷方法                 │
└───────────────────────────┬─────────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────────┐
│                   DataSourceFactory (工厂层)                      │
│          复权 / 请求去重(InflightDedup) / fallback / race        │
└───────────────────────────┬─────────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────────┐
│                     Coordinator (协助层)                          │
│        动态任务队列 / 并发控制 / 吞吐跟踪 / 批量优先               │
└───────────────────────────┬─────────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────────┐
│                      Providers (数据源层)                         │
│  ┌─────────┐ ┌──────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐  │
│  │ Tencent │ │ Sina │ │ EastMoney│ │ AkShare  │ │ HK Stock │  │
│  │ p=10    │ │ p=20 │ │ p=30     │ │ p=50     │ │ p=40     │  │
│  │ A+港    │ │ A股  │ │ A股      │ │ A+港     │ │ 港股     │  │
│  └─────────┘ └──────┘ └──────────┘ └──────────┘ └──────────┘  │
└─────────────────────────────────────────────────────────────────┘

辅助模块:
  ├── cache.py          两层缓存 (内存 LRU + 磁盘 feather)
  ├── circuit_breaker.py 熔断器 (3次失败/5min冷却)
  ├── rate_limiter.py   限流器 (最小间隔+随机抖动)
  ├── normalizer.py     股票代码标准化 (SH/SZ/BJ/HK)
  ├── adjustment.py     复权因子计算 (前复权/后复权)
  └── source_config.py  数据源并发/市场/吞吐配置
```

## 快速开始

### 安装依赖

```bash
pip install requests pandas
# 可选: akshare (兜底数据源)
pip install akshare
```

### 基本用法

```python
from app.services.kline import KlineService

service = KlineService()

# 获取单只K线 (前复权)
bars = service.get_kline("CNStock", "SH600519", "1D", limit=100, adj="qfq")
for bar in bars[-3:]:
    print(f"{bar['time']} | O:{bar['open']} H:{bar['high']} L:{bar['low']} C:{bar['close']}")

# 获取实时行情
ticker = service.get_ticker("CNStock", "SH600519")
print(f"最新价: {ticker['last']}")

# 批量K线
result = service.get_kline_batch(
    "CNStock", ["SH600519", "SZ000001", "SZ300750"], "1D", 300
)
for sym, bars in result.items():
    print(f"{sym}: {len(bars)} 根K线")

# 批量行情 — 传入逗号分隔的代码，自动切换批量模式
tickers = service.get_ticker("CNStock", "SH600519,SZ000001")
```

### 股票代码格式

```python
# A股 — 以下格式均可:
"600519"        # 自动推断为 SH600519
"SH600519"      # 带前缀
"600519.SH"     # 带后缀
"SZ000001"      # 深市

# 港股:
"700"           # 自动标准化为 HK00700
"HK00700"       # 带前缀
"00700.HK"      # 带后缀

# 批量 (逗号分隔):
"SH600519,SZ000001,SZ300750"
```

### 复权方式

```python
# 前复权 (默认) — 最新价不变，历史价下调
bars = service.get_kline("CNStock", "SH600519", "1D", adj="qfq")

# 后复权 — 最早价不变，新价上调
bars = service.get_kline("CNStock", "SH600519", "1D", adj="hfq")

# 不复权
bars = service.get_kline("CNStock", "SH600519", "1D", adj="")
```

### 支持的K线周期

| 周期 | 参数 | 存储 | TTL |
|------|------|------|-----|
| 1分钟 | `"1m"` | 内存 | 60s |
| 5分钟 | `"5m"` | 内存 | 120s |
| 15分钟 | `"15m"` | 内存 | 180s |
| 30分钟 | `"30m"` | 内存 | 300s |
| 1小时 | `"1H"` | 内存 | 300s |
| 日线 | `"1D"` | 磁盘 | 4h |
| 周线 | `"1W"` | 磁盘 | 24h |
| 月线 | `"1M"` | 磁盘 | 24h |

## 数据源说明

### A股数据源 (按优先级)

| 数据源 | 优先级 | 批量K线 | 单只行情 | 批量行情 | 特点 |
|--------|--------|---------|----------|----------|------|
| 腾讯 | 10 | ❌ | ✅ | ✅ (500只/次) | 首选，国内直连 |
| 新浪 | 20 | ❌ | ✅ | ✅ (500只/次) | 备选，速度较快 |
| 东方财富 | 30 | ❌ | ✅ | ✅ (6000只/次) | 最稳定，数据全 |
| AkShare | 50 | ❌ | ❌ | ✅ (全市场) | 兜底，需安装 akshare |

### 港股数据源

| 数据源 | 优先级 | 降级顺序 | 特点 |
|--------|--------|----------|------|
| 腾讯 fqkline | 40 | 日/周线首选 | 国内直连 |
| yfinance | — | 降级 1 | 海外源 |
| AkShare | — | 降级 2 | 国内源 |
| Twelve Data | — | 降级 3 | 最终兜底 |

## 核心机制

### 两层缓存

```
热数据 (行情/分钟线) → 内存 OrderedDict (LRU淘汰)
温数据 (日线/股票信息) → feather 文件 (进程重启还在)
```

- 内存缓存: 最大 10000 条，LRU 淘汰
- 磁盘缓存: `cache/` 目录，feather 格式，原子写入
- TTL 按数据类型自动配置 (行情 30s，日线 4h，基础信息 24h)

### 熔断器

```
Closed (正常) ──连续失败≥3次──→ Open (熔断) ──冷却5min──→ Half-Open (试探)
      ↑                                                        │
      └──────────────── 请求成功 ←──────────────────────────────┘
```

- 实时数据源熔断器: 3次失败 / 5分钟冷却
- 海外数据源熔断器: 2次失败 / 15分钟冷却

### 请求去重 (InflightDedup)

同一 symbol 并发请求时，只有一个线程实际发 API，其他线程等结果。
避免重复请求浪费 API 配额。

### 复权计算

除权除息数据从东财获取，缓存 24 小时。

```
前复权: 参考价 = (昨收 - 每股分红 + 配股价 × 配股比例) / (1 + 送股比例 + 配股比例)
后复权: 累乘复权因子的倒数
```

## 缓存管理

```python
service = KlineService()

# 查看缓存统计
print(service.cache_stats())

# 查看数据源吞吐统计
print(service.source_stats())

# 清除某只股票的缓存
service.invalidate(symbol="SH600519")

# 清除所有K线缓存
service.invalidate(data_type="kline")

# 清除全部缓存
service.invalidate()

# 预热 (批量拉取写入缓存)
service.prewarm_all(["SH600519", "SZ000001"], market="CNStock")
```

## 日志配置

```python
import logging

# 设置日志级别
logging.basicConfig(level=logging.INFO)

# 或通过环境变量
# export LOG_LEVEL=DEBUG
```

日志格式: `[时间] [级别] [模块名] 消息内容`

## 项目结构

```
kline_service/
├── app/
│   ├── __init__.py                  # 应用根模块
│   ├── services/
│   │   ├── __init__.py              # 服务层
│   │   └── kline.py                 # K线数据服务 (缓存层)
│   ├── data_sources/
│   │   ├── __init__.py              # 数据源模块
│   │   ├── factory.py               # DataSourceFactory (工厂层)
│   │   ├── coordinator.py           # Coordinator (协助层)
│   │   ├── cache.py                 # 两层缓存 (内存+磁盘)
│   │   ├── circuit_breaker.py       # 熔断器
│   │   ├── rate_limiter.py          # 限流器+重试装饰器
│   │   ├── normalizer.py            # 股票代码标准化
│   │   ├── adjustment.py            # 复权因子计算
│   │   ├── source_config.py         # 数据源配置+吞吐跟踪
│   │   ├── dispatcher.py            # 向后兼容层
│   │   └── provider/
│   │       ├── __init__.py          # Provider 框架 (自注册)
│   │       ├── tencent.py           # 腾讯财经
│   │       ├── sina.py              # 新浪财经
│   │       ├── eastmoney.py         # 东方财富
│   │       ├── akshare.py           # AkShare
│   │       └── hk_stock.py          # 港股数据源
│   └── utils/
│       ├── __init__.py              # 工具模块
│       └── logger.py                # 日志工具
├── cache/                           # 磁盘缓存目录 (自动创建)
├── README.md                        # 本文档
└── .gitignore
```

## 扩展新数据源

```python
# 1. 在 app/data_sources/provider/ 下创建新文件, 如 binance.py

from app.data_sources.provider import register

@register(priority=60)
class BinanceDataSource:
    """币安 — 加密货币数据源"""

    name = "binance"
    priority = 60

    capabilities = {
        "kline": True,
        "kline_tf": {"1m", "5m", "15m", "30m", "1H", "1D", "1W"},
        "quote": True,
        "batch_quote": True,
        "markets": {"Crypto"},
    }

    def fetch_kline(self, code, timeframe="1D", count=300, adj="", timeout=10):
        # 实现K线获取逻辑
        ...

    def fetch_quote(self, code, timeout=8):
        # 实现行情获取逻辑
        ...

    def fetch_quotes_batch(self, codes, timeout=10):
        # 实现批量行情逻辑
        ...

# 2. 在 source_config.py 的 SOURCE_CONFIGS 中添加配置
# 3. 重启服务，自动注册生效
```

## 注意事项

1. **AkShare 依赖**: 需要额外 `pip install akshare`，首次导入较慢
2. **磁盘缓存**: 默认写入 `cache/` 目录，确保有写入权限
3. **限流**: 各数据源有独立限流器，避免频繁请求被封 IP
4. **复权**: 除权除息数据缓存 24h，新上市/次新股可能数据不全
5. **港股降级**: 海外源 (yfinance/Twelve Data) 可能需要科学上网
