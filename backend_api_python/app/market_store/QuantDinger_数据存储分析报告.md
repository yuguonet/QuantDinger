# QuantDinger 市场数据存储分析
 
**项目地址**: https://github.com/brokermr810/QuantDinger
**分析时间**: 2026-04-18 17:06 GMT+8

---

## 核心结论：这些数据均未存入 PostgreSQL 本地数据库

项目使用的是 **Redis 缓存（或内存降级）+ TTL 过期** 的模式，而非持久化到数据库表。

---

## 各数据类型的存储情况

| 数据类型 | 数据源 | 存储位置 | TTL | 是否入 PG |
|---------|--------|---------|-----|----------|
| 加密货币 | CCXT → yfinance → CoinGecko | Redis/内存 | 120s | ❌ |
| 大宗商品 | Twelve Data → yfinance → Tiingo | Redis/内存 | 120s | ❌ |
| 外汇 | Twelve Data → yfinance → Tiingo | Redis/内存 | 120s | ❌ |
| 全球指数 | yfinance (标普/道琼斯/纳斯达克/DAX/日经等) | Redis/内存 | 120s | ❌ |
| 板块热力图 | yfinance (行业ETF: XLK/XLF/XLV等) | Redis/内存 | 120s | ❌ |
| 恐贪指数 | alternative.me API | Redis/内存 | 21600s (6h) | ❌ |
| VIX | yfinance → akshare | Redis/内存 | 21600s | ❌ |
| DXY 美元指数 | yfinance → akshare | Redis/内存 | 21600s | ❌ |
| 财经日历 | ⚠️ 模板/模拟数据（硬编码9个事件+随机偏移） | Redis/内存 | 3600s | ❌ |

---

## 缓存架构

```python
# data_providers/__init__.py
CACHE_TTL = {
    "crypto_heatmap": 300,
    "forex_pairs": 120,
    "stock_indices": 120,
    "market_overview": 120,
    "commodities": 120,
    "economic_calendar": 3600,
    "market_sentiment": 21600,   # 6小时
}
```

数据通过 `CacheManager` 写入 Redis（key 前缀 `dp:*`），当 `CACHE_ENABLED=true` 时使用 Redis，否则降级为内存字典。**缓存过期后数据即丢失**。

---

## 项目数据流架构

```
用户请求 → Flask API → data_providers/ (业务层)
                            ↓
                    data_sources/ (数据源适配层)
                            ↓
              外部API: yfinance / CCXT / Twelve Data / CoinGecko / Tiingo / akshare
                            ↓
                      Redis 缓存 (TTL过期)
                            ↓
                         ❌ 不写入 PostgreSQL
```

---

## PostgreSQL 中实际存储的内容

项目中唯一与市场数据相关的 PG 表是 `qd_market_symbols`，但它只存符号元数据（名称、市场类型、是否热门），不存价格/行情数据。

PG 主要用于：
- 用户认证与权限
- 策略与回测记录
- 计费/会员/积分
- 交易组合

---

## 财经日历的特殊问题

`news.py` 中的 `get_economic_calendar()` 返回的并非真实财经日历数据，而是 **9个硬编码事件模板**（非农、美联储、CPI等），通过 `random.uniform()` 生成随机的实际值。这不是接入真实 API（如 Investing.com 或 Forex Factory）的数据。

硬编码事件列表：
1. 美国非农就业数据
2. 美联储利率决议
3. 美国CPI月率
4. 欧洲央行利率决议
5. 日本央行利率决议
6. 美国初请失业金人数
7. 英国央行利率决议
8. 美国零售销售月率
9. OPEC月度报告

---

## 关键源码文件

| 文件路径 | 职责 |
|---------|------|
| `app/data_providers/__init__.py` | 缓存层（Redis/内存） |
| `app/data_providers/crypto.py` | 加密货币数据拉取 |
| `app/data_providers/forex.py` | 外汇数据拉取 |
| `app/data_providers/commodities.py` | 大宗商品数据拉取 |
| `app/data_providers/indices.py` | 全球指数数据拉取 |
| `app/data_providers/sentiment.py` | 恐贪指数/VIX/DXY/收益率曲线 |
| `app/data_providers/news.py` | 财经新闻 + 财经日历（模拟数据） |
| `app/data_providers/heatmap.py` | 板块/加密/外汇/商品热力图 |
| `app/routes/global_market.py` | 全局市场 API 路由 |
| `app/data/market_symbols_seed.py` | PG中的市场符号元数据查询 |

---

## 总结

所有行情类数据均采用 **实时拉取 + Redis 短期缓存** 的架构，没有任何历史数据持久化到本地数据库。如需历史分析、回测对比或离线查询，需自行增加数据入库逻辑。
